"""
proxies.py — free proxy fetcher, validator and rotating pool.

Supports four formats:
  ip:port
  proto://ip:port
  user:pass@host:port        (authenticated gateway - e.g. nullproxies.com)
  host:port:user:pass        (BrightData ISP sticky-IP format)

Every proxy may carry optional auth.  The pool hands out proxy dicts so the
browser worker can pass username/password to Playwright separately.
"""
import asyncio
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

# ── Public free-proxy sources (raw text) ──
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt",
    "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/protocols/http/data.txt",
    "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/protocols/socks5/data.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text",
]

# Local proxy files in the repo that should also be loaded (user uploads)
LOCAL_PROXY_FILES = [
    # Restricted ISP proxies — loaded first, given priority
    "ips-isp_proxy52026-08-08T18_08_53.473Z.txt",
    # Fallback ISP proxy pool
    "ips-isp_proxy12026-08-08T17_28_57.556Z.txt",
]

# Auth proxy format: user:pass@host:port
_AUTH_RE = re.compile(r"^([^:]+):([^@]+)@([^:]+):(\d+)$")
# BrightData sticky-IP format: host:port:user:pass
_BD_RE = re.compile(r"^([^:]+):(\d+):([^:]+):([^:]+)$")
# Simple: ip:port or host:port
_IPPORT_RE = re.compile(r"^([\d.]+):(\d+)$")
_HOSTPORT_RE = re.compile(r"^([a-zA-Z0-9.-]+):(\d+)$")
_PROTO_RE = re.compile(r"^(https?|socks4|socks5)://([^:]+):(\d+)$")

CHECK_URL = "https://api.ipify.org"
CHECK_TIMEOUT = 6.0


def normalize(proto: str, host: str, port: str,
              username: str = "", password: str = "") -> Dict[str, str]:
    """Build a proxy dict (stable key + playable fields)."""
    if username and password:
        key = f"{username}:{password}@{host}:{port}"
    else:
        key = f"{host}:{port}"
    return {
        "key": key,
        "proto": proto,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def parse_proxy_list(text: str) -> Dict[str, Dict[str, str]]:
    """Parse raw proxy text into {key: proxy_dict} (deduped)."""
    out: Dict[str, Dict[str, str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        proto = "http"
        m = _AUTH_RE.match(line)
        if m:
            username, password, host, port = m.groups()
            out[line] = normalize(proto, host, port, username, password)
            continue
        m = _BD_RE.match(line)
        if m:
            host, port, username, password = m.groups()
            out[line] = normalize(proto, host, port, username, password)
            continue
        m = _PROTO_RE.match(line)
        if m:
            proto, host, port = m.groups()
            out[line] = normalize(proto, host, port)
            continue
        if ":" in line:
            parts = line.split(":")
            if len(parts) >= 2:
                host, port = parts[0], parts[1]
                if _IPPORT_RE.match(f"{host}:{port}") or _HOSTPORT_RE.match(f"{host}:{port}"):
                    out[f"{host}:{port}"] = normalize(proto, host, port)
    return out


async def _fetch_one(url: str, session: aiohttp.ClientSession, timeout: float) -> Dict[str, Dict[str, str]]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                text = await r.text()
                return parse_proxy_list(text)
    except Exception:
        pass
    return {}


def _load_local_files() -> Dict[str, Dict[str, str]]:
    """Load proxy files committed in the repo (e.g. user-uploaded lists)."""
    out: Dict[str, Dict[str, str]] = {}
    for name in LOCAL_PROXY_FILES:
        path = Path(__file__).resolve().parent / name
        try:
            if path.exists():
                out.update(parse_proxy_list(path.read_text(errors="ignore")))
        except Exception:
            pass
    return out


async def fetch_free_proxies(max_proxies: int = 500) -> List[Dict[str, str]]:
    """Fetch proxies from all sources + local files, return unique proxy dicts."""
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        ) as session:
            results = await asyncio.gather(
                *[_fetch_one(url, session, 8.0) for url in PROXY_SOURCES],
                return_exceptions=True,
            )
    except Exception:
        results = []

    seen: Dict[str, Dict[str, str]] = _load_local_files()
    for res in results:
        if isinstance(res, dict):
            seen.update(res)
    proxies = list(seen.values())
    random.shuffle(proxies)
    return proxies[:max_proxies]


def proxy_url(p: Dict[str, str]) -> str:
    """Build a full proxy URL for aiohttp."""
    auth = f"{p['username']}:{p['password']}@" if p.get("username") else ""
    return f"{p['proto']}://{auth}{p['host']}:{p['port']}"


async def _check_one(p: Dict[str, str], session: aiohttp.ClientSession) -> Optional[Dict[str, str]]:
    try:
        async with session.get(
            CHECK_URL,
            proxy=proxy_url(p),
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT),
        ) as r:
            if r.status == 200:
                return p
    except Exception:
        pass
    return None


async def validate_proxies(proxies: List[Dict[str, str]], max_workers: int = 60) -> List[Dict[str, str]]:
    if not proxies:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            sem = asyncio.Semaphore(max_workers)

            async def _checked(p):
                async with sem:
                    return await _check_one(p, session)

            results = await asyncio.gather(
                *[_checked(p) for p in proxies], return_exceptions=True
            )
    except Exception:
        return []
    working = [r for r in results if isinstance(r, dict)]
    random.shuffle(working)
    return working


class ProxyPool:
    """Rotating pool of working proxies (with optional auth)."""

    def __init__(self):
        self._proxies: List[Dict[str, str]] = []
        self._used_at: dict = {}
        self._failed: set = set()
        self.last_refresh = 0.0
        self.fetched_count = 0
        self.valid_count = 0

    @property
    def count(self) -> int:
        return len(self._proxies)

    def stats(self) -> dict:
        return {
            "available": self.count,
            "fetched": self.fetched_count,
            "valid": self.valid_count,
            "failed": len(self._failed),
            "last_refresh": self.last_refresh,
        }

    async def refresh(self) -> None:
        fetched = await fetch_free_proxies(max_proxies=500)
        self.fetched_count = len(fetched)
        working = await validate_proxies(fetched, max_workers=80)
        self.valid_count = len(working)
        self._proxies = working
        self._used_at = {}
        self._failed = set()
        self.last_refresh = time.time()

    def take(self) -> Optional[Dict[str, str]]:
        if not self._proxies:
            return None
        now = time.time()
        candidates = [p for p in self._proxies if p.get("key") not in self._failed]
        if not candidates:
            self._failed = set()
            candidates = list(self._proxies)
        candidates.sort(key=lambda p: self._used_at.get(p.get("key"), 0))
        proxy = candidates[0]
        self._used_at[proxy.get("key")] = now
        return proxy

    def release(self, proxy: Optional[Dict[str, str]], ok: bool = True) -> None:
        if proxy is None:
            return
        if not ok:
            self._failed.add(proxy.get("key"))


# Module-level singleton pool
pool = ProxyPool()
