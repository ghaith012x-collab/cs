"""
browser_engine.py — SeleniumBase CDP engine driving Brave (unbranded Chromium).

Single engine, no fallback. The DRIVER is SeleniumBase
(https://github.com/seleniumbase/SeleniumBase) in CDP Mode (Chrome DevTools
Protocol): the browser is launched with the UC (undetected-chromedriver)
driver, then chromedriver is disconnected and the browser is re-driven over
a raw CDP websocket — no WebDriver is attached while navigating or clicking
CAPTCHA widgets. The BROWSER is Brave — a Chromium fork (unbranded
Chromium), NOT Google Chrome. Incognito is ALWAYS on, so no cookies / cache
/ IndexedDB ever touch disk.

seleniumbase_engine.py keeps the Playwright-compatible ``async_playwright``
contract the bot's workers, solver and mail client use, so every caller that
does ``from browser_engine import async_playwright`` is unchanged.
"""

from seleniumbase_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
