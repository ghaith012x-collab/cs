"""
incognito_mail.py — incognitomail.co temp-mail integration via Playwright.

incognitomail.co's public API requires a Cloudflare Turnstile token (the HMAC
key endpoint refuses requests without one), so the reliable way to use the
service is exactly what its own frontend does: load the real site in the
browser. This project already drives Playwright, so we:

  1. Open https://incognitomail.co/ in a background page
  2. Read the auto-generated inbox address from the DOM
  3. Later, poll the inbox for the Discord verification email
  4. Extract the verification link and return it to the automation

Never crashes the caller — every failure is caught and logged.
"""

import asyncio
import re
import time
from typing import Callable, Optional

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
VERIFY_LINK_PATTERNS = [
    r"https?://[^\s\"'<>]+discord[^\s\"'<>]*(?:verify|verification)[^\s\"'<>]*",
    r"https?://[^\s\"'<>]*(?:click|e)\.discord\.com[^\s\"'<>]*",
    r"https?://discord\.com/verify[^\s\"'<>]*",
]


class IncognitoMail:
    """Read-only incognitomail.co client backed by a Playwright page."""

    def __init__(self, context, log: Optional[Callable] = None):
        self._context = context
        self._log = log or (lambda msg, level="info": None)
        self._page = None
        self._email = ""

    @property
    def email(self) -> str:
        return self._email

    # ── Inbox creation ──────────────────────────────────

    async def create_inbox(self, timeout: float = 45.0) -> str:
        """Open incognitomail.co and read the auto-generated inbox address."""
        try:
            self._page = await self._context.new_page()
            self._log("[Mail] Opening incognitomail.co...")
            await self._page.goto(
                "https://incognitomail.co/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            deadline = time.time() + timeout
            while time.time() < deadline:
                email = await self._read_email()
                if email:
                    self._email = email
                    self._log(f"[Mail] ✓ Inbox ready: {email}")
                    return email
                await asyncio.sleep(2)
            self._log("[Mail] Could not read inbox address from page", level="warn")
            return ""
        except Exception as e:
            self._log(f"[Mail] create_inbox error: {e}", level="error")
            return ""

    async def _read_email(self) -> str:
        """Extract the current inbox address from the page DOM."""
        try:
            val = await self._page.evaluate("""() => {
                // 1) Any visible input holding an address
                const inputs = document.querySelectorAll('input');
                for (const i of inputs) {
                    const v = (i.value || '').trim();
                    if (/^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$/.test(v)) return v;
                }
                // 2) Fallback: first email-looking string in visible text
                const body = document.body ? document.body.innerText : '';
                const m = body.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/);
                return m ? m[0] : '';
            }""")
            if val:
                return str(val).strip()
        except Exception:
            pass
        return ""

    # ── Message polling ─────────────────────────────────

    async def wait_for_verification_link(self, keyword: str = "discord",
                                         timeout: float = 180.0,
                                         poll: float = 5.0) -> Optional[str]:
        """Poll the inbox until a message matches, then return its verification link."""
        if not self._page:
            self._log("[Mail] No inbox page open", level="warn")
            return None
        deadline = time.time() + timeout
        self._log(f"[Mail] Waiting for {keyword} verification email (up to {int(timeout)}s)...")
        while time.time() < deadline:
            link = await self._find_verification_link(keyword)
            if link:
                self._log(f"[Mail] ✓ Verification link found: {link[:80]}...")
                return link
            await self._refresh_inbox()
            await asyncio.sleep(poll)
        self._log(f"[Mail] No {keyword} verification email found within {int(timeout)}s",
                  level="warn")
        return None

    async def _refresh_inbox(self) -> None:
        """Reload the inbox so newly arrived messages show up."""
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)
        except Exception as e:
            self._log(f"[Mail] inbox refresh error: {e}", level="warn")

    async def _find_verification_link(self, keyword: str) -> Optional[str]:
        """Try to locate and open a matching message, then extract a verify link."""
        # 1) Direct scan of the whole page for verification links
        try:
            hrefs = await self._page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const h = a.href || '';
                    const t = (a.textContent || '').toLowerCase();
                    if (h.includes('discord') || t.includes('verify') || t.includes('confirm')) {
                        out.push(h);
                    }
                });
                return out;
            }""")
            for h in hrefs or []:
                if self._is_verify_link(h):
                    return h
        except Exception:
            pass

        # 2) Click a message row whose text matches the keyword, then rescan
        try:
            clicked = await self._page.evaluate(f"""() => {{
                const kw = '{keyword.lower()}';
                const candidates = document.querySelectorAll(
                    'button, [role="button"], li, [class*="item"], [class*="message"], [class*="row"]'
                );
                for (const el of candidates) {{
                    const t = (el.textContent || '').toLowerCase();
                    if (t.includes(kw) && el.offsetParent !== null &&
                        el.getBoundingClientRect().width > 100) {{
                        el.click();
                        return true;
                    }}
                }}
                return false;
            }}""")
            if clicked:
                await asyncio.sleep(3)
                hrefs = await self._page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('a[href]').forEach(a => out.push(a.href));
                    return out;
                }""")
                for h in hrefs or []:
                    if self._is_verify_link(h):
                        return h
        except Exception as e:
            self._log(f"[Mail] message click error: {e}", level="warn")

        # 3) Last resort: scan raw page text for any discord verify URL
        try:
            body = await self._page.evaluate("() => document.body ? document.body.innerText : ''")
            for pattern in VERIFY_LINK_PATTERNS:
                m = re.search(pattern, body or "", re.I)
                if m:
                    return m.group(0)
        except Exception:
            pass

        return None

    @staticmethod
    def _is_verify_link(href: str) -> bool:
        h = href.lower()
        return ("discord.com/verify" in h or "click.discord.com" in h
                or "e.discord.com" in h or ("discord" in h and "verify" in h))

    async def close(self) -> None:
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None
