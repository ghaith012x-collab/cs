#!/usr/bin/env python3
"""Harness: reproduce server.py's hCaptcha widget-ready + checkbox-click logic
against the truedriver engine with a CROSS-ORIGIN mock widget iframe, and
report exactly which step fails.

Mirrors server.py verbatim where it matters:
  - _widget_rendered (via _frame_js_ready)
  - _click_hcaptcha_checkbox strategies 1-3 with _confirm
"""
import asyncio
import http.server
import json
import os
import random
import socketserver
import threading
import time

os.environ.setdefault("CLEARCOTE_BINARY", "/home/daytona/.cache/clearcote/v0.1.0-pre.22/browser/chrome")

from truedriver_engine import async_playwright  # noqa: E402


def log(msg, level="info"):
    print(f"[{time.strftime('%H:%M:%S')}] [{level.upper()}] {msg}", flush=True)


# ── tiny static servers (different ports → real cross-origin iframe) ──
class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


TEST_DIR = os.path.dirname(os.path.abspath(__file__))


def serve(root, port):
    httpd = ReuseTCPServer(("127.0.0.1", port), Handler)
    httpd.directory = root
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def frame_js_ready(page, iframe, js):
    try:
        frame = await iframe.content_frame()
        if frame is None:
            log("frame_js_ready: content_frame() -> None", "warn")
            return False
        val = await frame.evaluate(js)
        return bool(val)
    except Exception as e:
        log(f"frame_js_ready exception: {e!r}", "warn")
        return False


async def widget_rendered(page, iframe):
    return await frame_js_ready(page, iframe, """() => {
        const body = document.body;
        if (!body) return false;
        if (document.readyState !== 'complete') return false;
        if (document.querySelector(
                '#checkbox, .checkbox, [role="checkbox"], input[type="checkbox"], ' +
                '[aria-checked], .button-submit, #menu-info, .display-menu-btn, ' +
                '.refresh.button, .hcaptcha-logo')) return true;
        if (body.getAttribute('aria-hidden') === 'true') return false;
        const laidOut = (el) => {
            if (!el) return false;
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') return false;
            const r = el.getBoundingClientRect();
            return !!r && r.width > 1 && r.height > 1;
        };
        for (const sel of ['#checkbox', '.checkbox', '[role="checkbox"]',
                           'input[type="checkbox"]', '[aria-checked]',
                           '.button-submit']) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                if (laidOut(el)) return true;
            }
        }
        for (const sel of ['#menu-info', '.display-menu-btn',
                           '.refresh.button', '.hcaptcha-logo']) {
            if (laidOut(document.querySelector(sel))) return true;
        }
        const t = (body.innerText || '').trim();
        return t.length >= 3;
    }""")


async def click_hcaptcha_checkbox(page, iframe):
    try:
        frame = await iframe.content_frame()
    except Exception:
        frame = None
    if frame is None:
        log("checkbox: widget frame not attached", "warn")
        return False

    # probe
    try:
        probe = await frame.evaluate("""() => {
            const body = document.body;
            const anyCheckbox = !!document.querySelector(
                '#checkbox, [role="checkbox"], .button-submit, input[type="checkbox"], [aria-checked]');
            const t = (body && body.innerText || '').slice(0, 80);
            return JSON.stringify({
                ariaHidden: body ? body.getAttribute('aria-hidden') : null,
                children: body ? body.children.length : -1,
                anyCheckbox,
                readyState: document.readyState,
                text: t.replace(/\\s+/g, ' ').trim()
            });
        }""")
        log(f"checkbox: widget frame probe: {probe}", "debug")
    except Exception as e:
        log(f"checkbox: probe error: {e!r}", "debug")

    async def confirm(attempt):
        for _ in range(3):
            try:
                flipped = await frame.evaluate(
                    "() => { const el = document.querySelector('[aria-checked]');"
                    " return !!el && el.getAttribute('aria-checked') === 'true'; }")
                if flipped:
                    log(f"checkbox {attempt} — CONFIRMED (aria-checked=true)")
                    return True
            except Exception:
                pass
            try:
                chall = page.locator(
                    'iframe[title*="hCaptcha challenge"], iframe[src*="hcaptcha-challenge"]')
                if await chall.count() > 0:
                    log(f"checkbox {attempt} — CONFIRMED (challenge spawned)")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    # Strategy 1: real locator click
    clicked_through = False
    for selector, label in (("#checkbox", "#checkbox"),
                            (".checkbox", ".checkbox"),
                            ("[role='checkbox']", "[role=checkbox]"),
                            ("input[type='checkbox']", "input[type=checkbox]"),
                            ("[aria-checked]", "[aria-checked]"),
                            (".button-submit", ".button-submit")):
        try:
            loc = frame.locator(selector).first
            if await loc.count() == 0:
                log(f"checkbox: {label} count == 0", "debug")
                continue
            try:
                box = await loc.bounding_box()
            except Exception:
                box = None
            log(f"checkbox: {label} bounding_box -> {box}", "debug")
            if box and box.get("width", 0) > 1 and box.get("height", 0) > 1:
                _cx = box["x"] + box["width"] / 2
                _cy = box["y"] + box["height"] / 2
                log(f"checkbox: {label} click center ({_cx:.1f}, {_cy:.1f})", "debug")
            await asyncio.sleep(random.uniform(0.2, 0.6))
            try:
                await loc.click(timeout=3000)
                log(f"checkbox: locator click via {label} OK", "debug")
            except Exception as e:
                log(f"checkbox: locator click via {label} error {e!r} — retry force", "debug")
                await loc.click(timeout=3000, force=True)
            clicked_through = True
            if await confirm(f"via {label}"):
                return True
            log(f"checkbox: click via {label} not confirmed — fallback", "debug")
            break
        except Exception as e:
            log(f"checkbox: click via {label} failed: {str(e)[:120]}", "debug")

    # Strategy 2: JS dispatch
    try:
        clicked = await frame.evaluate("""() => {
            const candidates = [
                document.getElementById('checkbox'),
                document.querySelector('.checkbox'),
                document.querySelector('[role="checkbox"]'),
                document.querySelector('input[type="checkbox"]'),
                document.querySelector('[aria-checked]'),
                document.querySelector('.button-submit')
            ];
            const el = candidates.find(e => {
                if (!e) return false;
                const cs = getComputedStyle(e);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                const r = e.getBoundingClientRect();
                return r && r.width > 1 && r.height > 1;
            });
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const x = r.left + r.width / 2;
            const y = r.top + r.height / 2;
            const opts = {bubbles: true, cancelable: true, view: window,
                          clientX: x, clientY: y, button: 0, buttons: 1};
            el.dispatchEvent(new PointerEvent('pointerdown', opts));
            el.dispatchEvent(new MouseEvent('mousedown', opts));
            el.dispatchEvent(new PointerEvent('pointerup', opts));
            el.dispatchEvent(new MouseEvent('mouseup', opts));
            el.dispatchEvent(new MouseEvent('click', opts));
            return true;
        }""")
        log(f"checkbox: JS dispatch ran={clicked}", "debug")
        if clicked and not clicked_through and await confirm("via JS dispatch"):
            return True
    except Exception as e:
        log(f"checkbox: JS dispatch failed: {str(e)[:120]}", "debug")

    # Strategy 3: coordinate mouse click at page position
    try:
        iframe_box = await iframe.bounding_box()
        rect = await frame.evaluate("""() => {
            const el = document.getElementById('checkbox')
                || document.querySelector('.checkbox')
                || document.querySelector('[role="checkbox"]')
                || document.querySelector('input[type="checkbox"]')
                || document.querySelector('[aria-checked]')
                || document.querySelector('.button-submit');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {left: r.left, top: r.top, width: r.width, height: r.height};
        }""")
        log(f"checkbox: iframe_box={iframe_box} rect={rect}", "debug")
        if iframe_box and (iframe_box.get("width", 0) > 1 or (rect and rect.get("width", 0) > 1)):
            if rect and rect.get("width", 0) > 1 and rect.get("height", 0) > 1:
                cx = iframe_box["x"] + rect["left"] + rect["width"] / 2
                cy = iframe_box["y"] + rect["top"] + rect["height"] / 2
            elif rect:
                cx = iframe_box["x"] + rect["left"] + 14
                cy = iframe_box["y"] + rect["top"] + 14
            else:
                cx = iframe_box["x"] + iframe_box.get("width", 0) * 0.12
                cy = iframe_box["y"] + iframe_box.get("height", 0) * 0.5
            await page.mouse.move(cx, cy)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await page.mouse.click(cx, cy)
            log(f"checkbox: coordinate mouse click at ({cx:.1f}, {cy:.1f})", "debug")
            if await confirm("via coordinate mouse click"):
                return True
            log("checkbox: coordinate click not confirmed", "debug")
    except Exception as e:
        log(f"checkbox: coordinate click failed: {str(e)[:120]}", "debug")

    # DOM dump
    try:
        html = await frame.evaluate(
            "() => (document.body ? document.body.outerHTML : '').slice(0, 2000)")
        log(f"checkbox: no clickable checkbox found — widget frame DOM:\n{html}", "debug")
    except Exception as e:
        log(f"checkbox: frame DOM dump failed: {e!r}", "debug")
    return False


async def main():
    serve(TEST_DIR, 8310)   # main page origin
    serve(TEST_DIR, 8311)   # widget origin (cross-origin iframe)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto("http://127.0.0.1:8310/td_page.html", wait_until="domcontentloaded")
    # Inject the cross-origin widget iframe
    await page.evaluate("""() => {
        const slot = document.getElementById('captcha-slot');
        const f = document.createElement('iframe');
        f.src = 'http://127.0.0.1:8311/td_widget.html';
        f.setAttribute('title', 'Widget');
        slot.appendChild(f);
    }""")
    await asyncio.sleep(2.0)

    widgets = page.locator('iframe[src*="td_widget.html"]')
    wcount = await widgets.count()
    log(f"widget locator count = {wcount}")
    if wcount == 0:
        log("FATAL: no widget iframe found by locator", "error")
        return

    rendered = None
    for wi in range(wcount):
        w = widgets.nth(wi)
        ok = await widget_rendered(page, w)
        log(f"widget[{wi}] rendered={ok}")
        if ok:
            rendered = w
            break

    if rendered is None:
        log("FATAL: no widget considered rendered", "error")
    else:
        ok = await click_hcaptcha_checkbox(page, rendered)
        log(f"RESULT: checkbox click success={ok}")

    # Read back what the widget saw
    try:
        state = await rendered.content_frame().evaluate("""() => JSON.stringify({
            flipped: window.__hcaptchaFlipped,
            clicked: window.__hcaptchaClicked,
            ariaChecked: document.getElementById('checkbox').getAttribute('aria-checked')
        })""")
        log(f"widget internal state: {state}")
    except Exception as e:
        log(f"readback error: {e!r}")

    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
