"""
browser_engine.py — ShardX anti-detect browser (ProxyShard, "ShardBrowser").

Single engine, no fallback. ShardX is a patched Chromium 149 that spoofs the
whole identity inside the C++ engine — TLS ClientHello / JA4, WebGL / WebGPU,
Client Hints, fonts, WebRTC, QUIC over SOCKS5 — driven over CDP by patchright
(stealth Playwright). shardx_engine.py keeps the Playwright-compatible
``async_playwright`` contract the bot's workers, solver and mail client use.

Each session gets a fresh, randomized profile with an isolated user-data-dir
and runs with --incognito; the profile is deleted on close so no identity or
cookie state ever carries between accounts.

Set SHARDX_CACHE_DIR to relocate the engine/fingerprint/profiles cache.
Requires the standard Chromium system libraries (the Dockerfile installs them).
"""

from shardx_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
