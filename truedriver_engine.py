"""
truedriver_engine.py — Playwright-compatible async API implemented
on top of truedriver (CDP-based browser automation).

Drop-in replacement for `from browser_engine import async_playwright`.

Set ENGINE=truedriver to force this engine, or set THORIUM_PATH to
the Thorium browser binary location (default: /usr/bin/thorium-browser).
"""

import asyncio
import base64
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import truedriver as td

ENGINE = "truedriver"

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _find_thorium() -> str:
    """Locate the Thorium browser binary."""
    env = os.environ.get("THORIUM_PATH", "").strip()
    if env and os.path.exists(env):
        return env
    for candidate in (
        "/usr/bin/thorium-browser",
        "/usr/bin/thorium",
        "/opt/thorium/thorium-browser",
    ):
        if os.path.exists(candidate):
            return candidate
    # Last resort: rely on system PATH
    return "thorium-browser"


def _run_sync(fn, *args, timeout=None):
    """Run a blocking truedriver call in a thread-pool to keep the
    event-loop free.  (truedriver's methods return immediately but some
    of them perform CDP round-trips synchronously; wrapping keeps
    everything looking like pure asyncio.)"""
    return asyncio.to_thread(fn, *args)


# ─────────────────────────────────────────────────────────────────
# Smart locator – Playwright-like lazy selector with per-frame context
# ─────────────────────────────────────────────────────────────────


class _Locator:
    """A lazy selector that resolves to a truedriver `Element` on every
    action call (`.click()`, `.fill()`, ...).  Playwright locator
    semantics: ``loc = page.locator('…')`` is instant; the actual DOM
    query happens inside `.click()` / `.fill()` / etc."""

    def __init__(
        self,
        tab: td.Tab,
        selector: str,
        *,
        frame_url: str = "",
        nth_index: Optional[int] = None,
    ):
        self._tab = tab
        self._raw_sel = selector
        self._frame_url = frame_url
        self._nth = nth_index

    # ------------------------------------------------------------------
    # Internal: build the final CSS selector & find the Element
    # ------------------------------------------------------------------

    def _final_selector(self) -> str:
        if self._nth is not None:
            return f"{self._raw_sel}:nth-child({self._nth + 1})"
        return self._raw_sel

    async def _try_find(self, timeout: float = 5) -> Optional[td.Element]:
        """Find element, returning None instead of raising on timeout."""
        sel = self._final_selector()
        try:
            # If we have a frame context, switch to it first
            if self._frame_url:
                frame_obj = self._tab.find_frame_by_url(self._frame_url)
                if frame_obj:
                    await _run_sync(self._tab.switch_to_frame, frame_obj)
                    try:
                        return await _run_sync(self._tab.find, sel, True, True, timeout)
                    finally:
                        await _run_sync(self._tab.switch_to_main_frame)
            return await _run_sync(self._tab.find, sel, True, True, timeout)
        except Exception:
            return None

    async def _require(self, timeout: float = 10) -> td.Element:
        """Find element, raise on failure."""
        el = await self._try_find(timeout)
        if el is None:
            raise RuntimeError(f"Element not found: {self._raw_sel}")
        return el

    async def _in_frame(self, coro):
        """Execute a coroutine within a frame context, switching back
        afterwards."""
        if self._frame_url:
            frame_obj = self._tab.find_frame_by_url(self._frame_url)
            if frame_obj:
                await _run_sync(self._tab.switch_to_frame, frame_obj)
                try:
                    return await coro
                finally:
                    await _run_sync(self._tab.switch_to_main_frame)
        return await coro

    # ------------------------------------------------------------------
    # Playwright locator API
    # ------------------------------------------------------------------

    async def click(self, timeout: float = 30, **kwargs):
        el = await self._require(timeout)
        async def _do():
            await _run_sync(el.scroll_into_view)
            await asyncio.sleep(0.1)
            await _run_sync(el.click)
        await self._in_frame(_do())

    async def fill(self, value: str, timeout: float = 30, **kwargs):
        el = await self._require(timeout)
        async def _do():
            await _run_sync(el.focus)
            await asyncio.sleep(0.05)
            await _run_sync(el.clear_input)
            await asyncio.sleep(0.05)
            await _run_sync(el.send_keys, value)
        await self._in_frame(_do())

    async def type(self, value: str, delay: float = 0.08, timeout: float = 30, **kwargs):
        el = await self._require(timeout)
        async def _do():
            await _run_sync(el.focus)
            for ch in value:
                await _run_sync(el.send_keys, ch)
                await asyncio.sleep(delay)
        await self._in_frame(_do())

    async def press(self, key: str, timeout: float = 30, **kwargs):
        el = await self._require(timeout)
        async def _do():
            await _run_sync(el.send_keys, key)
        await self._in_frame(_do())

    async def wait_for(self, state: str = "visible", timeout: float = 30, **kwargs):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            el = await self._try_find(timeout=4)
            if el is not None:
                if state == "visible":
                    try:
                        pos = await _run_sync(el.get_position)
                    except Exception:
                        pos = None
                    if pos is not None:
                        return
                elif state in ("attached",):
                    return
                elif state == "detached":
                    pass  # keep waiting for *absence*
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"Timed out waiting for {self._raw_sel} to be {state}"
        )

    async def is_visible(self, timeout: float = 5) -> bool:
        el = await self._try_find(timeout)
        if el is None:
            return False
        try:
            return (await _run_sync(el.get_position)) is not None
        except Exception:
            return False

    async def bounding_box(self) -> Optional[dict]:
        el = await self._try_find(timeout=4)
        if el is None:
            return None
        try:
            pos = await _run_sync(el.get_position)
        except Exception:
            return None
        if pos is None:
            return None
        return {"x": pos[0], "y": pos[1], "width": pos[2], "height": pos[3]}

    async def text_content(self) -> Optional[str]:
        el = await self._try_find(timeout=4)
        if el is None:
            return None
        try:
            return (await _run_sync(el.get, "textContent")) or ""
        except Exception:
            return ""

    async def inner_text(self) -> str:
        el = await self._try_find(timeout=4)
        if el is None:
            return ""
        try:
            return (await _run_sync(el.get, "innerText")) or ""
        except Exception:
            return ""

    async def input_value(self) -> str:
        el = await self._try_find(timeout=4)
        if el is None:
            return ""
        try:
            return (await _run_sync(el.get, "value")) or ""
        except Exception:
            return ""

    async def get_attribute(self, name: str) -> Optional[str]:
        el = await self._try_find(timeout=4)
        if el is None:
            return None
        try:
            return await _run_sync(el.get, name)
        except Exception:
            return None

    async def is_checked(self) -> bool:
        el = await self._try_find(timeout=3)
        if el is None:
            return False
        try:
            return (await _run_sync(el.get, "checked")) == "true"
        except Exception:
            return False

    async def is_disabled(self) -> bool:
        el = await self._try_find(timeout=3)
        if el is None:
            return True  # not found → assume disabled
        try:
            return (await _run_sync(el.get, "disabled")) == "true"
        except Exception:
            return True

    async def count(self) -> int:
        try:
            els = await _run_sync(self._tab.find_all, self._raw_sel, 3)
            return len(els)
        except Exception:
            return 0

    def nth(self, index: int) -> "_Locator":
        return _Locator(
            self._tab,
            self._raw_sel,
            frame_url=self._frame_url,
            nth_index=index,
        )

    def locator(self, selector: str) -> "_Locator":
        return _Locator(
            self._tab,
            f"{self._raw_sel} {selector}",
            frame_url=self._frame_url,
        )

    async def screenshot(self, path: str = None, type: str = "png", **kwargs) -> Optional[bytes]:
        el = await self._try_find(timeout=5)
        if el is None:
            return None
        async def _do():
            if path:
                await _run_sync(el.save_screenshot, path, type)
                return None
            b64 = await _run_sync(el.screenshot_b64, type)
            return base64.b64decode(b64)
        return await self._in_frame(_do())

    async def clear(self, timeout: float = 10, **kwargs):
        el = await self._require(timeout)
        await self._in_frame(_run_sync(el.clear_input))

    async def focus(self, timeout: float = 10, **kwargs):
        el = await self._require(timeout)
        await self._in_frame(_run_sync(el.focus))

    async def scroll_into_view_if_needed(self, timeout: float = 10, **kwargs):
        el = await self._try_find(timeout)
        if el is not None:
            await self._in_frame(_run_sync(el.scroll_into_view))

    async def element_handle(self, timeout: float = 10) -> Optional["_ElementHandle"]:
        el = await self._try_find(timeout)
        if el is None:
            return None
        return _ElementHandle(self._tab, el)

    async def evaluate(self, expression: str, **kwargs):
        el = await self._require(timeout=5)
        async def _do():
            return await _run_sync(el.apply, expression, kwargs.get("return_by_value", True))
        return await self._in_frame(_do())

    async def dispatch_event(self, event_type: str, **kwargs):
        el = await self._require(timeout=5)
        js = f"this.dispatchEvent(new Event('{event_type}', {{bubbles: true}}))"
        async def _do():
            await _run_sync(el.apply, js)
        await self._in_frame(_do())


# ─────────────────────────────────────────────────────────────────
# Frame locator – cross-origin iframe targeting
# ─────────────────────────────────────────────────────────────────


class _FrameLocator:
    """Created by ``page.frame_locator('iframe[src="…"]')``."""

    def __init__(self, tab: td.Tab, selector: str):
        self._tab = tab
        self._selector = selector
        # Try to extract a source URL from the selector
        self._frame_url = ""
        m = re.search(r'src[*^]?=["\']?([^"\'\]\s]+)', selector)
        if m:
            self._frame_url = m.group(1)

    def locator(self, selector: str) -> _Locator:
        return _Locator(self._tab, selector, frame_url=self._frame_url)

    def nth(self, index: int) -> "_FrameLocator":
        return self  # frame_locator nth is a no-op for our purposes


# ─────────────────────────────────────────────────────────────────
# Frame – a sub-page context
# ─────────────────────────────────────────────────────────────────


class _Frame:
    __slots__ = ("_tab", "_cdp_frame", "url")

    def __init__(self, tab: td.Tab, cdp_frame):
        self._tab = tab
        self._cdp_frame = cdp_frame
        self.url: str = getattr(cdp_frame, "url", "") or ""

    def locator(self, selector: str) -> _Locator:
        return _Locator(self._tab, selector, frame_url=self.url)

    async def evaluate(self, expression: str, **kwargs) -> Any:
        await _run_sync(self._tab.switch_to_frame, self._cdp_frame)
        try:
            return await _run_sync(self._tab.evaluate, expression)
        finally:
            await _run_sync(self._tab.switch_to_main_frame)

    async def content(self) -> str:
        return await self.evaluate("document.documentElement.outerHTML") or ""

    async def title(self) -> str:
        return await self.evaluate("document.title") or ""


# ─────────────────────────────────────────────────────────────────
# ElementHandle – raw DOM element reference
# ─────────────────────────────────────────────────────────────────


class _ElementHandle:
    __slots__ = ("_tab", "_el")

    def __init__(self, tab: td.Tab, element: td.Element):
        self._tab = tab
        self._el = element

    async def content_frame(self) -> Optional[_Frame]:
        src = await _run_sync(self._el.get, "src")
        if not src:
            return None
        frame = self._tab.find_frame_by_url(src)
        if frame:
            return _Frame(self._tab, frame)
        return None

    async def bounding_box(self) -> Optional[dict]:
        try:
            pos = await _run_sync(self._el.get_position)
        except Exception:
            return None
        if pos is None:
            return None
        return {"x": pos[0], "y": pos[1], "width": pos[2], "height": pos[3]}

    async def screenshot(self, path: str = None, type: str = "png") -> Optional[bytes]:
        if path:
            await _run_sync(self._el.save_screenshot, path, type)
            return None
        b64 = await _run_sync(self._el.screenshot_b64, type)
        return base64.b64decode(b64)

    async def click(self, **kwargs):
        await _run_sync(self._el.click)

    async def fill(self, value: str, **kwargs):
        await _run_sync(self._el.focus)
        await asyncio.sleep(0.05)
        await _run_sync(self._el.clear_input)
        await asyncio.sleep(0.05)
        await _run_sync(self._el.send_keys, value)

    async def get_attribute(self, name: str) -> Optional[str]:
        try:
            return await _run_sync(self._el.get, name)
        except Exception:
            return None

    async def is_checked(self) -> bool:
        try:
            return (await _run_sync(self._el.get, "checked")) == "true"
        except Exception:
            return False

    async def is_disabled(self) -> bool:
        try:
            return (await _run_sync(self._el.get, "disabled")) == "true"
        except Exception:
            return True

    async def evaluate(self, expression: str, **kwargs):
        return await _run_sync(self._el.apply, expression)

    async def dispatch_event(self, event_type: str, **kwargs):
        js = f"this.dispatchEvent(new Event('{event_type}', {{bubbles: true}}))"
        await _run_sync(self._el.apply, js)

    async def scroll_into_view_if_needed(self):
        await _run_sync(self._el.scroll_into_view)

    async def query_selector(self, selector: str) -> Optional["_ElementHandle"]:
        try:
            child = await _run_sync(self._el.query_selector, selector)
            if child:
                return _ElementHandle(self._tab, child)
        except Exception:
            pass
        return None


# ─────────────────────────────────────────────────────────────────
# Keyboard & Mouse
# ─────────────────────────────────────────────────────────────────


class _Keyboard:
    __slots__ = ("_tab",)

    def __init__(self, tab: td.Tab):
        self._tab = tab

    async def press(self, key: str, **kwargs):
        await _run_sync(
            self._tab.evaluate,
            "(()=>{const e=new KeyboardEvent('keydown',{key:%s,bubbles:true});"
            "document.activeElement.dispatchEvent(e);"
            "document.activeElement.dispatchEvent(new KeyboardEvent('keyup',{key:%s,bubbles:true}));"
            "})()" % (repr(key), repr(key)),
        )

    async def type(self, text: str, delay: float = 0, **kwargs):
        for ch in text:
            await _run_sync(
                self._tab.evaluate,
                "(()=>{const t=document.activeElement;if(!t)return;"
                "t.dispatchEvent(new KeyboardEvent('keydown',{key:%s,bubbles:true}));"
                "t.dispatchEvent(new InputEvent('input',{data:%s,bubbles:true}));"
                "t.dispatchEvent(new KeyboardEvent('keyup',{key:%s,bubbles:true}));"
                "})()" % (repr(ch), repr(ch), repr(ch)),
            )
            if delay:
                await asyncio.sleep(delay)


class _Mouse:
    __slots__ = ("_tab",)

    def __init__(self, tab: td.Tab):
        self._tab = tab

    async def move(self, x: float, y: float, **kwargs):
        steps = kwargs.get("steps", 10)
        await _run_sync(self._tab.mouse_move, x, y, steps)

    async def click(self, x: float, y: float, **kwargs):
        await _run_sync(self._tab.mouse_click, x, y)


# ─────────────────────────────────────────────────────────────────
# Page / Context / Browser wrappers
# ─────────────────────────────────────────────────────────────────


class _Page:
    """Wraps a truedriver Tab to look like a Playwright Page."""

    def __init__(self, tab: td.Tab, context: Optional["_BrowserContext"] = None):
        self._tab = tab
        self._context = context
        self.keyboard = _Keyboard(tab)
        self.mouse = _Mouse(tab)

    # -- navigation ---------------------------------------------------
    async def goto(self, url: str, timeout: float = 30, wait_until: str = "load", **kwargs):
        await _run_sync(self._tab.get, url, False, False, timeout)
        try:
            await _run_sync(self._tab.wait_for_ready_state, "complete", timeout)
        except Exception:
            pass

    async def reload(self, **kwargs):
        await _run_sync(self._tab.reload)

    async def close(self, **kwargs):
        try:
            await _run_sync(self._tab.close)
        except Exception:
            pass

    # -- locators -----------------------------------------------------
    def locator(self, selector: str) -> _Locator:
        return _Locator(self._tab, selector)

    def frame_locator(self, selector: str) -> _FrameLocator:
        return _FrameLocator(self._tab, selector)

    # -- frames -------------------------------------------------------
    @property
    def frames(self) -> List[_Frame]:
        try:
            cdp_frames = self._tab.get_frames()
        except Exception:
            return []
        return [_Frame(self._tab, f) for f in cdp_frames]

    # -- content / title / url ----------------------------------------
    async def title(self) -> str:
        try:
            return (await _run_sync(self._tab.evaluate, "document.title")) or ""
        except Exception:
            return ""

    @property
    def url(self) -> str:
        try:
            for f in self._tab.get_frames():
                u = getattr(f, "url", "") or ""
                if u and "about:blank" not in u:
                    return u
        except Exception:
            pass
        return ""

    async def content(self) -> str:
        try:
            return await _run_sync(self._tab.get_content) or ""
        except Exception:
            return ""

    # -- screenshots --------------------------------------------------
    async def screenshot(self, path: str = None, full_page: bool = False, type: str = "png", **kwargs):
        fmt = type if type in ("jpeg", "png") else "png"
        if path:
            await _run_sync(self._tab.save_screenshot, path, fmt, full_page)
            return None
        b64 = await _run_sync(self._tab.screenshot_b64, fmt, full_page)
        return base64.b64decode(b64)

    # -- evaluate / query --------------------------------------------
    async def evaluate(self, expression: str, **kwargs):
        return await _run_sync(self._tab.evaluate, expression)

    async def query_selector(self, selector: str) -> Optional[_ElementHandle]:
        try:
            el = await _run_sync(self._tab.find, selector, True, True, 4)
            return _ElementHandle(self._tab, el)
        except Exception:
            return None

    # -- convenience --------------------------------------------------
    async def wait_for_selector(self, selector: str, timeout: float = 30, state: str = "visible", **kwargs) -> Optional[_ElementHandle]:
        loc = _Locator(self._tab, selector)
        await loc.wait_for(state=state, timeout=timeout)
        el = await loc._try_find(timeout=4)
        if el:
            return _ElementHandle(self._tab, el)
        return None

    async def get_by_text(self, text: str) -> _Locator:
        return _Locator(self._tab, f'text="{text}"')

    # -- configuration (no-ops that don't break) -----------------------
    async def set_viewport_size(self, viewport: dict):
        w = viewport.get("width", 1280)
        h = viewport.get("height", 720)
        await _run_sync(self._tab.set_window_size, 0, 0, w, h)

    async def add_init_script(self, script: str = None, path: str = None, **kwargs):
        # truedriver doesn't have per-context init scripts; the bot's
        # stealth layer injects via evaluate() after navigation anyway.
        pass

    async def route(self, url: str, handler=None, **kwargs):
        # Minimal stub — the bot blocks images/analytics but we rely on
        # truedriver's built-in blocking profile for now.
        pass

    async def unroute(self, url: str, handler=None, **kwargs):
        pass


class _BrowserContext:
    """Wraps truedriver's concept of a browsing context (tab)."""

    def __init__(self, browser: td.Browser, page: _Page):
        self._browser = browser
        self._page = page
        self.browser = None  # set externally

    async def new_page(self) -> _Page:
        tab = await _run_sync(self._browser.get, "about:blank")
        page = _Page(tab, context=self)
        return page

    async def close(self, **kwargs):
        pass

    async def add_init_script(self, script: str = None, path: str = None, **kwargs):
        pass

    async def route(self, url: str, handler=None, **kwargs):
        pass


class _Browser:
    """Wraps a truedriver Browser."""

    def __init__(self, instance: td.Browser):
        self._instance = instance
        self.contexts: List[_BrowserContext] = []

    async def new_context(self, **kwargs) -> _Page:
        """Create a new context and return its default page."""
        viewport = kwargs.get("viewport")
        user_agent = kwargs.get("user_agent", "")
        proxy_cfg = kwargs.get("proxy")

        tab = await _run_sync(self._instance.get, "about:blank", False, False, 15)

        # Apply viewport
        if viewport:
            w = viewport.get("width", 1280)
            h = viewport.get("height", 720)
            await _run_sync(tab.set_window_size, 0, 0, w, h)

        # Apply user-agent
        if user_agent:
            try:
                await _run_sync(tab.set_user_agent, user_agent)
            except Exception:
                pass

        # Proxy is handled at Browser launch level (truedriver Config.proxy)
        page = _Page(tab)
        ctx = _BrowserContext(self._instance, page)
        ctx.browser = self
        page._context = ctx
        self.contexts.append(ctx)
        return page

    async def close(self, **kwargs):
        try:
            await _run_sync(self._instance.stop)
        except Exception:
            pass
        self.contexts.clear()


class _BrowserType:
    """Playwright-like BrowserType (chromium/firefox/webkit)."""

    async def launch(
        self,
        *,
        headless: bool = True,
        args: List[str] = None,
        executable_path: str = None,
        proxy: dict = None,
        channel: str = None,
        **kwargs,
    ) -> _Browser:
        exe = executable_path or _find_thorium()

        # Build proxy URL from Playwright-style proxy dict
        proxy_url = ""
        if proxy and proxy.get("server"):
            server = proxy["server"]
            user = proxy.get("username", "")
            pwd = proxy.get("password", "")
            host_part = server.replace("http://", "").replace("https://", "")
            if user and pwd:
                proxy_url = f"http://{user}:{pwd}@{host_part}"
            else:
                proxy_url = server

        # Extract UA from args
        ua = ""
        if args:
            for a in args:
                if a.startswith("--user-agent="):
                    ua = a.replace("--user-agent=", "")

        cfg = td.Config(
            browser_executable_path=exe,
            headless=headless,
            sandbox=False,
            browser_args=args if args else [],
            browser_connection_timeout=60,
            browser_connection_max_tries=3,
        )
        if proxy_url:
            cfg.proxy = proxy_url
        if ua:
            cfg.user_agent = ua

        instance = await td.start(cfg)
        return _Browser(instance)

    async def launch_persistent_context(self, user_data_dir: str = None, **kwargs) -> _Browser:
        return await self.launch(**kwargs)


class _Playwright:
    """Top-level Playwright-style entrypoint."""

    def __init__(self):
        self.chromium = _BrowserType()
        self.firefox = _BrowserType()  # both drive the same Thorium binary
        self.webkit = _BrowserType()

    async def start(self) -> "_Playwright":
        return self

    async def stop(self):
        pass


def async_playwright() -> _Playwright:
    return _Playwright()


__all__ = ["async_playwright", "ENGINE"]
