"""
db.py — Postgres persistence for Eyes GEN.

Uses DATABASE_URL (env).  Auto-migrates on startup (CREATE TABLE IF NOT EXISTS).
Stores every generated account: email, username, password, FULL token, proxy
used and validation status.  Also validates tokens against the Discord API so
the dashboard can show a live "valid tokens" count.
"""
import asyncio
import os
from typing import Dict, List, Optional

import aiohttp
import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: Optional[asyncpg.Pool] = None
_db_ready = False


async def init_db() -> bool:
    """Connect + auto-migrate. Safe to call at startup; no-ops without URL."""
    global _pool, _db_ready
    if not DATABASE_URL:
        print("[DB] DATABASE_URL not set - token saving disabled", flush=True)
        _db_ready = False
        return False
    try:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id          SERIAL PRIMARY KEY,
                    email       TEXT,
                    username    TEXT,
                    password    TEXT,
                    token       TEXT,
                    status      TEXT DEFAULT 'pending',
                    proxy       TEXT,
                    worker_id   TEXT,
                    created_at  TIMESTAMPTZ DEFAULT now()
                );
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_accounts_status
                ON accounts (status);
            """)
        _db_ready = True
        print("[DB] Connected - accounts table ready", flush=True)
        return True
    except Exception as e:
        print(f"[DB] init failed: {e}", flush=True)
        _db_ready = False
        return False


def db_ok() -> bool:
    return _db_ready and _pool is not None


async def save_account(email: str, username: str, password: str,
                       token: str, proxy: str = "", worker_id: str = "") -> bool:
    """Persist a generated account. Never raises."""
    if not db_ok():
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO accounts (email, username, password, token,
                                      status, proxy, worker_id)
                VALUES ($1, $2, $3, $4, 'pending', $5, $6)
                """,
                email, username, password, token, proxy, worker_id,
            )
        return True
    except Exception as e:
        print(f"[DB] save_account error: {e}", flush=True)
        return False


async def list_accounts(limit: int = 200) -> List[dict]:
    if not db_ok():
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT email, username, password, token, status, proxy,
                          worker_id, created_at
                   FROM accounts ORDER BY id DESC LIMIT $1""",
                limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[DB] list_accounts error: {e}", flush=True)
        return []


async def update_account_status(token: str, status: str) -> None:
    if not db_ok():
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE accounts SET status = $1 WHERE token = $2",
                status, token,
            )
    except Exception:
        pass


async def validate_token(token: str) -> bool:
    """Check a Discord token against the API. 200 => valid."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://discord.com/api/v9/users/@me",
                headers={"Authorization": token},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                return r.status == 200
    except Exception:
        return False


async def validate_all_tokens(accounts: List[dict]) -> int:
    """Validate every account's token concurrently. Returns valid count."""
    if not accounts:
        return 0
    valid = 0
    sem = asyncio.Semaphore(20)

    async def _check(acc: dict) -> bool:
        if not acc.get("token"):
            return False
        async with sem:
            ok = await validate_token(acc["token"])
            if ok:
                return True
            await update_account_status(acc["token"], "invalid")
            return False

    results = await asyncio.gather(
        *[_check(a) for a in accounts], return_exceptions=True
    )
    for r in results:
        if r is True:
            valid += 1
    return valid
