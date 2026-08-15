"""
truedriver_engine.py — Playwright-compatible async API backed by TrueDriver
(https://pypi.org/project/truedriver — a blazing-fast, async-first,
undetectable CDP automation framework, a fork of nodriver) driving
UNGOOGLED CHROMIUM in incognito mode ALWAYS.

This module preserves the bot's ``from browser_engine import async_playwright``
contract so every caller (server.py workers, captcha_solver.py, the HSW
solver) is unchanged:

    pw  = await async_playwright().start()
    b   = await pw.chromium.launch(headless=..., args=..., proxy={...}, **fp)
    ctx = await b.new_context(**opts)
    page = await ctx.new_page()

Why TrueDriver kills the old white-screen bug for good:

  · TrueDriver is PURE CDP. There is no WebDriver session that can go stale,
    no chromedriver re-attach, no "reattached JS context returns None for
    every evaluate()" failure mode, no tab that reads about:blank to the
    driver while the real page is committed. Every read runs over the same
    websocket that drove the navigation — the page either answers or it is
    genuinely dead.
  · Navigation uses Page.navigate + a tight document.readyState poll, so the
    bot returns the INSTANT Discord's DOM is interactive instead of waiting
    for hCaptcha subresources or the full load event.
  · Frame-scoped work (hCaptcha iframes) runs in the frame's OWN execution
    context (Runtime.getExecutionContexts), so reading question text and
    clicking inside cross-origin challenge frames just works — no DOM-dump
    fallbacks needed.
  · The browser is resolved in this order: $UNGOOGLED_CHROMIUM_BINARY /
    $CHROMIUM_BINARY / $BRAVE_BINARY, then ungoogled-chromium,
    chromium, chromium-browser, brave-browser, google-chrome on PATH, then
    TrueDriver's own managed Chrome. Incognito is ALWAYS on (--incognito +
    ephemeral profile dir deleted on exit): no cookies/cache/IndexedDB ever
    touch disk and every session is a clean disk-less identity.
"""

import asyncio
import base64
import inspect
import json
import os
import re
import shutil
import time

import truedriver
from truedriver import cdp

ENGINE = "truedriver"

# ═════════════════════════════════════════════════════════════════════
# Browser binary resolution (ungoogled chromium first)
# ═════════════════════════════════════════════════════════════════════

_BROWSER_CANDIDATES = (
    "ungoogled-chromium", "chromium", "chromium-browser",
    "brave-browser", "google-chrome", "google-chrome-stable",
    "chrome",
)


def _resolve_browser_binary() -> str:
    for env in ("UNGOOGLED_CHROMIUM_BINARY", "CHROMIUM_BINARY", "BRAVE_BINARY"):
        p = (os.environ.get(env) or "").strip()
        if p and os.path.exists(p):
            return p
    for cand in _BROWSER_CANDIDATES:
        p = shutil.which(cand)
        if p:
            return p
    return ""


# ═════════════════════════════════════════════════════════════════════
# JS helpers (Playwright evaluate() semantics)
# ═════════════════════════════════════════════════════════════════════

_FUNC_JS_RE = re.compile(
    r"^\s*(?:async\s+)?(?:\("
    r"|function\b"
    r"|[A-Za-z_$][\w$]*\s*=>)")

_MISSING = object()


def _wrap_eval_js(js: str, arg=_MISSING) -> str:
    """Wrap function-style JS like Playwright (call it), leave plain
    expressions untouched."""
    js = js.strip()
    if _FUNC_JS_RE.match(js):
        if arg is _MISSING:
            return f"({js})()"
        return f"({js})({json.dumps(arg)})"
    return js


def _is_async_fn(js: str) -> bool:
    return js.lstrip().startswith("async")


def _to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# Pick elements by role / text / label / CSS and run an action on the
# index-th match. Runs inside ANY document (top frame or iframe world) —
# rects are document-relative; the caller adds the frame offset before
# feeding coordinates to the mouse. Returns JSON.
_ACTION_JS = r"""/* injected per call */
(function (kind, a, b, exact, idx, action, payload) {
  function norm(s) { return (s == null ? "" : String(s)).replace(/\s+/g, " ").trim(); }
  var els;
  if (kind === "css") {
    els = Array.prototype.slice.call(document.querySelectorAll(a));
  } else if (kind === "role") {
    var tag = { textbox: "input[type='text'], input:not([type]), textarea, [contenteditable='true']",
                button: "button, input[type='button'], input[type='submit'], [role='button']",
                checkbox: "input[type='checkbox']", radio: "input[type='radio']",
                link: "a[href]", combobox: "select, [role='combobox']" }[a] || "";
    els = Array.prototype.slice.call(document.querySelectorAll("[role='" + a + "']" + (tag ? ", " + tag : "")));
    if (b) {
      var n = norm(b);
      els = els.filter(function (el) {
        var acc = norm(el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("placeholder") || "");
        var t = "";
        try { t = norm(el.innerText || el.value || ""); } catch (e) {}
        if (exact) { return acc === n || t === n; }
        return acc.indexOf(n) !== -1 || t.indexOf(n) !== -1;
      });
    }
  } else if (kind === "text") {
    var n = norm(a);
    var all = document.querySelectorAll("body *");
    els = Array.prototype.filter.call(all, function (el) {
      var t = "";
      try { t = norm(el.innerText || ""); } catch (e) {}
      if (!t) return false;
      var child = "";
      try { child = norm(Array.prototype.map.call(el.children, function (c) { return c.innerText || ""; }).join(" ")); } catch (e) {}
      if (child === t) return false;
      return exact ? t === n : t.indexOf(n) !== -1;
    });
  } else {
    var n = norm(a);
    var all2 = document.querySelectorAll("input, textarea, select, [aria-label], [title]");
    els = Array.prototype.filter.call(all2, function (el) {
      var acc = norm(el.getAttribute("aria-label") || el.getAttribute("title") || "");
      return exact ? acc === n : acc.indexOf(n) !== -1;
    });
  }
  if (!els.length) return JSON.stringify({ ok: false });
  var el = els[idx];
  if (!el) return JSON.stringify({ ok: false });
  var r = el.getBoundingClientRect();
  var base = {
    ok: true, x: r.x, y: r.y, w: r.width, h: r.height,
    text: norm(el.innerText || ""),
    value: (el.value != null ? String(el.value) : ""),
    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
    disabled: !!(el.disabled || el.getAttribute("disabled") != null || el.getAttribute("aria-disabled") === "true"),
    tag: el.tagName,
    src: el.getAttribute("src") || "",
    href: el.getAttribute("href") || "",
  };
  if (action === "list") {
    var out = els.map(function (el) {
      var r2 = el.getBoundingClientRect();
      return {
        ok: true, x: r2.x, y: r2.y, w: r2.width, h: r2.height,
        text: norm(el.innerText || ""),
        value: (el.value != null ? String(el.value) : ""),
        visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
        disabled: !!(el.disabled || el.getAttribute("disabled") != null || el.getAttribute("aria-disabled") === "true"),
        tag: el.tagName,
        src: el.getAttribute("src") || "",
        href: el.getAttribute("href") || "",
      };
    });
    return JSON.stringify(out);
  }
  if (!action) return JSON.stringify(base);
  if (action === "click") { el.click(); return JSON.stringify({ ok: true }); }
  if (action === "focus") { el.focus(); return JSON.stringify({ ok: true }); }
  if (action === "set") {
    el.focus();
    el.value = payload;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return JSON.stringify({ ok: true });
  }
  if (action === "attr") {
    return JSON.stringify({ ok: true, value: el.getAttribute(payload) || "" });
  }
  if (action === "dispatch") {
    el.dispatchEvent(new Event(payload, { bubbles: true }));
    return JSON.stringify({ ok: true });
  }
  return JSON.stringify(base);
})
"""


def _action_js(kind: str, a, b=None, exact=False, idx=0,
               action=None, payload=None) -> str:
    return f"({_ACTION_JS})({_to_json(kind)}, {_to_json(a)}, " \
           f"{_to_json(b or '')}, {_to_json(bool(exact))}, {int(idx)}, " \
           f"{_to_json(action or '')}, {_to_json(payload or '')})"


# ── HTTP helpers used for re-issuing navigations / blocking patterns ──
def _glob_to_regex(pattern: str):
    if pattern in ("**/*", "*", "**"):
        return re.compile(r".*")
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out))


# ═════════════════════════════════════════════════════════════════════
# Playwright-compatible surface
# ═════════════════════════════════════════════════════════════════════

def async_playwright():
    """Plain function (not a coroutine): callers do
    ``pw = await async_playwright().start()``."""
    return _Playwright()


class _Playwright:
    def __init__(self):
        self.chromium = _Chromium()

    async def start(self):
        return self

    async def stop(self):
        pass


_TRUEDRIVER_BLOCKED_ARGS = (
    "headless", "data-dir", "data_dir",
    "no-sandbox", "no_sandbox", "lang",
)


class _Chromium:
    async def launch(self, headless=False, args=None, proxy=None, **kwargs):
        # TrueDriver owns headless / sandbox / lang / user-data-dir itself
        # (passed as params) and REJECTS them as raw browser args.
        browser_args = [a for a in (args or [])
                        if not any(s in a.lower() for s in _TRUEDRIVER_BLOCKED_ARGS)]
        if "--incognito" not in browser_args:
            browser_args.append("--incognito")
        # Never allow TrueDriver's own sandbox handling to fight the
        # container: we run as root in Docker, --no-sandbox is required.
        ua = kwargs.get("user_agent") or ""
        proxy_cfg = _proxy_to_truedriver(proxy)
        binary = _resolve_browser_binary()
        td = await truedriver.start(
            headless=bool(headless),
            browser_executable_path=binary or None,
            browser_args=browser_args,
            user_agent=ua or None,
            proxy=proxy_cfg,
            sandbox=False,
        )
        if proxy_cfg and isinstance(proxy_cfg, dict) and proxy_cfg.get("username"):
            try:
                await td.setup_proxy_auth()
            except Exception:
                pass
        return _Browser(td, kwargs)


def _proxy_to_truedriver(proxy):
    """Playwright {server, username, password} → truedriver proxy config."""
    if not proxy:
        return None
    if isinstance(proxy, str):
        return proxy
    server = (proxy.get("server") or "").strip()
    if not server:
        return None
    if proxy.get("username"):
        return {
            "server": server,
            "username": proxy.get("username"),
            "password": proxy.get("password", ""),
        }
    return {"server": server}


class _Browser:
    def __init__(self, td, launch_kwargs):
        self._td = td
        self._launch_kwargs = launch_kwargs
        self._contexts = []

    @property
    def is_connected(self):
        try:
            return not self._td.stopped
        except Exception:
            return False

    async def new_context(self, **opts):
        ctx = _Context(self, opts)
        self._contexts.append(ctx)
        return ctx

    async def close(self):
        try:
            await self._td.stop()
        except Exception:
            pass

    # SeleniumBase compat shim used by is_alive() in server.py — keep a
    # no-op so the call site never crashes.
    async def _ensure_connected(self):
        return True


class _Context:
    def __init__(self, browser: _Browser, opts: dict):
        self._browser = browser
        self._opts = opts or {}
        self._init_scripts = []
        self._page = None

    async def add_init_script(self, script: str):
        self._init_scripts.append(script)

    async def new_page(self):
        tab = await self._new_tab()
        page = _Page(self, tab)
        self._page = page
        # Runtime domain on: receive ExecutionContextCreated events so we can
        # evaluate inside cross-origin iframes (truedriver 0.1.5 has no
        # Runtime.getExecutionContexts command).
        try:
            await page._tab.send(cdp.runtime.enable())
        except Exception:
            pass
        await page._apply_context_options(self._opts)
        for s in self._init_scripts:
            await page._add_init_script(s)
        await page._refresh_frames()
        return page

    async def _new_tab(self):
        """Grab a usable tab for this context WITHOUT truedriver's
        get(new_tab=True) path.

        truedriver's get(new_tab=True) sends Target.createTarget with
        enable_begin_frame_control=True, which newer Chromium builds reject
        with "Failed to open new tab - no browser is open" (code -32000) —
        the exact white-screen / dead-tab failure this engine replaced. The
        minimal createTarget works fine, so: reuse the browser's main tab
        when it exists (always true right after launch), otherwise create a
        fresh target with the minimal call and wait for truedriver to
        register it."""
        try:
            tabs = self._browser._td.tabs
            for t in tabs:
                if getattr(t, "type_", "page") == "page":
                    return t
        except Exception:
            pass
        try:
            tid = await self._browser._td.connection.send(
                cdp.target.create_target("about:blank"))
        except Exception:
            # Last resort: fall back to truedriver's own path (may fail on
            # some builds, but never worse than crashing the caller).
            tab = await self._browser._td.get("about:blank", new_tab=True)
            return tab
        for _ in range(60):
            await asyncio.sleep(0.05)
            try:
                for t in self._browser._td.tabs:
                    if getattr(t, "target_id", None) == tid:
                        return t
            except Exception:
                pass
        raise RuntimeError("new tab did not appear after create_target")

    async def new_cdp_session(self, page):
        return _CdpSession(page)

    async def close(self):
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None


class _CdpSession:
    """Minimal CDP session adapter for stealth.apply_cdp_stealth: sends
    named CDP commands (e.g. Page.addScriptToEvaluateOnNewDocument)."""

    def __init__(self, page):
        self._page = page

    async def send(self, method: str, params: dict = None):
        params = params or {}
        return await self._page._send_named(method, params)


class _Response:
    ok = True
    status = 200


class _Page:
    def __init__(self, context: _Context, tab):
        self._context = context
        self._browser = context._browser
        self._tab = tab
        self._routes = []          # (regex, handler)
        self._fetch_enabled = False
        self._frames_cache = []
        self._last_url = ""
        self._last_title = ""
        self._ctx_map = {}         # frame_id -> main-world execution context id
        self.keyboard = _Keyboard(self)
        self.mouse = _Mouse(self)
        self.driver = None         # SeleniumBase compat (always None here)
        try:
            self._tab.add_handler(cdp.page.FrameNavigated, self._on_frame_event)
            self._tab.add_handler(cdp.page.FrameDetached, self._on_frame_event)
            self._tab.add_handler(cdp.page.FrameAttached, self._on_frame_event)
            self._tab.add_handler(cdp.runtime.ExecutionContextCreated,
                                  self._on_ctx_created)
            self._tab.add_handler(cdp.runtime.ExecutionContextDestroyed,
                                  self._on_ctx_destroyed)
        except Exception:
            pass

    # ── events ─────────────────────────────────────────────────────────
    def _on_frame_event(self, event):
        try:
            asyncio.get_event_loop().create_task(self._refresh_frames())
        except Exception:
            pass

    def _on_ctx_created(self, event):
        try:
            ctx = getattr(event, "context", None)
            if ctx is None:
                return
            # Chrome reports the frame id in aux_data.frameId (context.frame_id
            # is None on recent protocol versions).
            fid = getattr(ctx, "frame_id", None)
            if fid is None:
                aux = getattr(ctx, "aux_data", None) or {}
                fid = aux.get("frameId") if isinstance(aux, dict) else None
            if fid is not None:
                self._ctx_map[fid] = ctx.id_
        except Exception:
            pass

    def _on_ctx_destroyed(self, event):
        try:
            cid = getattr(event, "execution_context_id", None)
            if cid is not None:
                for k in [k for k, v in self._ctx_map.items() if v == cid]:
                    del self._ctx_map[k]
        except Exception:
            pass

    async def _refresh_frames(self):
        try:
            self._frames_cache = await self._tab.get_frames()
            for f in self._frames_cache:
                if f.parent_id is None and f.url:
                    self._last_url = f.url or self._last_url
        except Exception:
            pass

    # ── low-level CDP ──────────────────────────────────────────────────
    async def _send_named(self, method: str, params: dict):
        """Map 'Page.addScriptToEvaluateOnNewDocument' style names to the
        truedriver cdp module and run them on the tab."""
        domain, _, fn_name = method.partition(".")
        fn_name = re.sub(r"(?<!^)(?=[A-Z])", "_", fn_name).lower()
        fn = getattr(getattr(cdp, domain, None), fn_name, None)
        if fn is None:
            raise AttributeError(f"cdp.{domain}.{fn_name} not found")
        return await self._tab.send(fn(**params))

    async def evaluate(self, expression: str, arg=None):
        return await self._evaluate(expression, arg)

    async def _raw_evaluate(self, js: str):
        """Evaluate a ready-to-run JS expression (no Playwright-style
        function wrapping) — used for the engine's own injected IIFEs."""
        remote, errors = await self._tab.send(cdp.runtime.evaluate(
            expression=js,
            context_id=None,
            await_promise=False,
            return_by_value=True,
            user_gesture=True,
            allow_unsafe_eval_blocked_by_csp=True,
        ))
        if errors:
            raise RuntimeError(f"raw evaluate error: {errors.text or errors}")
        if remote is not None:
            return getattr(remote, "value", None)
        return None

    async def _frame_raw_eval(self, frame, js: str):
        ctx = await self._frame_context(frame)
        if ctx is None:
            raise RuntimeError(f"no execution context for frame {frame.id_}")
        remote, errors = await self._tab.send(cdp.runtime.evaluate(
            expression=js,
            context_id=ctx,
            await_promise=False,
            return_by_value=True,
            user_gesture=True,
            allow_unsafe_eval_blocked_by_csp=True,
        ))
        if errors:
            raise RuntimeError(f"frame raw evaluate error: {errors.text or errors}")
        if remote is not None:
            return getattr(remote, "value", None)
        return None

    async def _evaluate(self, js: str, arg=None):
        # await_promise must be decided on the ORIGINAL js: _wrap_eval_js
        # wraps function-style js in parens, so an ``async () => ...``
        # becomes ``(async () => ...)()`` and no longer starts with "async"
        # (which silently made every async evaluate return its Promise
        # instead of the awaited value).
        await_promise = _is_async_fn(js)
        js = _wrap_eval_js(js, arg)
        remote, errors = await self._tab.send(cdp.runtime.evaluate(
            expression=js,
            context_id=None,
            await_promise=await_promise,
            return_by_value=True,
            user_gesture=True,
            allow_unsafe_eval_blocked_by_csp=True,
        ))
        if errors:
            raise RuntimeError(f"evaluate error: {errors.text or errors}")
        if remote is not None:
            return getattr(remote, "value", None)
        return None

    async def _frame_eval(self, frame, js: str, arg=None):
        ctx = await self._frame_context(frame)
        if ctx is None:
            raise RuntimeError(f"no execution context for frame {frame.id_}")
        await_promise = _is_async_fn(js)
        js = _wrap_eval_js(js, arg)
        remote, errors = await self._tab.send(cdp.runtime.evaluate(
            expression=js,
            context_id=ctx,
            await_promise=await_promise,
            return_by_value=True,
            user_gesture=True,
            allow_unsafe_eval_blocked_by_csp=True,
        ))
        if errors:
            raise RuntimeError(f"frame evaluate error: {errors.text or errors}")
        if remote is not None:
            return getattr(remote, "value", None)
        return None

    async def _frame_context(self, frame):
        """Execution context of a frame's MAIN world (tracked from
        Runtime.executionContextCreated events — truedriver 0.1.5's CDP
        spec has no Runtime.getExecutionContexts). Falls back to an
        isolated world (full DOM access) if the event hasn't arrived yet."""
        cid = self._ctx_map.get(frame.id_)
        if cid is not None:
            return cid
        for _ in range(6):
            await asyncio.sleep(0.15)
            cid = self._ctx_map.get(frame.id_)
            if cid is not None:
                return cid
        try:
            res = await self._tab.send(cdp.page.create_isolated_world(
                frame_id=frame.id_,
                world_name=f"pw-{frame.id_}",
            ))
            # truedriver returns the ExecutionContextId namedtuple directly
            return getattr(res, "id_", None) or res
        except Exception:
            return None

    async def _parent_frame(self, frame):
        try:
            for f in await self._tab.get_frames():
                if f.id_ == frame.parent_id:
                    return f
        except Exception:
            pass
        return None

    async def _frame_offset(self, frame):
        """Top-viewport offset of a frame (sum of hosting iframe rects up
        the frame tree)."""
        off = {"x": 0.0, "y": 0.0}
        cur = frame
        for _ in range(6):
            if cur is None or cur.parent_id is None:
                break
            parent = await self._parent_frame(cur)
            if parent is None:
                break
            try:
                rect = await self._frame_eval(parent, _FRAME_RECT_JS, (cur.url or ""))
                if isinstance(rect, str):
                    rect = json.loads(rect)
            except Exception:
                rect = None
            if rect and rect.get("ok"):
                off["x"] += rect["x"]
                off["y"] += rect["y"]
            cur = parent
        return off

    # ── context emulation ──────────────────────────────────────────────
    async def _apply_context_options(self, opts):
        vp = opts.get("viewport") or {}
        width = int(vp.get("width") or 1920)
        height = int(vp.get("height") or 1080)
        dsf = float(opts.get("device_scale_factor") or 1.0)
        try:
            await self._tab.send(cdp.emulation.set_device_metrics_override(
                width=width, height=height, device_scale_factor=dsf, mobile=False))
        except Exception:
            pass
        ua = opts.get("user_agent") or ""
        if ua:
            try:
                await self._tab.send(cdp.network.set_user_agent_override(
                    user_agent=ua,
                    accept_language=opts.get("locale") or "en-US,en;q=0.9",
                ))
            except Exception:
                pass
        tz = opts.get("timezone_id")
        if tz:
            try:
                await self._tab.send(cdp.emulation.set_timezone_override(timezone_id=tz))
            except Exception:
                pass
        locale = opts.get("locale")
        if locale:
            try:
                await self._tab.send(cdp.emulation.set_locale_override(locale=locale))
            except Exception:
                pass
        headers = opts.get("extra_http_headers")
        if headers:
            try:
                await self._tab.send(cdp.network.set_extra_http_headers(
                    headers=[cdp.fetch.HeaderEntry(name=k, value=str(v))
                             for k, v in headers.items()]))
            except Exception:
                pass

    async def _add_init_script(self, script: str):
        try:
            await self._tab.send(cdp.page.add_script_to_evaluate_on_new_document(source=script))
        except Exception:
            pass

    # ── navigation ─────────────────────────────────────────────────────
    async def goto(self, url: str, wait_until="domcontentloaded", timeout=30000):
        timeout_s = max(1.0, float(timeout) / 1000.0)
        try:
            await self._tab.send(cdp.page.navigate(url))
        except Exception:
            pass
        self._last_url = url
        if wait_until == "commit":
            return _Response()
        target = "interactive" if wait_until == "domcontentloaded" else "complete"
        deadline = time.time() + timeout_s
        last = ""
        while time.time() < deadline:
            try:
                rs = await self._evaluate("document.readyState")
                if rs:
                    last = str(rs)
                    if last == "complete" or (target == "interactive" and last in ("interactive", "complete")):
                        break
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await self._refresh_frames()
        return _Response()

    async def reload(self, timeout=30000, wait_until="domcontentloaded"):
        try:
            await self._tab.send(cdp.page.reload())
        except Exception:
            pass
        target = "interactive" if wait_until == "domcontentloaded" else "complete"
        deadline = time.time() + float(timeout) / 1000.0
        while time.time() < deadline:
            try:
                rs = await self._evaluate("document.readyState")
                if rs in ("complete",) or (target == "interactive" and rs in ("interactive", "complete")):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await self._refresh_frames()
        return _Response()

    async def wait_for_timeout(self, ms: float):
        await asyncio.sleep(max(0.0, ms) / 1000.0)

    async def title(self) -> str:
        try:
            t = await self._evaluate("document.title")
            return str(t or "")
        except Exception:
            return ""

    @property
    def url(self) -> str:
        return self._last_url or ""

    # ── locators ───────────────────────────────────────────────────────
    def locator(self, selector: str):
        return _Locator(self, "css", selector)

    async def click(self, selector: str, timeout=5000):
        await self.locator(selector).first.click(timeout=timeout)

    def get_by_role(self, role: str, name=None, exact=False):
        return _Locator(self, "role", role, name, exact=exact)

    def get_by_text(self, text, exact=False):
        return _Locator(self, "text", text, exact=exact)

    def get_by_label(self, label, exact=False):
        return _Locator(self, "label", label, exact=exact)

    def frame_locator(self, selector: str):
        return _FrameLocator(self, selector)

    @property
    def frames(self):
        return [_Frame(self, f) for f in self._frames_cache]

    async def wait_for_selector(self, selector: str, state="visible", timeout=5000):
        loc = self.locator(selector)
        try:
            await loc.wait_for(state=state, timeout=timeout)
            return await loc.first.element_handle(timeout=2000)
        except Exception:
            return None

    # ── request interception (route) ───────────────────────────────────
    async def route(self, pattern: str, handler):
        self._routes.append((_glob_to_regex(pattern), handler))
        if not self._fetch_enabled:
            self._fetch_enabled = True
            try:
                await self._tab.send(cdp.fetch.enable(patterns=[
                    cdp.fetch.RequestPattern(
                        url_pattern="*",
                        request_stage=cdp.fetch.RequestStage.REQUEST,
                    )
                ]))
                self._tab.add_handler(cdp.fetch.RequestPaused, self._on_request_paused)
            except Exception:
                pass

    async def unroute(self, pattern: str):
        rx = _glob_to_regex(pattern)
        self._routes = [(r, h) for r, h in self._routes if r.pattern != rx.pattern]

    async def _on_request_paused(self, event):
        url = (event.request.url or "") if event.request else ""
        request_id = event.request_id
        try:
            for rx, handler in self._routes:
                if rx.match(url or ""):
                    route = _Route(self, request_id)
                    res = handler(route)
                    if inspect.isawaitable(res):
                        await res
                    return
        except Exception:
            pass
        try:
            await self._tab.send(cdp.fetch.continue_request(request_id=request_id))
        except Exception:
            pass

    # ── screenshots ────────────────────────────────────────────────────
    async def screenshot(self, full_page=True, clip=None, type="png"):
        # truedriver's capture_screenshot takes ``format_`` (not ``format``)
        # and returns a base64 string (not a response object with .data).
        kwargs = {"format_": "png"}
        if clip:
            kwargs["clip"] = cdp.page.Viewport(
                x=float(clip.get("x", 0)), y=float(clip.get("y", 0)),
                width=float(clip.get("width", 100)),
                height=float(clip.get("height", 100)),
                scale=1.0)
            kwargs["capture_beyond_viewport"] = False
        elif full_page:
            kwargs["capture_beyond_viewport"] = True
        try:
            res = await self._tab.send(cdp.page.capture_screenshot(**kwargs))
            if isinstance(res, str) and res:
                return base64.b64decode(res)
        except Exception:
            pass
        # Retry a plain viewport capture (full-page can fail on zoom)
        try:
            res = await self._tab.send(cdp.page.capture_screenshot(format_="png"))
            if isinstance(res, str) and res:
                return base64.b64decode(res)
        except Exception:
            pass
        return b""

    async def close(self):
        try:
            await self._tab.close()
        except Exception:
            pass

    # SeleniumBase UC-compat stubs — turnstile bypasses map to CDP clicks.
    async def uc_click(self, selector, timeout=4000):
        loc = self.locator(selector)
        await loc.first.click(timeout=timeout)

    async def uc_gui_click_captcha(self, frame="iframe", retry=True):
        """OS-level-style click on the Turnstile/hCaptcha widget frame center
        (CDP trusted mouse — no PyAutoGUI needed)."""
        for sel in ("iframe[src*=\"challenges.cloudflare.com\"]",
                    "iframe[src*=\"turnstile\"]",
                    "div.cf-turnstile iframe"):
            loc = self.locator(sel)
            try:
                if await loc.count() > 0:
                    box = await loc.first.bounding_box()
                    if box:
                        await self.mouse.click(box["x"] + box["width"] / 2,
                                               box["y"] + box["height"] / 2)
                        return
            except Exception:
                continue


class _Route:
    def __init__(self, page, request_id):
        self._page = page
        self._request_id = request_id

    async def fulfill(self, status=200, content_type="text/html", body="",
                      headers=None):
        hs = [cdp.fetch.HeaderEntry(name="Content-Type", value=content_type)]
        for k, v in (headers or {}).items():
            hs.append(cdp.fetch.HeaderEntry(name=k, value=str(v)))
        try:
            await self._page._tab.send(cdp.fetch.fulfill_request(
                request_id=self._request_id,
                response_code=int(status),
                response_headers=hs,
                body=base64.b64encode(body.encode("utf-8", "replace")).decode(),
            ))
        except Exception:
            pass

    async def abort(self):
        try:
            await self._page._tab.send(cdp.fetch.fail_request(
                request_id=self._request_id,
                error_reason=cdp.network.ErrorReason.BLOCKED_BY_CLIENT,
            ))
        except Exception:
            pass

    async def continue_(self):
        try:
            await self._page._tab.send(cdp.fetch.continue_request(
                request_id=self._request_id))
        except Exception:
            pass


# JS: rect of the iframe hosting a given frame URL (run in parent frame)
_FRAME_RECT_JS = """(function (url) {
  var best = null, bestScore = -1;
  var frames = document.querySelectorAll('iframe');
  for (var i = 0; i < frames.length; i++) {
    var f = frames[i];
    var src = f.getAttribute('src') || '';
    var score = -1;
    if (url && src) {
      if (src === url) score = 3;
      else if (src.indexOf(url) !== -1 || url.indexOf(src) !== -1) score = 2;
    }
    if (score > bestScore) { bestScore = score; best = f; }
  }
  if (!best) {
    for (var j = 0; j < frames.length; j++) {
      var b2 = frames[j].getBoundingClientRect();
      if (b2.width > 50 && b2.height > 50) { best = frames[j]; break; }
    }
  }
  if (!best) return JSON.stringify({ ok: false });
  var r = best.getBoundingClientRect();
  return JSON.stringify({ ok: true, x: r.x, y: r.y, w: r.width, h: r.height });
})
"""


class _Frame:
    def __init__(self, page: _Page, cdp_frame):
        self._page = page
        self._frame = cdp_frame

    @property
    def url(self) -> str:
        try:
            return self._frame.url or ""
        except Exception:
            return ""

    @property
    def _id(self):
        return self._frame.id_

    async def evaluate(self, js: str, arg=None):
        return await self._page._frame_eval(self._frame, js, arg)

    def locator(self, selector: str):
        return _Locator(self._page, "css", selector, frame=self)

    async def wait_for_selector(self, selector: str, state="visible", timeout=5000):
        loc = self.locator(selector)
        await loc.wait_for(state=state, timeout=timeout)
        return None

    async def _offset(self):
        return await self._page._frame_offset(self._frame)

    def __repr__(self):
        return f"<Frame {self.url[:60]}>"


class _FrameLocator:
    def __init__(self, page: _Page, selector: str):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        """Playwright parity: frame_locator().first → same frame locator."""
        return self

    async def _resolve_frame(self) -> _Frame:
        """Find the child frame hosted by the iframe matching selector."""
        # find iframe elements via CSS and read their src
        try:
            raw = await self._page._evaluate(_CSS_SRC_JS, self._selector)
            iframes = json.loads(raw) if raw else []
        except Exception:
            iframes = []
        frames = await self._page._tab.get_frames()
        for target in iframes:
            src = target.get("src") or ""
            for f in frames:
                if f.parent_id is None:
                    continue
                furl = f.url or ""
                if src and (furl == src or furl == src.split("?")[0]
                            or (src in furl) or (furl in src)):
                    return _Frame(self._page, f)
        # Fallback: any child frame whose URL looks like hCaptcha
        for f in frames:
            if f.parent_id is None:
                continue
            if "hcaptcha" in (f.url or "").lower():
                return _Frame(self._page, f)
        # Last resort: the largest child frame
        best = None
        for f in frames:
            if f.parent_id is not None:
                best = f
                break
        if best is not None:
            return _Frame(self._page, best)
        raise TimeoutError(f"frame_locator '{self._selector}' matched no frame")

    def locator(self, selector: str):
        return _Locator(self._page, "css", selector, frame_locator=self)

    def get_by_role(self, role: str, name=None, exact=False):
        return _Locator(self._page, "role", role, name, exact=exact,
                        frame_locator=self)

    def get_by_text(self, text, exact=False):
        return _Locator(self._page, "text", text, exact=exact,
                        frame_locator=self)

    def get_by_label(self, label, exact=False):
        return _Locator(self._page, "label", label, exact=exact,
                        frame_locator=self)

    async def screenshot(self, type="png", timeout=8000):
        try:
            loc = self.locator("body").first
            box = await loc.bounding_box()
            if box:
                return await self._page.screenshot(
                    full_page=False,
                    clip={"x": box["x"], "y": box["y"],
                          "width": box["width"], "height": box["height"]})
        except Exception:
            pass
        return await self._page.screenshot(full_page=False)


_CSS_SRC_JS = """(function (sel) {
  var out = [];
  var els = document.querySelectorAll(sel);
  for (var i = 0; i < els.length; i++) {
    var r = els[i].getBoundingClientRect();
    out.push({ src: els[i].getAttribute('src') || '',
               x: r.x, y: r.y, w: r.width, h: r.height,
               visible: !!(els[i].offsetWidth || els[i].offsetHeight) });
  }
  return JSON.stringify(out);
})
"""


class _Locator:
    def __init__(self, page: _Page, kind="css", a="", b=None, exact=False,
                 index=None, frame_locator=None, frame=None):
        self._page = page
        self._kind = kind
        self._a = a
        self._b = b
        self._exact = exact
        self._index = index
        self._frame_locator = frame_locator
        self._frame = frame

    @property
    def first(self):
        return _Locator(self._page, self._kind, self._a, self._b,
                        self._exact, 0, self._frame_locator, self._frame)

    def nth(self, i):
        return _Locator(self._page, self._kind, self._a, self._b,
                        self._exact, int(i), self._frame_locator, self._frame)

    # ── scope resolution ───────────────────────────────────────────────
    async def _scope_frame(self):
        if self._frame is not None:
            return self._frame
        if self._frame_locator is not None:
            return await self._frame_locator._resolve_frame()
        return None

    async def _scope_eval(self, js, arg=None):
        """Run a raw (already-invoked) IIFE in the locator's scope."""
        fr = await self._scope_frame()
        if fr is not None:
            return await self._page._frame_raw_eval(fr._frame, js)
        return await self._page._raw_evaluate(js)

    async def _scope_offset(self):
        fr = await self._scope_frame()
        if fr is not None:
            return await fr._offset()
        return {"x": 0.0, "y": 0.0}

    def _action_js(self, action=None, payload=None):
        return _action_js(self._kind, self._a, self._b, self._exact,
                          self._index or 0, action, payload)

    async def _query(self, timeout=5.0):
        deadline = time.time() + max(0.1, timeout)
        last = []
        while time.time() < deadline:
            try:
                raw = await self._scope_eval(self._action_js("list"))
                if raw:
                    arr = json.loads(raw)
                    if isinstance(arr, list):
                        last = arr
            except Exception:
                pass
            idx = self._index or 0
            if len(last) > idx:
                break
            await asyncio.sleep(0.08)
        return last

    async def _target(self, timeout=5.0):
        arr = await self._query(timeout)
        idx = self._index or 0
        if idx >= len(arr):
            raise TimeoutError(
                f"locator {self._kind}='{self._a}' (index {idx}) not found")
        return arr[idx]

    # ── public ops ─────────────────────────────────────────────────────
    async def count(self):
        try:
            arr = await self._query(timeout=0.3)
            return len(arr)
        except Exception:
            return 0

    async def is_visible(self):
        try:
            info = await self._target(timeout=1.0)
            return bool(info.get("visible"))
        except Exception:
            return False

    async def inner_text(self):
        info = await self._target()
        return info.get("text") or ""

    async def get_attribute(self, name: str):
        try:
            raw = await self._scope_eval(self._action_js("attr", name))
            d = json.loads(raw) if raw else {}
            return d.get("value") if d.get("ok") else None
        except Exception:
            return None

    async def input_value(self):
        info = await self._target()
        return info.get("value") or ""

    async def bounding_box(self):
        info = await self._target()
        off = await self._scope_offset()
        if not info.get("visible") and info.get("w", 0) <= 0:
            return None
        return {
            "x": float(info.get("x", 0)) + off["x"],
            "y": float(info.get("y", 0)) + off["y"],
            "width": float(info.get("w", 0)),
            "height": float(info.get("h", 0)),
        }

    async def focus(self, timeout=5000):
        try:
            await self._scope_eval(self._action_js("focus"), timeout=1)
        except Exception:
            pass
        await self._target(timeout)

    async def click(self, timeout=5000, force=False):
        info = await self._target(timeout)
        off = await self._scope_offset()
        cx = float(info.get("x", 0)) + float(info.get("w", 0)) / 2 + off["x"]
        cy = float(info.get("y", 0)) + float(info.get("h", 0)) / 2 + off["y"]
        if info.get("visible") or force:
            await self._page.mouse.click(cx, cy)
            return
        # Not visible → JS dispatch fallback (captcha buttons sometimes
        # report zero size while still accepting clicks).
        try:
            raw = await self._scope_eval(self._action_js("click"))
            d = json.loads(raw) if raw else {}
            if d.get("ok"):
                return
        except Exception:
            pass
        raise TimeoutError(f"click failed on {self._kind}='{self._a}'")

    async def fill(self, text, timeout=5000):
        info = await self._target(timeout)
        if info.get("visible") or True:
            try:
                await self._scope_eval(self._action_js("set", str(text)))
                return
            except Exception:
                pass
        # fallback: click + trusted keyboard
        await self.click(timeout=timeout)
        await self._page.keyboard.type(str(text), delay=20)

    async def type(self, text, delay=30, timeout=5000):
        try:
            await self._scope_eval(self._action_js("focus"))
        except Exception:
            pass
        await self._page.keyboard.type(str(text), delay=delay)

    async def press(self, key, timeout=5000):
        try:
            await self._scope_eval(self._action_js("focus"))
        except Exception:
            pass
        await self._page.keyboard.press(key)

    async def dispatch_event(self, event_type: str):
        await self._scope_eval(self._action_js("dispatch", event_type))

    async def evaluate(self, js: str, arg=None):
        return await self._scope_eval(js, arg)

    async def wait_for(self, state="visible", timeout=5000):
        deadline = time.time() + max(0.1, timeout)
        last = []
        while time.time() < deadline:
            try:
                arr = await self._query(timeout=0.15)
                last = arr
            except Exception:
                last = []
            idx = self._index or 0
            if state == "hidden":
                if idx >= len(last) or not last[idx].get("visible"):
                    return
            else:
                if idx < len(last):
                    if state == "attached":
                        return
                    if last[idx].get("visible"):
                        return
            await asyncio.sleep(0.08)
        raise TimeoutError(
            f"wait_for {state} timeout on {self._kind}='{self._a}'")

    async def element_handle(self, timeout=5000):
        info = await self._target(timeout)
        off = await self._scope_offset()
        return _ElementHandle(self._page, info, off)

    async def screenshot(self, type="png", timeout=8000):
        try:
            handle = await self.element_handle(timeout=timeout)
            return await handle.screenshot(type=type)
        except Exception:
            return b""

    async def content_frame(self):
        """Resolve the frame hosted by this (iframe) element."""
        if self._frame is not None:
            return self._frame
        try:
            info = await self._target(timeout=4.0)
        except Exception:
            return None
        src = info.get("src") or ""
        try:
            frames = await self._page._tab.get_frames()
        except Exception:
            return None
        for f in frames:
            if f.parent_id is None:
                continue
            furl = f.url or ""
            if src and (furl == src or (src in furl) or (furl in src)):
                return _Frame(self._page, f)
        for f in frames:
            if f.parent_id is not None and "hcaptcha" in (f.url or "").lower():
                return _Frame(self._page, f)
        return None


class _ElementHandle:
    def __init__(self, page: _Page, info: dict, offset: dict):
        self._page = page
        self._info = info
        self._offset = offset

    async def screenshot(self, type="png", timeout=4000):
        x = float(self._info.get("x", 0)) + self._offset["x"]
        y = float(self._info.get("y", 0)) + self._offset["y"]
        w = max(1.0, float(self._info.get("w", 0)))
        h = max(1.0, float(self._info.get("h", 0)))
        try:
            return await self._page.screenshot(
                full_page=False,
                clip={"x": x, "y": y, "width": w, "height": h})
        except Exception:
            return b""


class _Mouse:
    def __init__(self, page: _Page):
        self._page = page
        self._x = 0.0
        self._y = 0.0

    async def move(self, x, y, steps=10):
        x, y = float(x), float(y)
        sx, sy = self._x, self._y
        self._x, self._y = x, y
        if steps and steps > 1:
            dx = (x - sx) / steps
            dy = (y - sy) / steps
            for i in range(1, steps + 1):
                await self._page._tab.send(cdp.input_.dispatch_mouse_event(
                    "mouseMoved", x=sx + dx * i, y=sy + dy * i))
        else:
            await self._page._tab.send(cdp.input_.dispatch_mouse_event(
                "mouseMoved", x=x, y=y))

    async def down(self, x=None, y=None, button="left"):
        x = float(x) if x is not None else self._x
        y = float(y) if y is not None else self._y
        self._x, self._y = x, y
        await self._page._tab.send(cdp.input_.dispatch_mouse_event(
            "mousePressed", x=x, y=y, button=cdp.input_.MouseButton(button),
            buttons=1, click_count=1))

    async def up(self, x=None, y=None, button="left"):
        x = float(x) if x is not None else self._x
        y = float(y) if y is not None else self._y
        self._x, self._y = x, y
        await self._page._tab.send(cdp.input_.dispatch_mouse_event(
            "mouseReleased", x=x, y=y, button=cdp.input_.MouseButton(button),
            buttons=0, click_count=1))

    async def click(self, x, y, button="left"):
        x, y = float(x), float(y)
        self._x, self._y = x, y
        await self._page._tab.send(cdp.input_.dispatch_mouse_event(
            "mousePressed", x=x, y=y, button=cdp.input_.MouseButton(button),
            buttons=1, click_count=1))
        await asyncio.sleep(0.03)
        await self._page._tab.send(cdp.input_.dispatch_mouse_event(
            "mouseReleased", x=x, y=y, button=cdp.input_.MouseButton(button),
            buttons=0, click_count=1))

    async def dblclick(self, x, y, button="left"):
        await self.click(x, y, button)
        await self.click(x, y, button)


_KEY_MAP = {
    "Enter": (13, "Enter", "Enter"),
    "Backspace": (8, "Backspace", "Backspace"),
    "Tab": (9, "Tab", "Tab"),
    "Space": (32, " ", "Space"),
    "Escape": (27, "Escape", "Escape"),
    "Delete": (46, "Delete", "Delete"),
    "ArrowDown": (40, "ArrowDown", "ArrowDown"),
    "ArrowUp": (38, "ArrowUp", "ArrowUp"),
    "ArrowLeft": (37, "ArrowLeft", "ArrowLeft"),
    "ArrowRight": (39, "ArrowRight", "ArrowRight"),
    "Home": (36, "Home", "Home"),
    "End": (35, "End", "End"),
    "PageUp": (33, "PageUp", "PageUp"),
    "PageDown": (34, "PageDown", "PageDown"),
    "Control": (17, "Control", "ControlLeft"),
    "Shift": (16, "Shift", "ShiftLeft"),
    "Alt": (18, "Alt", "AltLeft"),
    "Meta": (91, "Meta", "MetaLeft"),
}
_MOD_BITS = {"Control": 2, "Shift": 8, "Alt": 1, "Meta": 4}


class _Keyboard:
    def __init__(self, page: _Page):
        self._page = page

    async def _key_event(self, ev_type, key, code, vk, text=None,
                         modifiers=0):
        kwargs = {
            "type_": ev_type,
            "key": key,
            "code": code,
            "windows_virtual_key_code": vk,
            "native_virtual_key_code": vk,
            "modifiers": modifiers,
        }
        if text is not None and ev_type == "keyDown":
            kwargs["text"] = text
        await self._page._tab.send(cdp.input_.dispatch_key_event(**kwargs))

    async def press(self, key: str):
        key = key.strip()
        modifiers = 0
        if "+" in key:
            parts = [p.strip() for p in key.split("+") if p.strip()]
            mod = parts[0]
            if mod in _MOD_BITS:
                modifiers = _MOD_BITS[mod]
                rest = parts[-1]
            else:
                rest = key
        else:
            rest = key
        if rest in _KEY_MAP:
            vk, k, code = _KEY_MAP[rest]
        else:
            vk = ord(rest[0].upper()) if len(rest) == 1 else 0
            k = rest
            code = rest
        if modifiers:
            mod_key = [m for m, b in _MOD_BITS.items() if b == modifiers]
            if mod_key:
                mvk, mk, mcode = _KEY_MAP[mod_key[0]]
                await self._key_event("keyDown", mk, mcode, mvk,
                                      modifiers=0)
        await self._key_event("keyDown", k, code, vk, modifiers=modifiers)
        await asyncio.sleep(0.02)
        await self._key_event("keyUp", k, code, vk, modifiers=modifiers)
        if modifiers:
            mod_key = [m for m, b in _MOD_BITS.items() if b == modifiers]
            if mod_key:
                mvk, mk, mcode = _KEY_MAP[mod_key[0]]
                await self._key_event("keyUp", mk, mcode, mvk, modifiers=0)

    async def type(self, text: str, delay: int = 0):
        for ch in text:
            if ch == "\n":
                await self.press("Enter")
            elif ch == "\t":
                await self.press("Tab")
            else:
                try:
                    await self._page._tab.send(cdp.input_.insert_text(text=ch))
                except Exception:
                    # non-insertable char → raw key event
                    vk = ord(ch) if ord(ch) < 128 else 0
                    await self._key_event("keyDown", ch, "", vk, text=ch)
                    await self._key_event("keyUp", ch, "", vk)
            if delay:
                await asyncio.sleep(delay / 1000.0)


CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
