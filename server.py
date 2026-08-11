import asyncio
import base64
import json
import os
import random
import socket
import time
from typing import Optional

from browser_engine import async_playwright, ENGINE

from captcha_solver import (
    NoCaptchaAI,
    extract_hcaptcha_sitekey,
    extract_hcaptcha_rqdata,
    extract_funcaptcha_task,
    read_hcaptcha_token,
    set_hcaptcha_token_on_page,
    solve_funcaptcha_pixels,
    solve_hcaptcha_accessibility,
)
from cybertemp import TempMail
from draxon import DraxonMail


# ── TOR Control ───────────────────────────────────────────

def _tor_newnym():
    """Signal TOR to switch to a new identity (fresh exit node)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect(("127.0.0.1", 9051))
        s.recv(1024)
        s.sendall(b"AUTHENTICATE\r\n")
        auth_resp = s.recv(1024).decode().strip()
        if "250" not in auth_resp:
            s.close()
            print(f"[TOR] auth failed: {auth_resp}", flush=True)
            return False
        s.sendall(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(1024).decode().strip()
        s.close()
        if "250" in resp:
            time.sleep(3)
            return True
        print(f"[TOR] newnym rejected: {resp}", flush=True)
    except Exception as e:
        print(f"[TOR] newnym error: {e}", flush=True)
    return False


def _tor_check():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 9050))
        s.close()
        return True
    except:
        return False


USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
]

PAST_CAPTCHA_KEYWORDS = ['/channels', '/verify', '/welcome', '/login', '@me', 'discord.com/app']

_BIO_POOL = [
    "just vibing",
    "professional sleeper",
    "i like turtles",
    "certified yapper",
    "caffeine powered",
    "music > everything",
    "gamer for life",
    "casually existing",
    "be nice or leave",
    "no thoughts, only vibes",
]

import stealth
from stealth import (
    apply_cdp_stealth,
    build_context_options,
    build_init_script,
    launch_args,
)

# Legacy constant kept for compatibility — replaced by stealth.build_init_script
INIT_SCRIPT = build_init_script(
    {"cores": 8, "device_memory": 8, "touch_points": 0, "locale": "en-US",
     "languages": ["en-US", "en"], "locale_profile": None,
     "gpu": None, "pixel_ratio": 1.0},
    USER_AGENTS[0],
)

NAV_TIMEOUT_MS = 60000


# ═══════════════════════════════════════════════════════════════
# Human Behavior Simulation & Fingerprint Randomization
# ═══════════════════════════════════════════════════════════════

async def human_type(page, selector: str, text: str):
    """Type with variable speed, occasional pauses, and rare backspaces.
    Human-like typing with Gaussian-distributed delays."""
    try:
        await page.click(selector)
    except Exception:
        pass
    for i, char in enumerate(text):
        # Base 50-150ms per char with Gaussian distribution
        delay = max(20, random.gauss(80, 30)) / 1000.0
        if random.random() < 0.05:  # 5% chance of pause
            delay += random.gauss(800, 200) / 1000.0
        if random.random() < 0.02 and i > 0:  # 2% chance of backspace
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.gauss(150, 50) / 1000.0)
        await page.keyboard.type(char, delay=int(delay * 1000))
        await asyncio.sleep(delay)

async def human_mouse_move(page, x: int, y: int):
    """Bezier curve mouse movement instead of instant teleport.
    Creates a natural, curved mouse path between current position and target."""
    try:
        current = await page.evaluate("() => ({x: window.mouseX || 0, y: window.mouseY || 0})")
    except Exception:
        current = {'x': x - 100, 'y': y - 50}
    if not current or current.get('x') == 0:
        current = {'x': x - 100, 'y': y - 50}
    steps = random.randint(8, 20)
    for t in range(1, steps + 1):
        progress = t / steps
        # Quadratic bezier with random control point
        cp_x = (current['x'] + x) / 2 + random.randint(-30, 30)
        cp_y = (current['y'] + y) / 2 + random.randint(-20, 20)
        bx = (1-progress)**2 * current['x'] + 2*(1-progress)*progress * cp_x + progress**2 * x
        by = (1-progress)**2 * current['y'] + 2*(1-progress)*progress * cp_y + progress**2 * y
        await page.mouse.move(bx, by)
        await asyncio.sleep(random.gauss(0.008, 0.003))

import hashlib

def generate_fingerprint(worker_id: str, session_seed: str = "") -> dict:
    """Deterministic but unique fingerprint per worker+session.
    Returns dict with font, canvas_noise, webgl_vendor, webgl_renderer, etc."""
    seed_input = f"{worker_id}:{session_seed or time.time()}"
    seed = hashlib.sha256(seed_input.encode()).hexdigest()

    fonts = ["Arial", "Times New Roman", "Helvetica", "Georgia", "Courier New", "Verdana"]
    font = fonts[int(seed[:8], 16) % len(fonts)]
    color_depths = [24, 24, 24, 30]
    color_depth = color_depths[int(seed[24:32], 16) % len(color_depths)]
    pixel_ratio = 1.0 + (int(seed[24:32], 16) % 5) / 10  # 1.0 - 1.4

    # Consistent identity for the stealth layer (stealth.build_init_script /
    # build_context_options consume these). GPU comes from stealth so the
    # WebGL strings match the platform implied by the chosen UA.
    ua = USER_AGENTS[int(seed[:8], 16) % len(USER_AGENTS)]
    from stealth import _LOCALE_PROFILES, pick_gpu, ua_platform
    profile = _LOCALE_PROFILES[int(seed[32:40], 16) % len(_LOCALE_PROFILES)]
    gpu = pick_gpu(ua_platform(ua)["ch_platform"], int(seed[16:24], 16))

    return {
        "font": font,
        "canvas_noise": 0,
        "webgl_vendor": gpu["webgl_vendor"],
        "webgl_renderer": gpu["webgl_renderer"],
        "color_depth": color_depth,
        "pixel_ratio": pixel_ratio,
        "seed": int(seed, 16),
        "ua": ua,
        "locale": profile["locale"],
        "languages": profile["languages"],
        "locale_profile": profile,
        "cores": [4, 6, 8, 8, 12, 16][int(seed[8:16], 16) % 6],
        "device_memory": [4, 8, 8, 16, 16, 32][int(seed[8:16], 16) % 6],
        "touch_points": 0,
        "gpu": gpu,
    }


class DiscordAutomation:
    def __init__(self, headless: bool = False, email: str = "",
                 proxy=None, worker_id: str = "B1", domain: str = "vibify.cc"):
        self.headless = headless
        self.worker_id = worker_id
        self._domain = (domain or "andrewslife.tattoo").strip().lower() or "andrewslife.tattoo"
        # proxy: dict {proto, host, port, username, password, key} or None
        self.proxy = proxy
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._ua = ""
        self._tor_enabled = False
        self._screenshots: list = []
        self._activity_log: list = []
        self._email = (email or os.environ.get("ACCOUNT_EMAIL", "")).strip()
        self._username = ""
        self._password = ""
        self._token = ""
        self._solver = NoCaptchaAI(log=self._log)
        self._mail: Optional[TempMail] = None
        self._user_id = ""
        self._avatar_data = ""
        self._bio = ""
        self._humanized = False
        # Set when Discord asks for phone verification after account creation
        # — the worker then rotates proxy + fingerprint + mail domain and retries.
        self.phone_verify_detected = False
        self._fingerprint = generate_fingerprint(worker_id)

    def _log(self, message: str, level: str = "info") -> None:
        tagged = f"[{self.worker_id}] {message}"
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "timestamp": time.time(),
            "level": level,
            "message": tagged
        }
        self._activity_log.append(entry)
        if len(self._activity_log) > 500:
            self._activity_log = self._activity_log[-500:]
        print(f"[{entry['time']}] [{level.upper()}] {tagged}", flush=True)

    def get_activity_log(self) -> list:
        return self._activity_log

    async def initialize(self) -> None:
        self._playwright = await async_playwright().start()

        # Best-human-stealth launch flags (patchright = minimal set, stock
        # playwright = full hardening set). See stealth.launch_args().
        args = launch_args(headless=self.headless)
        self._log(f"[Engine] {ENGINE} launch args: {len(args)}")

        self._ua = random.choice(USER_AGENTS)
        self._fingerprint = generate_fingerprint(self.worker_id)
        # Keep the fingerprint's UA in sync with the one we actually use.
        self._ua = self._fingerprint.get("ua") or self._ua
        self._log(f"Fingerprint: font={self._fingerprint['font']}, gpu={self._fingerprint['webgl_renderer'][:40]}..., dpr={self._fingerprint['pixel_ratio']}")
        self._browser = await self._playwright.chromium.launch(headless=self.headless, args=args)

        # Standard desktop viewport (1920x1080) — most common real resolution
        await self._build_context()

        # Done — context created by _build_context with full CDP evasion

    async def _build_context(self) -> None:
        """Build a fresh browser context with current self.proxy.
        Shared by initialize() and switch_proxy()."""
        vp = {'width': 1920, 'height': 1080}
        ctx_opts = build_context_options(
            self._fingerprint, self._ua, proxy=self.proxy, viewport=vp
        )
        if self.proxy and isinstance(self.proxy, dict):
            p = self.proxy
            server = f"{p.get('proto', 'http')}://{p.get('host')}:{p.get('port')}"
            self._log(f"Proxy: {server} (auth={'yes' if p.get('username') else 'no'})")
        elif _tor_check():
            self._tor_enabled = True
            self._log("[TOR] Using TOR SOCKS5 proxy...")
            if _tor_newnym():
                self._log("[TOR] New identity requested")
            ctx_opts['proxy'] = {'server': 'socks5://127.0.0.1:9050'}
            await asyncio.sleep(1)
        else:
            self._log("[TOR] [FATAL] TOR SOCKS5 (127.0.0.1:9050) NOT reachable - TOR-only mode requires TOR running on this instance", level="error")
            self._tor_enabled = False
            raise RuntimeError("TOR not available - TOR-only mode requires TOR on 127.0.0.1:9050")

        self._context = await self._browser.new_context(**ctx_opts)
        self._log(f"User-Agent: {self._ua[:60]}...")
        await self._context.add_init_script(
            build_init_script(self._fingerprint, self._ua)
        )
        self._page = await self._context.new_page()

        # CDP-level webdriver removal — runs BEFORE init scripts, catches early checks
        await apply_cdp_stealth(self._context, self._page)

    async def switch_proxy(self, new_proxy=None) -> bool:
        """Swap to a new proxy AND a fresh fingerprint without restarting the
        browser. Returns True on success."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._page = None
        self._context = None
        self.proxy = new_proxy
        # Fresh fingerprint per session — same UA/GPU/font on a new IP is a
        # fingerprinting red flag and a known trigger for phone verification.
        self.rotate_fingerprint()
        try:
            await self._build_context()
            label = 'proxy ' + str(new_proxy.get('key','?')[:40]) if new_proxy else 'fresh TOR circuit'
            self._log(f"[Switch] Context rebuilt with {label} + new fingerprint")
            return True
        except Exception as e:
            self._log(f"[Switch] Context rebuild failed: {e}", level="error")
            return False

    def rotate_fingerprint(self) -> None:
        """Regenerate fingerprint + UA for a brand-new browser identity."""
        try:
            self._fingerprint = generate_fingerprint(self.worker_id)
            self._ua = self._fingerprint.get("ua") or self._ua
            fp = self._fingerprint
            self._log(f"[Fingerprint] Rotated: font={fp['font']}, gpu={fp['webgl_renderer'][:36]}..., ua={self._ua[:48]}...")
        except Exception as e:
            self._log(f"[Fingerprint] rotation error: {e}", level="warn")

    async def _rebuild_context_with_tor(self) -> bool:
        """Close the context and reopen WITH a fresh TOR circuit."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if not _tor_check():
                self._log("[Nav] TOR not available for rebuild", level="error")
                return False
            if _tor_newnym():
                self._log("[Nav] Fresh TOR circuit requested")
            await asyncio.sleep(1)
            self._context = await self._browser.new_context(
                **build_context_options(
                    self._fingerprint, self._ua,
                    proxy={'proto': 'socks5', 'host': '127.0.0.1', 'port': '9050'},
                    viewport=random.choice([
                        {'width': 860, 'height': 640},
                        {'width': 1024, 'height': 768},
                        {'width': 900, 'height': 700},
                    ]),
                )
            )
            await self._context.add_init_script(
                build_init_script(self._fingerprint, self._ua)
            )
            self._page = await self._context.new_page()
            await apply_cdp_stealth(self._context, self._page)
            self._log("[Nav] Rebuilt browser context WITH fresh TOR proxy")
            return True
        except Exception as e:
            self._log(f"[Nav] context rebuild failed: {e}", level="error")
            return False

    async def _goto_register(self) -> bool:
        """Navigate to discord.com/register — single attempt, no retries.

        If the form doesn't render, we return False immediately so the worker
        can rotate to a fresh TOR circuit. Retrying the same URL on the same
        circuit is pointless — if Discord blocked that exit node, it won't
        unblock on retry."""
        url = "https://discord.com/register"
        # 25s cap: a working residential session renders Discord's register in
        # a few seconds. Burning the old 90s timeout per dead session made the
        # worker look like it was only generating mail (one new inbox per ~90s).
        timeout_ms = 25000

        self._log(f"[Nav] Navigating to {url} (timeout={timeout_ms}ms)...")
        try:
            # domcontentloaded (not "load"): "load" waits for EVERY subresource
            # including the hCaptcha widget iframe and all its JS, which through
            # slow proxies hangs for tens of seconds. The form-poll loop below
            # already waits for the Discord SPA to boot, so we lose nothing.
            await self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            self._log("[Nav] Page DOM ready (not waiting for hCaptcha subresources)")
        except Exception as e:
            err = str(e)[:120]
            self._log(f"[Nav] Page.goto error: {err}", level="warn")

        # ── Check what we got ──
        try:
            page_title = await asyncio.wait_for(self._page.title(), timeout=3.0)
            page_url = await asyncio.wait_for(self._page.evaluate("location.href"), timeout=3.0)
        except Exception:
            page_title = "(unknown)"
            page_url = "(unknown)"
        self._log('[Nav] Page: title="' + str(page_title)[:80] + '" url=' + str(page_url)[:80])

        # ── Dead proxy (cannot reach Discord at all) ──
        dead_proxy = (
            "chrome-error://" in (page_url or "") or
            "about:blank" == (page_url or "") or
            (not page_title and "error" in (page_url or "").lower())
        )
        if dead_proxy:
            proxy_label = "PROXY SESSION" if self.proxy else "TOR CIRCUIT"
            self._log(f"[Nav] {proxy_label} DEAD (url={page_url[:60]}) - rotating to fresh circuit", level="warn")
            return False

        # ── Quick body text check (403/Forbidden/Cloudflare) ──
        try:
            body_text = await asyncio.wait_for(
                self._page.evaluate("document.body ? document.body.innerText.substring(0, 500) : ''"),
                timeout=3.0)
            if body_text and any(kw in body_text.lower() for kw in (
                "forbidden", "403 forbidden", "access denied", "cloudflare",
                "attention required", "rate limit", "ratelimited", "rate limited",
                "too many requests", "slowdown", "try again later", "429",
            )):
                self._log(f"[Nav] BLOCKED — body contains: {body_text[:100]}", level="warn")
                return False
        except Exception:
            pass

        # ── Rate-limit detection — rotate TOR circuit fast ──
        # Discord/Cloudflare throttle abused TOR exit nodes with 429s that
        # render as "rate limited / too many requests" text on the page.
        try:
            rl_text = (body_text or "").lower()
        except Exception:
            rl_text = ""
        if any(kw in rl_text for kw in ("rate limit", "ratelimited", "rate limited",
                                        "too many requests", "slowdown", "429")):
            self._log("[Nav] RATE LIMITED (429) - rotating TOR circuit", level="warn")
            return False

        # ── Block keywords in title ──
        blocked_keywords = ["attention required", "just a moment", "blocked",
                           "cloudflare", "ddos-guard", "captcha",
                           "checking your browser", "verify you are human",
                           "forbidden", "403", "access denied",
                           "you do not have permission", "error 1020",
                           "rate limit", "ratelimited", "rate limited",
                           "too many requests", "slowdown", "try again later"]
        title_lower = (page_title or "").lower()
        if any(kw in title_lower for kw in blocked_keywords):
            self._log('[Nav] BLOCKED by Cloudflare/firewall (title: "' + str(page_title)[:60] + '")', level="warn")
            return False

        # ── Check if Discord SPA shell loaded ──
        try:
            app_mount = await asyncio.wait_for(
                self._page.evaluate("document.querySelector('#app-mount') !== null"),
                timeout=5.0)
            if app_mount:
                self._log("[Nav] Discord SPA app-mount detected")
        except Exception:
            app_mount = False

        # ── Poll for form elements ──
        # Discord uses aria-label on inputs, not name/id — use broad selectors
        self._log("[Nav] Waiting for registration form to render...")
        blank_streak = 0
        for poll_sec in range(1, 31):
            try:
                checks = await asyncio.wait_for(self._page.evaluate("""() => {
                    const body = document.body;
                    if (!body) return JSON.stringify({error: "no-body"});
                    const text = body.innerText || "";
                    // Broad selectors — Discord uses aria-label, not name
                    const email = document.querySelector('input[name="email"], input[type="email"], input[aria-label*="email" i], input[aria-label*="Email"], input[id*="email" i]');
                    const username = document.querySelector('input[name="username"], input[aria-label*="username" i], input[aria-label*="display" i]');
                    const password = document.querySelector('input[name="password"], input[type="password"], input[aria-label*="password" i]');
                    const hasAge = /birthday|date of birth|born|how old/i.test(text.substring(0, 400));
                    const hasMonth = document.querySelector('[class*="month" i], [aria-label*="month" i], select');
                    const isLogin = /login|sign in|welcome back/i.test(text.substring(0, 400));
                    const hasQR = document.querySelector('img[src*="qr" i], [class*="qr" i]');
                    const continueBtn = document.querySelector('button[type="submit"], button[class*="continue" i]');
                    const allInputs = document.querySelectorAll('input');
                    const allButtons = document.querySelectorAll('button');
                    return JSON.stringify({
                        email: email !== null,
                        username: username !== null,
                        password: password !== null,
                        ageGate: hasAge || hasMonth !== null,
                        isLogin: isLogin,
                        hasQR: hasQR,
                        hasButton: continueBtn !== null,
                        hasAppMount: document.querySelector("#app-mount") !== null,
                        inputCount: allInputs.length,
                        buttonCount: allButtons.length,
                        textPreview: text.substring(0, 250)
                    });
                }"""), timeout=2.0)
                state = json.loads(checks)
            except Exception:
                state = None

            if state:
                # Log every 5s with input/button counts for debugging
                if poll_sec % 5 == 0:
                    self._log(f"[Nav] Poll {poll_sec}s: email={state.get('email')} ageGate={state.get('ageGate')} login={state.get('isLogin')} inputs={state.get('inputCount')} buttons={state.get('buttonCount')} text={state.get('textPreview','')[:60]}")

                if state.get("email") and state.get("username"):
                    self._log(f"[Nav] SUCCESS! Full form rendered after {poll_sec}s")
                    return True
                if state.get("email") and state.get("password"):
                    self._log(f"[Nav] SUCCESS! Email+password form rendered after {poll_sec}s")
                    return True
                if state.get("ageGate"):
                    self._log(f"[Nav] Age gate detected after {poll_sec}s - returning true, form filler handles it")
                    return True

                # BLANK RENDER fast-fail — SPA shell mounted but React painted
                # nothing (0 inputs, 0 buttons, empty text).
                #
                # IMPORTANT: through TOR, Discord's React bundle routinely takes
                # 10-18s to boot (older logs show 15s of inputs=0 then success).
                # A 3s threshold was killing healthy-but-slow circuits. Only
                # rotate after 15s of sustained blankness — that's a genuinely
                # dead/rate-limited node, not a slow boot.
                if (state.get("hasAppMount") and not state.get("inputCount")
                        and not state.get("buttonCount")
                        and not (state.get("textPreview") or "").strip()):
                    blank_streak += 1
                    if blank_streak >= 20:
                        proxy_label = "proxy session" if self.proxy else "TOR exit"
                        self._log(f"[Nav] BLANK RENDER for 20s (SPA mounted, no content) - {proxy_label} likely dead/rate-limited - rotating circuit", level="warn")
                        return False
                else:
                    blank_streak = 0

                if state.get("isLogin") and poll_sec >= 10:
                    self._log(f"[Nav] Login page detected (redirected from register)")
                    break

            # Check for redirect to app
            try:
                cur = await asyncio.wait_for(self._page.evaluate("location.href"), timeout=1.0)
                if "discord.com/app" in cur or "discord.com/channels" in cur:
                    self._log(f"[Nav] Redirected to app: {cur[:60]}")
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)

        # ── Form never rendered — dump page state for debugging ──
        try:
            dump = await asyncio.wait_for(self._page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input')).slice(0, 20).map(function(e) { return {
                    type: e.type, name: e.name, id: e.id, ariaLabel: e.getAttribute('aria-label') || '',
                    placeholder: e.placeholder || '', visible: e.offsetParent !== null
                }; });
                const buttons = Array.from(document.querySelectorAll('button')).slice(0, 10).map(function(e) { return {
                    text: (e.innerText || '').substring(0, 40), type: e.type, visible: e.offsetParent !== null
                }; });
                return JSON.stringify({
                    title: document.title,
                    url: location.href,
                    bodyText: (document.body?.innerText || '').substring(0, 200),
                    inputs: inputs,
                    buttons: buttons,
                    hasAppMount: document.querySelector('#app-mount') !== null
                });
            }"""), timeout=2.0)
            self._log(f"[Nav] DEBUG page state: {dump}")
        except Exception:
            pass

        proxy_label = "fresh proxy session" if self.proxy else "fresh TOR circuit"
        self._log(f"[Nav] Form did not render within 30s - rotating to {proxy_label}", level="warn")
        return False

    async def capture_screenshot(self) -> str:
        if not self._page:
            return ""
        screenshot = await self._page.screenshot(full_page=True)
        b64 = base64.b64encode(screenshot).decode('utf-8')
        self._screenshots.append(b64)
        if len(self._screenshots) > 100:
            self._screenshots = self._screenshots[-50:]
        return b64

    async def start_discord_signup(self) -> bool:
        if not self._page:
            await self.initialize()
        self.phone_verify_detected = False

        # No hardcoded email — try DraxonMails first (instant REST inbox on a
        # discord-friendly domain, no browser launch), fall back to cybertemp.
        if not self._email:
            self._log("[Mail] No email configured - creating DraxonMails inbox (discord-friendly)...")
            try:
                self._mail = DraxonMail(log=self._log)
                self._email = await self._mail.create_inbox()
            except Exception as e:
                self._log(f"[Mail] Draxon inbox error: {e}", level="error")
                self._email = ""
            if not self._email:
                self._log(f"[Mail] Draxon unavailable - falling back to cybertemp.xyz (@{self._domain})...", level="warn")
                try:
                    self._mail = TempMail(log=self._log, proxy=self.proxy,
                                          headless=self.headless, domain=self._domain)
                    self._email = await self._mail.create_inbox()
                except Exception as e:
                    self._log(f"[Mail] cybertemp inbox error: {e}", level="error")
                    self._email = ""

        if not self._email:
            self._log("[FAIL] No email available - aborting signup", level="error")
            return False

        self._log("=" * 40)
        self._log(f"Starting Discord signup with email: {self._email}")
        self._log("=" * 40)

        try:
            # Refresh-retry loop: a loaded drag captcha ("Wait! Are you
            # human?") can't be solved in-browser, so we refresh the page
            # for a fresh captcha up to 3 times before giving up and letting
            # the worker rotate proxy + fingerprint + mail domain.
            refresh_attempt = 0
            while True:
                refresh_attempt += 1
                if not await self._goto_register():
                    self._log("[FAIL] Could not navigate to Discord /register - aborting", level="error")
                    return False
                self._log("[Nav] Discord site rendered")
                await asyncio.sleep(1.5)
                await self.capture_screenshot()

                # Fill the form
                form_ok = await self._fill_registration_form()
                if not form_ok:
                    self._log("[FAIL] Form filling failed", level="error")
                    success = False
                    break

                self._log("[OK] Form filled - now solving captcha...")
                solve_result = await self._solve_hcaptcha_if_present()
                if solve_result == "refresh" and refresh_attempt < 3:
                    self._log(f"[Captcha] Refreshing page for fresh captcha ({refresh_attempt}/3)...")
                    await asyncio.sleep(1.5)
                    continue
                success = bool(solve_result)
                break

            if success:
                self._log("[OK] CAPTCHA SOLVED! Registration submitted.")
                # Discord can demand phone verification right after account
                # creation. Detect it BEFORE waiting on email — if present,
                # abort this attempt so the worker rotates proxy + fingerprint
                # + mail domain and retries (phone-gated accounts are dead).
                await asyncio.sleep(5)
                if await self._detect_phone_verification():
                    self.phone_verify_detected = True
                    self._log("[Phone] [DETECTED] Phone verification required - rotating proxy+fingerprint+domain", level="warn")
                    return False
                # Auto-verify: complete Discord email verification
                await self._verify_account_email()
                # Login + grab the FULL token from localStorage
                self._token = await self._extract_token()
                if self._token:
                    self._log("[Token] [OK] Full token captured")
                    self._log(f"[Account] @{self._username or self._email.split('@')[0]} is in Discord and confirmed")
                    self._log(f"[Account] Email={self._email} | User={self._username} | Pass={self._password} | Date={time.strftime('%Y-%m-%d %H:%M')}")
                    await self._humanize_account()
                else:
                    self._log("[Token] No token yet (account may still be pending)", level="warn")
            else:
                self._log("[FAIL] Captcha solving failed", level="error")

        except Exception as e:
            self._log(f"Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            success = False

        await self.capture_screenshot()
        return success

    async def _detect_phone_verification(self) -> bool:
        """Check the current page for Discord's phone-verification screen.

        Discord shows this right after account creation (or as a login gate)
        when it suspects automation. Markers: a phone/tel input, or a phone
        heading/body. Returns True when the account needs a phone number."""
        try:
            result = await asyncio.wait_for(self._page.evaluate("""() => {
                // Phone input (name=phone / type=tel / aria/placeholder)
                const phoneInput = document.querySelector(
                    'input[name="phone"], input[type="tel"], ' +
                    'input[aria-label*="phone" i], input[placeholder*="phone" i], ' +
                    'input[autocomplete="tel"]');
                if (phoneInput && phoneInput.offsetParent !== null) {
                    return 'input';
                }
                const text = (document.body ? document.body.innerText : '').toLowerCase();
                const markers = [
                    'verify your phone', 'phone verification', 'verify your account',
                    'add a phone number', 'phone number required',
                    'we need to verify your account', 'enter your phone number',
                    'confirm your phone', 'what\'s your phone number',
                    'verify via phone', 'add your phone number',
                ];
                for (const kw of markers) {
                    if (text.includes(kw)) return 'text:' + kw;
                }
                return '';
            }"""), timeout=4.0)
            return bool(result)
        except Exception:
            return False

    async def _verify_account_email(self) -> bool:
        """Wait for the Discord verification email and open its link (best effort).
        Aborts early if Discord instead demands phone verification."""
        if not self._mail:
            return False
        try:
            link = await self._mail.wait_for_verification_link(timeout=150)
            if not link:
                self._log("[Mail] No verification link found yet - account may still be created", level="warn")
                return False
            self._log(f"[Mail] Opening verification link: {link[:80]}...")
            await self._page.goto(link, wait_until='domcontentloaded', timeout=NAV_TIMEOUT_MS)
            await asyncio.sleep(2)
            # Discord shows a verification success page (or redirects to login)
            try:
                page_text = await self._page.evaluate(
                    "() => document.body.innerText.substring(0, 300)")
            except Exception:
                page_text = ""
            if any(w in (page_text or "").lower()
                   for w in ('verified', 'success', 'confirmation', 'you\'re all set')):
                self._log("[Mail] [OK] Email verification completed")
            await self.capture_screenshot()
            self._log("[Mail] [OK] Verification link opened")
            return True
        except Exception as e:
            self._log(f"[Mail] verification error: {e}", level="warn")
            return False

    async def _past_captcha(self) -> bool:
        """True when the page has moved past the captcha into Discord."""
        try:
            cur_url = self._page.url
            return any(k in cur_url for k in PAST_CAPTCHA_KEYWORDS)
        except:
            return False

    async def _detect_challenge_mode(self, iframe_element) -> str:
        """Identify the hCaptcha state: 'checkbox' or 'drag'.

        A checkbox widget is solvable via the NoCaptchaAI token API. A drag
        puzzle (piece to position) must be solved in-browser with the mouse.
        When there is no checkbox, an active challenge is showing - classify
        it as a drag puzzle so the in-browser solver gets a chance (it
        self-verifies and fast-fails back to the API if nothing is found).
        """
        try:
            frame = await iframe_element.content_frame()
            if not frame:
                return "drag"
            try:
                await frame.wait_for_selector('#checkbox', state='visible', timeout=1500)
                return "checkbox"
            except Exception:
                return "drag"
        except Exception:
            return "drag"

    async def _try_solve_drag(self, iframe) -> Optional[bool]:
        """Drag puzzles are no longer supported — the accessibility text
        solver is the only solver. Return None so the caller keeps trying.
        """
        self._log("[Captcha] Drag solver removed — using accessibility solver only",
                  level="warn")
        return None

    async def _extract_sitekey_with_retry(self, timeout: float = 15.0,
                                          poll: float = 3.0) -> str:
        """Extract the hCaptcha sitekey, polling until it is valid.

        The captcha iframe mounts before its src carries the sitekey, so
        extracting too early returns a partial/garbage value. Poll every
        `poll` seconds for up to `timeout` seconds and only accept a
        well-formed UUID sitekey (extraction is validated upstream).
        """
        deadline = time.time() + timeout
        attempts = 0
        while True:
            attempts += 1
            sitekey = await extract_hcaptcha_sitekey(self._page)
            if sitekey:
                self._log(f"[Captcha] Sitekey ready (attempt {attempts}): {sitekey[:16]}...")
                return sitekey
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._log(f"[Captcha] Sitekey not ready yet (attempt {attempts}) - "
                      f"retrying in {int(min(poll, remaining))}s", level="warn")
            await asyncio.sleep(min(poll, remaining))
        self._log("[Captcha] Sitekey never appeared after retries", level="error")
        return ""

    async def _click_form_submit(self) -> bool:
        """Click Create Account / Continue after the captcha token is in place."""
        try:
            result = await self._page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (btn.offsetParent === null) continue;
                    const t = btn.textContent.toLowerCase().trim();
                    if (t.includes('create account') || t.includes('continue') || t.includes('sign up')) {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return t.slice(0, 24);
                    }
                }
                const submit = document.querySelector('[type="submit"]');
                if (submit && submit.offsetParent !== null) {
                    submit.click();
                    return 'submit_btn';
                }
                const form = document.querySelector('form');
                if (form) {
                    if (form.requestSubmit) { form.requestSubmit(); return 'requestSubmit'; }
                    form.submit();
                    return 'form_submit';
                }
                return '';
            }""")
            if result:
                self._log(f"[Captcha] [OK] Submit clicked: {result}")
                return True
        except Exception as e:
            self._log(f"[Captcha] submit click error: {e}", level="warn")
        return False

    async def _solve_hcaptcha_if_present(self) -> bool:
        """Detect and solve the hCaptcha challenge.

        Smart detection: polls the DOM every second with real JS introspection.
        Knows the difference between widget-loading, challenge-loading, and
        challenge-ready states. Waits up to 60s for the challenge to fully render.
        """
        try:
            self._log("[Captcha] Checking for hCaptcha...")

            if await self._past_captcha():
                self._log(f"[Captcha] Already past captcha - at {self._page.url[:50]}")
                return True

            # ── Phase 1: Wait for ANY hCaptcha iframe to appear (widget or challenge) ──
            # The widget iframe (newassets.hcaptcha.com) loads first, then the
            # challenge iframe loads inside it. Poll every 0.5s for up to 50s —
            # if the challenge hasn't rendered by then, rotate instead of
            # hanging forever on a dead captcha.
            self._log("[Captcha] Waiting for hCaptcha to load (polling DOM every 0.5s, max 50s)...")
            deadline = time.time() + 50.0
            last_state = "waiting"
            iframe = None

            while time.time() < deadline:
                if await self._past_captcha():
                    self._log(f"[Captcha] Already past captcha — at {self._page.url[:50]}")
                    return True

                # Use JS to introspect the real hCaptcha state on the page
                try:
                    state = await self._page.evaluate("""() => {
                        // Check for hCaptcha widget iframe (the container)
                        const widgetFrame = document.querySelector('iframe[src*="newassets.hcaptcha.com"]');
                        // Check for hCaptcha challenge iframe (the actual puzzle)
                        const challengeFrame = document.querySelector('iframe[src*="hcaptcha-challenge"], iframe[title*="hCaptcha challenge"]');
                        // Check for anchor/hidden iframe (checkbox mode)
                        const anchorFrame = document.querySelector('iframe[src*="hcaptcha.com/1/api"]');
                        // Check for the captcha container div
                        const captchaDiv = document.querySelector('[data-hcaptcha-widget-id], .h-captcha');

                        // A rendered challenge (drag puzzle etc.) expands the
                        // widget iframe TALL, unlike the short checkbox widget.
                        const wRect = widgetFrame ? widgetFrame.getBoundingClientRect() : null;
                        const challengeTall = !!widgetFrame && widgetFrame.offsetParent !== null
                                              && wRect.height >= 250;

                        return {
                            hasWidget: !!widgetFrame,
                            hasChallenge: !!challengeFrame,
                            hasAnchor: !!anchorFrame,
                            hasContainer: !!captchaDiv,
                            challengeTall: challengeTall,
                            widgetSrc: widgetFrame ? widgetFrame.src.substring(0, 80) : '',
                            challengeSrc: challengeFrame ? (challengeFrame.src || challengeFrame.title || 'present').substring(0, 60) : '',
                            widgetVisible: widgetFrame ? (widgetFrame.offsetParent !== null) : false,
                            challengeVisible: challengeFrame ? (challengeFrame.offsetParent !== null) : false,
                            frameCount: document.querySelectorAll('iframe').length,
                            bodyText: (document.body ? document.body.innerText.substring(0, 200) : '').toLowerCase(),
                        };
                    }""", timeout=3000)
                except Exception as e:
                    state = {"hasWidget": False, "hasChallenge": False, "error": str(e)[:60]}

                # ── Fast-fail: rate limiting ──
                body = state.get("bodyText", "")
                if any(k in body for k in ("rate limit", "ratelimited", "too many requests",
                                           "slowdown", "try again later", "you are being rate")):
                    self._log("[Captcha] RATE LIMITED — rotating circuit", level="warn")
                    return False

                # ── State machine ──
                new_state = "waiting"
                if (state.get("hasChallenge") and state.get("challengeVisible")) or state.get("challengeTall"):
                    new_state = "challenge-ready"
                elif state.get("hasChallenge"):
                    new_state = "challenge-loading"
                elif state.get("hasWidget"):
                    new_state = "widget-loaded"
                elif state.get("hasContainer") or state.get("hasAnchor"):
                    new_state = "widget-loading"

                # Log ONLY state changes — the old per-poll "[Ns] state=..."
                # counter spammed a line every 0.5s and hid the real status.
                if new_state != last_state:
                    self._log(f"[Captcha] State: {last_state} → {new_state}")
                    last_state = new_state

                # ── Challenge is fully loaded → grab the iframe and solve ──
                if new_state == "challenge-ready":
                    try:
                        iframe = await self._page.query_selector(
                            'iframe[title*="hCaptcha challenge"], iframe[src*="hcaptcha-challenge"]'
                        )
                    except Exception:
                        iframe = None
                    if not iframe:
                        # Drag challenges render INSIDE the widget iframe (it
                        # grows tall) — use it directly instead of waiting.
                        try:
                            iframe = await self._page.query_selector(
                                'iframe[src*="newassets.hcaptcha.com"]'
                            )
                        except Exception:
                            iframe = None
                    if iframe:
                        self._log("[Captcha] [READY] Challenge rendered (widget) — solving now")
                        break

                # ── If widget is visible but no challenge yet, keep waiting ──
                if new_state in ("widget-loaded", "widget-loading"):
                    await asyncio.sleep(0.5)
                    continue

                # ── If nothing is visible yet, keep polling ──
                await asyncio.sleep(0.5)

            # ── Fallback: if we timed out but have ANY hCaptcha iframe, try it ──
            if not iframe:
                try:
                    iframe = await self._page.query_selector('iframe[src*="hcaptcha.com"]')
                    if iframe:
                        self._log("[Captcha] Using fallback iframe (challenge may not be fully loaded)",
                                  level="warn")
                except Exception:
                    pass

            if not iframe:
                # No hCaptcha iframe - check for FunCAPTCHA (Arkose) instead
                try:
                    if await self._past_captcha():
                        self._log(f"[Captcha] Registration went through - at {self._page.url[:50]}")
                        return True
                    page_text = await self._page.evaluate(
                        "() => document.body.innerText.substring(0, 500)")
                    has_captcha_text = ('captcha' in page_text.lower()
                                        or 'security' in page_text.lower()
                                        or 'verify' in page_text.lower())
                    if has_captcha_text:
                        self._log("[Captcha] FunCAPTCHA detected - pixel tile solver...")
                        return await self._solve_funcaptcha()
                    self._log(f"[Captcha] No captcha indicators on page: {self._page.url[:40]}", level="warn")
                    return False
                except Exception as e:
                    self._log(f"[Captcha] Captcha check error: {e}", level="warn")
                return False

            # Which challenge is showing?
            mode = await self._detect_challenge_mode(iframe)
            self._log(f"[Captcha] Challenge mode: {mode}")

            # ── DRAG CHALLENGE ("Wait! Are you human?") → REFRESH + RETRY ──
            # Drag puzzles can't be solved in-browser reliably — the
            # accessibility route grinds for minutes. Instead, signal the
            # caller to refresh the page for a FRESH captcha (often a
            # checkbox type). Capped at 3 refreshes, then the worker
            # rotates proxy + fingerprint + mail domain.
            if mode == "drag":
                self._log("[Captcha] Drag challenge loaded — refreshing page for a fresh captcha")
                return "refresh"

            # ── ACCESSIBILITY CHALLENGE (checkbox widget / other types) ──
            # Opens the 3-dots menu and uses the Accessibility Challenge,
            # which gives a text/audio question that's solvable locally
            # (math, word puzzles) with Ollama vision as fallback.
            self._log("[Captcha] Trying accessibility challenge (only solver)...")
            acc_result = await solve_hcaptcha_accessibility(self._page, iframe, log=self._log)
            if acc_result:
                self._log("[Captcha] [OK] Accessibility challenge solved!")
                # ── Detect ANY new hCaptcha that appears after a completed one ──
                # hCaptcha chains captchas: right after one finishes, a brand-new
                # challenge can pop up. The WIDGET iframe (newassets.hcaptcha.com)
                # stays in the DOM forever, so we ONLY look for the challenge
                # iframe (title="hCaptcha challenge" / hcaptcha-challenge.html)
                # to avoid re-solving the idle widget.
                solved_srcs = set()
                try:
                    solved_srcs.add(await iframe.get_attribute("src") or "")
                except Exception:
                    pass
                idle_checks = 0
                for check_i in range(4):
                    await asyncio.sleep(1.0)
                    if await self._past_captcha():
                        self._log("[Captcha] Page past captcha — clicking Create Account")
                        await self._click_form_submit()
                        return True
                    new_challenge = None
                    try:
                        new_challenge = await self._page.query_selector(
                            'iframe[title="hCaptcha challenge"], '
                            'iframe[src*="hcaptcha-challenge"]'
                        )
                    except Exception:
                        new_challenge = None
                    if new_challenge:
                        idle_checks = 0
                        try:
                            new_src = await new_challenge.get_attribute("src") or ""
                        except Exception:
                            new_src = ""
                        if new_src in solved_srcs:
                            # Same challenge element still closing — not new.
                            self._log(f"[Captcha] Same challenge still present ({check_i+1}/6)")
                            continue
                        solved_srcs.add(new_src)
                        self._log("[Captcha] NEW captcha detected — clicking 3-dots + accessibility again")
                        acc_result = await solve_hcaptcha_accessibility(
                            self._page, new_challenge, log=self._log
                        )
                        if not acc_result:
                            self._log("[Captcha] Chain captcha failed", level="error")
                            return False
                        continue  # solved — keep checking for the next one
                    idle_checks += 1
                    if idle_checks >= 2:
                        self._log("[Captcha] No new challenge — captcha fully done!")
                        break
                    self._log(f"[Captcha] No challenge yet ({check_i+1}/6)...")
                # Captcha chain finished — proceed to Create Account
                await self._click_form_submit()
                await asyncio.sleep(1.5)
                return True
            else:
                self._log("[Captcha] [FAIL] Accessibility challenge did not solve",
                          level="error")
                await asyncio.sleep(2)
                return False

        except Exception as e:
            self._log(f"[Captcha] Flow error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _solve_funcaptcha(self) -> bool:
        """Solve FunCAPTCHA tile challenges with the offline pixel solver."""
        try:
            task = await extract_funcaptcha_task(self._page)
            if task:
                self._log(f"[FunCAPTCHA] Challenge: {task[:80]}")
            solved = await solve_funcaptcha_pixels(self._page, log=self._log)
            if solved:
                await self._click_form_submit()
                return True
            self._log("[FunCAPTCHA] [FAIL] Could not solve challenge", level="error")
            return False
        except Exception as e:
            self._log(f"[FunCAPTCHA] Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _select_dob(self, label: str, option_text: str) -> bool:
        """Select DOB dropdown. Discord uses custom React-Select components."""
        try:
            self._log(f"Selecting {label}: {option_text}")

            # Strategy 1: JS click on placeholder, then find and click option
            success = await self._page.evaluate(f"""
                async () => {{
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    let targetEl = null;
                    while (node = walker.nextNode()) {{
                        if (node.textContent.trim() === '{label}') {{
                            const parent = node.parentElement;
                            if (parent && parent.offsetParent !== null &&
                                !parent.querySelector('input[name="email"]')) {{
                                targetEl = parent;
                                break;
                            }}
                        }}
                    }}

                    if (!targetEl) return 'no_element';

                    let clickTarget = targetEl;
                    for (let i = 0; i < 5; i++) {{
                        clickTarget = clickTarget.parentElement;
                        if (!clickTarget) break;
                        const style = window.getComputedStyle(clickTarget);
                        if (style.cursor === 'pointer' ||
                            clickTarget.getAttribute('tabindex') !== null ||
                            clickTarget.className.includes('control') ||
                            clickTarget.className.includes('css-')) {{
                            break;
                        }}
                    }}

                    if (!clickTarget) clickTarget = targetEl;
                    clickTarget.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
                    await new Promise(r => setTimeout(r, 600));

                    const allOptions = document.querySelectorAll(
                        '[id*="option"], [role="option"], [class*="option"]'
                    );
                    for (const opt of allOptions) {{
                        const text = opt.textContent.trim();
                        if (text === '{option_text}') {{
                            opt.scrollIntoView({{block: 'nearest'}});
                            opt.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                            opt.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                            opt.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                            return 'selected';
                        }}
                    }}
                    return 'option_not_found';
                }}
            """)

            if success and 'selected' in str(success):
                self._log(f"Selected {label}: {option_text} ({success})")
                await asyncio.sleep(0.4)
                return True

            self._log(f"JS result for {label}: {success}")

            # Strategy 2: Click the placeholder text, then type to filter
            try:
                placeholder = self._page.get_by_text(label, exact=True)
                count = await asyncio.wait_for(placeholder.count(), timeout=2.0)
                if count > 0:
                    await placeholder.first.click()
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.type(option_text, delay=30)
                    await asyncio.sleep(0.4)
                    await self._page.keyboard.press('Enter')
                    await asyncio.sleep(0.4)
                    self._log(f"Selected {label} via text click")
                    return True
            except:
                pass

            # Strategy 3: Tab navigation
            try:
                idx = {"Month": 0, "Day": 1, "Year": 2}.get(label, 0)
                pw = self._page.locator('input[name="password"]')
                count = await asyncio.wait_for(pw.count(), timeout=2.0)
                if count > 0:
                    await pw.click()
                    await asyncio.sleep(0.2)
                    for _ in range(idx + 1):
                        await self._page.keyboard.press('Tab')
                        await asyncio.sleep(0.15)
                    await self._page.keyboard.press('Space')
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.type(option_text, delay=30)
                    await asyncio.sleep(0.3)
                    await self._page.keyboard.press('Enter')
                    await asyncio.sleep(0.4)
                    self._log(f"Selected {label} via tab")
                    return True
            except:
                pass

            self._log(f"All DOB strategies failed for {label}")
            return False

        except Exception as e:
            self._log(f"DOB error for {label}: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _fill_registration_form(self) -> bool:
        try:
            self._log("=" * 40)
            self._log("FILLING REGISTRATION FORM")
            self._log("=" * 40)
            self._log(f"Email: {self._email}")

            # ── Handle Discord age gate (birthday modal before form) ──
            for _ in range(6):
                try:
                    age_text = await self._page.evaluate(
                        "() => (document.body.innerText || '').substring(0, 300)")
                except Exception:
                    age_text = ""
                has_age_gate = any(w in age_text.lower() for w in
                                   ('birthday', 'date of birth', 'born', 'how old'))
                has_form = ('email' in age_text.lower() and 'username' in age_text.lower())

                if has_form:
                    self._log("[Form] Registration form detected — no age gate")
                    break
                if has_age_gate:
                    self._log("[Form] Age gate detected — filling DOB...")
                    # Discord age gate: pick adult DOB
                    try:
                        await self._page.evaluate("""() => {
                            const pickers = document.querySelectorAll('input, [role="combobox"], [class*="select"]');
                            for (const p of pickers) {
                                if (p.offsetParent === null) continue;
                                const label = (p.getAttribute('aria-label') || p.getAttribute('placeholder') || '').toLowerCase();
                                // Click and select month/day/year
                                p.click();
                                break;
                            }
                        }""")
                    except Exception:
                        pass
                    # Try typing DOB fields directly
                    try:
                        inputs = self._page.locator('input[type="text"], input:not([type])')
                        count = await inputs.count()
                        if count >= 3:
                            await inputs.nth(0).fill("January")
                            await inputs.nth(1).fill("15")
                            await inputs.nth(2).fill("1995")
                            await self._page.keyboard.press("Enter")
                    except Exception:
                        pass
                    try:
                        await self._page.keyboard.type("01", delay=30)
                        await self._page.keyboard.press("Tab")
                        await self._page.keyboard.type("15", delay=30)
                        await self._page.keyboard.press("Tab")
                        await self._page.keyboard.type("1995", delay=30)
                        await self._page.keyboard.press("Enter")
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    continue
                # Neither form nor age gate — page may still be loading
                self._log(f"[Form] Waiting for page content... ({_+1}/6)")
                await asyncio.sleep(1.5)

            # ── Wait for email input with multiple fallback selectors ──
            email_input = None
            for selector in (
                'input[name="email"]',
                'input[type="email"]',
                'input[id*="email" i]',
                'input[aria-label*="email" i]',
                'input[placeholder*="email" i]',
                'input[autocomplete="email"]',
            ):
                try:
                    await self._page.wait_for_selector(selector, timeout=8000)
                    email_input = self._page.locator(selector)
                    self._log(f"[Form] Email input found: {selector}")
                    break
                except Exception:
                    continue

            if not email_input:
                # Last resort: type into the first visible text input
                try:
                    all_inputs = self._page.locator('input:not([type="hidden"]):not([type="submit"])')
                    count = await all_inputs.count()
                    if count > 0:
                        email_input = all_inputs.first
                        self._log("[Form] Using first visible input as email", level="warn")
                except Exception:
                    pass

            if not email_input:
                self._log("[Form] No email input found on page", level="error")
                await self.capture_screenshot()
                return False

            await email_input.click()
            await asyncio.sleep(random.uniform(0.4, 0.9))
            await human_type(self._page, 'input[name="email"]', self._email)
            await asyncio.sleep(random.uniform(0.5, 1.2))

            # Generate username with random digits suffix (more human-like)
            consonants = 'bcdfghjklmnpqrstvwxyz'
            vowels = 'aeiou'
            username = ''
            for _ in range(random.randint(8, 12)):
                username += random.choice(vowels if random.random() < 0.35 else consonants)
            # Add 3-4 random digits so Discord doesn't flag as bot-pattern
            username += str(random.randint(100, 9999))
            self._username = username
            display_name = self._username[:15]

            self._log(f"Display name: {display_name}")
            try:
                await self._page.wait_for_selector('input[name="global_name"]', timeout=5000)
                await self._page.locator('input[name="global_name"]').click()
                await asyncio.sleep(random.uniform(0.3, 0.7))
                await human_type(self._page, 'input[name="global_name"]', display_name)
                await asyncio.sleep(random.uniform(0.4, 1.0))
            except:
                pass
            await self._human_pause()

            self._log(f"Username: {self._username}")
            await self._page.locator('input[name="username"]').click()
            await asyncio.sleep(random.uniform(0.4, 0.9))
            await human_type(self._page, 'input[name="username"]', self._username)
            await asyncio.sleep(random.uniform(0.5, 1.2))

            # Generate password
            first = random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')
            body = ''
            for _ in range(random.randint(8, 11)):
                body += random.choice(vowels if random.random() < 0.35 else consonants)
            specials = '!@#$%&*'
            self._password = first + body + random.choice(specials) + str(random.randint(1, 99))

            self._log("Filling password")
            await self._page.locator('input[name="password"]').click()
            await asyncio.sleep(random.uniform(0.4, 0.9))
            await human_type(self._page, 'input[name="password"]', self._password)
            await asyncio.sleep(random.uniform(0.5, 1.2))

            # DOB
            month_val = random.randint(1, 12)
            day_val = str(random.randint(1, 28))
            year_val = str(random.randint(1990, 1999))
            months = ['January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December']
            month_name = months[month_val - 1]
            self._log(f"DOB: {month_name} {day_val}, {year_val}")

            await self._select_dob("Month", month_name)
            await self._human_pause()
            await self._select_dob("Day", day_val)
            await self._human_pause()
            await self._select_dob("Year", year_val)
            await self._human_pause()

            # ── ToS Checkbox — FIND THE CORRECT ONE (Terms of Service, not newsletter) ──
            self._log("Checking ToS checkbox...")
            tos_checked = False

            try:
                tos_result = await self._page.evaluate("""() => {
                    // Find ANY text node containing ToS-like keywords
                    const tosKeywords = ['terms of service', 'terms of use', 'terms & conditions',
                                        'terms and conditions', 'i have read', 'read and agree',
                                        'agree to', 'by creating'];
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                    let node;
                    while (node = walker.nextNode()) {
                        const t = node.textContent.trim().toLowerCase();
                        if (!tosKeywords.some(k => t.includes(k))) continue;

                        // Walk UP to find a clickable container that has a checkbox nearby
                        let el = node.parentElement;
                        for (let i = 0; i < 10 && el; i++) {
                            // Look for checkbox inside this container
                            const cb = el.querySelector('input[type="checkbox"]');
                            if (cb) {
                                cb.scrollIntoView({block: 'center'});
                                // Prefer clicking the wrapping label / clickable parent
                                // (trusted gesture -> React state actually updates).
                                const clickable = cb.closest('label') || cb.parentElement;
                                let r = cb.getBoundingClientRect();
                                if (clickable && clickable.offsetParent !== null) {
                                    const cr = clickable.getBoundingClientRect();
                                    if (cr.width >= 5 && cr.height >= 5) r = cr;
                                }
                                window.__tosPoint = {x: r.x + r.width/2, y: r.y + r.height/2, kind: 'native'};
                                return JSON.stringify(window.__tosPoint);
                            }
                            // Also try role=checkbox
                            const roleCb = el.querySelector('[role="checkbox"]');
                            if (roleCb) {
                                roleCb.scrollIntoView({block: 'center'});
                                const rr = roleCb.getBoundingClientRect();
                                window.__tosPoint = {x: rr.x + rr.width/2, y: rr.y + rr.height/2, kind: 'role'};
                                return JSON.stringify(window.__tosPoint);
                            }
                            el = el.parentElement;
                        }
                    }

                    // FALLBACK: check ALL visible unchecked checkboxes (Discord has max 2)
                    const allCbs = document.querySelectorAll('input[type="checkbox"]');
                    let checked = 0;
                    for (const cb of allCbs) {
                        if (cb.offsetParent === null) continue;
                        if (cb.checked) continue;
                        cb.scrollIntoView({block: 'center'});
                        cb.click();
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                        cb.dispatchEvent(new Event('input', { bubbles: true }));
                        checked++;
                    }
                    if (checked > 0) return 'fallback_checked_' + checked;

                    // Also try role checkboxes
                    const roleCbs = document.querySelectorAll('[role="checkbox"]');
                    for (const rc of roleCbs) {
                        if (rc.offsetParent === null) continue;
                        if (rc.getAttribute('aria-checked') === 'true') continue;
                        rc.click();
                        rc.setAttribute('aria-checked', 'true');
                        rc.dispatchEvent(new Event('change', { bubbles: true }));
                        checked++;
                    }
                    if (checked > 0) return 'role_fallback_' + checked;

                    return 'not_found';
                }""")
                tos_point = None
                try:
                    if tos_result and tos_result.startswith('{'):
                        tos_point = json.loads(tos_result)
                except Exception:
                    tos_point = None
                if tos_point and 'x' in tos_point and 'y' in tos_point:
                    # REAL trusted mouse click — this is what makes Discord's
                    # React form actually register the checkbox state.
                    await self._page.mouse.click(float(tos_point['x']), float(tos_point['y']))
                    await asyncio.sleep(1.2)
                    self._log(f"[OK] ToS real mouse click dispatched (kind={tos_point.get('kind')})")
                    tos_checked = True
                elif tos_result and tos_result != 'not_found':
                    tos_checked = True
                    self._log(f"[OK] ToS checked via JS: {tos_result}")
                else:
                    self._log(f"[WARN] ToS checkbox not found by JS ({tos_result}) - trying Playwright locator...")
                    # Playwright fallback: click any visible checkbox input
                    try:
                        checkboxes = self._page.locator('input[type="checkbox"]:visible')
                        cb_count = await checkboxes.count()
                        for i in range(cb_count):
                            cb = checkboxes.nth(i)
                            is_checked = await cb.is_checked()
                            if not is_checked:
                                await cb.scroll_into_view_if_needed()
                                await cb.check(force=True)
                                self._log(f"[OK] ToS checkbox {i} checked via Playwright")
                                tos_checked = True
                    except Exception as pw_e:
                        self._log(f"Playwright checkbox fallback error: {pw_e}", level="warn")
            except Exception as e:
                self._log(f"ToS JS evaluate error: {e}", level="warn")

            if tos_checked:
                self._log("[OK] ToS checkbox checked")
            else:
                self._log("[WARN] No ToS checkbox found - the Create Account button may be disabled")

            # Wait for React to process the checkbox change
            await asyncio.sleep(2.0)  # Increased: React needs time to re-render enabled state

            # ── VERIFY ToS is actually checked before trying Create Account ──
            try:
                verify = await self._page.evaluate("""() => {
                    const cbs = document.querySelectorAll('input[type="checkbox"]');
                    let checked = 0;
                    for (const cb of cbs) {
                        if (cb.checked) checked++;
                    }
                    const roleCbs = document.querySelectorAll('[role="checkbox"][aria-checked="true"]');
                    return { native: checked, role: roleCbs.length };
                }""")
                self._log(f"[Form] Checkbox state: native={verify.get('native',0)} role={verify.get('role',0)}")
            except Exception:
                pass

            # ── Create Account Button — try multiple strategies ────────
            self._log("Clicking Create Account...")
            create_clicked = False

            for click_attempt in range(4):
                if create_clicked:
                    break
                if click_attempt > 0:
                    self._log(f"Retrying Create Account click (attempt {click_attempt+1}/4)...")
                    # Re-check ALL unchecked checkboxes on retry (React may have reset them)
                    try:
                        checkboxes = self._page.locator('input[type="checkbox"]:visible')
                        cb_count = await checkboxes.count()
                        rechecked = 0
                        for i in range(cb_count):
                            try:
                                if not await checkboxes.nth(i).is_checked():
                                    await checkboxes.nth(i).scroll_into_view_if_needed()
                                    await checkboxes.nth(i).check(force=True)
                                    rechecked += 1
                            except Exception:
                                pass
                        if rechecked:
                            self._log(f"Re-checked {rechecked} checkbox(es)")
                            # Also re-check role checkboxes
                            try:
                                await self._page.evaluate("""() => {
                                    const rcs = document.querySelectorAll('[role="checkbox"]');
                                    for (const rc of rcs) {
                                        if (rc.getAttribute('aria-checked') !== 'true') {
                                            rc.click();
                                            rc.setAttribute('aria-checked', 'true');
                                            rc.dispatchEvent(new Event('change', {bubbles: true}));
                                        }
                                    }
                                }""")
                            except Exception:
                                pass
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)  # Longer wait for React to process

                try:
                    result = await self._page.evaluate("""() => {
                        // Strategy 1: Find button by text content (most reliable)
                        const btns = document.querySelectorAll('button, [role="button"], [type="submit"]');
                        for (const btn of btns) {
                            if (btn.offsetParent === null) continue;
                            // Check if disabled
                            if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                            const t = (btn.textContent || '').toLowerCase().trim();
                            const v = (btn.value || '').toLowerCase().trim();
                            if (t.includes('create account') || t.includes('sign up') || t.includes('continue') ||
                                v.includes('create account') || v.includes('sign up')) {
                                btn.scrollIntoView({block: 'center'});
                                btn.click();
                                return 'btn_' + t.slice(0, 20);
                            }
                        }

                        // Strategy 2: Find any submit-type button
                        for (const btn of btns) {
                            if (btn.offsetParent === null) continue;
                            if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                            if (btn.getAttribute('type') === 'submit' || btn.tagName === 'BUTTON') {
                                const t = btn.textContent.toLowerCase().trim();
                                if (t.length > 4) {  // has meaningful text
                                    btn.scrollIntoView({block: 'center'});
                                    btn.click();
                                    return 'btntype_' + t.slice(0, 20);
                                }
                            }
                        }

                        // Strategy 3: Form submit
                        const forms = document.querySelectorAll('form');
                        for (const form of forms) {
                            if (form.offsetParent === null) continue;
                            if (form.requestSubmit) {
                                form.requestSubmit();
                                return 'form_requestSubmit';
                            }
                            form.submit();
                            return 'form_submit';
                        }

                        return 'failed';
                    }""")
                    if result and result != 'failed':
                        create_clicked = True
                        self._log(f"[OK] Account button clicked: {result}")
                        break
                except Exception as e:
                    self._log(f"Create Account JS attempt {click_attempt+1} error: {e}", level="warn")

                # Playwright fallback: click the button directly
                if not create_clicked:
                    try:
                        btn_selectors = [
                            'button:has-text("Create Account")',
                            'button:has-text("Sign Up")',
                            'button:has-text("Continue")',
                            'button[type="submit"]',
                        ]
                        for sel in btn_selectors:
                            try:
                                btn = self._page.locator(sel).first
                                if await btn.count() > 0:
                                    is_disabled = await btn.is_disabled()
                                    if not is_disabled:
                                        await btn.scroll_into_view_if_needed()
                                        await btn.click()
                                        self._log(f"[OK] Playwright click: {sel}")
                                        create_clicked = True
                                        break
                            except Exception:
                                continue
                    except Exception as pw_e:
                        self._log(f"Playwright button click error: {pw_e}", level="warn")

                # Last resort: Enter key on password field
                if not create_clicked:
                    try:
                        await self._page.locator('input[name="password"]').press('Enter')
                        self._log("Pressed Enter on password field")
                        create_clicked = True
                    except Exception:
                        pass

            if create_clicked:
                self._log("[OK] Create Account submitted - waiting for response...")
            else:
                self._log("[FAIL] Could not click Create Account after all attempts!", level="error")
                await self.capture_screenshot()
                return False

            await asyncio.sleep(2)
            await self.capture_screenshot()

            return True


        except Exception as e:
            self._log(f"Form filling error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _human_pause(self) -> None:
        await asyncio.sleep(random.uniform(0.1, 0.5))

    async def live_camera_loop(self, interval: int = 4) -> None:
        while True:
            await self.capture_screenshot()
            await asyncio.sleep(interval)

    async def _extract_token(self, attempts: int = 4) -> str:
        """Login to Discord with the created account and grab the FULL token
        from localStorage. Discord stores it under 'token'."""
        if not (self._email and self._password):
            return ""
        try:
            for i in range(attempts):
                try:
                    await self._page.goto("https://discord.com/login",
                                          wait_until="domcontentloaded",
                                          timeout=NAV_TIMEOUT_MS)
                    break
                except Exception:
                    await asyncio.sleep(2)
            await asyncio.sleep(2)
            try:
                email_input = self._page.locator('input[name="email"]').first
                await email_input.fill(self._email, timeout=8000)
                pw_input = self._page.locator('input[name="password"]').first
                await pw_input.fill(self._password, timeout=8000)
                await pw_input.press("Enter")
                self._log("[Token] Submitted login form")
            except Exception as e:
                self._log(f"[Token] Login fill error: {e}", level="warn")
                return ""
            # Wait for token to appear (abort early if phone-gated at login)
            for _ in range(10):
                await asyncio.sleep(2.0)
                try:
                    if await self._detect_phone_verification():
                        self.phone_verify_detected = True
                        self._log("[Phone] [DETECTED] login gated by phone verification", level="warn")
                        return ""
                except Exception:
                    pass
                try:
                    token = await self._page.evaluate(
                        "() => localStorage.getItem('token') || ''"
                    )
                    if token and len(token) > 20:
                        return token.strip()
                except Exception:
                    pass
            return ""
        except Exception as e:
            self._log(f"[Token] extract error: {e}", level="warn")
            return ""

    def get_account(self) -> dict:
        """Return the generated account info (email, user, pass, full token)."""
        return {
            "email": self._email,
            "username": self._username,
            "password": self._password,
            "token": self._token,
            "proxy": self.proxy,
            "worker_id": self.worker_id,
            "user_id": self._user_id,
            "avatar": self._avatar_data,
            "bio": self._bio,
            "humanized": self._humanized,
            "domain": self._domain,
        }

    async def close(self) -> None:
        if self._mail:
            try:
                await self._mail.close()
            except Exception:
                pass
            self._mail = None
        if self._page:
            try:
                await self._page.close()
            except:
                pass
            self._page = None
        if self._context:
            try:
                await self._context.close()
            except:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except:
                pass
            self._playwright = None

    def get_screenshots(self) -> list:
        return self._screenshots

    def get_latest_screenshot(self) -> str:
        if self._screenshots:
            return self._screenshots[-1]
        return ""


async def run_discord_automation():
    # Standalone CLI path — use a residential session when available
    # (vaultproxies.txt / VAULTPROXY_* env), TOR otherwise.
    proxy = None
    try:
        from proxies import pool as _proxy_pool
        if _proxy_pool.count == 0:
            await _proxy_pool.refresh()
        if _proxy_pool.count > 0:
            proxy = _proxy_pool.take()
            print(f"[CLI] Using proxy session: {proxy.get('key', '?')[:48]}...", flush=True)
    except Exception as e:
        print(f"[CLI] Proxy pool unavailable ({e}) — using TOR", flush=True)
    bot = DiscordAutomation(headless=True, proxy=proxy)
    try:
        await bot.initialize()
        success = await bot.start_discord_signup()
        if success:
            print("[OK] Discord automation completed")
        else:
            print("[FAIL] Discord automation failed")
        await asyncio.sleep(5)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(run_discord_automation())
