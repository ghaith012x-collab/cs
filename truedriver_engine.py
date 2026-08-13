"""
truedriver_engine.py — Playwright-compatible async API implemented on top of
truedriver 0.1.5+ (CDP-based browser automation), driving the Clearcote
stealth Chromium binary.

All truedriver Browser / Tab / Element methods are async.
Drop-in replacement for ``from browser_engine import async_playwright``.

The DRIVER is truedriver (pure CDP — no Playwright driver anywhere). The
BROWSER is Clearcote's de-Googled Chromium: its C++ fingerprint machinery is
driven by command-line switches (--fingerprint=..., --fingerprint-platform,
--timezone, --accept-lang ...), so launch() translates the bot's per-session
fingerprint seed into the same engine switches the Clearcote SDK would pass.

Set CLEARCOTE_BINARY to override the Clearcote browser binary location.
"""

import asyncio
import base64
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import truedriver as td

ENGINE = "clearcote"


class _AuthRelay:
    """Local HTTP proxy that injects ``Proxy-Authorization`` for an upstream
    authenticated proxy (vaultproxies etc.).

    Why this exists: the vaultproxies gateway answers CONNECT with a 407 that
    Chromium cannot answer itself — inline ``user:pass@`` in ``--proxy-server``
    is ignored (truedriver strips the credentials) and the gateway rejects CDP
    Fetch ``authRequired`` responses. The page then hangs forever with
    ``title="(unknown)" url="(unknown)"`` — the exact error this repo hit with
    Clearcote. A plain upstream-facing proxy that adds the Basic header on
    CONNECT works (it is the same path the aiohttp probe, which succeeds,
    uses): the browser points at 127.0.0.1 with no credentials and the relay
    authenticates upstream. This is the same relay the ShardX engine used.
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
                # Bounded: a wedged upstream (TCP accepts but never answers the
                # CONNECT) must fail the browser's tunnel FAST — an unbounded
                # readline here hangs the renderer forever, which is the
                # title="(unknown)" url="(unknown)" error. On timeout we close
                # both ends so Chromium commits a clean proxy error page and
                # the worker rotates instead of wedging.
                try:
                    resp = await asyncio.wait_for(
                        ureader.readline(), timeout=10)
                except asyncio.TimeoutError:
                    writer.close()
                    uwriter.close()
                    return
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
# Helpers
# ─────────────────────────────────────────────────────────────────


def _unwrap_evaluate(result):
    """Normalize truedriver Tab.evaluate() results.

    truedriver returns ``(RemoteObject, errors)`` instead of the value
    whenever the JS result is falsy (0, false, '', null) — its guard is
    ``if remote_object.value:``. The bot relies on falsy results (e.g.
    input_idx == 0, '' = "no phone verify", false = "not mounted"), so
    unwrap the RemoteObject back to its real JS value.
    """
    if isinstance(result, tuple) and len(result) == 2:
        ro = result[0]
        if ro is None:
            return None
        if getattr(ro, "unserializable_value", None) is not None:
            return ro.unserializable_value
        return getattr(ro, "value", None)
    return result


def _is_function_expr(expr: str) -> bool:
    """True when the expression is a JS function (arrow or declaration)."""
    return (
        "=>" in expr
        or expr.startswith("function(")
        or expr.startswith("function (")
        or expr.startswith("async function(")
        or expr.startswith("async function (")
    )


def _build_eval_expr(expression: str, args: tuple):
    """Prepare an expression for truedriver's Tab.evaluate().

    Playwright auto-invokes function expressions and passes positional args
    to them. truedriver does neither, so:
      - function bodies are wrapped into an IIFE,
      - positional args are JSON-embedded and spread into the call,
      - async functions get await_promise=True so the Promise resolves.

    Returns (expression_to_run, await_promise).
    """
    s = expression.strip()
    is_func = _is_function_expr(s)
    await_promise = s.startswith("async ")
    if is_func and args:
        args_json = "[" + ", ".join(
            json.dumps(a, ensure_ascii=False, default=str) for a in args
        ) + "]"
        return f"((..._pw) => ({s})(..._pw))({args_json})", await_promise
    if is_func:
        return f"({s})()", await_promise
    return s, await_promise


def _find_browser() -> str:
    """Locate the Clearcote stealth Chromium binary.

    Order: ``CLEARCOTE_BINARY`` env > the Clearcote SDK's ``executable_path()``
    (which downloads/verifies the pinned release on first use) > PATH fallback.
    """
    env = os.environ.get("CLEARCOTE_BINARY", "").strip()
    if env and os.path.exists(env):
        return env
    try:
        from clearcote import executable_path
        return executable_path(quiet=True)
    except Exception:
        pass
    return "chrome"  # rely on PATH


def _clearcote_launch_args(kwargs: dict) -> list:
    """Translate Clearcote persona kwargs into engine command-line switches.

    Clearcote's fingerprint machinery lives in the binary itself (C++ getters,
    coherent TLS/JA3, per-seed personas) and is configured purely via launch
    switches. This accepts the same fingerprint kwargs the Clearcote SDK's
    ``launch()`` takes (fingerprint seed, platform, timezone, accept_language,
    brand, gpu_*, ...) and emits the equivalent ``--fingerprint-*`` switches,
    so the browser gets a real persona even though the driver is truedriver.

    Best-effort: if the Clearcote SDK internals move, degrade to the bare
    seed switch rather than failing the whole launch.
    """
    try:
        from clearcote._fingerprint import FINGERPRINT_KEYS, fingerprint_args
        from clearcote._fontpersona import ensure_persona_fonts
    except Exception:
        seed = kwargs.get("fingerprint")
        return [f"--fingerprint={seed}"] if seed else []
    fp = {k: kwargs[k] for k in FINGERPRINT_KEYS if kwargs.get(k) is not None}
    if not fp:
        return []
    # One seed → one coherent machine identity; a fresh seed → a fresh,
    # unlinkable one. Default to a random seed when the caller didn't pin one
    # (solver utilities) so concurrent launches never collide.
    if fp.get("fingerprint") in (None, ""):
        fp["fingerprint"] = f"cc-{os.getpid()}-{random.getrandbits(64):016x}"
    # Give the seeded persona a real machine's font list (same step the SDK
    # runs before building switches). Best-effort, never blocks a launch.
    ensure_persona_fonts(fp)
    return fingerprint_args(fp)


def _apply_browser_fonts(exe: str) -> None:
    """Point the Clearcote binary at its bundled font clones (Linux).

    The SDK merges FONTCONFIG_FILE into Playwright's launch env; truedriver
    spawns the browser as a child process that inherits our env, so we set it
    directly instead. Best-effort — never block a launch on font wiring.
    """
    try:
        from clearcote._fonts import linux_font_env
        for key, value in (linux_font_env(exe) or {}).items():
            os.environ[key] = value
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────
# Smart locator – Playwright-like lazy selector with per-frame context
# ─────────────────────────────────────────────────────────────────


class _Locator:
    """A lazy selector that resolves to a truedriver ``Element`` on every
    action call.  Playwright locator semantics: ``loc = page.locator('…')``
    is instant; the actual DOM query happens inside ``.click()`` etc."""

    def __init__(
        self,
        tab: td.Tab,
        selector: str,
        *,
        frame_url: str = "",
        frame_title: str = "",
        nth_index: Optional[int] = None,
    ):
        self._tab = tab
        self._raw_sel = selector
        self._frame_url = frame_url
        self._frame_title = frame_title
        self._nth = nth_index

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _find_frame_element(self) -> Optional[td.Element]:
        """Return the IFRAME element (in the MAIN document) that this
        locator's frame context refers to, matching by src URL or title
        attribute.

        truedriver 0.1.5's switch_to_frame() is broken (it calls
        cdp.runtime.get_execution_contexts, which does not exist), so
        frame-scoped queries cannot rely on it. Instead locate the iframe
        element in the main document and pierce into its content document
        via CDP DOM, which works across origins (hCaptcha widget frames
        live on newassets.hcaptcha.com while the page is discord.com).
        """
        try:
            # Always query the MAIN document: a stale _current_frame_id
            # from an earlier switch_to_frame would otherwise scope
            # query_selector_all to the wrong frame's document.
            await self._tab.switch_to_main_frame()
            iframes = await self._tab.query_selector_all("iframe")
        except Exception:
            return None
        for f in iframes:
            if self._frame_url:
                try:
                    src = f.get("src") or ""
                except Exception:
                    src = ""
                if src and (self._frame_url in src or src in self._frame_url):
                    return f
            if self._frame_title:
                try:
                    title = f.get("title") or ""
                except Exception:
                    title = ""
                if title and title.strip() == self._frame_title.strip():
                    return f
        return None

    async def _find_all_matches(self, timeout: float = 5) -> List[td.Element]:
        """All DOM matches of the raw selector, frame-aware.

        Playwright resolves ``.first`` / ``.nth(i)`` / ``.last`` by INDEX
        AMONG ALL MATCHES of the selector. The old implementation rewrote
        the selector into CSS ``:nth-child(n)`` (position among siblings),
        which silently matched nothing for most real elements — ``body`` is
        the 2nd child of <html>, an id'd checkbox is rarely its parent's
        first child, and ``[role="checkbox"]`` matches several nodes. Keep
        the raw selector and index into the result list instead, preserving
        the Playwright contract server.py / captcha_solver.py are written
        against (the hCaptcha widget scan uses ``widgets.nth(i)`` and the
        checkbox code uses ``locator(...).first`` inside the widget frame).

        Frame-scoped locators (frame_url / frame_title) resolve the iframe
        element in the main document and query its content document
        (element.query_selector_all) — that pierces into cross-origin
        iframe DOM via CDP, which switch_to_frame-based find_all never
        reached. Like find_all, poll up to `timeout` so the inner document
        (which mounts a moment after the iframe element) is caught.
        """
        try:
            if self._frame_url or self._frame_title:
                deadline = time.monotonic() + max(float(timeout), 2.0)
                while True:
                    frame_el = await self._find_frame_element()
                    if frame_el is not None:
                        try:
                            matches = await frame_el.query_selector_all(self._raw_sel)
                        except Exception:
                            matches = []
                        if matches:
                            return matches
                    if time.monotonic() >= deadline:
                        return []
                    await asyncio.sleep(0.25)
            return await self._tab.find_all(self._raw_sel, timeout)
        except Exception:
            return []

    async def _try_find(self, timeout: float = 5) -> Optional[td.Element]:
        els = await self._find_all_matches(timeout)
        if not els:
            return None
        if self._nth is None or self._nth == 0:
            return els[0]
        if self._nth > 0:
            return els[self._nth] if self._nth < len(els) else None
        idx = len(els) + self._nth  # negative index: .last == nth(-1)
        return els[idx] if 0 <= idx < len(els) else None

    async def _require(self, timeout: float = 10) -> td.Element:
        el = await self._try_find(timeout)
        if el is None:
            raise RuntimeError(f"Element not found: {self._raw_sel}")
        return el

    async def _in_frame(self, coro):
        """Execute within frame context, switching back afterwards."""
        if self._frame_url:
            frame_obj = await self._tab.find_frame_by_url(self._frame_url)
            if frame_obj:
                await self._tab.switch_to_frame(frame_obj)
                try:
                    return await coro
                finally:
                    await self._tab.switch_to_main_frame()
        return await coro

    # ------------------------------------------------------------------
    # Playwright locator API
    # ------------------------------------------------------------------

    async def click(self, timeout: float = 30, **kwargs):
        el = await self._require(timeout)
        async def _do():
            await el.scroll_into_view()
            await asyncio.sleep(0.1)
            await el.click()
        await self._in_frame(_do())

    async def fill(self, value: str, timeout: float = 30, **kwargs):
        el = await self._require(timeout)
        async def _do():
            await el.focus()
            await asyncio.sleep(0.05)
            await el.clear_input()
            await asyncio.sleep(0.05)
            await el.send_keys(value)
        await self._in_frame(_do())

    async def type(self, value: str, delay: float = 0.08, timeout: float = 30, **kwargs):
        el = await self._require(timeout)
        sleep_s = delay / 1000.0 if delay >= 1 else delay  # ms or seconds
        async def _do():
            await el.focus()
            for ch in value:
                await el.send_keys(ch)
                await asyncio.sleep(sleep_s)
        await self._in_frame(_do())

    async def press(self, key: str, timeout: float = 30, **kwargs):
        el = await self._require(timeout)
        await self._in_frame(el.send_keys(key))

    async def wait_for(self, state: str = "visible", timeout: float = 30, **kwargs):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            el = await self._try_find(timeout=4)
            if el is not None:
                if state == "visible":
                    try:
                        pos = await el.get_position()
                    except Exception:
                        pos = None
                    if pos is not None:
                        return
                elif state in ("attached",):
                    return
                elif state == "detached":
                    pass
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"Timed out waiting for {self._raw_sel} to be {state}"
        )

    async def is_visible(self, timeout: float = 5) -> bool:
        el = await self._try_find(timeout)
        if el is None:
            return False
        try:
            return (await el.get_position()) is not None
        except Exception:
            return False

    async def bounding_box(self) -> Optional[dict]:
        el = await self._try_find(timeout=4)
        if el is None:
            return None
        try:
            pos = await el.get_position()
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
            return (await el.apply("(el) => (el.textContent || '')")) or ""
        except Exception:
            return ""

    async def inner_text(self) -> str:
        el = await self._try_find(timeout=4)
        if el is None:
            return ""
        try:
            return (await el.apply("(el) => (el.innerText || '')")) or ""
        except Exception:
            return ""

    async def input_value(self) -> str:
        el = await self._try_find(timeout=4)
        if el is None:
            return ""
        try:
            return el.get("value") or ""
        except Exception:
            return ""

    async def get_attribute(self, name: str) -> Optional[str]:
        el = await self._try_find(timeout=4)
        if el is None:
            return None
        try:
            return el.get(name)
        except Exception:
            return None

    async def is_checked(self) -> bool:
        el = await self._try_find(timeout=3)
        if el is None:
            return False
        try:
            return el.get("checked") == "true"
        except Exception:
            return False

    async def is_disabled(self) -> bool:
        el = await self._try_find(timeout=3)
        if el is None:
            return True
        try:
            return el.get("disabled") == "true"
        except Exception:
            return True

    async def count(self) -> int:
        return len(await self._find_all_matches(timeout=3))

    def nth(self, index: int) -> "_Locator":
        return _Locator(
            self._tab,
            self._raw_sel,
            frame_url=self._frame_url,
            frame_title=self._frame_title,
            nth_index=index,
        )

    @property
    def first(self) -> "_Locator":
        return self.nth(0)

    @property
    def last(self) -> "_Locator":
        return _Locator(self._tab, self._raw_sel,
                        frame_url=self._frame_url,
                        frame_title=self._frame_title,
                        nth_index=-1)

    def locator(self, selector: str) -> "_Locator":
        return _Locator(
            self._tab,
            f"{self._raw_sel} {selector}",
            frame_url=self._frame_url,
            frame_title=self._frame_title,
        )

    async def screenshot(self, path: str = None, type: str = "png", **kwargs) -> Optional[bytes]:
        el = await self._try_find(timeout=5)
        if el is None:
            return None
        async def _do():
            if path:
                await el.save_screenshot(path, type)
                return None
            b64 = await el.screenshot_b64(type)
            return base64.b64decode(b64)
        return await self._in_frame(_do())

    async def clear(self, timeout: float = 10, **kwargs):
        el = await self._require(timeout)
        await self._in_frame(el.clear_input())

    async def focus(self, timeout: float = 10, **kwargs):
        el = await self._require(timeout)
        await self._in_frame(el.focus())

    async def scroll_into_view_if_needed(self, timeout: float = 10, **kwargs):
        el = await self._try_find(timeout)
        if el is not None:
            await self._in_frame(el.scroll_into_view())

    async def element_handle(self, timeout: float = 10) -> Optional["_ElementHandle"]:
        el = await self._try_find(timeout)
        if el is None:
            return None
        return _ElementHandle(self._tab, el)

    async def content_frame(self, timeout: float = 10) -> Optional["_Frame"]:
        """Playwright-compatible: the content frame of the iframe this
        locator resolves to (None when it isn't an attached iframe).

        server.py's hCaptcha helpers (``_widget_rendered``,
        ``_challenge_rendered``, ``_detect_challenge_mode``) and the
        accessibility solver all call ``locator.content_frame()``; without
        it every readiness check threw AttributeError and the widget was
        never considered ready, so the checkbox was never clicked.
        """
        el = await self._try_find(timeout)
        if el is None:
            return None
        return await _ElementHandle(self._tab, el).content_frame()

    async def evaluate(self, expression: str, *args, **kwargs):
        expr, await_promise = _build_eval_expr(expression, args)
        el = await self._require(timeout=5)
        async def _do():
            return await el.apply(
                expr,
                kwargs.get("return_by_value", True),
                await_promise=await_promise,
            )
        return await self._in_frame(_do())

    async def dispatch_event(self, event_type: str, **kwargs):
        el = await self._require(timeout=5)
        js = f"this.dispatchEvent(new Event('{event_type}', {{bubbles: true}}))"
        await self._in_frame(el.apply(js))


# ─────────────────────────────────────────────────────────────────
# Frame locator – cross-origin iframe targeting
# ─────────────────────────────────────────────────────────────────


class _FrameLocator:
    """Created by ``page.frame_locator('iframe[src="…"]')``."""

    def __init__(self, tab: td.Tab, selector: str):
        self._tab = tab
        self._selector = selector
        self._frame_url = ""
        self._frame_title = ""
        m = re.search(r'src[*^]?=["\']?([^"\'\]\s]+)', selector)
        if m:
            self._frame_url = m.group(1)
        else:
            # The accessibility solver locates the challenge iframe by
            # title (iframe[title="hCaptcha challenge"]) — match that too.
            # Capture the WHOLE quoted value: the title contains a space.
            m = re.search(r'title=["\']([^"\']+)["\']', selector)
            if m:
                self._frame_title = m.group(1)

    def locator(self, selector: str) -> _Locator:
        return _Locator(self._tab, selector,
                        frame_url=self._frame_url,
                        frame_title=self._frame_title)

    def nth(self, index: int) -> "_FrameLocator":
        return self

    def get_by_role(self, role: str, name: str = None, **kwargs) -> _Locator:
        sel = f'[role="{role}"], {role}'
        if name:
            sel = f'{role}:has-text("{name}"), [role="{role}"]:has-text("{name}"), [aria-label="{name}"]'
        return _Locator(self._tab, sel, frame_url=self._frame_url,
                        frame_title=self._frame_title)

    def get_by_label(self, text: str, **kwargs) -> _Locator:
        return _Locator(self._tab, f'[aria-label="{text}"], [aria-labelledby*="{text}"]',
                        frame_url=self._frame_url, frame_title=self._frame_title)

    def get_by_text(self, text: str, **kwargs) -> _Locator:
        return _Locator(self._tab, f'text="{text}"', frame_url=self._frame_url,
                        frame_title=self._frame_title)


# ─────────────────────────────────────────────────────────────────
# Frame – a sub-page context
# ─────────────────────────────────────────────────────────────────


class _Frame:
    __slots__ = ("_tab", "_cdp_frame", "url", "_ctx_id")

    def __init__(self, tab: td.Tab, cdp_frame):
        self._tab = tab
        self._cdp_frame = cdp_frame
        self.url: str = getattr(cdp_frame, "url", "") or ""
        self._ctx_id = None

    def locator(self, selector: str) -> _Locator:
        return _Locator(self._tab, selector, frame_url=self.url)

    async def _context_id(self):
        """A REAL execution context id for this frame.

        truedriver 0.1.5's switch_to_frame() fails to resolve the frame's
        execution context (it calls cdp.runtime.get_execution_contexts,
        which does not exist in this version), so tab.evaluate() after a
        switch silently runs in the MAIN frame — every hCaptcha readiness
        check and checkbox probe then inspected discord.com instead of the
        widget, and the checkbox was never clicked. Page.createIsolatedWorld
        returns a genuine context id for the frame and works for cross-
        origin / OOPIF frames (newassets.hcaptcha.com inside discord.com).
        """
        if self._ctx_id is None:
            try:
                from truedriver import cdp
                self._ctx_id = await self._tab.send(
                    cdp.page.create_isolated_world(self._cdp_frame.id_)
                )
            except Exception:
                self._ctx_id = None
        return self._ctx_id

    async def evaluate(self, expression: str, *args, **kwargs) -> Any:
        expr, await_promise = _build_eval_expr(expression, args)
        prev_ctx = getattr(self._tab, "_current_execution_context_id", None)
        try:
            # tab.evaluate() honors _current_execution_context_id; set it to
            # this frame's isolated world so Runtime.evaluate targets the
            # frame, then restore whatever context was active before.
            self._tab._current_execution_context_id = await self._context_id()
            return _unwrap_evaluate(
                await self._tab.evaluate(expr, await_promise=await_promise)
            )
        finally:
            self._tab._current_execution_context_id = prev_ctx

    async def content(self) -> str:
        return await self.evaluate("document.documentElement.outerHTML") or ""

    async def title(self) -> str:
        return await self.evaluate("document.title") or ""

    async def wait_for_selector(
        self, selector: str, timeout: float = 30, state: str = "visible", **kwargs
    ) -> Optional["_ElementHandle"]:
        """Playwright-compatible frame.wait_for_selector()."""
        loc = _Locator(self._tab, selector, frame_url=self.url)
        await loc.wait_for(state=state, timeout=timeout)
        el = await loc._try_find(timeout=4)
        if el:
            return _ElementHandle(self._tab, el)
        return None


# ─────────────────────────────────────────────────────────────────
# ElementHandle – raw DOM element reference
# ─────────────────────────────────────────────────────────────────


class _ElementHandle:
    __slots__ = ("_tab", "_el")

    def __init__(self, tab: td.Tab, element: td.Element):
        self._tab = tab
        self._el = element

    async def content_frame(self) -> Optional[_Frame]:
        src = self._el.get("src")
        if not src:
            return None
        try:
            frame = await self._tab.find_frame_by_url(src)
        except Exception:
            return None
        if frame:
            return _Frame(self._tab, frame)
        return None

    async def bounding_box(self) -> Optional[dict]:
        try:
            pos = await self._el.get_position()
        except Exception:
            return None
        if pos is None:
            return None
        return {"x": pos[0], "y": pos[1], "width": pos[2], "height": pos[3]}

    async def screenshot(self, path: str = None, type: str = "png") -> Optional[bytes]:
        if path:
            await self._el.save_screenshot(path, type)
            return None
        b64 = await self._el.screenshot_b64(type)
        return base64.b64decode(b64)

    async def click(self, **kwargs):
        await self._el.click()

    async def fill(self, value: str, **kwargs):
        await self._el.focus()
        await asyncio.sleep(0.05)
        await self._el.clear_input()
        await asyncio.sleep(0.05)
        await self._el.send_keys(value)

    async def get_attribute(self, name: str) -> Optional[str]:
        try:
            return self._el.get(name)
        except Exception:
            return None

    async def is_checked(self) -> bool:
        try:
            return self._el.get("checked") == "true"
        except Exception:
            return False

    async def is_disabled(self) -> bool:
        try:
            return self._el.get("disabled") == "true"
        except Exception:
            return True

    async def evaluate(self, expression: str, **kwargs):
        expr, await_promise = _build_eval_expr(expression, ())
        return await self._el.apply(expr, await_promise=await_promise)

    async def dispatch_event(self, event_type: str, **kwargs):
        js = f"this.dispatchEvent(new Event('{event_type}', {{bubbles: true}}))"
        await self._el.apply(js)

    async def scroll_into_view_if_needed(self):
        await self._el.scroll_into_view()

    async def query_selector(self, selector: str) -> Optional["_ElementHandle"]:
        try:
            child = await self._el.query_selector(selector)
            if child:
                return _ElementHandle(self._tab, child)
        except Exception:
            pass
        return None


# ─────────────────────────────────────────────────────────────────
# Keyboard & Mouse
# ─────────────────────────────────────────────────────────────────


class _CdpSession:
    """Minimal CDP session wrapper — enough for stealth.js injection."""
    __slots__ = ("_tab",)

    def __init__(self, tab):
        self._tab = tab

    async def send(self, method: str, params: dict = None, **kwargs):
        """Send a CDP command via truedriver's raw CDP transport."""
        try:
            # Use tab.evaluate as fallback for Page.addScriptToEvaluateOnNewDocument
            # which is the only CDP command the bot uses through this path.
            if method == "Page.addScriptToEvaluateOnNewDocument":
                src_code = (params or {}).get("source", "")
                if src_code:
                    # Inject via evaluate — runs on next page load in practice
                    await self._tab.evaluate(
                        f"Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}}); ({src_code})()"
                    )
            else:
                # Fallback: try raw CDP via tab.send
                import cdp
                if hasattr(cdp, 'page') and hasattr(cdp.page, 'add_script_to_evaluate_on_new_document'):
                    cmd = cdp.page.add_script_to_evaluate_on_new_document((params or {}).get("source", ""))
                    await self._tab.send(cmd)
        except Exception:
            pass  # CDP stealth is best-effort


class _Keyboard:
    """Real CDP keyboard input.

    The old implementation dispatched synthetic, untrusted KeyboardEvent /
    InputEvent objects from JS (document.activeElement.dispatchEvent(...)).
    React-controlled inputs — Discord's register form — ignore those because
    the element value never actually changes, so the bot logged 'filled'
    while every field stayed empty and the form failed native validation
    with "Please fill out this field".

    These methods send trusted Input.dispatchKeyEvent / Input.insertText
    over CDP instead: the browser itself generates the key/input events,
    element.value updates, and React state follows.
    """

    __slots__ = ("_tab",)

    _SPECIAL = {
        "Enter": td.SpecialKeys.ENTER,
        "Tab": td.SpecialKeys.TAB,
        "Space": td.SpecialKeys.SPACE,
        "Backspace": td.SpecialKeys.BACKSPACE,
        "Escape": td.SpecialKeys.ESCAPE,
        "Delete": td.SpecialKeys.DELETE,
        "ArrowLeft": td.SpecialKeys.ARROW_LEFT,
        "ArrowUp": td.SpecialKeys.ARROW_UP,
        "ArrowRight": td.SpecialKeys.ARROW_RIGHT,
        "ArrowDown": td.SpecialKeys.ARROW_DOWN,
    }

    def __init__(self, tab: td.Tab):
        self._tab = tab

    async def _dispatch(self, payloads) -> None:
        for payload in payloads:
            await self._tab.send(td.cdp.input_.dispatch_key_event(**payload))

    async def _insert(self, text: str) -> bool:
        """Fallback: Input.insertText — still real browser input, so
        React-controlled fields accept it."""
        try:
            await self._tab.send(td.cdp.input_.insert_text(text))
            return True
        except Exception:
            return False

    async def press(self, key: str, **kwargs):
        special = self._SPECIAL.get(key)
        try:
            if special is not None:
                payloads = td.KeyEvents(special).to_cdp_events(
                    td.KeyPressEvent.DOWN_AND_UP)
            elif len(key) == 1:
                payloads = td.KeyEvents.from_text(
                    key, td.KeyPressEvent.DOWN_AND_UP)
            else:
                payloads = []
            if payloads:
                await self._dispatch(payloads)
                return
        except Exception:
            pass
        await self._insert(key)

    async def type(self, text: str, delay: float = 0, **kwargs):
        # delay is in MILLISECONDS (Playwright API). human_type() passes
        # int(delay*1000) = ~75ms; treating it as seconds made each char
        # take 75s (a 22-char email = 27 minutes).
        for ch in text:
            try:
                payloads = td.KeyEvents.from_text(
                    ch, td.KeyPressEvent.DOWN_AND_UP)
                if payloads:
                    await self._dispatch(payloads)
                else:
                    await self._insert(ch)
            except Exception:
                await self._insert(ch)
            if delay:
                await asyncio.sleep(delay / 1000.0)


class _Mouse:
    __slots__ = ("_tab",)

    def __init__(self, tab: td.Tab):
        self._tab = tab

    async def move(self, x: float, y: float, **kwargs):
        steps = kwargs.get("steps", 10)
        await self._tab.mouse_move(x, y, steps)

    async def click(self, x: float, y: float, **kwargs):
        await self._tab.mouse_click(x, y)


# ─────────────────────────────────────────────────────────────────
# Page / Context / Browser wrappers
# ─────────────────────────────────────────────────────────────────


class _Page:
    """Wraps a truedriver Tab to look like a Playwright Page."""

    def __init__(self, tab: td.Tab, context: Optional["_BrowserContext"] = None):
        self._tab = tab
        self._context = context
        self._cached_frames: List[_Frame] = []
        self.keyboard = _Keyboard(tab)
        self.mouse = _Mouse(tab)

    # -- navigation ---------------------------------------------------
    async def goto(self, url: str, timeout: float = 30, wait_until: str = "load", **kwargs):
        # Navigate via raw CDP Page.navigate. We deliberately do NOT use
        # truedriver's tab.get(): it ends with an UNBOUNDED `await wait()`
        # that never returns on pages like Discord (websockets + polling
        # never go "idle"), which made goto() hang for minutes.
        nav_timeout = min(timeout, 30)
        from truedriver import cdp

        async def _navigate():
            try:
                await self._tab.send(cdp.page.navigate(url))
            except Exception:
                # Fallback: JS navigation
                try:
                    await self._tab.evaluate(f"location.href = {url!r}")
                except Exception:
                    pass
            # Wait for DOM ready (bounded)
            state = "interactive" if wait_until == "domcontentloaded" else "complete"
            try:
                await self._tab.wait_for_ready_state(state, nav_timeout)
            except Exception:
                pass
            # Cache frames after navigation
            try:
                self._cached_frames = [_Frame(self._tab, f) for f in await self._tab.get_frames()]
            except Exception:
                self._cached_frames = []

        # Hard cap: even if a CDP call stalls, goto() always returns.
        try:
            await asyncio.wait_for(_navigate(), timeout=nav_timeout + 5)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass

    async def reload(self, **kwargs):
        await self._tab.reload()
        # Refresh cached frames after reload
        try:
            self._cached_frames = [_Frame(self._tab, f) for f in await self._tab.get_frames()]
        except Exception:
            self._cached_frames = []

    async def close(self, **kwargs):
        try:
            await self._tab.close()
        except Exception:
            pass

    # -- locators -----------------------------------------------------
    def locator(self, selector: str) -> _Locator:
        return _Locator(self._tab, selector)

    async def click(self, selector: str, **kwargs):
        await _Locator(self._tab, selector).click(**kwargs)

    async def fill(self, selector: str, value: str, **kwargs):
        await _Locator(self._tab, selector).fill(value, **kwargs)

    async def type(self, selector: str, value: str, **kwargs):
        await _Locator(self._tab, selector).type(value, **kwargs)

    async def press(self, selector: str, key: str, **kwargs):
        await _Locator(self._tab, selector).press(key, **kwargs)

    def frame_locator(self, selector: str) -> _FrameLocator:
        return _FrameLocator(self._tab, selector)

    # -- frames -------------------------------------------------------
    @property
    def frames(self) -> List[_Frame]:
        return self._cached_frames

    # -- content / title / url ----------------------------------------
    async def title(self) -> str:
        try:
            return _unwrap_evaluate(await self._tab.evaluate("document.title")) or ""
        except Exception:
            return ""

    @property
    def url(self) -> str:
        for f in self._cached_frames:
            u = getattr(f, "url", "") or ""
            if u and "about:blank" not in u:
                return u
        return ""

    async def content(self) -> str:
        try:
            return await self._tab.get_content() or ""
        except Exception:
            return ""

    # -- screenshots --------------------------------------------------
    async def screenshot(self, path: str = None, full_page: bool = False, type: str = "png", **kwargs):
        fmt = type if type in ("jpeg", "png") else "png"
        try:
            if path:
                await self._tab.save_screenshot(path, fmt, full_page)
                return None
            try:
                b64 = await asyncio.wait_for(self._tab.screenshot_b64(fmt, full_page), timeout=15)
            except Exception:
                # Full-page capture can fail/hang on SPAs (Discord) — retry viewport
                try:
                    b64 = await asyncio.wait_for(self._tab.screenshot_b64(fmt, False), timeout=8)
                except Exception:
                    return None
            if not b64:
                return None
            return base64.b64decode(b64)
        except Exception:
            return None

    # -- evaluate / query --------------------------------------------
    async def evaluate(self, expression: str, *args, **kwargs):
        # truedriver passes expressions directly to CDP Runtime.evaluate
        # which does NOT auto-invoke function expressions or forward
        # positional args like Playwright. _build_eval_expr handles both:
        # function bodies are IIFE-wrapped, args are JSON-embedded and
        # spread into the call, async functions are awaited.
        expr, await_promise = _build_eval_expr(expression, args)
        try:
            return _unwrap_evaluate(
                await self._tab.evaluate(expr, await_promise=await_promise)
            )
        except Exception:
            return None

    async def query_selector(self, selector: str) -> Optional[_ElementHandle]:
        try:
            el = await self._tab.find(selector, True, True, 4)
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

    def get_by_text(self, text: str, **kwargs) -> _Locator:
        return _Locator(self._tab, f'text="{text}"')

    def get_by_role(self, role: str, name: str = None, **kwargs) -> _Locator:
        sel = f'[role="{role}"], {role}'
        if name:
            sel = f'{role}:has-text("{name}"), [role="{role}"]:has-text("{name}"), [aria-label="{name}"]'
        return _Locator(self._tab, sel)

    def get_by_label(self, text: str, **kwargs) -> _Locator:
        return _Locator(self._tab, f'[aria-label="{text}"], [aria-labelledby*="{text}"]')

    # -- configuration (no-ops that don't break) -----------------------
    async def set_viewport_size(self, viewport: dict):
        w = viewport.get("width", 1280)
        h = viewport.get("height", 720)
        try:
            await self._tab.set_window_size(0, 0, w, h)
        except Exception:
            pass

    async def add_init_script(self, script: str = None, path: str = None, **kwargs):
        pass  # stealth JS injects via evaluate() after navigation

    async def route(self, url: str, handler=None, **kwargs):
        pass

    async def new_cdp_session(self, page=None) -> "_CdpSession":
        """Minimal CDP session for stealth patches (Page.addScriptToEvaluateOnNewDocument)."""
        return _CdpSession(self._tab)

    async def unroute(self, url: str, handler=None, **kwargs):
        pass


class _BrowserContext:
    def __init__(self, browser: td.Browser, tab: td.Tab):
        self._browser = browser
        self._tab = tab
        self.browser = None  # set externally by _Browser.new_context()

    async def new_page(self) -> _Page:
        page = _Page(self._tab, context=self)
        return page

    async def close(self, **kwargs):
        pass

    async def add_init_script(self, script: str = None, path: str = None, **kwargs):
        pass

    async def route(self, url: str, handler=None, **kwargs):
        pass

    async def new_cdp_session(self, page=None) -> "_CdpSession":
        """Minimal CDP session for stealth patches (Page.addScriptToEvaluateOnNewDocument)."""
        return _CdpSession(self._tab)


class _Browser:
    def __init__(self, instance: td.Browser, relay: Optional[_AuthRelay] = None):
        self._instance = instance
        self._relay = relay
        self.contexts: List[_BrowserContext] = []

    async def _get_or_create_tab(self) -> td.Tab:
        """Return a FRESH page tab in this browser.

        Every ``new_context()`` gets its own tab, so multiple contexts can
        live side-by-side in one browser — e.g. the Discord page AND the
        temp-mail inbox share the worker browser instead of a second launch.
        truedriver's ``Browser.get()`` reuses the first page target, which
        would hand two contexts the same tab and let one navigation clobber
        the other (the old "context rebuild" StopIteration loop came from
        relying on that path after tabs were closed). Create a fresh target
        and wait (bounded) for it to appear in the target inventory.
        """
        from truedriver import cdp

        try:
            target_id = await self._instance.connection.send(
                cdp.target.create_target(
                    "about:blank", new_window=False, enable_begin_frame_control=True
                )
            )
        except Exception:
            target_id = None

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                await self._instance.update_targets()
            except Exception:
                pass
            for t in self._instance.targets:
                if (getattr(t, "type_", "") == "page"
                        and (target_id is None or t.target_id == target_id)):
                    try:
                        t.browser = self._instance
                    except Exception:
                        pass
                    # CRITICAL: wire CDP Fetch proxy auth on this tab so the
                    # proxy's 407 challenge gets answered. truedriver only
                    # applies setup_proxy_auth() to tabs IT creates — this
                    # raw create_target path bypasses that, leaving the tab
                    # without a Fetch handler. The vaultproxies gateway then
                    # rejects the unauthenticated CONNECT with 407 and the
                    # page hangs: title="(unknown)" url="(unknown)" /
                    # chrome-error://chromewebdata/. aiohttp probes passed
                    # because they embed credentials in the proxy URL, but
                    # Chromium cannot — it needs CDP Fetch auth per tab.
                    try:
                        auth = getattr(self._instance, "_proxy_auth", None)
                        if auth:
                            await t.setup_proxy_auth()
                            # Log at most once per browser session
                            if not getattr(self._instance, "_proxy_auth_logged", False):
                                print(f"[Engine] CDP Fetch proxy auth wired for tab (user={auth.get('username','?')[:20]}...)", flush=True)
                                self._instance._proxy_auth_logged = True
                    except Exception as e:
                        print(f"[Engine] CDP Fetch proxy auth FAILED: {e}", flush=True)
                    return t
            await asyncio.sleep(0.2)
        raise RuntimeError("could not create a browser tab (target never appeared)")

    async def new_context(self, **kwargs) -> "_BrowserContext":
        viewport = kwargs.get("viewport")
        user_agent = kwargs.get("user_agent", "")

        tab = await self._get_or_create_tab()

        if viewport:
            w = viewport.get("width", 1280)
            h = viewport.get("height", 720)
            try:
                await tab.set_window_size(0, 0, w, h)
            except Exception:
                pass

        if user_agent:
            try:
                await tab.set_user_agent(user_agent)
            except Exception:
                pass

        ctx = _BrowserContext(self._instance, tab)
        ctx.browser = self
        self.contexts.append(ctx)
        return ctx

    async def close(self, **kwargs):
        try:
            await self._instance.stop()
        except Exception:
            pass
        if self._relay is not None:
            try:
                await self._relay.stop()
            except Exception:
                pass
            self._relay = None
        self.contexts.clear()


class _BrowserType:
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
        exe = executable_path or _find_browser()

        # Clearcote persona switches (--fingerprint=..., --fingerprint-platform,
        # --timezone, --accept-lang ...) — the binary's C++ layer reads them at
        # startup. Plus the bundled-font env so the persona's fonts resolve.
        cc_args = _clearcote_launch_args(kwargs)
        browser_args = list(args or []) + cc_args
        _apply_browser_fonts(exe)

        # ── Authenticated HTTP(S) proxy → local auth relay ──
        # vaultproxies' gateway answers CONNECT with a 407 that Chromium
        # cannot answer itself (inline user:pass@ in --proxy-server is
        # ignored and the gateway rejects CDP Fetch authRequired) — the
        # page then hangs with title="(unknown)" url="(unknown)". The
        # proven fix is a local relay that injects Proxy-Authorization on
        # CONNECT at the TCP layer: the browser points at 127.0.0.1 with
        # no credentials, the relay authenticates upstream (exactly the
        # path the working aiohttp probe uses). Same relay ShardX used.
        relay = None
        proxy_url = ""
        use_cdp_auth = False
        if proxy and proxy.get("server"):
            server = str(proxy["server"])
            user = proxy.get("username", "") or ""
            pwd = proxy.get("password", "") or ""
            if user and server.startswith(("http://", "https://")):
                u = urlparse(server)
                relay = _AuthRelay(u.hostname, u.port or 80, user, pwd)
                await relay.start()
                proxy_url = f"http://127.0.0.1:{relay.port}"
            else:
                host_part = server.replace("http://", "").replace("https://", "")
                if user and pwd:
                    proxy_url = f"http://{user}:{pwd}@{host_part}"
                    use_cdp_auth = True
                else:
                    proxy_url = server

        ua = ""
        if browser_args:
            for a in browser_args:
                if a.startswith("--user-agent="):
                    ua = a.replace("--user-agent=", "")

        # Fast-poll the CDP endpoint instead of sleeping 25s before the first
        # connection test (that fixed delay was the "stuck for 25s" every
        # launch — mail AND Discord). Boot is normally 1-3s; up to 30s of
        # 0.5s polls covers slow cold starts without costing anything when
        # the browser comes up fast.
        cfg = td.Config(
            browser_executable_path=exe,
            headless=headless,
            sandbox=False,
            browser_args=browser_args,
            browser_connection_timeout=0.5,
            browser_connection_max_tries=60,
        )
        if proxy_url:
            # Dict form — creds are NEVER inline here. With the relay the
            # URL is a bare 127.0.0.1; without one truedriver strips the
            # creds anyway and CDP Fetch auth handles the 407.
            cfg.proxy = {"server": proxy_url}
        if ua:
            cfg.user_agent = ua

        instance = await td.start(cfg)
        # Legacy path (no relay — e.g. non-http proxies with creds): store
        # proxy auth so _get_or_create_tab() can wire CDP Fetch auth on every
        # raw create_target tab. Must be a DICT with "username"/"password"
        # keys — Browser.setup_proxy_auth() reads self._proxy_auth["username"].
        if use_cdp_auth and proxy and proxy.get("username"):
            instance._proxy_auth = {
                "username": proxy.get("username"),
                "password": proxy.get("password", ""),
            }
        return _Browser(instance, relay=relay)

    async def launch_persistent_context(self, user_data_dir: str = None, **kwargs) -> _Browser:
        return await self.launch(**kwargs)


class _Playwright:
    def __init__(self):
        self.chromium = _BrowserType()
        self.firefox = _BrowserType()
        self.webkit = _BrowserType()

    async def start(self) -> "_Playwright":
        return self

    async def stop(self):
        pass


def async_playwright() -> _Playwright:
    return _Playwright()


__all__ = ["async_playwright", "ENGINE"]
