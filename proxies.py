"""
proxies.py — rotating pool of PAID residential proxy sessions (vaultproxies.com).

Sessions come from vaultproxies.txt (user:pass@host:port per line) or the
VAULTPROXY_* env vars. Format support:
  user:pass@host:port        (authenticated proxy - vaultproxies.com)
  proto://user:pass@host:port
  host:port:user:pass        (BrightData ISP sticky-IP format)

The pool hands out proxy dicts so the browser worker can pass
username/password to Playwright separately.
"""
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

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


def configured() -> bool:
    """True when residential proxy sessions are available (vaultproxies.txt
    file or VAULTPROXY_* env vars). Used by app.py to decide whether the
    workers must ALWAYS use proxies (no TOR fallback)."""
    return bool(_vault_proxy_urls())


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
        # Paid-only: the pool is fed solely by vaultproxies.com residential
        # sessions. Sessions are freshly issued (TTL ~10min) so slow online
        # validation is skipped; workers rotate on failure via pool.release().
        vault = vault_proxies()
        self.fetched_count = len(vault)
        self.valid_count = len(vault)
        self._proxies = vault
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
