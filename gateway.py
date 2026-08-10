#!/usr/bin/env python3
"""
gateway.py - Mullvad VPN gateway for EYES GEN.

Run this ON YOUR VPS (any Linux machine with /dev/net/tun + root that has
Mullvad installed). It exposes the Mullvad tunnel as:

  SOCKS5 proxy  on 0.0.0.0:1080   -> every connection goes out through Mullvad
  Control API   on 0.0.0.0:8081   -> GET /rotate (switch Mullvad server = new IP)
                                     GET /status  (connected? current IP?)

Then point your Railway bot at it with two env vars:
  MULLVAD_GATEWAY=socks5://[user:pass@]YOUR_VPS_IP:1080
  MULLVAD_GATEWAY_CONTROL=http://YOUR_VPS_IP:8081

Optional auth on the SOCKS5 proxy (set both to enable):
  GATEWAY_USER=myuser
  GATEWAY_PASS=mypass

Run (as root, with your 16-digit account number):
  sudo env MULLVAD_LOGIN=1234567890123456 python3 gateway.py

Or in the background:
  sudo nohup env MULLVAD_LOGIN=1234567890123456 python3 gateway.py > gateway.log 2>&1 &
"""
import argparse
import asyncio
import json
import os
import random
import re
import socket
import struct
import subprocess
import sys

# ── config ────────────────────────────────────────────────────
SOCKS_PORT = int(os.environ.get("GATEWAY_SOCKS_PORT", "1080"))
CTRL_PORT = int(os.environ.get("GATEWAY_CTRL_PORT", "8081"))
AUTH_USER = os.environ.get("GATEWAY_USER", "")
AUTH_PASS = os.environ.get("GATEWAY_PASS", "")
ACCOUNT = (os.environ.get("MULLVAD_LOGIN") or "").strip()

# Countries with many Mullvad servers - good rotation diversity.
COUNTRIES = [
    "us", "ca", "gb", "de", "fr", "nl", "se", "ch", "no", "dk",
    "fi", "at", "be", "it", "es", "pt", "ie", "pl", "cz", "ro",
    "jp", "sg", "hk", "au", "nz",
]

_state = {"ip": "", "country": "", "connected": False}


# ── Mullvad CLI helpers ───────────────────────────────────────

async def _run_cmd(cmd: str, timeout: float = 30.0) -> tuple:
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = (stdout + stderr).decode("utf-8", errors="replace").strip()
        return out, proc.returncode == 0
    except asyncio.TimeoutError:
        return "timed out", False
    except Exception as e:
        return str(e), False


async def _mullvad(cmd: str, timeout: float = 30.0) -> tuple:
    return await _run_cmd(f"mullvad {cmd}", timeout)


async def _current_ip() -> str:
    out, _ = await _run_cmd(
        "curl -s https://am.i.mullvad.net/ip 2>/dev/null || curl -s https://api.ipify.org 2>/dev/null",
        timeout=15,
    )
    if re.match(r"^[\d.]+$", out) and len(out) < 40:
        return out
    return ""


async def connect_once() -> bool:
    """Login (if needed) and connect to Mullvad. Returns True when connected."""
    if ACCOUNT:
        out, ok = await _mullvad("account get")
        if not ok:
            await _mullvad(f"account login {ACCOUNT}")
            await _mullvad("relay update")
    out, ok = await _mullvad("connect", timeout=45)
    ok = ok or "already connected" in out.lower()
    if ok:
        _state["connected"] = True
        _state["ip"] = await _current_ip()
    return ok


async def rotate() -> dict:
    """Switch to a random Mullvad country -> fresh IP."""
    country = random.choice(COUNTRIES)
    await _mullvad("disconnect")
    await asyncio.sleep(0.5)
    out, ok = await _mullvad(f"relay set location {country}")
    if not ok:
        await _mullvad("relay set location any")
    ok2 = await connect_once()
    if not ok2:
        return {"ok": False, "error": "connect failed after rotation",
                "country": country, "ip": ""}
    _state["country"] = country
    _state["ip"] = await _current_ip()
    print(f"[rotate] -> {country.upper()} IP={_state['ip']}", flush=True)
    return {"ok": True, "country": country, "ip": _state["ip"]}


# ── SOCKS5 proxy (pure asyncio, routes via the Mullvad tunnel) ─

async def _socks_negotiate(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    """SOCKS5 greeting. Returns True if the client is authenticated."""
    try:
        data = await asyncio.wait_for(reader.readexactly(2), timeout=10)
        ver, nmeth = data[0], data[1]
        if ver != 5:
            return False
        methods = await asyncio.wait_for(reader.readexactly(nmeth), timeout=10)
        if AUTH_USER or AUTH_PASS:
            if 2 not in methods:
                writer.write(b"\x05\xff")
                await writer.drain()
                return False
            writer.write(b"\x05\x02")  # we want user/pass auth
            await writer.drain()
            cred = await asyncio.wait_for(reader.readexactly(2), timeout=10)
            ulen = cred[1]
            uname = (await asyncio.wait_for(reader.readexactly(ulen), timeout=10)).decode()
            plen = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            passwd = (await asyncio.wait_for(reader.readexactly(plen), timeout=10)).decode()
            if uname != AUTH_USER or passwd != AUTH_PASS:
                writer.write(b"\x01\x01")
                await writer.drain()
                return False
            writer.write(b"\x01\x00")
            await writer.drain()
            return True
        else:
            if 0 not in methods:
                writer.write(b"\x05\xff")
                await writer.drain()
                return False
            writer.write(b"\x05\x00")
            await writer.drain()
            return True
    except Exception:
        return False


async def _socks_request(reader: asyncio.StreamReader) -> tuple:
    """Parse a SOCKS5 CONNECT request. Returns (host, port) or None."""
    try:
        head = await asyncio.wait_for(reader.readexactly(4), timeout=10)
        ver, cmd, rsv, atyp = head
        if ver != 5 or cmd != 1:  # only CONNECT
            return None
        if atyp == 1:  # IPv4
            host = socket.inet_ntoa(await asyncio.wait_for(reader.readexactly(4), timeout=10))
        elif atyp == 3:  # domain
            n = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            host = (await asyncio.wait_for(reader.readexactly(n), timeout=10)).decode()
        elif atyp == 4:  # IPv6
            host = socket.inet_ntop(socket.AF_INET6, await asyncio.wait_for(reader.readexactly(16), timeout=10))
        else:
            return None
        port = struct.unpack(">H", await asyncio.wait_for(reader.readexactly(2), timeout=10))[0]
        return host, port
    except Exception:
        return None


async def handle_socks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        if not await _socks_negotiate(reader, writer):
            writer.close()
            return
        target = await _socks_request(reader)
        if not target:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)  # unsupported
            await writer.drain()
            writer.close()
            return
        host, port = target
        try:
            remote_r, remote_w = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=15
            )
        except Exception:
            writer.write(b"\x05\x05\x00\x01" + b"\x00" * 6)  # connection refused
            await writer.drain()
            writer.close()
            return
        # success reply: version, rep=0, rsv, atyp=1, bnd.addr=0.0.0.0:0
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()

        async def _pipe(src, dst):
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            _pipe(reader, remote_w), _pipe(remote_r, writer), return_exceptions=True
        )
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ── Control HTTP API (tiny, no deps) ──────────────────────────

def _http_json(writer: asyncio.StreamWriter, obj: dict) -> None:
    body = json.dumps(obj).encode()
    head = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Access-Control-Allow-Origin: *\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
    )
    writer.write(head + body)


async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        parts = line.decode(errors="replace").split()
        path = parts[1] if len(parts) > 1 else "/"
        # drain the rest of the request
        try:
            while await reader.readline():
                pass
        except Exception:
            pass
        if path.startswith("/rotate"):
            res = await rotate()
            _http_json(writer, res)
        elif path.startswith("/status"):
            _state["connected"] = True
            _state["ip"] = await _current_ip()
            _http_json(writer, {
                "ok": True,
                "connected": bool(_state["connected"]),
                "ip": _state["ip"],
                "country": _state["country"],
            })
        else:
            _http_json(writer, {"ok": True, "service": "mullvad-gateway",
                                "endpoints": ["/rotate", "/status"]})
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ── main ──────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 52, flush=True)
    print("  Mullvad Gateway - SOCKS5 proxy + rotate API", flush=True)
    if not ACCOUNT:
        print("  [warn] MULLVAD_LOGIN not set - skipping auto-login", flush=True)
    print(f"  SOCKS5 :0.0.0.0:{SOCKS_PORT} (auth={'yes' if AUTH_USER else 'no'})", flush=True)
    print(f"  Control:0.0.0.0:{CTRL_PORT}  /rotate /status", flush=True)
    print("=" * 52, flush=True)

    if ACCOUNT:
        await connect_once()

    socks_srv = await asyncio.start_server(handle_socks, "0.0.0.0", SOCKS_PORT)
    ctrl_srv = await asyncio.start_server(handle_http, "0.0.0.0", CTRL_PORT)
    print(f"[ready] SOCKS5 on :{SOCKS_PORT}, control on :{CTRL_PORT}", flush=True)
    async with socks_srv, ctrl_srv:
        await asyncio.gather(socks_srv.serve_forever(), ctrl_srv.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
