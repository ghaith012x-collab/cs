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
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

# ── Public free-proxy sources (raw text) ──
# These are FREE and rotate automatically. Quality is low (most will fail
# validation, and Discord/Cloudflare blocks known datacenter IPs), but they
# cost nothing and the pool validates every proxy before use.
#
# ProxyScrape v4 — most reliable free source, returns raw ip:port per line.
# Proxifly — GitHub-hosted txt files updated every 5 min (CDN, no rate limit).
# PubProxy — REST API with format=txt for raw output.
PROXY_SOURCES = [
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=displayproxies&protocol=http&timeout=15000&limit=500",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks4/data.txt",
    "http://pubproxy.com/api/proxy?format=txt&type=http&limit=20",
]
LOCAL_PROXY_FILES = []  # extra proxy files (besides vaultproxies.txt)

# Residential proxy sessions file (gitignored). Format: one user:pass@host:port
# per line. Sessions from vaultproxies.com expire after their TTL — swap in
# fresh ones (just change the string after "-s-") without touching code.
VAULTPROXY_FILE = "vaultproxies.txt"

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


def _vault_proxy_urls() -> List[str]:
    """Residential proxy session URLs (user:pass@host:port).
    Priority: VAULTPROXY_URLS env -> composed from VAULTPROXY_HOST/PORT/
    USER_PREFIX/PASS/TTL + VAULTPROXY_SESSIONS (comma/newline list — just
    change the string after -s- when sessions rotate) -> vaultproxies.txt."""
    urls: List[str] = []
    env_urls = (os.environ.get("VAULTPROXY_URLS") or "").strip()
    if env_urls:
        urls += [u.strip() for u in re.split(r"[\n,;]+", env_urls) if u.strip()]
    host = (os.environ.get("VAULTPROXY_HOST") or "").strip()
    port = (os.environ.get("VAULTPROXY_PORT") or "80").strip()
    user_prefix = (os.environ.get("VAULTPROXY_USER_PREFIX") or "").strip()
    passwd = (os.environ.get("VAULTPROXY_PASS") or "").strip()
    sessions = (os.environ.get("VAULTPROXY_SESSIONS") or "").strip()
    if host and passwd and sessions:
        ttl = (os.environ.get("VAULTPROXY_TTL") or "600").strip()
        for s in re.split(r"[\s,;]+", sessions):
            if s:
                urls.append(f"{user_prefix}{s}-ttl-{ttl}:{passwd}@{host}:{port}")
    p = Path(__file__).resolve().parent / VAULTPROXY_FILE
    if p.exists():
        urls += [l.strip() for l in p.read_text(errors="ignore").splitlines()
                 if l.strip() and not l.startswith("#")]
    return urls


def vault_proxies() -> List[Dict[str, str]]:
    """Parsed vaultproxy sessions. Marked vault=True so the pool skips slow
    online validation — these are freshly issued residential sessions."""
    out: Dict[str, Dict[str, str]] = {}
    for line in _vault_proxy_urls():
        parsed = parse_proxy_list(line)
        if parsed:
            key = next(iter(parsed))
            pd = parsed[key]
            pd["vault"] = True
            out[key] = pd
    return list(out.values())


# ── Mullvad gateway proxy ────────────────────────────────────
# Run Mullvad on an external VPS with /dev/net/tun + root, expose it as a
# SOCKS5 proxy, then point the Railway container at it via MULLVAD_GATEWAY:
#   socks5://[user:pass@]host:port
# All bot browser traffic flows through the VPS's Mullvad tunnel. The VPS
# also runs a tiny rotate API (MULLVAD_GATEWAY_CONTROL on the app side) so
# the bot can request a fresh Mullvad server/IP before each attempt.
_MULLVAD_GATEWAY_RE = re.compile(
    r"^(socks5|http)://(?:([^:@/]+):([^@/]+)@)?([^:/]+):(\d+)$"
)


def mullvad_gateway() -> Optional[Dict[str, str]]:
    """Build the Mullvad gateway proxy dict from MULLVAD_GATEWAY env.
    Returns None when unset or malformed. Marked vault=True so the pool
    never runs slow online validation against our own gateway."""
    raw = (os.environ.get("MULLVAD_GATEWAY") or "").strip()
    if not raw:
        return None
    m = _MULLVAD_GATEWAY_RE.match(raw)
    if not m:
        return None
    proto, user, pw, host, port = m.groups()
    return {
        "key": raw,
        "proto": proto,
        "host": host,
        "port": port,
        "username": user or "",
        "password": pw or "",
        "vault": True,
        "mullvad": True,
    }


def configured() -> bool:
    """True when residential proxy sessions are available (vaultproxies.txt
    file or VAULTPROXY_* env vars). Used by app.py to decide whether the
    workers must ALWAYS use proxies (no TOR fallback)."""
    return bool(_vault_proxy_urls())


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
        self.gateway_proxy: Optional[Dict[str, str]] = None

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
        gw = mullvad_gateway()
        self.gateway_proxy = gw  # exposed so app.py can auto-rotate before each attempt
        if gw:
            # Gateway mode: ONLY the Mullvad gateway is used — the bot
            # rotates via the gateway control API instead of a proxy pool.
            self.fetched_count = 1
            self.valid_count = 1
            self._proxies = [gw]
            self._used_at = {}
            self._failed = set()
            self.last_refresh = time.time()
            return
        self.gateway_proxy = None
        vault = vault_proxies()
        fetched = await fetch_free_proxies(max_proxies=500)
        self.fetched_count = len(fetched) + len(vault)
        # Vault sessions are freshly issued (TTL ~10min) — skip slow ipify
        # validation for them; validate only the free/local-file ones.
        to_validate = [p for p in fetched if not p.get("vault")]
        working = await validate_proxies(to_validate, max_workers=80)
        self.valid_count = len(working) + len(vault)
        self._proxies = vault + working
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
        # The Mullvad gateway is the ONLY proxy in gateway mode - it must stay
        # available even after failed attempts (rotation happens on the VPS).
        if not ok and not proxy.get("mullvad"):
            self._failed.add(proxy.get("key"))


# Module-level singleton pool
pool = ProxyPool()
