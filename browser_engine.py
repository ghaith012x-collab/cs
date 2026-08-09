"""
browser_engine.py — stealth browser engine loader.

Uses **Patchright** (the actively-maintained Playwright fork that patches the
low-level CDP startup sequence — `Runtime.enable`, `Target.setAutoAttach`,
`Page.addScriptToEvaluateOnNewDocument` ordering — which is exactly what
modern anti-bots like hCaptcha / Cloudflare check) when it is installed,
and falls back to stock Playwright otherwise.

Both expose the same async API, so every `from playwright.async_api import
async_playwright` in the codebase is replaced with:

    from browser_engine import async_playwright, ENGINE

ENGINE == "patchright"  → low-level CDP patching + stealth launch defaults
ENGINE == "playwright"  → stock engine (full JS/CSS stealth still applies)
"""
import os

try:
    from patchright.async_api import async_playwright
    ENGINE = "patchright"
except ImportError:  # pragma: no cover - fallback path
    from playwright.async_api import async_playwright
    ENGINE = "playwright"

# Launching real Chrome (channel="chrome") gives the closest TLS/HTTP2/JA3
# fingerprint to a genuine browser. Set PW_CHANNEL=chrome when a system
# Chrome/Chromium binary is available; otherwise the bundled Chromium is used.
CHANNEL = os.environ.get("PW_CHANNEL", "").strip() or None
if CHANNEL == "default":
    CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
