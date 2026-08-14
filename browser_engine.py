"""
browser_engine.py — TrueDriver engine driving ungoogled Chromium.

Single engine, no fallback. The DRIVER is TrueDriver
(https://pypi.org/project/truedriver — a blazing fast, async-first,
undetectable CDP automation framework; a fork of nodriver). The BROWSER is
UNGOOGLED CHROMIUM (resolved via $UNGOOGLED_CHROMIUM_BINARY / $CHROMIUM_BINARY
/ $BRAVE_BINARY, then ungoogled-chromium / chromium / brave-browser /
google-chrome on PATH). Incognito is ALWAYS on, so no cookies / cache /
IndexedDB ever touch disk.

truedriver_engine.py keeps the Playwright-compatible ``async_playwright``
contract the bot's workers, solver and mail client use, so every caller that
does ``from browser_engine import async_playwright`` is unchanged.
"""

from truedriver_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
