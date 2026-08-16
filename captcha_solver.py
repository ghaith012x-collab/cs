#!/usr/bin/env python3
"""NoneCap hCaptcha solver — thin async client + DOM helpers.

All captcha solving is delegated to the NoneCap API
(https://nonecap.com). Send the exact sitekey (read from the live page only
after the widget has fully rendered) plus the current page URL; NoneCap
returns a real P1_ token that we inject into the page's ``h-captcha-response``
field before submitting the form.

Auth: reads ``NONECAP_API_KEY`` from the environment (falls back to
``API_KEY``). The key is a ``nc_live_…`` bearer token minted at
https://dashboard.nonecap.com/keys.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Dict, Optional

import aiohttp

NONECAP_BASE = os.environ.get(
    "NONECAP_BASE", "https://api.nonecap.com/v1"
).rstrip("/")

_SITEKEY_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _api_key() -> str:
    return (os.environ.get("NONECAP_API_KEY") or os.environ.get("API_KEY")
            or "").strip()


class NoneCapClient:
    """Async client for the NoneCap hCaptcha solve API."""

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self.stats = {"calls": 0, "ok": 0, "failed": 0}

    @property
    def configured(self) -> bool:
        return bool(_api_key())

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        }

    async def solve(self, sitekey: str, pageurl: str, rqdata: str = "",
                    proxy: Optional[str] = None,
                    timeout: float = 120.0) -> Optional[dict]:
        """Create a solve, poll to terminal, and return token + solve_id.

        Failed solves are never charged by NoneCap.
        """
        if not self.configured:
            self._log("[NoneCap] No API key (set NONECAP_API_KEY)", level="error")
            return None
        self.stats["calls"] += 1
        payload = {
            "type": "hcaptcha_enterprise" if rqdata else "hcaptcha",
            "sitekey": sitekey,
            "url": pageurl,
        }
        if rqdata:
            payload["rqdata"] = rqdata
        if proxy:
            payload["proxy"] = proxy
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as s:
                async with s.post(
                    f"{NONECAP_BASE}/solves",
                    params={"wait": 30},
                    headers=self._headers(),
                    json=payload,
                ) as r:
                    if r.status not in (200, 202):
                        body = await r.text()
                        self._log(
                            f"[NoneCap] Solve rejected (HTTP {r.status}): {body[:200]}",
                            level="warn")
                        self.stats["failed"] += 1
                        return None
                    solve = await r.json()
                solve_id = str(solve.get("id") or "")
                # wait=30 may leave the solve pending; poll until terminal.
                if str(solve.get("status")) in ("pending", "solving"):
                    solve = await self._poll(solve_id, timeout)
                token = solve.get("token") if isinstance(solve, dict) else None
                if solve_id and token:
                    self.stats["ok"] += 1
                    self._log(
                        f"[NoneCap] [OK] Token received ({len(token)} chars, {solve_id})")
                    return {"token": str(token), "solve_id": solve_id}
                err = (solve or {}).get("error") or {}
                self._log(
                    f"[NoneCap] Solve failed: {err.get('code', 'unknown')} "
                    f"{str(err.get('message', ''))[:120]}",
                    level="warn")
                self.stats["failed"] += 1
                return None
        except Exception as e:
            self._log(f"[NoneCap] Solve error: {e}", level="error")
            self.stats["failed"] += 1
            return None

    async def _poll(self, solve_id: str, timeout: float) -> Optional[dict]:
        deadline = time.time() + max(timeout, 30)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as s:
                while time.time() < deadline:
                    async with s.get(
                        f"{NONECAP_BASE}/solves/{solve_id}",
                        params={"wait": 15},
                        headers=self._headers(),
                    ) as r:
                        if r.status != 200:
                            return None
                        data = await r.json()
                    if str(data.get("status")) not in ("pending", "solving"):
                        return data
        except Exception:
            pass
        return None

    async def report(self, solve_id: str, outcome: str = "accepted",
                     reason: str = "") -> None:
        """Tell NoneCap whether the token was accepted downstream."""
        if not solve_id or not self.configured:
            return
        item = {"solve_id": solve_id, "outcome": outcome}
        if reason:
            item["reason"] = reason
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as s:
                async with s.post(
                    f"{NONECAP_BASE}/feedback",
                    headers=self._headers(),
                    json={"feedback": [item]},
                ) as r:
                    self._log(
                        f"[NoneCap] Feedback reported ({outcome}, HTTP {r.status})")
        except Exception as e:
            self._log(f"[NoneCap] Feedback error: {e}", level="warn")


# ═══════════════════════════════════════════════════════════════
# DOM helpers
# ═══════════════════════════════════════════════════════════════

def _is_valid_sitekey(value: str) -> bool:
    return bool(_SITEKEY_RE.match((value or "").strip()))


async def extract_hcaptcha_sitekey(page) -> str:
    """Pull the hCaptcha sitekey from the live DOM/iframe src/hcaptcha global.

    Only accepts a well-formed UUID so a half-mounted widget can never
    produce a garbage sitekey.
    """
    try:
        sk = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            return el ? el.getAttribute('data-sitekey') : '';
        }""")
        if _is_valid_sitekey(str(sk)):
            return str(sk).strip()
    except Exception:
        pass
    try:
        src = await page.evaluate("""() => {
            const f = document.querySelector('iframe[src*="hcaptcha.com"]');
            return f ? f.src : '';
        }""")
        m = re.search(r"sitekey=([^&]+)", src or "")
        if m and _is_valid_sitekey(m.group(1)):
            return m.group(1)
    except Exception:
        pass
    try:
        sitekey = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            for (const f of iframes) {
                const m = (f.src || '').match(/sitekey=([^&#]+)/);
                if (m) return m[1];
            }
            return '';
        }""")
        if _is_valid_sitekey(sitekey):
            return sitekey.strip()
    except Exception:
        pass
    try:
        sk = await page.evaluate("""() => {
            if (window.hcaptcha && window.hcaptcha.getSitekey) {
                try { return window.hcaptcha.getSitekey(); } catch(e) {}
            }
            return '';
        }""")
        if _is_valid_sitekey(str(sk)):
            return str(sk).strip()
    except Exception:
        pass
    return ""


async def extract_hcaptcha_rqdata(page) -> str:
    """Pull the hCaptcha Enterprise rqdata from the live page (best effort)."""
    try:
        val = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            if (el) {
                const v = el.getAttribute('data-rqdata') || el.getAttribute('rqdata');
                if (v && v.length > 8) return v;
            }
            for (const s of document.querySelectorAll('script')) {
                const t = s.textContent || '';
                const m = t.match(/"rqdata"\s*:\s*"([^"]{8,})"/) ||
                          t.match(/'rqdata'\s*:\s*'([^']{8,})'/) ||
                          t.match(/rqdata\s*[:=]\s*["']([^"']{8,})["']/);
                if (m) return m[1];
            }
            return '';
        }""")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return ""


async def read_hcaptcha_token(page) -> Optional[str]:
    """Read the current h-captcha-response token from the page."""
    try:
        token = await page.evaluate("""() => {
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            if (ta && ta.value && ta.value.length > 20) return ta.value;
            if (window.hcaptcha && window.hcaptcha.getResponse) {
                const r = window.hcaptcha.getResponse();
                if (r && r.length > 20) return r;
            }
            return '';
        }""")
        if token:
            return token
    except Exception:
        pass
    return None


async def set_hcaptcha_token_on_page(page, token: str) -> bool:
    """Inject a solved NoneCap token into the hCaptcha textarea."""
    try:
        result = await page.evaluate("""(tok) => {
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            if (ta) {
                ta.value = tok;
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            return false;
        }""", token)
        return bool(result)
    except Exception:
        return False


def proxy_url_from_bot_proxy(proxy: Optional[dict]) -> str:
    """Convert the bot's proxy dict into a URL string NoneCap accepts.

    Credentials are included so the solve egresses from the same sticky exit
    IP the browser will submit the token from (required for IP-bound
    enterprise sitekeys).
    """
    if not isinstance(proxy, dict):
        return ""
    host = (proxy.get("host") or "").strip()
    port = str(proxy.get("port") or "").strip()
    if not host or not port:
        return ""
    scheme = (proxy.get("proto") or "http").strip().lower()
    if scheme == "socks5h":
        scheme = "socks5"
    user = (proxy.get("username") or "").strip()
    pwd = (proxy.get("password") or "").strip()
    auth = f"{user}:{pwd}@" if user else ""
    return f"{scheme}://{auth}{host}:{port}"
