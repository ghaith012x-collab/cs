"""
browser_engine.py — ShardX (ShardBrowser) anti-detect browser.

Single engine, no fallback. The DRIVER is patchright (stealth-patched
Playwright) connecting over CDP to the ShardX engine; the BROWSER is
ShardBrowser — a patched Chromium 149 that does ALL fingerprint spoofing in
C++ (TLS ClientHello / JA4, WebGL + WebGPU, Client Hints with GREASE, fonts,
WebRTC policy, headless marker stripping, CDP side-channel closing). There is
no JS shim layer for detectors to trip on.

shardx_engine.py keeps the Playwright-compatible ``async_playwright`` contract
the bot's workers, solver and mail client use, so every caller that does
``from browser_engine import async_playwright`` is unchanged.

Set SHARDX_CACHE_DIR to relocate the engine/fingerprint/profiles cache.
"""

from shardx_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
