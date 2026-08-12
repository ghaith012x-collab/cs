"""
browser_engine.py — Clearcote stealth Chromium, driven by the truedriver CDP driver.

Single engine, no fallback. Clearcote's de-Googled Chromium ships engine-level
fingerprint personas (C++ getters, coherent TLS/JA3, per-seed personas);
truedriver_engine.py drives that binary over pure CDP through a
Playwright-compatible API — no Playwright driver involved.

Stealth model (same as the ShardX era): the engine owns the whole identity.
Each launch passes a fresh random fingerprint seed, so the C++ layer mints a
new, unlinkable persona per session — UA + Client Hints, WebGL/WebGPU, fonts,
canvas, TLS. The bot only pins the UI locale (en-US) on top so Discord
renders in English.

Set CLEARCOTE_BINARY to override the Clearcote browser binary location.
"""

from truedriver_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
