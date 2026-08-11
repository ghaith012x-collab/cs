"""
browser_engine.py — Clearcote stealth Chromium, driven by the truedriver CDP driver.

Single engine, no fallback. Clearcote's de-Googled Chromium ships engine-level
fingerprint personas (C++ getters, coherent TLS/JA3, per-seed personas);
truedriver_engine.py drives that binary over pure CDP through a
Playwright-compatible API — no Playwright driver involved.

Set CLEARCOTE_BINARY to override the Clearcote browser binary location.
"""

from truedriver_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
