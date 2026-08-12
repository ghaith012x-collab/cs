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
import base64
import os
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from patchright.async_api import async_playwright as _patchright_async_playwright
from shardx import ShardX

ENGINE = "shardx"


class _AuthRelay:
    """Local HTTP proxy that injects ``Proxy-Authorization`` for an upstream
    authenticated proxy (vaultproxies etc.).

    The ShardX engine ignores inline ``user:pass@`` in ``--proxy-server`` and
    the gateway rejects CDP Fetch ``authRequired`` responses, but a plain
    upstream-facing proxy that adds the Basic header on CONNECT works — this
    is exactly the path the aiohttp probe (which succeeds) uses. The browser
    points at 127.0.0.1 with no credentials; the relay authenticates upstream.
    """

    def __init__(self, host: str, port: int, username: str, password: str):
        self._host = host
        self._port = port
        self._basic = base64.b64encode(
            f"{username}:{password}".encode()).decode()
        self._server: Optional[asyncio.AbstractServer] = None
        self.port: Optional[int] = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _handle(self, reader, writer):
        try:
            first_line = await reader.readline()
            if not first_line:
                writer.close()
                return
            headers = []
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                headers.append(line)
            is_connect = first_line.lstrip().upper().startswith(b"CONNECT ")
            try:
                ureader, uwriter = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port), timeout=10)
            except Exception:
                writer.close()
                return
            out = bytearray(first_line)
            out += b"Proxy-Authorization: Basic " + self._basic.encode() + b"\r\n"
            for h in headers:
                if h.lower().startswith(b"proxy-authorization"):
                    continue
                out += h
            out += b"\r\n"
            uwriter.write(out)
            await uwriter.drain()
            if is_connect:
                resp = await ureader.readline()
                resp_headers = []
                while True:
                    line = await ureader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    resp_headers.append(line)
                writer.write(resp)
                for h in resp_headers:
                    writer.write(h)
                writer.write(b"\r\n")
                await writer.drain()
                if not resp.startswith(b"HTTP/1.1 200"):
                    writer.close()
                    uwriter.close()
                    return
            await self._pump(reader, writer, ureader, uwriter)
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    async def _pump(self, reader, writer, ureader, uwriter):
        async def pump(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pump(reader, uwriter), pump(ureader, writer), return_exceptions=True)
        try:
            writer.close()
        except Exception:
            pass

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
        # Plain English UI: Discord's language follows Accept-Language and
        # navigator.language. The engine's profile library randomizes locale,
        # so pin en-US here unless the caller passes an explicit
        # accept_language. (Language is a product choice, not fingerprinting
        # — the engine still owns TLS/UA/WebGL/fonts.)
        accept_language = kwargs.get("accept_language") or "en-US,en;q=0.9"
        locale = _locale_from_accept_language(accept_language) or "en-US"
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
        page = await self._ctx.new_page(**kwargs)
        try:
            await self.browser._attach_proxy_auth(self._ctx, page)
        except Exception:
            pass
        return page

    async def close(self, **kwargs):
        return await self._ctx.close()

    def __getattr__(self, name):
        # Everything else delegates to the real patchright context.
        return getattr(self._ctx, name)


class _Browser:
    def __init__(self, pw, browser, session, profile, sdk, proxy_user="", proxy_pass="", relay=None):
        self._pw = pw
        self._browser = browser
        self._session = session
        self._profile = profile
        self._sdk = sdk
        self._proxy_user = proxy_user or ""
        self._proxy_pass = proxy_pass or ""
        self._relay = relay
        self._contexts: List[_BrowserContext] = []
        self._closed = False

    async def _attach_proxy_auth(self, ctx, page):
        """Answer HTTP-proxy 407 challenges over CDP Fetch.

        The ShardX fork ignores inline ``user:pass@`` in ``--proxy-server``
        (the SDK's own comment admits stock Chromium does too), so an
        authenticated gateway like vaultproxies 407-challenges every request
        and the page never loads. This is the same mechanism the previous
        truedriver engine needed: enable Fetch with auth-request handling on
        the page's CDP session and answer ``authRequired`` with the
        credentials. Mirrors Playwright's own proxy-auth implementation.
        """
        if not self._proxy_user:
            return
        try:
            cdp = await ctx.new_cdp_session(page)
            await cdp.send("Fetch.enable", {
                "handleAuthRequests": True,
                "patterns": [{"urlPattern": "*", "requestStage": "Response"}],
            })

            async def _continue_response(params):
                try:
                    await cdp.send("Fetch.continueResponse",
                                   {"requestId": params["requestId"]})
                except Exception:
                    pass

            async def _provide_credentials(params):
                try:
                    await cdp.send("Fetch.continueWithAuth", {
                        "requestId": params["requestId"],
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": self._proxy_user,
                            "password": self._proxy_pass,
                        },
                    })
                except Exception:
                    pass

            cdp.on("Fetch.requestPaused",
                   lambda p: asyncio.create_task(_continue_response(p)))
            cdp.on("Fetch.authRequired",
                   lambda p: asyncio.create_task(_provide_credentials(p)))
        except Exception as e:
            print(f"[Engine] CDP Fetch proxy auth FAILED: {e}", flush=True)

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
        page = await self._browser.new_page(**kwargs)
        try:
            await self._attach_proxy_auth(page.context, page)
        except Exception:
            pass
        return page

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
        if self._relay is not None:
            try:
                await self._relay.stop()         # stop the auth relay
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

        proxy = proxy or {}
        proxy_user = proxy.get("username", "") or ""
        proxy_pass = proxy.get("password", "") or ""

        # Authenticated HTTP(S) proxy → local auth relay. The engine cannot
        # authenticate against the gateway itself (inline creds in
        # --proxy-server are ignored; CDP Fetch auth is rejected), so point it
        # at 127.0.0.1 and let the relay add Proxy-Authorization upstream —
        # the same path the (working) aiohttp probe uses.
        relay = None
        server = proxy.get("server", "") or ""
        if proxy_user and server.startswith(("http://", "https://")):
            u = urlparse(server)
            relay = _AuthRelay(u.hostname, u.port or 80, proxy_user, proxy_pass)
            await relay.start()
            proxy_url = f"http://127.0.0.1:{relay.port}"
        else:
            proxy_url = _proxy_url(proxy)

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
        wrapper = _Browser(pw, browser, session, profile, sdk,
                           proxy_user=proxy_user,
                           proxy_pass=proxy_pass,
                           relay=relay)
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
