"""
browser_engine.py — stealth browser engine loader.

Uses the engine specified by ENGINE env var:
  ENGINE=truedriver  → truedriver + Thorium (CDP-based, shipped blind)
  ENGINE=patchright   → patchright Playwright fork (low-level CDP patching)
  (default)           → auto-selects truedriver if Thorium binary is found,
                        otherwise falls back to patchright → stock Playwright.
"""

import os

_engine = os.environ.get("ENGINE", "").strip().lower()

# ── Engine resolution ──────────────────────────────────────────────

if _engine == "truedriver":
    from truedriver_engine import async_playwright, ENGINE  # type: ignore[import-not-found]
elif _engine == "patchright":
    try:
        from patchright.async_api import async_playwright
        ENGINE = "patchright"
    except ImportError:
        from playwright.async_api import async_playwright
        ENGINE = "playwright"
elif _engine == "playwright":
    from playwright.async_api import async_playwright
    ENGINE = "playwright"
else:
    # Auto-detect: if Thorium binary exists, prefer truedriver
    _thorium_found = False
    for _p in ("/usr/bin/thorium-browser", "/usr/bin/thorium",
               "/opt/thorium/thorium-browser"):
        if os.path.exists(_p):
            _thorium_found = True
            break

    if _thorium_found:
        try:
            from truedriver_engine import async_playwright, ENGINE  # type: ignore[import-not-found]
        except ImportError:
            try:
                from patchright.async_api import async_playwright
                ENGINE = "patchright"
            except ImportError:
                from playwright.async_api import async_playwright
                ENGINE = "playwright"
    else:
        try:
            from patchright.async_api import async_playwright
            ENGINE = "patchright"
        except ImportError:
            from playwright.async_api import async_playwright
            ENGINE = "playwright"

# ── Channel ─────────────────────────────────────────────────────────

CHANNEL = os.environ.get("PW_CHANNEL", "").strip() or None
if CHANNEL == "default":
    CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
