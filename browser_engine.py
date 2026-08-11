"""
browser_engine.py — truedriver + Thorium browser engine.

Single engine, no fallback. Set THORIUM_PATH to override the
Thorium browser binary location (default: /usr/bin/thorium-browser).
"""

from truedriver_engine import async_playwright, ENGINE

CHANNEL = None

__all__ = ["async_playwright", "ENGINE", "CHANNEL"]
