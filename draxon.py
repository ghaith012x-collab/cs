"""
draxon.py — temp-mail client for DraxonMails (https://mail.draxon.one).

DraxonMails exposes a public REST API:
    GET /api/random?type=discord   -> {"address", "local", "domain"} (fresh inbox)
    GET /api/inboxes/{address}     -> [messages]  (html / body / markdown fields)
    GET /api/domains?type=discord  -> discord-friendly public domains

Public inbox reads are free (no signup). An optional DRAXON_API_KEY env var is
sent as an X-Api-Key header when present. Messages expire after ~10 minutes.

This class is a drop-in replacement for cybertemp.TempMail (same public API:
email, provider, create_inbox(), wait_for_verification_link(), close()).
Pure HTTP — no browser, so inbox creation is instant instead of a full
stealth-browser launch. cybertemp stays as a fallback.
"""

import asyncio
import os
import random
import re
import string
import time
from typing import Callable, Optional
from urllib.parse import quote

import aiohttp

from cybertemp import VERIFY_LINK_PATTERNS

DRAXON_BASE = "https://mail.draxon.one"
PROVIDER = "draxon"

# Discord verification links appear as:
#   https://click.discord.com/ls/click?upn=...      (tracking redirect)
#   https://discord.com/register/verify?token=...
#   https://discord.com/verify?token=...
#   https://e.discord.com/...                       (email gateway)
# `*` (not `+`) after :// so bare https://discord.com/... matches too.
DISCORD_LINK_RE = re.compile(
    r"https?://[^\s\"'<>]*(?:discord(?:app)?\.com|click\.discord\.com|e\.discord\.com)[^\s\"'<>]*",
    re.IGNORECASE,
)
_VERIFY_TOKENS = ("verify", "verification", "email", "ls/click", "click.discord",
                  "token", "upn", "confirm")

_EMAIL_RANDOM = random.SystemRandom()


class DraxonMail:
    """Async DraxonMails client (TempMail-compatible)."""

    def __init__(self, log: Optional[Callable] = None,
                 proxy: Optional[dict] = None, headless: bool = True,
                 domain: str = ""):
        self._log = log or (lambda msg, level="info": None)
        # proxy/headless/domain accepted for TempMail-compat; Draxon's API is
        # server-side so no browser or proxy is needed.
        self._proxy = proxy
        self.headless = headless
        self._domain = domain or ""
        self._api_key = (os.environ.get("DRAXON_API_KEY") or "").strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._address = ""
        self._provider = ""

    # ── Public API (TempMail-compatible) ───────────────────

    @property
    def email(self) -> str:
        return self._address

    @property
    def provider(self) -> str:
        return self._provider or "none"

    async def create_inbox(self, timeout: float = 30.0) -> str:
        """Fetch a fresh random inbox on a discord-friendly domain."""
        try:
            data = await self._api_get("/api/random", params={"type": "discord"},
                                       timeout=timeout)
            addr = (data or {}).get("address") or ""
            if not addr or "@" not in addr:
                self._log(f"[Mail] Draxon random address invalid: {data}", level="warn")
                return ""
            self._address = addr
            self._provider = PROVIDER
            self._log(f"[Mail] [OK] Inbox ready: {self._address} ({PROVIDER})")
            return self._address
        except Exception as e:
            self._log(f"[Mail] Draxon unavailable: {e}", level="error")
            return ""

    async def wait_for_verification_link(
        self, keyword: str = "discord",
        timeout: float = 150.0, poll: float = 3.0,
    ) -> Optional[str]:
        """Poll the Draxon inbox until a matching message arrives, then return
        its Discord verification URL (or None on timeout)."""
        if not self._address:
            self._log("[Mail] No Draxon inbox open — cannot poll", level="warn")
            return None
        deadline = time.time() + timeout
        self._log(f"[Mail] Draxon waiting for {keyword} verification email "
                  f"(up to {int(timeout)}s)...")
        while time.time() < deadline:
            try:
                messages = await self._api_get(
                    f"/api/inboxes/{quote(self._address, safe='')}", timeout=15.0)
            except Exception as e:
                self._log(f"[Mail] Draxon poll error: {e}", level="warn")
                await asyncio.sleep(poll)
                continue
            for msg in (messages or []):
                text = self._message_text(msg)
                if keyword and keyword.lower() not in text.lower():
                    continue
                link = self._extract_verify_link(text)
                if link:
                    self._log(f"[Mail] [OK] Verification link found: {link[:90]}...")
                    return link
            await asyncio.sleep(poll)
        self._log(f"[Mail] No {keyword} verification email within {int(timeout)}s "
                  f"(draxon)", level="warn")
        return None

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    # ── HTTP helpers ───────────────────────────────────────

    async def _api_get(self, path: str, params: Optional[dict] = None,
                       timeout: float = 15.0) -> object:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
        # Draxon gates automation-looking UAs (aiohttp/Python defaults) behind an
        # API key but serves browser UAs without one. Send a Chrome UA so the
        # free tier works; DRAXON_API_KEY env is honored when provided.
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        async with self._session.get(DRAXON_BASE + path, params=params,
                                     headers=headers) as resp:
            status = resp.status
            if status != 200:
                body = (await resp.text()).strip()[:160]
                self._log(f"[Mail] Draxon API {status}: {body}", level="warn")
                return None
            try:
                return await resp.json()
            except Exception:
                return None

    # ── Message parsing ────────────────────────────────────

    @staticmethod
    def _message_text(msg: dict) -> str:
        parts: list = []
        for key in ("subject", "from", "from_address", "introduction", "summary"):
            v = msg.get(key)
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, dict):
                for sub in ("address", "email", "name"):
                    sv = v.get(sub)
                    if isinstance(sv, str):
                        parts.append(sv)
        for key in ("html", "body", "markdown", "text", "content"):
            v = msg.get(key)
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, dict):
                for sub in ("html", "text", "markdown"):
                    sv = v.get(sub)
                    if isinstance(sv, str):
                        parts.append(sv)
        return "\n".join(parts)

    def _extract_verify_link(self, text: str) -> Optional[str]:
        # 1) Dedicated Discord URL scan — matches bare https://discord.com/...
        #    and the click.discord.com tracking redirects, then filters to
        #    verification-flavoured URLs (same approach as the official client).
        for m in DISCORD_LINK_RE.findall(text):
            lowered = m.lower()
            if any(tok in lowered for tok in _VERIFY_TOKENS):
                return self._clean_link(m)
        # 2) cybertemp patterns (backward compat / escaped variants)
        for pattern in VERIFY_LINK_PATTERNS:
            m = re.search(pattern, text, re.I)
            if m:
                return self._clean_link(m.group(0))
        # 3) HTML anchor hrefs
        for m in re.finditer(r'href=["\']([^"\']+)["\']', text, re.I):
            href = self._clean_link(m.group(1))
            h = href.lower()
            if ("discord.com/verify" in h or "discord.com/register" in h
                    or "click.discord.com" in h or "e.discord.com" in h):
                return href
            if "discord.com" in h and ("verify" in h or "confirm" in h):
                return href
        # 4) Unquoted / escaped hrefs inside HTML
        for m in re.finditer(r'href=([^\s>]+)', text, re.I):
            href = self._clean_link(m.group(1).strip("\"'"))
            h = href.lower()
            if "discord.com" in h and "verify" in h:
                return href
        return None

    @staticmethod
    def _clean_link(url: str) -> str:
        return url.replace("&amp;", "&").rstrip(".,);\"'<>")


def _local() -> str:
    return "".join(_EMAIL_RANDOM.choices(string.ascii_lowercase + string.digits, k=12))
