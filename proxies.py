"""
proxies.py — free proxy fetcher, validator and rotating pool.

Fetches fresh free proxies from several public sources, validates them with a
fast HTTP check, and hands them out to the browser workers (one per browser,
rotated after each use).  Everything is best-effort: no proxy source failing
ever crashes the app.
"""
import asyncio
import random
import re
import time
from typing import List, Optional

import aiohttp

# ── Public free-proxy sources (raw text, ip:port or proto://ip:port) ──
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

_IPPORT_RE = re.compile(r"^([\d.]+):(\d+)$")
_PROTO_RE = re.compile(r"^(https?|socks4|socks5)://([\d.]+):(\d+)$")

# Check target: a lightweight always-up endpoint
CHECK_URL = "https://api.ipify.org"
CHECK_TIMEOUT = 6.0


def parse_proxy_list(text: str) -> List[str]:
    """Parse raw proxy text into 'proto://ip:port' strings (deduped)."""
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        proto = "http"
        m = _PROTO_RE.match(line)
        if m:
            proto = m.group(1)
            ip, port = m.group(2), m.group(3)
        else:
            # maybe 'ip:port' or 'ip:port:user:pass'
            parts = line.split(":")
            if len(parts) >= 2:
                ip, port = parts[0], parts[1]
            else:
                continue
        if not _IPPORT_RE.match(f"{ip}:{port}"):
            continue
        out[f"{proto}://{ip}:{port}"] = True
    return list(out.keys())


async def _fetch_one(url: str, session: aiohttp.ClientSession, timeout: float) -> List[str]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                text = await r.text()
                return parse_proxy_list(text)
    except Exception:
        pass
    return []


async def fetch_free_proxies(max_proxies: int = 400) -> List[str]:
    """Fetch proxies from all sources in parallel, return merged unique list."""
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        ) as session:
            results = await asyncio.gather(
                *[_fetch_one(url, session, 8.0) for url in PROXY_SOURCES],
                return_exceptions=True,
            )
    except Exception:
        return []

    seen: dict = {}
    for res in results:
        if isinstance(res, list):
            for p in res:
                seen[p] = True
    proxies = list(seen.keys())
    random.shuffle(proxies)
    return proxies[:max_proxies]


async def _check_one(proxy: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Return the proxy string if it works, else None."""
    try:
        async with session.get(
            CHECK_URL,
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT),
        ) as r:
            if r.status == 200:
                return proxy
    except Exception:
        pass
    return None


async def validate_proxies(proxies: List[str], max_workers: int = 60) -> List[str]:
    """Concurrently validate proxies, return the working ones."""
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
    working = [r for r in results if isinstance(r, str)]
    random.shuffle(working)
    return working


class ProxyPool:
    """Rotating pool of working proxies.

    - refresh(): fetch + validate a fresh batch (call at Start and periodically)
    - take():   return the least-recently-used working proxy (or None)
    - release(): mark a proxy as failed so it gets skipped
    """

    def __init__(self):
        self._proxies: List[str] = []
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

    def take(self) -> Optional[str]:
        if not self._proxies:
            return None
        # Pick the least-recently-used (and not failed) proxy
        now = time.time()
        candidates = [p for p in self._proxies if p not in self._failed]
        if not candidates:
            self._failed = set()
            candidates = list(self._proxies)
        candidates.sort(key=lambda p: self._used_at.get(p, 0))
        proxy = candidates[0]
        self._used_at[proxy] = now
        return proxy

    def release(self, proxy: Optional[str], ok: bool = True) -> None:
        if proxy is None:
            return
        if not ok:
            self._failed.add(proxy)
        self._used_at[proxy] = time.time()


# Module-level singleton pool
pool = ProxyPool()
