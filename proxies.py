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
import asyncio
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

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
        self.used_count = 0
        self.worked_count = 0
        self.failed_count = 0

    @property
    def count(self) -> int:
        return len(self._proxies)

    def stats(self) -> dict:
        return {
            "available": self.count,
            "fetched": self.fetched_count,
            "valid": self.valid_count,
            "used": self.used_count,
            "working": self.worked_count,
            "failed": self.failed_count,
            "last_refresh": self.last_refresh,
        }

    async def refresh(self) -> None:
        # Paid-only: the pool is fed solely by vaultproxies.com residential
        # sessions. Sessions are freshly issued (TTL ~10min) so slow online
        # validation is skipped; workers rotate on failure via pool.release().
        vault = vault_proxies()
        self.fetched_count = len(vault)
        # Vault sessions are freshly issued residential sessions — trusted as
        # valid. Bulk-testing 5000+ through one account rate-limits and marks
        # good proxies failed (pool collapsed to 1 valid). They self-verify
        # during real signups: release(ok=False) blacklists the dead ones.
        for pr in vault:
            pr["_valid"] = True
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
        # Proven-valid sessions first, then untested ones — dead ones are
        # already excluded by the _failed filter above.
        candidates.sort(key=lambda p: (
            0 if p.get("_valid") else 1,
            self._used_at.get(p.get("key"), 0),
        ))
        proxy = candidates[0]
        self._used_at[proxy.get("key")] = now
        self.used_count += 1
        return proxy

    def release(self, proxy: Optional[Dict[str, str]], ok: bool = True) -> None:
        if proxy is None:
            return
        if ok:
            self.worked_count += 1
        else:
            self.failed_count += 1
            self._failed.add(proxy.get("key"))

    async def probe(self, proxy: Optional[Dict[str, str]],
                    timeout: float = 3.0) -> bool:
        """Fast one-shot liveness check for a single session.

        Used by the worker BEFORE launching a browser so dead sessions are
        blacklisted in ~3s instead of burning an 8s goto + a browser launch.
        Mirrors validate_all()'s request shape (HTTPS through the proxy).
        """
        if not proxy or not proxy.get("host") or not proxy.get("port"):
            return False
        host, port = proxy.get("host"), proxy.get("port")
        if proxy.get("username"):
            purl = "http://{}:{}@{}:{}".format(
                proxy["username"], proxy["password"], host, port)
        else:
            purl = "http://{}:{}".format(host, port)
        import aiohttp
        try:
            conn = aiohttp.TCPConnector(ssl=False)
            timeout_obj = aiohttp.ClientTimeout(total=timeout)
            try:
                async with aiohttp.ClientSession(connector=conn,
                                                 timeout=timeout_obj) as s:
                    async with s.get("https://api.ipify.org", proxy=purl) as r:
                        return r.status == 200
            finally:
                await conn.close()
        except Exception:
            return False

    async def sweep(self, window: float = 10.0, concurrency: int = 500,
                    timeout: float = 6.0,
                    log: Optional[Callable] = None) -> dict:
        """Insanely-fast startup sweep: concurrently test EVERY loaded session
        (including freshly-issued vault ones) and blacklist the dead ones so
        only valid sessions are handed to workers.

        Bounded by `window` seconds — whatever wasn't tested by then stays
        available (it was previously trusted) and the worker's per-attempt
        probe covers it. Returns {tested, valid, failed, untested}.
        """
        stats = {"tested": 0, "valid": 0, "failed": 0, "untested": 0}
        targets = [p for p in self._proxies if p.get("key") not in self._failed]
        total = len(targets)
        if not targets:
            return stats

        import aiohttp
        deadline = time.monotonic() + window
        sem = asyncio.Semaphore(concurrency)
        conn = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency,
                                    ssl=False)
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        last_log = [0.0]

        def _purl(p: Dict[str, str]) -> str:
            if p.get("username"):
                return "http://{}:{}@{}:{}".format(
                    p["username"], p["password"], p["host"], p["port"])
            return "http://{}:{}".format(p["host"], p["port"])

        async def _one(proxy: Dict[str, str]) -> None:
            async with sem:
                if time.monotonic() >= deadline:
                    return  # window expired — leave the rest untested
                ok = False
                try:
                    async with aiohttp.ClientSession(
                            connector=conn, timeout=timeout_obj) as s:
                        async with s.get("https://api.ipify.org",
                                         proxy=_purl(proxy)) as r:
                            ok = r.status == 200
                except Exception:
                    ok = False
                if ok:
                    proxy["_valid"] = True
                    self._failed.discard(proxy.get("key"))
                    stats["valid"] += 1
                else:
                    proxy["_valid"] = False
                    self._failed.add(proxy.get("key"))
                    stats["failed"] += 1
                stats["tested"] += 1
                now = time.monotonic()
                if now - last_log[0] >= 2.0:
                    last_log[0] = now
                    if log:
                        log(f"[Proxy] Sweep {stats['tested']}/{total} tested — "
                            f"{stats['valid']} valid, {stats['failed']} dead")

        try:
            await asyncio.gather(*[_one(p) for p in targets],
                                 return_exceptions=True)
        finally:
            await conn.close()

        stats["untested"] = total - stats["tested"]
        self.valid_count = sum(1 for p in self._proxies if p.get("_valid"))
        return stats

    async def validate_all(self, concurrency: int = 30,
                           timeout: float = 8.0) -> int:
        """Live-test proxies with a quick HTTPS request through each one, so
        'valid' is a real measured number instead of an assumption.

        Resets valid_count to the count already proven working, then raises it
        as new proxies pass. Proxies that fail are added to _failed so take()
        stops handing them out. Only unvalidated or previously-failed proxies
        are retested on later passes (working ones are skipped for speed).
        """
        import aiohttp

        targets = [
            p for p in self._proxies
            if not p.get("vault")
            and (not p.get("_valid") or p.get("key") in self._failed)
        ]
        if not targets:
            # All trusted (vault) or already-valid — nothing to test.
            return self.valid_count

        self.valid_count = sum(1 for p in self._proxies if p.get("_valid"))
        sem = asyncio.Semaphore(concurrency)
        conn = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency,
                                    ssl=False)
        timeout_obj = aiohttp.ClientTimeout(total=timeout)

        async def _one(proxy: Dict[str, str]) -> bool:
            host, port = proxy.get("host"), proxy.get("port")
            if not host or not port:
                return False
            if proxy.get("username"):
                purl = "http://{}:{}@{}:{}".format(
                    proxy["username"], proxy["password"], host, port)
            else:
                purl = "http://{}:{}".format(host, port)
            try:
                async with aiohttp.ClientSession(
                        connector=conn, timeout=timeout_obj) as s:
                    async with s.get("https://api.ipify.org", proxy=purl) as r:
                        return r.status == 200
            except Exception:
                return False

        async def _guard(proxy: Dict[str, str]) -> bool:
            async with sem:
                ok = await _one(proxy)
            if ok:
                proxy["_valid"] = True
                self._failed.discard(proxy.get("key"))
                self.valid_count += 1
            else:
                proxy["_valid"] = False
                self._failed.add(proxy.get("key"))
            return ok

        try:
            await asyncio.gather(*[_guard(p) for p in targets],
                                 return_exceptions=True)
        finally:
            await conn.close()
        return self.valid_count


# Module-level singleton pool
pool = ProxyPool()
