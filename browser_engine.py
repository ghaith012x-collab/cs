"""
browser_engine.py — truedriver driver + Clearcote stealth Chromium.

Single engine, no fallback. The DRIVER is truedriver (pure CDP — no
Playwright driver anywhere); the BROWSER is Clearcote's de-Googled Chromium,
whose C++ fingerprint machinery (TLS ClientHello / JA4, WebGL / WebGPU,
Client Hints, fonts, timezone) is driven by command-line switches minted per
session from a fresh fingerprint seed — one seed = one coherent, unlinkable
machine identity per launch.

truedriver_engine.py keeps the Playwright-compatible ``async_playwright``
contract the bot's workers, solver and mail client use, so every caller
that does ``from browser_engine import async_playwright`` is unchanged.

Set CLEARCOTE_BINARY to override the Clearcote browser binary location.
"""

from truedriver_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
