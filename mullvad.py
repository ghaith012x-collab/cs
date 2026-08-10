"""
mullvad.py — Mullvad VPN rotation driver.

Uses the Mullvad CLI (`mullvad`) to:
  - Log in with MULLVAD_LOGIN env var (16-digit account number)
  - Rotate IP by switching relay location (disconnect → random country → connect)
  - Report current public IP for verification

Usage:
    vpn = MullvadVPN(log=print)
    await vpn.login()
    await vpn.rotate()        # new IP
    print(vpn.current_ip)

Mullvad is a system-level WireGuard VPN. When connected, ALL traffic
(including Playwright browsers) routes through the Mullvad tunnel.
This means the browser does NOT need a proxy configured — it just
inherits the VPN tunnel automatically.
"""

import asyncio
import os
import random
import re
from typing import Callable, Optional


# ── Countries with many servers (good for rotation diversity) ──
ROTATION_COUNTRIES = [
    "us", "ca", "gb", "de", "fr", "nl", "se", "ch", "no", "dk",
    "fi", "at", "be", "it", "es", "pt", "ie", "pl", "cz", "ro",
    "jp", "sg", "hk", "au", "nz",
]

# How long to wait for VPN connect/disconnect operations
VPN_CMD_TIMEOUT = 25.0  # seconds per CLI command


class MullvadVPN:
    """Async wrapper around the Mullvad CLI for IP rotation."""

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self._account: str = ""
        self._current_ip: str = ""
        self._connected: bool = False

    # ── Public API ────────────────────────────────────────

    @property
    def current_ip(self) -> str:
        return self._current_ip

    @property
    def connected(self) -> bool:
        return self._connected

    async def login(self) -> bool:
        """Log into Mullvad with the account number from MULLVAD_LOGIN env var."""
        self._account = (os.environ.get("MULLVAD_LOGIN") or "").strip()
        if not self._account:
            self._log("[Mullvad] MULLVAD_LOGIN not set — VPN disabled", level="warn")
            return False
        if not re.match(r"^\d{16}$", self._account):
            self._log("[Mullvad] MULLVAD_LOGIN must be a 16-digit number", level="error")
            return False

        out, ok = await self._run(f"account login {self._account}")
        if ok:
            self._log(f"[Mullvad] Logged in (account ...{self._account[-4:]})")
            # Refresh relay list
            await self._run("relay update")
            # Connect
            await self.connect()
            return True
        self._log(f"[Mullvad] Login failed: {out[:120]}", level="error")
        return False

    async def connect(self) -> bool:
        """Connect to the VPN. Returns True if connected."""
        out, ok = await self._run("connect", timeout=30.0)
        if ok or "already connected" in out.lower():
            self._connected = True
            self._current_ip = await self._fetch_ip()
            self._log(f"[Mullvad] Connected — IP: {self._current_ip}")
            return True
        self._log(f"[Mullvad] Connect failed: {out[:100]}", level="warn")
        return False

    async def disconnect(self) -> bool:
        """Disconnect from the VPN."""
        out, ok = await self._run("disconnect")
        self._connected = False
        return ok

    async def rotate(self, country: str = "") -> str:
        """Rotate to a new IP address.

        If `country` is provided, use that specific country.
        Otherwise pick a random country from ROTATION_COUNTRIES.

        Returns the new public IP, or empty string on failure.
        """
        if not self._account:
            self._log("[Mullvad] Not logged in — cannot rotate", level="warn")
            return ""

        # Pick target location
        target = country or random.choice(ROTATION_COUNTRIES)
        old_ip = self._current_ip

        self._log(f"[Mullvad] Rotating IP → {target.upper()} (was {old_ip or 'unknown'})...")

        # Disconnect first
        await self.disconnect()
        await asyncio.sleep(1.0)

        # Set new relay location
        out, ok = await self._run(f"relay set location {target}")
        if not ok:
            self._log(f"[Mullvad] Failed to set relay location: {out[:100]}", level="warn")
            # Fall back to 'any' so we at least connect
            await self._run("relay set location any")

        # Reconnect
        connected = await self.connect()
        if not connected:
            self._log("[Mullvad] Could not reconnect after rotation", level="error")
            return ""

        new_ip = self._current_ip
        if new_ip and new_ip != old_ip:
            self._log(f"[Mullvad] Rotated: {old_ip or 'none'} → {new_ip} ({target.upper()})")
        else:
            self._log(f"[Mullvad] Connected to {target.upper()} — IP: {new_ip or 'unknown'}",
                      level="warn" if new_ip == old_ip else "info")

        return new_ip

    async def status(self) -> dict:
        """Get current VPN status details."""
        out, _ = await self._run("status")
        connected = "Connected" in out
        self._connected = connected

        # Extract relay info if connected
        relay = ""
        m = re.search(r"to\s+(\S+)", out)
        if m:
            relay = m.group(1)

        # Extract IP
        ip = ""
        m = re.search(r"IPv4:\s*([\d.]+)", out)
        if m:
            ip = m.group(1)
            self._current_ip = ip

        return {
            "connected": connected,
            "relay": relay,
            "ip": ip,
        }

    # ── Internal ──────────────────────────────────────────

    async def _run(self, cmd: str, timeout: float = VPN_CMD_TIMEOUT) -> tuple:
        """Run a Mullvad CLI command. Returns (stdout_stderr, success_bool)."""
        full_cmd = f"mullvad {cmd}"
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    full_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=timeout,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            out = (stdout + stderr).decode("utf-8", errors="replace").strip()
            ok = proc.returncode == 0 or "already" in out.lower() or "connected" in out.lower()
            return out, ok
        except asyncio.TimeoutError:
            return "command timed out", False
        except FileNotFoundError:
            return "mullvad CLI not found — install with: apt install mullvad-vpn", False
        except Exception as e:
            return str(e), False

    async def _fetch_ip(self) -> str:
        """Fetch current public IP via Mullvad's own endpoint."""
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    "curl -s https://am.i.mullvad.net/ip 2>/dev/null || curl -s https://api.ipify.org 2>/dev/null",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=10.0,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            ip = stdout.decode("utf-8", errors="replace").strip()
            if re.match(r"^[\d.]+$", ip) and len(ip) < 40:
                return ip
        except Exception:
            pass
        return ""
