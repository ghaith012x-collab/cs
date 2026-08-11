"""
cybertemp.py — temp-mail client for cybertemp.xyz, delivering to the configured
# Discord-friendly domain (default @mikerossy.com).

Why a browser?
  cybertemp.xyz gates its JSON API behind an antibot Proof-of-Work layer
  (WASM hashcash -> `cfg_v` cookie + CSRF). A plain HTTP client cannot read
  mail without solving that challenge, and the official REST API (v1) needs
  a paid API key (50 free requests/day with a key; Eco plan otherwise). So
  instead this module drives a real stealth browser — the same engine the
  bot uses for Discord (browser_engine + stealth) — the PoW auto-solves
  in-page, and the inbox is read through the site's own frontend API with
  the browser's cookies. No API key, no daily request cap.

Discord signup flow:
  1. create_inbox()                -> random addr@configured-domain (Discord-capable)
  2. (Discord registration happens)
  3. wait_for_verification_link()  -> polls the inbox, extracts the verify URL

Never crashes the caller — every failure is caught, logged and surfaced as
an empty string / None so the automation can react gracefully.
"""

import asyncio
import hashlib
import random
import re
import string
import time
from typing import Callable, Optional

from browser_engine import async_playwright, ENGINE
from stealth import (
    _LOCALE_PROFILES,
    apply_cdp_stealth,
    build_context_options,
    build_init_script,
    launch_args,
    pick_gpu,
    ua_platform,
)

CYBERTEMP_URL = "https://www.cybertemp.xyz/en/"
CYBERTEMP_DOMAIN = "andrewslife.tattoo"
PROVIDER = "cybertemp"

# Discord verification emails link to:
#   https://discord.com/register/verify?token=...
#   https://discord.com/verify?token=...
#   https://click.discord.com/ls/click?upn=...      (tracking redirect)
#   https://e.discord.com/...                       (email gateway)
VERIFY_LINK_PATTERNS = [
    r"https?://[^\s\"'<>]+discord\.com/register/verify[^\s\"'<>]*",
    r"https?://[^\s\"'<>]+discord\.com/verify[^\s\"'<>]*",
    r"https?://[^\s\"'<>]*(?:click|e)\.discord\.com[^\s\"'<>]*",
    r"https?://[^\s\"'<>]+discord[^\s\"'<>]*(?:verify|verification)[^\s\"'<>]*",
]

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

_EMAIL_RANDOM = random.SystemRandom()


def _build_fingerprint() -> dict:
    """A coherent, unique fingerprint in the same shape server.py uses."""
    seed = int(hashlib.sha256(f"ct:{time.time():.6f}:{_EMAIL_RANDOM.random()}"
                              .encode()).hexdigest(), 16)
    ua = _UA_POOL[seed % len(_UA_POOL)]
    profile = _LOCALE_PROFILES[seed % len(_LOCALE_PROFILES)]
    gpu = pick_gpu(ua_platform(ua)["ch_platform"], seed)
    return {
        "font": "Arial",
        "canvas_noise": 0,
        "webgl_vendor": gpu["webgl_vendor"],
        "webgl_renderer": gpu["webgl_renderer"],
        "color_depth": 24,
        "pixel_ratio": 1.0 + (seed % 5) / 10,
        "seed": seed,
        "ua": ua,
        "locale": profile["locale"],
        "languages": profile["languages"],
        "locale_profile": profile,
        "cores": [4, 6, 8, 8, 12, 16][seed % 6],
        "device_memory": [4, 8, 8, 16, 16, 32][seed % 6],
        "touch_points": 0,
        "gpu": gpu,
    }


class TempMail:
    """Async temp-mail client: cybertemp.xyz -> @configured-domain via stealth browser.

    Drop-in replacement for duckmail.TempMail (same public API):
      email, provider, create_inbox(), wait_for_verification_link(), close().
    """

    def __init__(self, log: Optional[Callable] = None,
                 proxy: Optional[dict] = None, headless: bool = True,
                 domain: str = CYBERTEMP_DOMAIN):
        self._log = log or (lambda msg, level="info": None)
        # proxy: dict {proto, host, port, username, password} — same shape the
        # worker passes to build_context_options (rides the same residential IP).
        self._proxy = proxy
        self.headless = headless
        self._domain = (domain or CYBERTEMP_DOMAIN).strip().lower() or CYBERTEMP_DOMAIN
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._fingerprint: dict = {}
        self._ua = ""
        self._address = ""
        self._provider = ""

    # ── Public API (duckmail-compatible) ────────────────────

    @property
    def email(self) -> str:
        return self._address

    @property
    def provider(self) -> str:
        return self._provider or "none"

    async def create_inbox(self, timeout: float = 45.0) -> str:
        """Open cybertemp in a stealth browser and provision addr@{domain}."""
        try:
            await self._ensure_browser()
            await self._goto_site()
            local = "".join(_EMAIL_RANDOM.choices(
                string.ascii_lowercase + string.digits, k=12))
            addr = f"{local}@{self._domain}"
            real = await self._set_address(addr, timeout=timeout)
            if real:
                self._address = real
            else:
                self._address = addr
            self._provider = PROVIDER
            self._log(f"[Mail] [OK] Inbox ready: {self._address} ({PROVIDER})")
            return self._address
        except Exception as e:
            self._log(f"[Mail] cybertemp.xyz unavailable: {e}", level="error")
            return ""

    async def wait_for_verification_link(
        self, keyword: str = "discord",
        timeout: float = 240.0, poll: float = 4.0,
    ) -> Optional[str]:
        """Poll the inbox until a matching message arrives, then return its
        Discord verification URL (or None on timeout)."""
        if not self._address or not self._page:
            self._log("[Mail] No inbox open — cannot poll", level="warn")
            return None
        deadline = time.time() + timeout
        self._log(f"[Mail] Waiting for {keyword} verification email "
                  f"(up to {int(timeout)}s)...")
        while time.time() < deadline:
            try:
                messages = await self._list_messages()
                for msg in messages:
                    if not self._matches(msg, keyword):
                        continue
                    link = self._extract_verify_link(msg)
                    if link:
                        self._log(f"[Mail] [OK] Verification link found: {link[:90]}...")
                        return link
                    self._log("[Mail] Matching message but no verify link in body "
                              "— keeping polling", level="warn")
            except Exception as e:
                self._log(f"[Mail] poll error: {e}", level="warn")
            await asyncio.sleep(poll)
        self._log(f"[Mail] No {keyword} verification email within {int(timeout)}s",
                  level="warn")
        return None

    async def close(self) -> None:
        for close_fn, obj in (
            (self._close_page, self._page),
            (self._close_ctx, self._context),
            (self._close_browser, self._browser),
            (self._close_pw, self._playwright),
        ):
            try:
                await close_fn(obj)
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None

    @staticmethod
    async def _close_page(page) -> None:
        if page is not None:
            await page.close()

    @staticmethod
    async def _close_ctx(ctx) -> None:
        if ctx is not None:
            await ctx.close()

    @staticmethod
    async def _close_browser(browser) -> None:
        if browser is not None:
            await browser.close()

    @staticmethod
    async def _close_pw(pw) -> None:
        if pw is not None:
            await pw.stop()

    # ── Browser lifecycle ──────────────────────────────────

    async def _ensure_browser(self) -> None:
        if self._page is not None:
            return
        self._log(f"[Mail] Launching {ENGINE} browser for cybertemp.xyz "
                  f"(headless={self.headless})...")
        self._playwright = await async_playwright().start()
        args = launch_args(headless=self.headless)
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless, args=args)
        self._fingerprint = _build_fingerprint()
        self._ua = self._fingerprint["ua"]
        ctx_opts = build_context_options(
            self._fingerprint, self._ua, proxy=self._proxy,
            viewport={"width": 1366, "height": 768},
        )
        self._context = await self._browser.new_context(**ctx_opts)
        await self._context.add_init_script(
            build_init_script(self._fingerprint, self._ua))
        self._page = await self._context.new_page()
        await apply_cdp_stealth(self._context, self._page)
        self._log("[Mail] Browser context ready")

    async def _goto_site(self) -> None:
        try:
            await self._page.goto(CYBERTEMP_URL, wait_until="domcontentloaded",
                                  timeout=60000)
        except Exception as e:
            self._log(f"[Mail] cybertemp goto: {e}", level="warn")
        # Wait for the app shell / email form (antibot PoW solves in-page)
        deadline = time.time() + 30.0
        while time.time() < deadline:
            try:
                ready = await asyncio.wait_for(self._page.evaluate(
                    "() => !!document.querySelector('input')"), timeout=5.0)
                if ready:
                    return
            except Exception:
                pass
            await asyncio.sleep(1.5)
        raise RuntimeError("cybertemp page did not render an email form")

    # ── Address provisioning ───────────────────────────────

    async def _set_address(self, addr: str, timeout: float = 45.0) -> str:
        """Enter addr on the site. Returns the REAL composed address read back
        from the page (best effort — falls back to the requested addr)."""
        deadline = time.time() + timeout

        # 1) Locate the email input (visible, non-hidden, best heuristic)
        input_idx = None
        while time.time() < deadline and input_idx is None:
            try:
                input_idx = await asyncio.wait_for(self._page.evaluate(
                    """() => {
                        const inputs = Array.from(document.querySelectorAll('input'));
                        let best = -1, score = -1;
                        for (let i = 0; i < inputs.length; i++) {
                            const el = inputs[i];
                            if (el.type === 'hidden' || el.offsetParent === null) continue;
                            const p = (el.placeholder || '').toLowerCase();
                            const l = ((el.getAttribute('aria-label') || '') + ' ' +
                                       (el.getAttribute('name') || '') + ' ' + (el.id || '')).toLowerCase();
                            let s = 0;
                            if (p.includes('@') || p.includes('email')) s += 10;
                            if (l.includes('email') || l.includes('temporary')) s += 8;
                            if (p.includes('your') || p.includes('address')) s += 4;
                            if (el.type === 'email') s += 6;
                            if (s > score) { score = s; best = i; }
                        }
                        return best;
                    }"""), timeout=5.0)
            except Exception:
                pass
            if input_idx is None or input_idx < 0:
                await asyncio.sleep(1.0)
        if input_idx is None or input_idx < 0:
            raise RuntimeError("cybertemp email input not found")

        # 2) Try to switch the domain dropdown to the configured domain (best effort).
        #    If that works we only type the local part; otherwise type the
        #    full address (the site accepts a complete address too).
        domain_ok = False
        try:
            root = self._domain.split(".")[0]
            res = await asyncio.wait_for(self._page.evaluate(
                """async (wanted, root) => {
                    const pick = (t) => {
                        const s = t.toLowerCase();
                        return s.includes(wanted) || s === root || s.includes('.' + root);
                    };
                    const known = /(andrewcluh|vibify|boostwave|[a-z0-9-]+\\.(cc|top|xyz|com))/i;
                    const els = Array.from(document.querySelectorAll('div,span,button,li,[role="button"],option'));
                    let clicked = false;
                    for (const el of els) {
                        const t = (el.textContent || '').trim();
                        if (t && t.length <= 24 && known.test(t) && el.offsetParent !== null) {
                            el.click(); clicked = true; break;
                        }
                    }
                    if (!clicked) return 'no_dropdown';
                    await new Promise(r => setTimeout(r, 1000));
                    for (const el of document.querySelectorAll('div,span,li,button,[role="option"],option')) {
                        const t = (el.textContent || '').trim();
                        if (pick(t) && el.offsetParent !== null) { el.click(); return 'selected'; }
                    }
                    return 'option_not_found';
                }""", self._domain, root), timeout=10.0)
            domain_ok = res == "selected"
        except Exception:
            domain_ok = False
        if domain_ok:
            self._log(f"[Mail] {self._domain} selected in domain dropdown")

        local = addr.split("@")[0]
        fill_value = local if domain_ok else addr
        await self._page.locator("input").nth(input_idx).fill(fill_value)
        try:
            await self._page.keyboard.press("Enter")
        except Exception:
            pass
        await asyncio.sleep(2.0)

        # 3) Read back the real composed address (page text > input value)
        real = ""
        try:
            real = await asyncio.wait_for(self._page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText : '';
                    const m = t.match(/(?:email is|address is|your email|current)[^\\n]{0,90}?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,})/i);
                    if (m) return m[1];
                    for (const el of document.querySelectorAll('input')) {
                        if (el.value && el.value.includes('@')) return el.value;
                    }
                    return '';
                }"""), timeout=6.0)
        except Exception:
            real = ""
        if not real:
            real = addr
        elif self._domain not in real:
            self._log(f"[Mail] Composed address is {real} (not @{self._domain}) "
                      f"— continuing with it", level="warn")
        return real

    # ── Inbox reads ────────────────────────────────────────

    _FETCH_INBOX_JS = """async (email) => {
        try {
            const r = await fetch('/api/inbox?email=' + encodeURIComponent(email) + '&limit=50', {
                headers: { 'Accept': 'application/json' }
            });
            const txt = await r.text();
            let data = null;
            try { data = JSON.parse(txt); } catch (e) {}
            return { status: r.status, data: data };
        } catch (e) {
            return { status: 0, error: String(e).substring(0, 200) };
        }
    }"""

    async def _list_messages(self) -> list:
        if not self._page or not self._address:
            return []
        # The antibot PoW cookie (cfg_v) lands a few seconds after load —
        # retry while the API reports 401/403. Bounded so we never hang.
        for attempt in range(25):
            try:
                res = await asyncio.wait_for(
                    self._page.evaluate(self._FETCH_INBOX_JS, self._address),
                    timeout=15.0)
            except Exception as e:
                self._log(f"[Mail] inbox fetch error: {e}", level="warn")
                await asyncio.sleep(2.0)
                continue
            status = res.get("status", 0)
            if status in (401, 403, 0):
                if attempt == 0:
                    self._log("[Mail] Antibot PoW not ready — waiting...", level="warn")
                await asyncio.sleep(2.0)
                continue
            if status != 200:
                self._log(f"[Mail] Inbox API status {status}", level="warn")
                break
            msgs = self._extract_message_objects(res.get("data"))
            if msgs:
                return msgs
            # Empty/unknown shape — fall through to DOM scrape below
            break
        return await self._dom_messages()

    async def _dom_messages(self) -> list:
        """Fallback: scrape visible message text / discord links from the DOM."""
        try:
            text = await asyncio.wait_for(self._page.evaluate(
                """() => {
                    let t = '';
                    for (const sel of ['main', '[class*="message" i]', '[class*="inbox" i]',
                                       '[class*="mail" i]', '[class*="email" i]']) {
                        for (const el of document.querySelectorAll(sel)) {
                            t += (el.innerText || '') + '\\n';
                        }
                    }
                    for (const a of document.querySelectorAll('a[href*="discord"], a[href*="verify"]')) {
                        t += a.href + '\\n';
                    }
                    return t.substring(0, 30000);
                }"""), timeout=10.0)
        except Exception:
            text = ""
        if text and any(k in text.lower() for k in
                        ("discord", "verify", "verification", "welcome")):
            return [{"body": {"text": text}}]
        return []

    # ── Message parsing ────────────────────────────────────

    def _extract_message_objects(self, data) -> list:
        out: list = []

        def walk(node, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(node, dict):
                keys = set(node.keys())
                if keys & {"subject", "body", "from", "sender", "html",
                           "text", "content", "message", "summary"}:
                    out.append(self._normalize(node))
                for v in node.values():
                    walk(v, depth + 1)
            elif isinstance(node, list):
                for v in node:
                    walk(v, depth + 1)

        walk(data)
        return out

    def _normalize(self, m: dict) -> dict:
        out: dict = {}
        for k in ("subject", "id", "message_id", "introduction", "summary"):
            if k in m:
                out[k] = m[k]
        frm = m.get("from") or m.get("sender")
        if isinstance(frm, dict):
            out["from"] = {"address": frm.get("address") or frm.get("email") or ""}
        elif isinstance(frm, str):
            out["from"] = {"address": frm}
        body = m.get("body") or m.get("content") or m.get("message")
        if body is None and ("html" in m or "text" in m):
            body = {"html": m.get("html"), "text": m.get("text")}
        out["body"] = body
        return out

    def _matches(self, msg: dict, keyword: str) -> bool:
        text = self._message_text(msg)
        kw = keyword.lower()
        if kw and kw in text.lower():
            return True
        frm = msg.get("from")
        if isinstance(frm, dict):
            sender = str(frm.get("address", "")).lower()
            if kw and kw in sender:
                return True
        return False

    @staticmethod
    def _message_text(msg: dict) -> str:
        parts: list = []
        for key in ("subject", "introduction", "summary"):
            v = msg.get(key)
            if isinstance(v, str):
                parts.append(v)
        body = msg.get("body")
        if isinstance(body, str):
            parts.append(body)
        elif isinstance(body, dict):
            for k in ("html", "text"):
                v = body.get(k)
                if isinstance(v, str):
                    parts.append(v)
        frm = msg.get("from")
        if isinstance(frm, dict):
            parts.append(str(frm.get("address", "")))
        return "\n".join(parts)

    def _extract_verify_link(self, msg: dict) -> Optional[str]:
        text = self._message_text(msg)
        # 1) Direct URL scan (works for plain-text bodies)
        for pattern in VERIFY_LINK_PATTERNS:
            m = re.search(pattern, text, re.I)
            if m:
                return self._clean_link(m.group(0))
        # 2) HTML anchor hrefs
        for m in re.finditer(r'href=["\']([^"\']+)["\']', text, re.I):
            href = self._clean_link(m.group(1))
            h = href.lower()
            if ("discord.com/verify" in h or "discord.com/register" in h
                    or "click.discord.com" in h or "e.discord.com" in h):
                return href
            if "discord.com" in h and ("verify" in h or "confirm" in h):
                return href
        # 3) Escaped/encoded variants inside HTML body
        for m in re.finditer(r'href=([^\s>]+)', text, re.I):
            href = self._clean_link(m.group(1).strip("'\"'"))
            h = href.lower()
            if "discord.com" in h and "verify" in h:
                return href
        return None

    @staticmethod
    def _clean_link(url: str) -> str:
        return url.replace("&amp;", "&").rstrip(".,);\"'<>")
