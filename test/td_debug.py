#!/usr/bin/env python3
import asyncio
import http.server
import os
import socketserver
import threading

os.environ.setdefault("CLEARCOTE_BINARY", "/home/daytona/.cache/clearcote/v0.1.0-pre.22/browser/chrome")
from truedriver_engine import async_playwright


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


async def main():
    serve(TEST_DIR, 8320)
    serve(TEST_DIR, 8321)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto("http://127.0.0.1:8320/td_page.html", wait_until="domcontentloaded")

    # 1) evaluate sanity
    r = await page.evaluate("1 + 1")
    print("evaluate(1+1) ->", repr(r))

    # 2) injection with error surfacing
    inj = await page.evaluate("""() => {
        try {
            const slot = document.getElementById('captcha-slot');
            const f = document.createElement('iframe');
            f.src = 'http://127.0.0.1:8321/td_widget.html';
            f.setAttribute('title', 'Widget');
            slot.appendChild(f);
            return 'injected';
        } catch (e) { return 'ERR:' + e.message; }
    }""")
    print("injection ->", repr(inj))
    await asyncio.sleep(2.0)

    js = await page.evaluate("""() => JSON.stringify([...document.querySelectorAll('iframe')].map(f => f.src))""")
    print("JS iframes after inject:", js)

    # 3) engine locator counts
    print("engine count('iframe'):", await page.locator('iframe').count())
    print("engine count('iframe[src*=...]'):", await page.locator('iframe[src*="td_widget.html"]').count())

    tab = page._tab
    # 4) CSS select path
    try:
        els = await tab.select_all('iframe', 3)
        print("tab.select_all('iframe'):", len(els))
    except Exception as ex:
        print("tab.select_all error:", repr(ex))
    # 5) text find path
    try:
        els = await tab.find_all('iframe', 3)
        print("tab.find_all('iframe') [text] :", len(els))
    except Exception as ex:
        print("tab.find_all('iframe') [text] error:", repr(ex)[:120])

    # 6) frames
    try:
        frames = await tab.get_frames()
        print("CDP frames:", [(f.id_[:8], f.url[:60]) for f in frames])
    except Exception as ex:
        print("get_frames error:", repr(ex))

    await browser.close()


asyncio.run(main())
