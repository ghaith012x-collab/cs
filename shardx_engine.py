"""
shardx_engine.py — Playwright-compatible async API backed by ShardX
(ProxyShard's free, open-source anti-detect browser, ShardBrowser).

The engine is a patched Chromium 149 that does ALL fingerprint spoofing in
C++ — TLS ClientHello / JA4 selection, WebGL + WebGPU, Client Hints (UA-CH
with GREASE), font enumeration, WebRTC policy, QUIC over SOCKS5, headless
marker stripping, CDP side-channel closing. There is no JS shim layer for
detectors to trip on. This module is a thin adapter that preserves the bot's
``from browser_engine import async_playwright`` contract:

    pw  = await async_playwright().start()
    b   = await pw.chromium.launch(headless=..., args=..., proxy={...}, **fp)
    ctx = await b.new_context(**opts)
    page = await ctx.new_page()

How it works per session:
  · launch() mints a FRESH saved profile (unique id, randomized coherent
    hardware, isolated user-data-dir) so every account gets a unique,
    unlinkable machine identity.
  · The profile is launched with --incognito, so nothing (cookies / cache /
    IndexedDB) ever touches disk.
  · patchright (stealth-patched Playwright) connects over CDP, so pages,
    locators, frames and evaluate() are the real Playwright API.
  · close() tears the engine process down AND deletes the ephemeral profile
    — true incognito semantics between sessions.

Runtime: the ShardX engine (~170 MB) + 170-profile fingerprint library
auto-download from the ProxyShard CDN on first use into
~/.cache/shardx-sdk (override with SHARDX_CACHE_DIR). Requires the standard
Chromium system libraries on the host (the project Dockerfile installs
them).

Set SHARDX_CACHE_DIR to relocate the engine/fingerprint/profiles cache.
"""

import asyncio
import os
import threading
from typing import Any, Dict, List, Optional

from patchright.async_api import async_playwright as _patchright_async_playwright
from shardx import ShardX

ENGINE = "shardx"

# ─────────────────────────────────────────────────────────────────
# SDK singleton
# ─────────────────────────────────────────────────────────────────

_sdk: Optional[ShardX] = None
_sdk_lock = threading.Lock()

# Serialize create_profile + launch: concurrent workers must never race the
# runtime install (first use downloads the engine) or the per-profile
# user-data-dir tree.
_LAUNCH_LOCK = threading.Lock()

_PLATFORM_MAP = {
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
}


def _get_sdk() -> ShardX:
    global _sdk
    if _sdk is None:
        with _sdk_lock:
            if _sdk is None:
                cache = os.environ.get("SHARDX_CACHE_DIR", "").strip()
                kwargs: Dict[str, Any] = {}
                if cache:
                    kwargs["cache_dir"] = cache
                    kwargs["profiles_dir"] = os.path.join(cache, "profiles")
                _sdk = ShardX(**kwargs)
    return _sdk


def _proxy_url(proxy: Optional[dict]) -> Optional[str]:
    """Translate the Playwright-style proxy dict to a ShardX proxy URL.

    Accepts both ``{server, username, password}`` (server may already carry
    user:pass@) and raw URL strings passed as ``{"server": url}``.
    """
    if not proxy or not proxy.get("server"):
        return None
    server = str(proxy["server"])
    user = proxy.get("username") or ""
    pwd = proxy.get("password") or ""
    if user:
        proto, sep, rest = server.partition("://")
        if sep and "@" not in rest:
            return f"{proto}://{user}:{pwd}@{rest}"
    return server


def _locale_from_accept_language(accept_language: str) -> str:
    return (accept_language or "en-US").split(",")[0].strip() or "en-US"


def _build_profile(sdk: ShardX, kwargs: dict):
    """Mint a fresh per-session profile and align it with the bot's choices.

    The engine owns the machine identity (TLS, WebGL/WebGPU, UA-CH, fonts);
    we only pin the bot's explicit locale/timezone/UA on top so the session
    matches the bot's intended region and logging.
    """
    platform = str(kwargs.get("platform") or "windows").lower()
    profile = sdk.create_profile(platform=_PLATFORM_MAP.get(platform, "Windows"))

    cfg = profile.config
    nav = cfg.get("navigator")
    if isinstance(nav, dict):
        ua = kwargs.get("user_agent") or ""
        if ua:
            nav["user_agent"] = ua  # engine normalises Chrome/ version
        accept_language = kwargs.get("accept_language") or ""
        if accept_language:
            locale = _locale_from_accept_language(accept_language)
            nav["accept_language"] = accept_language
            nav["language"] = locale
            nav["languages"] = [locale, "en"]
            cfg["icu_locale"] = locale
    tz = kwargs.get("timezone") or ""
    if tz:
        cfg["timezone"] = tz
    return profile


# ─────────────────────────────────────────────────────────────────
# Adapter classes
# ─────────────────────────────────────────────────────────────────


class _BrowserContext:
    """Thin wrapper so close() can deregister from the owning browser."""

    def __init__(self, ctx, browser: "_Browser"):
        self._ctx = ctx
        self.browser = browser

    async def add_init_script(self, script, **kwargs):
        return await self._ctx.add_init_script(script)

    async def new_page(self, **kwargs):
        return await self._ctx.new_page(**kwargs)

    async def close(self, **kwargs):
        return await self._ctx.close()

    def __getattr__(self, name):
        # Everything else delegates to the real patchright context.
        return getattr(self._ctx, name)


class _Browser:
    def __init__(self, pw, browser, session, profile, sdk):
        self._pw = pw
        self._browser = browser
        self._session = session
        self._profile = profile
        self._sdk = sdk
        self._contexts: List[_BrowserContext] = []
        self._closed = False

    async def new_context(self, **kwargs) -> _BrowserContext:
        # Proxy rides at browser launch (--proxy-server); a context-level
        # proxy would be rejected/ignored over CDP.
        kwargs.pop("proxy", None)
        ctx = await self._browser.new_context(**kwargs)
        wrapped = _BrowserContext(ctx, self)
        self._contexts.append(wrapped)
        return wrapped

    async def new_page(self, **kwargs):
        # Playwright semantics: new isolated context + page.
        return await self._browser.new_page(**kwargs)

    async def close(self, **kwargs):
        if self._closed:
            return
        self._closed = True
        try:
            await self._browser.close()          # disconnect patchright
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._session.stop)  # SIGTERM the engine
        except Exception:
            pass
        try:
            # Incognito: wipe the ephemeral profile so no cookies/cache
            # survive to the next account.
            await asyncio.to_thread(self._sdk.delete_profile, self._profile.id)
        except Exception:
            pass
        try:
            await self._pw.stop()                # stop the patchright driver
        except Exception:
            pass
        self._contexts.clear()


class _BrowserType:
    def __init__(self, owner: "_Playwright" = None):
        self._owner = owner

    async def launch(
        self,
        *,
        headless: bool = True,
        args: Optional[List[str]] = None,
        executable_path: Optional[str] = None,
        proxy: Optional[dict] = None,
        channel: Optional[str] = None,
        **kwargs,
    ) -> _Browser:
        sdk = _get_sdk()

        def _spawn():
            # Serialize the whole spawn: concurrent workers must never race
            # the runtime install or the per-profile user-data-dir tree.
            with _LAUNCH_LOCK:
                profile = _build_profile(sdk, kwargs)
                extra = list(args or [])
                # Root/Docker hardening the ShardX SDK requires explicitly.
                for flag in ("--no-sandbox", "--disable-dev-shm-usage"):
                    if flag not in extra:
                        extra.append(flag)
                if "--incognito" not in extra:
                    extra.append("--incognito")
                proxy_url = _proxy_url(proxy)
                session = sdk.launch(
                    profile,
                    proxy=proxy_url,
                    cdp=True,
                    headless=bool(headless),
                    extra_args=extra,
                    screen_mode="profile",
                )
            return session, profile

        session, profile = await asyncio.to_thread(_spawn)
        if not session.cdp_url:
            try:
                await asyncio.to_thread(session.stop)
            except Exception:
                pass
            try:
                await asyncio.to_thread(sdk.delete_profile, profile.id)
            except Exception:
                pass
            raise RuntimeError(
                "ShardX engine failed to expose a CDP endpoint — check that the "
                "Chromium system libraries are installed (see Dockerfile)."
            )

        pw = await _patchright_async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(session.cdp_url)
        wrapper = _Browser(pw, browser, session, profile, sdk)
        if self._owner is not None:
            self._owner._browsers.append(wrapper)
        return wrapper

    async def launch_persistent_context(self, user_data_dir: str = None, **kwargs) -> _Browser:
        return await self.launch(**kwargs)


class _Playwright:
    def __init__(self):
        self.chromium = _BrowserType(self)
        self.firefox = _BrowserType(self)
        self.webkit = _BrowserType(self)
        self._browsers: List[_Browser] = []

    async def start(self) -> "_Playwright":
        return self

    async def stop(self):
        for b in list(self._browsers):
            try:
                await b.close()
            except Exception:
                pass
        self._browsers.clear()


def async_playwright() -> _Playwright:
    return _Playwright()


__all__ = ["async_playwright", "ENGINE"]
