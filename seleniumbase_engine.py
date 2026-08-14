"""
seleniumbase_engine.py — Playwright-compatible async API backed by SeleniumBase
(https://github.com/seleniumbase/SeleniumBase) driving the BRAVE browser
(unbranded Chromium) in CDP Mode (Chrome DevTools Protocol).

This module is a thin adapter that preserves the bot's
``from browser_engine import async_playwright`` contract:

    pw  = await async_playwright().start()
    b   = await pw.chromium.launch(headless=..., args=..., proxy={...}, **fp)
    ctx = await b.new_context(**opts)
    page = await ctx.new_page()

The engine itself is SeleniumBase. What this swap buys the bot:

  · CDP Mode is the stealth successor to UC Mode: the browser is launched
    with the UC (undetected-chromedriver) driver, then
    ``uc_activate_cdp_mode()`` (module-level, SeleniumBase >= 4.51)
    disconnects chromedriver entirely and re-drives the same browser over a
    raw CDP websocket — no WebDriver is attached while navigating or while
    clicking Cloudflare Turnstile / hCaptcha widgets, so there is nothing
    for anti-bot fingerprinting to detect during the sensitive moments.
    WebDriver is only reattached around DOM reads/writes that have no CDP
    equivalent (JS evaluation with args, screenshots, ActionChains drags).
  · The browser is Brave — a Chromium fork (unbranded Chromium), NOT Google
    Chrome — resolved via SeleniumBase's built-in ``browser="brave"``
    support or BRAVE_BINARY / binary_location.
  · Incognito is ALWAYS on: ``incognito=True`` + ``--incognito`` launch arg,
    so no cookies / cache / IndexedDB ever touch disk, every session is a
    clean disk-less identity, and closing the browser wipes the ephemeral
    user-data-dir.
  · Headless runs use a self-managed virtual display (``sbvirtualdisplay``
    — headed browser under Xvfb, needed for PyAutoGUI GUI clicks; falls back
    to ``headless2`` when the xvfb binary is absent). The display is started
    by this engine because SeleniumBase's standalone ``Driver()`` only
    auto-starts Xvfb from its pytest layer.

Because Selenium is synchronous, every blocking driver call runs inside
``asyncio.to_thread`` so the async callers (and their asyncio.wait_for caps)
keep working.

Set BRAVE_BINARY to point at a Brave binary explicitly if it is not at a
standard install path. If the chromedriver version doesn't match Brave's
Chromium major version, run ``sbase install chromedriver <major-version>``
(or set ``uc_driver_version`` in this file).
"""

import asyncio
import base64
import io
import json
import os
import random
import re
import shutil
import tempfile
import time
from urllib.parse import urljoin

from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

ENGINE = "seleniumbase"

# ─────────────────────────────────────────────────────────────────
# JS helpers (Playwright evaluate() semantics over Selenium)
# ─────────────────────────────────────────────────────────────────

# Playwright evaluate() wraps function-like strings and calls them; plain
# expressions evaluate directly. Selenium execute_script does neither
# automatically, so detect which form we got.
_FUNC_JS_RE = re.compile(r"^\s*(?:async\s+)?(?:\(|function\b|[\w$]+\s*=>)")


def _wrap_eval_js(js: str, has_arg: bool = False) -> str:
    js = js.strip()
    if _FUNC_JS_RE.match(js):
        body = f"({js})" if js.startswith("(") else f"({js})"
        return f"{body}(arguments[0])" if has_arg else f"{body}()"
    return js


# get_by_role / get_by_text / get_by_label matching, injected inside the
# page or frame scope. Returns JSON: [{x, y, w, h, text, value, visible,
# disabled, ok}]. Coordinates are relative to the CURRENT context viewport
# (frame-relative when run inside an iframe) — the wrapper adds the frame
# offset before feeding coordinates to the mouse.
_PICK_JS = r"""/* injected per call */
(function (kind, a, b, exact) {
  function norm(s) { return (s == null ? "" : String(s)).replace(/\s+/g, " ").trim(); }
  var els;
  if (kind === "role") {
    var tag = { textbox: "input[type='text'], input:not([type]), textarea, [contenteditable='true']",
                button: "button, input[type='button'], input[type='submit']",
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
  return JSON.stringify(els.map(function (el) {
    var r = el.getBoundingClientRect();
    var text = "";
    try { text = norm(el.innerText || el.value || ""); } catch (e) {}
    return { x: r.x, y: r.y, w: r.width, h: r.height, text: text,
             value: (el.value != null ? String(el.value) : ""),
             visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
             disabled: !!(el.disabled || el.getAttribute("disabled") != null || el.getAttribute("aria-disabled") === "true") };
  }));
})
"""


def _pick_js(kind: str, a, b=None, exact: bool = False) -> str:
    return f"({_PICK_JS})({json.dumps(kind)}, {json.dumps(a)}, {json.dumps(b or '')}, {json.dumps(bool(exact))})"


# Focus / center the N-th match for real-input actions (click / fill / type).
# Returns JSON: {ok, x, y} where x/y is the element center in the CURRENT
# context viewport (frame-relative inside an iframe).
_ACTION_JS = r"""/* injected per call */
(function (kind, a, b, exact, idx) {
  function norm(s) { return (s == null ? "" : String(s)).replace(/\s+/g, " ").trim(); }
  var els;
  if (kind === "role") {
    var tag = { textbox: "input[type='text'], input:not([type]), textarea, [contenteditable='true']",
                button: "button, input[type='button'], input[type='submit']",
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
  var el = els[idx] || els[0];
  if (!el) return JSON.stringify({ ok: false });
  el.scrollIntoView({ block: "center", inline: "center" });
  var r = el.getBoundingClientRect();
  try { el.focus(); } catch (e) {}
  return JSON.stringify({ ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2 });
})
"""


def _action_js(kind: str, a, b=None, exact: bool = False, idx: int = 0) -> str:
    return f"({_ACTION_JS})({json.dumps(kind)}, {json.dumps(a)}, {json.dumps(b or '')}, {json.dumps(bool(exact))}, {int(idx)})"


# ─────────────────────────────────────────────────────────────────
# Brave binary resolution
# ─────────────────────────────────────────────────────────────────

_BRAVE_PATHS = [
    "/usr/bin/brave-browser",
    "/usr/bin/brave",
    "/opt/brave.com/brave/brave-browser",
    "/opt/brave.com/brave/brave",
    "/snap/bin/brave",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
]


def _find_brave() -> str:
    """Locate a Brave (unbranded Chromium) binary. Empty string when absent."""
    env = os.environ.get("BRAVE_BINARY", "").strip()
    if env and os.path.exists(env):
        return env
    for p in _BRAVE_PATHS:
        if os.path.exists(p):
            return p
    for name in ("brave-browser", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return ""


# ─────────────────────────────────────────────────────────────────
# Proxy translation (Playwright dict → SeleniumBase string)
# ─────────────────────────────────────────────────────────────────

def _proxy_to_sb(proxy) -> str:
    if not proxy:
        return None
    if isinstance(proxy, str):
        return proxy or None
    server = str(proxy.get("server") or "")
    if not server:
        return None
    user = proxy.get("username") or ""
    pwd = proxy.get("password") or ""
    if not user:
        return server
    hostport = server
    for scheme in ("http://", "https://", "socks5h://", "socks5://", "socks4://"):
        if hostport.lower().startswith(scheme):
            hostport = hostport[len(scheme):]
            break
    if "@" in hostport:
        return hostport
    return f"{user}:{pwd}@{hostport}"


def _locale_from_accept_language(accept_language: str) -> str:
    first = (accept_language or "en-US").split(",")[0].strip()
    return first[:5] if first else "en-US"


# ─────────────────────────────────────────────────────────────────
# Frame scope management
# ─────────────────────────────────────────────────────────────────


def _enter_scope(driver, chain):
    driver.switch_to.default_content()
    for el in chain:
        driver.switch_to.frame(el)


def _exit_scope(driver):
    try:
        driver.switch_to.default_content()
    except Exception:
        pass


def _js_rect(driver, el):
    """Viewport-relative rect of `el` in the CURRENT frame context."""
    try:
        out = driver.execute_script(
            "var r = arguments[0].getBoundingClientRect();"
            "return [r.x, r.y, r.width, r.height];", el)
        if out and len(out) == 4:
            return {"x": float(out[0]), "y": float(out[1]),
                    "width": float(out[2]), "height": float(out[3])}
    except Exception:
        pass
    try:
        r = el.rect
        return {"x": float(r.get("x", 0)), "y": float(r.get("y", 0)),
                "width": float(r.get("width", 0)), "height": float(r.get("height", 0))}
    except Exception:
        return {"x": 0, "y": 0, "width": 0, "height": 0}


def _frame_offset(driver, chain):
    """Viewport offset (dx, dy) to add to frame-relative coordinates so they
    become top-level viewport coordinates. (0, 0) for page scope."""
    if not chain:
        return (0.0, 0.0)
    dx = dy = 0.0
    try:
        driver.switch_to.default_content()
        for i, el in enumerate(chain):
            r = _js_rect(driver, el)
            dx += r["x"]
            dy += r["y"]
            if i < len(chain) - 1:
                driver.switch_to.frame(el)
    except Exception:
        return (0.0, 0.0)
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    return (dx, dy)


# ─────────────────────────────────────────────────────────────────
# Mouse / keyboard (real input via ActionChains)
# ─────────────────────────────────────────────────────────────────

_KEYS_MAP = {
    "enter": Keys.ENTER, "return": Keys.ENTER, "backspace": Keys.BACKSPACE,
    "tab": Keys.TAB, "space": Keys.SPACE, "escape": Keys.ESCAPE,
    "esc": Keys.ESCAPE, "delete": Keys.DELETE, "del": Keys.DELETE,
    "arrowleft": Keys.ARROW_LEFT, "arrowright": Keys.ARROW_RIGHT,
    "arrowup": Keys.ARROW_UP, "arrowdown": Keys.ARROW_DOWN,
    "home": Keys.HOME, "end": Keys.END, "pageup": Keys.PAGE_UP,
    "pagedown": Keys.PAGE_DOWN, "insert": Keys.INSERT,
    "control": Keys.CONTROL, "shift": Keys.SHIFT, "alt": Keys.ALT,
    "meta": Keys.META, "command": Keys.COMMAND, "cmd": Keys.COMMAND,
    "win": Keys.META, "windows": Keys.META,
    "f1": Keys.F1, "f2": Keys.F2, "f3": Keys.F3, "f4": Keys.F4,
    "f5": Keys.F5, "f6": Keys.F6, "f7": Keys.F7, "f8": Keys.F8,
    "f9": Keys.F9, "f10": Keys.F10, "f11": Keys.F11, "f12": Keys.F12,
}


def _to_key(key: str):
    k = (key or "").strip()
    low = k.lower()
    if low in _KEYS_MAP:
        return _KEYS_MAP[low]
    if len(k) == 1:
        return k
    return k


class _Mouse:
    def __init__(self, browser):
        self._browser = browser
        self._x = 0.0
        self._y = 0.0

    @property
    def driver(self):
        return self._browser.driver

    async def _chain(self, actions):
        await asyncio.to_thread(actions.perform)

    async def move(self, x, y, steps: int = 1, **kwargs):
        x, y = float(x), float(y)
        steps = max(1, int(steps or 1))
        if steps > 1:
            for i in range(1, steps + 1):
                await self._move_to(self._x + (x - self._x) * i / steps,
                                    self._y + (y - self._y) * i / steps)
        else:
            await self._move_to(x, y)

    async def _move_to(self, x, y):
        actions = ActionChains(self.driver)
        actions.move_by_offset(x - self._x, y - self._y)
        await self._chain(actions)
        self._x, self._y = x, y

    async def click(self, x, y, **kwargs):
        x, y = float(x), float(y)
        actions = ActionChains(self.driver)
        actions.move_by_offset(x - self._x, y - self._y)
        actions.click()
        await self._chain(actions)
        self._x, self._y = x, y

    async def down(self, **kwargs):
        await self._chain(ActionChains(self.driver).click_and_hold())

    async def up(self, **kwargs):
        await self._chain(ActionChains(self.driver).release())

    async def wheel(self, delta_x: int = 0, delta_y: int = 0, **kwargs):
        try:
            actions = ActionChains(self.driver)
            actions.scroll_by_amount(int(delta_x), int(delta_y))
            await self._chain(actions)
        except Exception:
            pass


class _Keyboard:
    def __init__(self, browser):
        self._browser = browser

    @property
    def driver(self):
        return self._browser.driver

    async def _send(self, keys):
        await asyncio.to_thread(ActionChains(self.driver).send_keys(keys).perform)

    async def type(self, text: str, delay: int = 0, **kwargs):
        for ch in str(text):
            await self._send(ch)
            if delay:
                await asyncio.sleep(delay / 1000.0)

    async def press(self, key: str, **kwargs):
        parts = [p for p in (key or "").split("+") if p.strip()]
        if len(parts) > 1:
            mods = [_to_key(p) for p in parts[:-1]]
            last = _to_key(parts[-1])
            actions = ActionChains(self.driver)
            for m in mods:
                actions.key_down(m)
            actions.send_keys(last)
            for m in reversed(mods):
                actions.key_up(m)
            await asyncio.to_thread(actions.perform)
        else:
            await self._send(_to_key(key))

    async def down(self, key: str, **kwargs):
        await asyncio.to_thread(ActionChains(self.driver).key_down(_to_key(key)).perform)

    async def up(self, key: str, **kwargs):
        await asyncio.to_thread(ActionChains(self.driver).key_up(_to_key(key)).perform)


# ─────────────────────────────────────────────────────────────────
# CDP session (for apply_cdp_stealth / init scripts)
# ─────────────────────────────────────────────────────────────────


class _CdpSession:
    def __init__(self, driver):
        self._driver = driver

    async def send(self, method: str, params: dict = None):
        return await asyncio.to_thread(
            self._driver.execute_cdp_cmd, method, params or {})


# ─────────────────────────────────────────────────────────────────
# Frames
# ─────────────────────────────────────────────────────────────────


class _Frame:
    """A live frame: page + the iframe WebElement chain that leads to it."""

    def __init__(self, page, element=None, parent_chain=None, chain=None):
        self._page = page
        self._element = element
        self._chain = list(parent_chain or [])
        if element is not None:
            self._chain.append(element)
        if chain is not None:
            self._chain = list(chain)

    @property
    def driver(self):
        return self._page.driver

    @property
    def chain(self):
        return list(self._chain)

    def _enter(self):
        _enter_scope(self.driver, self._chain)

    def _exit(self):
        _exit_scope(self.driver)

    @property
    def url(self) -> str:
        try:
            src = self._element.get_attribute("src") or ""
        except Exception:
            src = ""
        if src.startswith(("http://", "https://")):
            return src
        if src:
            return urljoin(self._page.url, src)
        return ""

    async def title(self) -> str:
        try:
            self._enter()
            return await asyncio.to_thread(lambda: self.driver.title)
        except Exception:
            return ""
        finally:
            self._exit()

    async def evaluate(self, js, arg=None):
        self._enter()
        try:
            script = _wrap_eval_js(js, arg is not None)
            args = [arg] if arg is not None else []
            return await asyncio.to_thread(self.driver.execute_script, script, *args)
        finally:
            self._exit()

    def locator(self, sel):
        return _Locator(self, sel)

    def get_by_role(self, role, name=None, exact: bool = False):
        return _Locator(self, None, kind="role", kind_args=(role, name, exact))

    def get_by_text(self, text, exact: bool = False):
        return _Locator(self, None, kind="text", kind_args=(text, None, exact))

    def get_by_label(self, label, exact: bool = False):
        return _Locator(self, None, kind="label", kind_args=(label, None, exact))

    async def query_selector(self, sel):
        try:
            self._enter()
            els = self.driver.find_elements(sel)
            el = els[0] if els else None
        except Exception:
            el = None
        finally:
            self._exit()
        if el is None:
            return None
        return _Element(self._page, el, parent_chain=self._chain)

    async def wait_for_selector(self, sel, state: str = "visible", timeout: float = 30000):
        deadline = time.time() + (timeout or 30000) / 1000.0
        while time.time() < deadline:
            try:
                self._enter()
                els = self.driver.find_elements(sel)
                found = False
                for el in els:
                    if state == "hidden":
                        found = True
                        break
                    try:
                        if el.is_displayed():
                            found = True
                            break
                    except Exception:
                        continue
            except Exception:
                found = False
            finally:
                self._exit()
            if found:
                return _Element(self._page, els[0], parent_chain=self._chain) \
                    if state != "hidden" and els else None
            await asyncio.sleep(0.1)
        raise TimeoutError(f"wait_for_selector timed out: {sel}")

    async def bounding_box(self):
        if self._element is None:
            return None
        try:
            await asyncio.to_thread(
                self.driver.execute_script,
                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                self._element)
        except Exception:
            pass
        return _js_rect(self.driver, self._element)

    async def content_frame(self):
        # A frame_locator's content frame is the frame itself.
        return self

    async def close(self, **kwargs):
        pass


# ─────────────────────────────────────────────────────────────────
# Elements (query_selector results)
# ─────────────────────────────────────────────────────────────────


class _Element:
    def __init__(self, page, element, parent_chain=None):
        self._page = page
        self._el = element
        self._chain = list(parent_chain or [])

    @property
    def driver(self):
        return self._page.driver

    async def get_attribute(self, name: str) -> str:
        try:
            return await asyncio.to_thread(self._el.get_attribute, name)
        except Exception:
            return None

    async def bounding_box(self):
        try:
            await asyncio.to_thread(
                self.driver.execute_script,
                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                self._el)
        except Exception:
            pass
        return _js_rect(self.driver, self._el)

    async def evaluate(self, js, arg=None):
        tag = ""
        try:
            tag = (self._el.tag_name or "").lower()
        except Exception:
            pass
        if tag == "iframe":
            frame = _Frame(self._page, self._el, parent_chain=self._chain)
            return await frame.evaluate(js, arg)
        script = _wrap_eval_js(js, True)
        try:
            return await asyncio.to_thread(
                self.driver.execute_script, script, self._el)
        except Exception:
            return None

    async def click(self, **kwargs):
        point = await self.bounding_box()
        if point and (point.get("width") or point.get("height")):
            await self._page.mouse.click(point["x"] + point["width"] / 2,
                                         point["y"] + point["height"] / 2)

    async def is_visible(self) -> bool:
        try:
            return bool(await asyncio.to_thread(self._el.is_displayed))
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────
# Locators
# ─────────────────────────────────────────────────────────────────


class _ElementNotFound(RuntimeError):
    pass


class _Locator:
    def __init__(self, scope, selector, index=None, kind=None, kind_args=None):
        self._scope = scope            # _Page or _Frame
        self._sel = selector
        self._index = index
        self._kind = kind              # "role" / "text" / "label" or None (CSS)
        self._kind_args = kind_args or (None, None, False)

    @property
    def driver(self):
        return self._scope.driver

    @property
    def chain(self):
        return getattr(self._scope, "chain", [])

    def _enter(self):
        _enter_scope(self.driver, self.chain)

    def _exit(self):
        _exit_scope(self.driver)

    def _offset(self):
        return _frame_offset(self.driver, self.chain)

    # ── element resolution ───────────────────────────────────────
    def _els(self):
        """CSS path: list of WebElements in scope."""
        self._enter()
        try:
            if self._sel is None:
                return []
            if str(self._sel).lstrip().startswith(("//", "(")):
                return self.driver.find_elements(str(self._sel), by="xpath")
            return self.driver.find_elements(str(self._sel))
        except Exception:
            return []
        finally:
            self._exit()

    def _js_pick(self):
        """JS-kind path: JSON list of {x,y,w,h,text,value,visible,disabled}."""
        kind, a, b, exact = self._kind, self._kind_args[0], self._kind_args[1], self._kind_args[2]
        self._enter()
        try:
            raw = self.driver.execute_script(_pick_js(kind, a, b, exact))
            records = json.loads(raw) if raw else []
        except Exception:
            records = []
        finally:
            self._exit()
        dx, dy = self._offset()
        for r in records:
            r["x"] += dx
            r["y"] += dy
        return records

    def _pick_records(self):
        if self._kind:
            return self._js_pick()
        els = self._els()
        records = []
        for el in els:
            try:
                vis = bool(el.is_displayed())
            except Exception:
                vis = False
            try:
                txt = el.text or ""
            except Exception:
                txt = ""
            try:
                val = el.get_attribute("value") or ""
            except Exception:
                val = ""
            try:
                disabled = bool(el.get_attribute("disabled") is not None) or not bool(el.is_enabled())
            except Exception:
                disabled = False
            try:
                r = _js_rect(self.driver, el)
            except Exception:
                r = {"x": 0, "y": 0, "width": 0, "height": 0}
            records.append({"x": r["x"], "y": r["y"], "w": r["width"], "h": r["height"],
                            "text": txt, "value": val, "visible": vis, "disabled": disabled,
                            "_el": el})
        return records

    def _record(self, index=None):
        records = self._pick_records()
        if not records:
            return None
        idx = self._index if index is None else index
        if idx is None:
            idx = 0
        try:
            return records[idx]
        except Exception:
            return records[0] if records else None

    def _act_point(self, idx=None):
        """Center point (viewport coords) of the target element for real input."""
        if self._kind:
            kind, a, b, exact = self._kind, self._kind_args[0], self._kind_args[1], self._kind_args[2]
            self._enter()
            try:
                raw = self.driver.execute_script(
                    _action_js(kind, a, b, exact, idx or 0))
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {}
            finally:
                self._exit()
            if not data.get("ok"):
                raise _ElementNotFound(f"locator not found: kind={kind} {a}")
            dx, dy = self._offset()
            return {"x": data["x"] + dx, "y": data["y"] + dy}
        rec = self._record(idx)
        if rec is None:
            raise _ElementNotFound(f"locator not found: {self._sel}")
        try:
            el = rec.get("_el")
            if el is not None:
                await_thread = asyncio.to_thread(
                    self.driver.execute_script,
                    "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                    el)
                asyncio.get_running_loop().run_until_complete(await_thread)  # never: we're async
        except Exception:
            pass
        r = _js_rect(self.driver, rec.get("_el")) if rec.get("_el") is not None else rec
        return {"x": r["x"] + r["width"] / 2, "y": r["y"] + r["height"] / 2}

    # ── public Playwright-ish API ────────────────────────────────
    @property
    def first(self):
        return _Locator(self._scope, self._sel, index=0, kind=self._kind, kind_args=self._kind_args)

    @property
    def last(self):
        return _Locator(self._scope, self._sel, index=-1, kind=self._kind, kind_args=self._kind_args)

    def nth(self, index: int):
        return _Locator(self._scope, self._sel, index=int(index), kind=self._kind, kind_args=self._kind_args)

    async def count(self) -> int:
        if self._kind:
            return len(self._js_pick())
        return len(self._els())

    async def click(self, timeout: float = 30000, **kwargs):
        point = await self._act_point()
        await self._scope.mouse.click(point["x"], point["y"])

    async def hover(self, **kwargs):
        point = await self._act_point()
        await self._scope.mouse.move(point["x"], point["y"])

    async def inner_text(self) -> str:
        rec = self._record()
        if rec is None:
            raise _ElementNotFound(f"locator not found: {self._sel or self._kind}")
        return rec.get("text", "")

    async def text_content(self) -> str:
        return await self.inner_text()

    async def all_inner_texts(self) -> list:
        return [r.get("text", "") for r in self._pick_records()]

    async def get_attribute(self, name: str) -> str:
        rec = self._record()
        if rec is None:
            raise _ElementNotFound(f"locator not found: {self._sel or self._kind}")
        el = rec.get("_el")
        if el is not None:
            return await asyncio.to_thread(el.get_attribute, name)
        return None

    async def bounding_box(self):
        rec = self._record()
        if rec is None:
            return None
        return {"x": rec["x"], "y": rec["y"], "width": rec["w"], "height": rec["h"]}

    async def input_value(self) -> str:
        rec = self._record()
        if rec is None:
            raise _ElementNotFound(f"locator not found: {self._sel or self._kind}")
        return rec.get("value", "")

    async def is_visible(self) -> bool:
        rec = self._record()
        return bool(rec and rec.get("visible"))

    async def is_checked(self) -> bool:
        rec = self._record()
        if rec is None:
            return False
        el = rec.get("_el")
        if el is not None:
            try:
                return bool(await asyncio.to_thread(el.is_selected))
            except Exception:
                pass
        return False

    async def is_disabled(self) -> bool:
        rec = self._record()
        return bool(rec and rec.get("disabled"))

    async def fill(self, value: str, timeout: float = 30000, **kwargs):
        # Focus the target (JS-kind locators focus via _action_js; CSS via JS).
        if self._kind:
            self._act_point()
        else:
            rec = self._record()
            if rec is None:
                raise _ElementNotFound(f"locator not found: {self._sel}")
            el = rec.get("_el")
            if el is not None:
                self._enter()
                try:
                    await asyncio.to_thread(
                        self.driver.execute_script,
                        "arguments[0].scrollIntoView({block:'center', inline:'center'});"
                        "arguments[0].focus();",
                        el)
                finally:
                    self._exit()
        kb = self._scope.keyboard
        await kb.press("Control+a")
        await kb.press("Delete")
        await kb.type(str(value))

    async def type(self, text: str, delay: int = 0, **kwargs):
        if self._kind:
            self._act_point()
        else:
            rec = self._record()
            if rec is None:
                raise _ElementNotFound(f"locator not found: {self._sel}")
            el = rec.get("_el")
            if el is not None:
                self._enter()
                try:
                    await asyncio.to_thread(
                        self.driver.execute_script,
                        "arguments[0].scrollIntoView({block:'center', inline:'center'});"
                        "arguments[0].focus();",
                        el)
                finally:
                    self._exit()
        await self._scope.keyboard.type(str(text), delay=delay)

    async def press(self, key: str, **kwargs):
        if self._kind:
            self._act_point()
        else:
            rec = self._record()
            if rec is None:
                raise _ElementNotFound(f"locator not found: {self._sel}")
            el = rec.get("_el")
            if el is not None:
                self._enter()
                try:
                    await asyncio.to_thread(
                        self.driver.execute_script,
                        "arguments[0].scrollIntoView({block:'center', inline:'center'});"
                        "arguments[0].focus();",
                        el)
                finally:
                    self._exit()
        await self._scope.keyboard.press(key)

    async def select_option(self, value=None, label=None, index=None, **kwargs):
        rec = self._record()
        if rec is None:
            raise _ElementNotFound(f"locator not found: {self._sel or self._kind}")
        el = rec.get("_el")
        if el is None:
            return
        from selenium.webdriver.support.ui import Select
        self._enter()
        try:
            sel = Select(el)
            if value is not None:
                await asyncio.to_thread(sel.select_by_value, str(value))
            elif label is not None:
                await asyncio.to_thread(sel.select_by_visible_text, str(label))
            elif index is not None:
                await asyncio.to_thread(sel.select_by_index, int(index))
        finally:
            self._exit()

    async def wait_for(self, state: str = "visible", timeout: float = 30000, **kwargs):
        deadline = time.time() + (timeout or 30000) / 1000.0
        while time.time() < deadline:
            try:
                if state == "hidden":
                    if await self.count() == 0:
                        return
                elif await self.count() > 0:
                    if state in ("attached", "visible"):
                        return
                    if await self.is_visible():
                        return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError(f"locator.wait_for timed out: {self._sel or self._kind}")

    async def evaluate(self, js, arg=None):
        rec = self._record()
        if rec is None:
            raise _ElementNotFound(f"locator not found: {self._sel or self._kind}")
        el = rec.get("_el")
        if el is None:
            return None
        self._enter()
        try:
            script = _wrap_eval_js(js, True)
            return await asyncio.to_thread(self.driver.execute_script, script, el)
        finally:
            self._exit()

    async def content_frame(self):
        rec = self._record()
        if rec is None or rec.get("_el") is None:
            return None
        el = rec["_el"]
        tag = ""
        try:
            tag = (el.tag_name or "").lower()
        except Exception:
            pass
        if tag != "iframe":
            try:
                tag = await asyncio.to_thread(el.get_attribute, "tag")
            except Exception:
                pass
        return _Frame(self._scope, el, parent_chain=self.chain)

    async def count_visible(self) -> int:
        return sum(1 for r in self._pick_records() if r.get("visible"))


# ─────────────────────────────────────────────────────────────────
# Page
# ─────────────────────────────────────────────────────────────────


class _Page:
    def __init__(self, browser, context=None):
        self._browser = browser
        self._context = context
        self.keyboard = _Keyboard(browser)
        self.mouse = browser._mouse

    @property
    def driver(self):
        return self._browser.driver

    async def goto(self, url, wait_until: str = None, timeout: float = 30000, **kwargs):
        driver = self.driver
        browser = self._browser
        if browser._cdp_active():
            # CDP Mode: navigate over the raw CDP channel with WebDriver
            # detached — no WebDriver is attached while the page (and any
            # Cloudflare challenge) loads.
            try:
                await asyncio.to_thread(
                    browser._stealth_do, lambda: driver.cdp.open(url))
            except Exception:
                try:
                    await asyncio.to_thread(
                        driver.execute_cdp_cmd, "Page.navigate", {"url": url})
                except Exception:
                    await asyncio.to_thread(driver.get, url)
        else:
            try:
                await asyncio.to_thread(
                    driver.execute_cdp_cmd, "Page.navigate", {"url": url})
            except Exception:
                await asyncio.to_thread(driver.get, url)
        target = "complete" if wait_until in (None, "load") else "interactive"
        deadline = time.time() + (timeout or 30000) / 1000.0
        while time.time() < deadline:
            try:
                rs = await asyncio.wait_for(
                    asyncio.to_thread(driver.execute_script, "return document.readyState"),
                    timeout=2.0)
            except Exception:
                rs = ""
            if target == "interactive" and rs in ("interactive", "complete"):
                return
            if target == "complete" and rs == "complete":
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Navigation timeout: {url}")

    async def evaluate(self, js, arg=None):
        script = _wrap_eval_js(js, arg is not None)
        args = [arg] if arg is not None else []
        return await asyncio.to_thread(self.driver.execute_script, script, *args)

    async def title(self) -> str:
        try:
            return await asyncio.to_thread(lambda: self.driver.title)
        except Exception:
            return ""

    @property
    def url(self) -> str:
        try:
            return self.driver.current_url
        except Exception:
            return ""

    async def reload(self, **kwargs):
        driver = self.driver
        if self._browser._cdp_active():
            try:
                await asyncio.to_thread(
                    self._browser._stealth_do, lambda: driver.cdp.reload())
                return
            except Exception:
                pass
        try:
            await asyncio.to_thread(
                driver.execute_cdp_cmd, "Page.reload", {"ignoreCache": False})
        except Exception:
            await asyncio.to_thread(driver.refresh)

    async def close(self, **kwargs):
        try:
            handles = await asyncio.to_thread(self.driver.window_handles)
            if len(handles) > 1:
                await asyncio.to_thread(self.driver.close)
        except Exception:
            pass

    def locator(self, sel):
        return _Locator(self, sel)

    def get_by_role(self, role, name=None, exact: bool = False):
        return _Locator(self, None, kind="role", kind_args=(role, name, exact))

    def get_by_text(self, text, exact: bool = False):
        return _Locator(self, None, kind="text", kind_args=(text, None, exact))

    def get_by_label(self, label, exact: bool = False):
        return _Locator(self, None, kind="label", kind_args=(label, None, exact))

    def frame_locator(self, sel):
        deadline = time.time() + 10.0
        while True:
            try:
                self.driver.switch_to.default_content()
                els = self.driver.find_elements(str(sel))
                if els:
                    return _Frame(self, els[0])
            except Exception:
                pass
            if time.time() >= deadline:
                raise _ElementNotFound(f"frame_locator not found: {sel}")
            time.sleep(0.2)

    async def query_selector(self, sel):
        try:
            els = self.driver.find_elements(str(sel))
            el = els[0] if els else None
        except Exception:
            el = None
        if el is None:
            return None
        return _Element(self, el)

    @property
    def frames(self):
        try:
            iframes = self.driver.find_elements("iframe")
        except Exception:
            iframes = []
        return [_Frame(self, el) for el in iframes]

    async def wait_for_selector(self, sel, state: str = "visible", timeout: float = 30000):
        deadline = time.time() + (timeout or 30000) / 1000.0
        while time.time() < deadline:
            try:
                els = self.driver.find_elements(str(sel))
                if els:
                    if state == "hidden":
                        return
                    if state == "attached":
                        return _Element(self, els[0])
                    for el in els:
                        try:
                            if el.is_displayed():
                                return _Element(self, el)
                        except Exception:
                            continue
            except Exception:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError(f"wait_for_selector timed out: {sel}")

    async def wait_for_timeout(self, ms: float):
        await asyncio.sleep((ms or 0) / 1000.0)

    # ── SeleniumBase CDP-stealth actions ───────────────────────────
    # Discord sits behind Cloudflare, and a Turnstile captcha widget can
    # appear while navigating. Turnstile must NEVER be pressed with standard
    # Selenium clicks — Cloudflare fingerprints synthetic event triggers.
    # Use SeleniumBase's CDP-native methods instead, which run with the
    # WebDriver detached over the raw CDP channel:
    #   · cdp_click("selector")         — CDP click (simulated mouse click
    #     over the raw CDP protocol, no WebDriver attached);
    #   · uc_click("selector")          — UC-mode stealth click: with CDP
    #     active it auto-redirects to the CDP click; otherwise it schedules
    #     the click, disconnects chromedriver during it, reconnects after;
    #   · uc_gui_click_captcha()        — OS-level GUI click (PyAutoGUI) on
    #     the captcha checkbox, for widgets rendered inside an iframe. No JS
    #     injection at all, so the click is indistinguishable from a real
    #     user's mouse at the OS layer.
    async def cdp_click(self, selector, timeout=None, scroll=True, **kwargs):
        """SeleniumBase CDP-native stealth click: driver.cdp.click(selector).
        Runs with WebDriver detached (raw CDP), then reattaches."""
        return await asyncio.to_thread(
            self._browser._stealth_do,
            lambda: self.driver.cdp.click(selector, timeout=timeout,
                                          scroll=scroll))

    async def uc_click(self, selector, by="css selector", timeout=6000,
                       reconnect_time=None, **kwargs):
        """SeleniumBase UC stealth click: uc_click(driver, selector). With
        CDP Mode active it auto-redirects to the CDP click while WebDriver
        is detached (via _stealth_do); otherwise it disconnects
        chromedriver for the click itself."""
        from seleniumbase.core.browser_launcher import (
            uc_click as _sb_uc_click)
        # Playwright-style timeout is in ms; SeleniumBase's uc_click() takes
        # SECONDS (it forwards to wait_for_selector).
        sb_timeout = (timeout or 6000) / 1000.0
        return await asyncio.to_thread(
            self._browser._stealth_do,
            lambda: _sb_uc_click(self.driver, selector, by=by,
                                 timeout=sb_timeout,
                                 reconnect_time=reconnect_time))

    async def uc_gui_click_captcha(self, frame="iframe", retry=False,
                                   blind=False, **kwargs):
        """SeleniumBase UC OS-level captcha click (PyAutoGUI). Auto-detects
        Cloudflare Turnstile vs Google reCAPTCHA; use for iframe widgets:
        uc_gui_click_captcha(driver)."""
        from seleniumbase.core.browser_launcher import (
            uc_gui_click_captcha as _sb_uc_gui_click_captcha)
        return await asyncio.to_thread(
            _sb_uc_gui_click_captcha, self.driver, frame=frame,
            retry=retry, blind=blind)

    async def click(self, sel, **kwargs):
        await self.locator(sel).click(**kwargs)

    async def fill(self, sel, value, **kwargs):
        await self.locator(sel).fill(value, **kwargs)

    async def press(self, key: str, **kwargs):
        await self.keyboard.press(key)

    async def type(self, text: str, delay: int = 0, **kwargs):
        await self.keyboard.type(text, delay=delay)

    async def route(self, pattern, handler):
        # Playwright request interception has no SeleniumBase equivalent here.
        # The caller (hCaptcha hsw flow) uses it as an optimization to serve an
        # empty shell; loading the real page is harmless and keeps the token
        # generation working.
        print(f"[Engine] page.route({pattern}) ignored — SeleniumBase engine has no request interception", flush=True)

    async def screenshot(self, path=None, clip=None, full_page: bool = False,
                         type=None, **kwargs):
        driver = self.driver
        if full_page:
            try:
                data = await asyncio.to_thread(
                    driver.execute_cdp_cmd, "Page.captureScreenshot",
                    {"format": "png", "captureBeyondViewport": True, "fromSurface": True})
                png = base64.b64decode(data.get("data", "") or "")
            except Exception:
                png = await asyncio.to_thread(driver.get_screenshot_as_png)
        else:
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
        if clip:
            try:
                img = Image.open(io.BytesIO(png)).convert("RGB")
                x, y = float(clip["x"]), float(clip["y"])
                w, h = float(clip["width"]), float(clip["height"])
                img = img.crop((int(x), int(y), int(x + w), int(y + h)))
                buf = io.BytesIO()
                img.save(buf, "PNG")
                png = buf.getvalue()
            except Exception:
                pass
        if path:
            with open(path, "wb") as f:
                f.write(png)
            return None
        return png


# ─────────────────────────────────────────────────────────────────
# Context / Browser / Playwright
# ─────────────────────────────────────────────────────────────────


class _Context:
    def __init__(self, browser):
        self._browser = browser
        self._window = None

    @property
    def driver(self):
        return self._browser.driver

    async def new_page(self, **kwargs):
        driver = self.driver
        if self._browser._cdp_active():
            # CDP Mode: the raw CDP websocket (driver.cdp) is bound to the tab
            # that was active when CDP Mode was activated. Opening a NEW tab
            # here SPLITS the two channels: cdp.open() navigates the old tab
            # while title()/evaluate()/execute_script() read the new blank one
            # — the bot lands on a white screen that never renders. Reuse the
            # current window so navigation and reads always hit the same tab.
            try:
                self._window = driver.current_window_handle
            except Exception:
                pass
            return _Page(self._browser, self)
        try:
            await asyncio.to_thread(driver.switch_to.new_window, "tab")
        except Exception:
            pass
        try:
            self._window = driver.current_window_handle
        except Exception:
            pass
        return _Page(self._browser, self)

    async def add_init_script(self, script, **kwargs):
        if not script:
            return
        self._browser._init_scripts.append(script)
        try:
            await asyncio.to_thread(
                self.driver.execute_cdp_cmd,
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
        except Exception:
            pass

    async def new_cdp_session(self, page=None, **kwargs):
        return _CdpSession(self.driver)

    async def close(self, **kwargs):
        if not self._window:
            return
        try:
            handles = await asyncio.to_thread(self.driver.window_handles)
            if self._window in handles and len(handles) > 1:
                await asyncio.to_thread(self.driver.switch_to.window, self._window)
                await asyncio.to_thread(self.driver.close)
        except Exception:
            pass
        self._window = None


class _Browser:
    def __init__(self, driver, data_dir=None):
        self._driver = driver
        self._data_dir = data_dir
        self._closed = False
        self._init_scripts = []
        self._mouse = _Mouse(self)
        self._contexts = []

    @property
    def driver(self):
        return self._driver

    # ── CDP Mode bridge ────────────────────────────────────────────
    # The engine boots into SeleniumBase CDP Mode (raw Chrome DevTools
    # Protocol): the browser is launched with the UC driver, then
    # ``uc_activate_cdp_mode()`` (module-level, SeleniumBase >= 4.51)
    # disconnects chromedriver and hands control to a raw CDP websocket.
    # WebDriver is reattached right after launch so the rest of this adapter
    # (raw DOM calls) keeps working unchanged; navigation and stealth clicks
    # detach it again so those sensitive moments happen with NO WebDriver
    # attached.
    def _cdp_active(self) -> bool:
        """True when CDP Mode is on (raw CDP websocket available).
        driver.cdp.* works over the raw websocket whether or not WebDriver is
        currently attached; _ensure_stealth()/_ensure_connected() manage the
        attach state around each sensitive op."""
        d = self._driver
        try:
            return bool(getattr(d, "_is_using_cdp", False))
        except Exception:
            return False

    def _ensure_stealth(self) -> bool:
        """Detach WebDriver so the raw CDP channel takes over.
        Returns True if the driver was detached here."""
        d = self._driver
        try:
            if getattr(d, "_is_using_cdp", False) and getattr(
                    d, "_is_connected", True):
                d.disconnect()
                return True
        except Exception:
            pass
        return False

    def _ensure_connected(self) -> bool:
        """Reattach WebDriver for raw DOM ops. Returns True if reattached."""
        d = self._driver
        try:
            if getattr(d, "_is_using_cdp", False) and not getattr(
                    d, "_is_connected", True):
                d.connect()
                return True
        except Exception:
            pass
        return False

    def _stealth_do(self, fn, *args, **kwargs):
        """Run ``fn`` with WebDriver detached (raw CDP), then reattach so
        the rest of the Playwright-style adapter keeps working unchanged.
        Navigation and stealth clicks go through this bridge."""
        was_cdp = self._ensure_stealth()
        try:
            return fn(*args, **kwargs)
        finally:
            if was_cdp:
                self._ensure_connected()

    async def new_context(self, **kwargs):
        # SeleniumBase incognito session: one driver per browser; contexts are
        # thin wrappers that open a fresh tab and can apply init scripts.
        ctx = _Context(self)
        self._contexts.append(ctx)
        # Best-effort: apply Playwright-style context extras.
        headers = kwargs.get("extra_http_headers") or {}
        if headers:
            try:
                await asyncio.to_thread(
                    self._driver.execute_cdp_cmd,
                    "Network.enable", {})
                await asyncio.to_thread(
                    self._driver.execute_cdp_cmd,
                    "Network.setExtraHTTPHeaders",
                    {"headers": {str(k): str(v) for k, v in headers.items()}})
            except Exception:
                pass
        vp = kwargs.get("viewport") or {}
        if vp.get("width") and vp.get("height"):
            try:
                await asyncio.to_thread(
                    self._driver.set_window_size, int(vp["width"]), int(vp["height"]))
            except Exception:
                pass
        return ctx

    async def new_page(self, **kwargs):
        ctx = _Context(self)
        self._contexts.append(ctx)
        return await ctx.new_page()

    async def close(self, **kwargs):
        if self._closed:
            return
        self._closed = True
        # Close the raw CDP websocket cleanly before killing the browser.
        try:
            cdp_base = getattr(self._driver, "cdp_base", None)
            if cdp_base is not None and hasattr(cdp_base, "stop"):
                await asyncio.to_thread(cdp_base.stop)
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._driver.quit)
        except Exception:
            pass
        # Stop the virtual display this engine started for headed-under-xvfb
        # runs (if any).
        try:
            display = getattr(self._driver, "_sb_display", None)
            if display is not None and hasattr(display, "stop"):
                await asyncio.to_thread(display.stop)
        except Exception:
            pass
        if self._data_dir:
            try:
                await asyncio.to_thread(shutil.rmtree, self._data_dir, ignore_errors=True)
            except Exception:
                pass


class _BrowserType:
    def __init__(self, owner=None):
        self._owner = owner

    async def launch(
        self,
        *,
        headless: bool = True,
        args=None,
        executable_path=None,
        proxy=None,
        channel=None,
        **kwargs,
    ) -> _Browser:
        return await asyncio.to_thread(
            self._launch_sync, headless=headless, args=args,
            proxy=proxy, kwargs=kwargs)

    def _launch_sync(self, headless, args, proxy, kwargs):
        binary = (kwargs.pop("binary_location", None)
                  or os.environ.get("BRAVE_BINARY", "").strip()
                  or _find_brave())
        ua = kwargs.get("user_agent") or ""
        tz = kwargs.get("timezone") or ""
        accept_lang = kwargs.get("accept_language") or "en-US,en;q=0.9"
        locale = _locale_from_accept_language(accept_lang)

        # Never pass Playwright's own headless flag — this engine decides
        # headed-under-xvfb vs headless2 itself (UC Mode is detectable in
        # true headless). --incognito is always enforced below.
        extra = [a for a in (args or []) if a not in (
            "--headless", "--headless=new", "--incognito")]
        for flag in ("--no-sandbox", "--disable-dev-shm-usage", "--incognito"):
            if flag not in extra:
                extra.append(flag)
        if tz:
            # SeleniumBase's Driver() has no timezone kwarg — pass it as the
            # Chromium switch instead (the same flag Playwright uses).
            tz_flag = f"--timezone={tz}"
            if tz_flag not in extra:
                extra.append(tz_flag)

        data_dir = tempfile.mkdtemp(prefix="sb-brave-")
        launch_kw = {
            "uc": True,
            "browser": "brave",       # unbranded Chromium
            "incognito": True,        # always incognito
            "user_data_dir": data_dir,
            "disable_csp": True,
        }
        if binary:
            launch_kw["binary_location"] = binary
        if ua:
            # SeleniumBase's Driver() user-agent kwarg is ``agent`` (not
            # ``user_agent`` — that raised a TypeError on every launch).
            launch_kw["agent"] = ua
        if locale:
            launch_kw["locale"] = locale
        proxy_str = _proxy_to_sb(proxy)
        if proxy_str:
            launch_kw["proxy"] = proxy_str
        if extra:
            # chromium_arg is COMMA-separated ("ARG1,ARG2"), not space-joined
            # — SeleniumBase splits on "," and would treat a space-joined
            # string as ONE bogus flag.
            launch_kw["chromium_arg"] = ",".join(extra)

        driver = None
        display = None
        if headless:
            # Preferred: headed browser under a virtual display (UC stealth
            # works; docs: "UC Mode is detectable in Headless Mode"). The
            # standalone Driver() does NOT auto-start Xvfb — that's the pytest
            # layer's job — so start the display here and keep it alive for
            # the browser's lifetime. If Xvfb is absent, fall back to
            # Chromium's new headless mode.
            try:
                from sbvirtualdisplay import Display as SBDisplay
                display = SBDisplay(backend="xvfb", visible=True,
                                    size=(1920, 1080), use_xauth=True)
                display.start()
            except Exception:
                display = None
            try:
                driver = Driver(**launch_kw)
            except Exception as e1:
                if display is not None:
                    try:
                        display.stop()
                    except Exception:
                        pass
                    display = None
                # Fallback: Chrome's new headless mode.
                launch_kw["headless2"] = True
                try:
                    driver = Driver(**launch_kw)
                except Exception as e2:
                    if data_dir:
                        shutil.rmtree(data_dir, ignore_errors=True)
                    raise RuntimeError(
                        "SeleniumBase engine failed to launch Brave "
                        f"(xvfb error: {str(e1)[:160]}; headless2 error: "
                        f"{str(e2)[:160]}). Install Brave (see Dockerfile) or "
                        "point BRAVE_BINARY at the binary. If chromedriver "
                        "version mismatch: `sbase install chromedriver <Brave's "
                        "Chromium major version>`."
                    ) from e2
            if display is not None and driver is not None:
                # Keep the virtual display alive for the browser's lifetime;
                # _Browser.close() stops it.
                try:
                    driver._sb_display = display
                except Exception:
                    pass
        else:
            driver = Driver(**launch_kw)

        try:
            driver.set_window_size(1920, 1080)
        except Exception:
            pass

        # ── Upgrade: SeleniumBase CDP Mode (raw Chrome DevTools Protocol) ──
        # CDP Mode is the stealth successor to UC Mode. uc_activate_cdp_mode()
        # disconnects chromedriver and re-drives the already-running Brave
        # browser over a raw CDP websocket — no WebDriver attached while
        # navigating or clicking CAPTCHA widgets (the moments anti-bot
        # fingerprinting cares about). WebDriver is reconnected right after
        # so the adapter's DOM calls keep working; goto()/uc_click()/
        # cdp_click() detach it again on demand via _stealth_do().
        try:
            # CDP Mode resolves the browser executable from
            # ``seleniumbase.config.binary_location``; when that is unset it
            # falls back to Chrome-only path probing and raises "Could not
            # find a valid chrome browser binary" even though Driver() just
            # launched Brave fine (browser="brave"). Point it at the
            # resolved Brave binary BEFORE activating CDP Mode.
            from seleniumbase import config as _sb_config
            if binary:
                _sb_config.binary_location = binary
            from seleniumbase.core.browser_launcher import (
                uc_activate_cdp_mode as _sb_activate_cdp_mode)
            _sb_activate_cdp_mode(driver)
            driver.connect()
        except Exception as e:
            if data_dir:
                shutil.rmtree(data_dir, ignore_errors=True)
            raise RuntimeError(
                "SeleniumBase engine failed to activate CDP Mode "
                f"({str(e)[:200]}). Install Brave (see Dockerfile) or "
                "point BRAVE_BINARY at the binary; if the chromedriver "
                "version mismatches run `sbase install chromedriver <Brave's "
                "Chromium major version>`."
            ) from e

        try:
            print(
                "[Engine] SeleniumBase CDP engine ready: browser=brave "
                f"binary={binary or 'auto'} headless={bool(headless)} "
                "incognito=always cdp=raw", flush=True)
        except Exception:
            pass
        return _Browser(driver, data_dir=data_dir)

    async def launch_persistent_context(self, user_data_dir=None, **kwargs) -> _Browser:
        return await self.launch(**kwargs)


class _Playwright:
    def __init__(self):
        self.chromium = _BrowserType(self)
        self.firefox = _BrowserType(self)
        self.webkit = _BrowserType(self)
        self._browsers = []

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
