"""
browser_engine.py — ShardX anti-detect browser (ProxyShard, "ShardBrowser").

Single engine, no fallback. ShardX is a patched Chromium 149 that spoofs the
whole identity inside the C++ engine — TLS ClientHello / JA4, WebGL / WebGPU,
Client Hints, fonts, WebRTC, QUIC over SOCKS5 — driven over CDP by patchright
(stealth Playwright). shardx_engine.py keeps the Playwright-compatible
``async_playwright`` contract the bot's workers, solver and mail client use.

Stealth model — fingerprints are ALWAYS randomized, every session:
  · launch() mints a FRESH profile: a random device from the bundled 170-profile
    fingerprint library, plus randomized hardware (cores / device_memory) and
    platform version, and launch() re-randomizes those again (randomize=True).
    No two sessions ever share an identity.
  · --incognito + ephemeral user-data-dir, and the profile is deleted on
    close — zero cookie/cache/fingerprint state carries between accounts.
  · The bot only pins the UI locale (en-US) on top so Discord renders in
    English; the engine owns UA, TLS, WebGL, fonts, timezone, everything else.

Set SHARDX_CACHE_DIR to relocate the engine/fingerprint/profiles cache.
Requires the standard Chromium system libraries (the Dockerfile installs them).
"""

from shardx_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
