import asyncio
import base64
import json
import os
import random
import threading
import time
from typing import Dict, List, Optional

import aiohttp

from flask import Flask, jsonify, request, Response

try:
    import db
    _db_available = True
except ImportError:
    db = None
    _db_available = False
    print("[app] db.py not found - token saving disabled", flush=True)

try:
    from proxies import pool as proxy_pool, configured as _proxies_configured
    _proxies_available = True
except ImportError:
    proxy_pool = None
    _proxies_configured = lambda: False
    _proxies_available = False
    print("[app] proxies.py not found - direct connections only", flush=True)

# "force use the proxies no matter what" — when residential sessions are
# configured (vaultproxies.txt in the repo, or VAULTPROXY_* env) the workers
# NEVER fall back to TOR. Set PROXY_MODE=force to force even without a file.
PROXY_FORCE = (
    (os.environ.get("PROXY_MODE") or "").strip().lower()
    in ("force", "1", "true", "yes")
    or _proxies_configured()
)

from server import DiscordAutomation

# ── Global state (Flask thread + asyncio thread) ──

_loop: Optional[asyncio.AbstractEventLoop] = None
_running = False
_start_time = 0.0

# worker_id -> worker state
_workers: Dict[str, dict] = {}
WORKER_COUNT = 1
WORKER_IDS = [f"B{i+1}" for i in range(WORKER_COUNT)]

_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Discord-friendly cybertemp.xyz mail domains (from their public /api/domains:
# discord:true and status:active). Shown as the selectable database in the
# dashboard domain picker. mikerossy.com is the locked default.
# Domains that already triggered Discord phone verification are removed here
# (mikerossy.com, blobers.it.com, vibify.cc, vibeify.cc). The worker also
# burns a domain at runtime when a signup ends in phone verification, so it is
# never reused. DraxonMails (discord-friendly) is the primary mail source;
# this list is the fallback pool for cybertemp.
CYBERTEMP_DISCORD_DOMAINS = [
    "andrewslife.tattoo",
    "dianeplumber.mom",
    "mikethe.guru",
    "philipsllc.lol",
    "turkinster.us",
]

DEFAULT_CONFIG = {
    "headless": True,
    "web_port": 8080,
    "camera_interval": 3,
    "worker_count": WORKER_COUNT,
    "mail_domains": ["andrewslife.tattoo"],
    "custom_email": "",
}


def load_config(path: str = _config_path) -> dict:
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                saved = json.load(f)
                for key, value in DEFAULT_CONFIG.items():
                    if key in saved:
                        config[key] = saved[key]
        except Exception:
            pass
    config["web_port"] = int(os.environ.get("PORT", config.get("web_port", 8080)))
    return config


def save_config(config: dict, path: str = _config_path) -> None:
    try:
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass


# Shared ring buffer so app-level lines (proxy stats, worker outcomes) reach
# the web terminal, not just stdout.
_APP_LOGS: list = []

# Domains that got burned at runtime (phone verification on signup). Seeded
# from config.json and persisted back so burned domains stay burned.
# Already-tried domains (mikerossy.com, blobers.it.com, vibify.cc, vibeify.cc)
# are burned up front so a stale config.json can never revive them.
_BURNED_DOMAINS: set = {
    "mikerossy.com", "blobers.it.com", "vibify.cc", "vibeify.cc",
}


def _load_burned() -> None:
    global _BURNED_DOMAINS
    _BURNED_DOMAINS = {"mikerossy.com", "blobers.it.com", "vibify.cc", "vibeify.cc"}
    try:
        cfg = load_config()
        _BURNED_DOMAINS.update(
            str(d).strip().lower() for d in (cfg.get("burned_domains") or []))
    except Exception:
        pass


def _burn_domain(domain: str) -> None:
    """Permanently remove a domain from the pool after a phone-verification hit."""
    d = (domain or "").strip().lower()
    if not d:
        return
    _BURNED_DOMAINS.add(d)
    try:
        cfg = load_config()
        burned = [str(x).strip().lower() for x in (cfg.get("burned_domains") or [])]
        if d not in burned:
            burned.append(d)
        cfg["burned_domains"] = burned
        mail = [str(x).strip().lower() for x in (cfg.get("mail_domains") or [])]
        if d in mail:
            mail.remove(d)
        cfg["mail_domains"] = mail
        save_config(cfg)
    except Exception:
        pass


def _pick_domain(cfg: dict) -> str:
    """Pick a fresh, non-burned domain from the configured list (falls back to
    the full discord-friendly pool)."""
    pools = [
        [str(x).strip().lower() for x in (cfg.get("mail_domains") or []) if str(x).strip()],
        list(CYBERTEMP_DISCORD_DOMAINS),
    ]
    for pool in pools:
        fresh = [d for d in pool if d not in _BURNED_DOMAINS]
        if fresh:
            return random.choice(fresh)
    return random.choice(pools[1] or ["andrewslife.tattoo"])


def _log(msg: str, level: str = "info"):
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "timestamp": time.time(),
        "level": level,
        "message": msg,
    }
    _APP_LOGS.append(entry)
    if len(_APP_LOGS) > 400:
        _APP_LOGS[:] = _APP_LOGS[-400:]
    print(f"[{level.upper()}] {msg}", flush=True)


# ── Worker management (runs in the asyncio thread) ──

def _init_worker(wid: str) -> dict:
    return {
        "id": wid,
        "bot": None,
        "status": "idle",          # idle | starting | running | done | error
        "step": "",
        "email": "",
        "username": "",
        "password": "",
        "token": "",
        "proxy": "",
        "started_at": 0,
        "finished_at": 0,
        "screenshots": 0,
        "last_shot_b64": "",
    }


async def _worker_capture_loop(wid: str, cfg: dict, stagger: int) -> None:
    """Capture screenshots for this worker, staggered so browsers don't all
    upload at the same time: B1 immediately, B2 after 2s, B3 after 4s..."""
    bot: DiscordAutomation = _workers[wid]["bot"]
    base = max(1, int(cfg.get("camera_interval", 3)))
    # One image every base seconds across ALL browsers: each worker waits
    # base * len(WORKER_IDS) between its own uploads, staggered base apart.
    interval = base * len(WORKER_IDS)
    await asyncio.sleep(stagger)
    while _running and bot is not None and bot._page is not None:
        try:
            shot = await bot.capture_screenshot()
            if shot:
                _workers[wid]["last_shot_b64"] = shot
                _workers[wid]["screenshots"] += 1
        except Exception:
            pass
        await asyncio.sleep(interval)


async def _next_proxy(force: bool = False):
    """Grab the least-recently-used proxy session, or None (auto mode only —
    the caller may fall back to TOR). In force mode the pool is refreshed on
    demand so proxies are ALWAYS used and the bot never silently goes TOR."""
    if not (_proxies_available and proxy_pool is not None):
        return None
    if proxy_pool.count == 0:
        try:
            await proxy_pool.refresh()
        except Exception:
            pass
    if proxy_pool.count > 0:
        return proxy_pool.take()
    return None


def _proxy_stats_line(wid: str) -> None:
    """Log live proxy usage counters (used / working / failed) for the terminal."""
    try:
        s = proxy_pool.stats() if proxy_pool is not None else {}
    except Exception:
        return
    _log(
        f"[{wid}] [Proxy] Used {s.get('used', 0)} sessions, "
        f"Working {s.get('working', 0)}, Failed {s.get('failed', 0)}"
    )


async def _run_worker(wid: str, cfg: dict, proxy=None) -> None:
    """Worker loop: one proxy session per signup attempt, rotating on failure.
    Falls back to proxy pool + TOR as needed."""
    state = _workers[wid]
    state["status"] = "starting"
    state["started_at"] = time.time()
    max_tries = 30 if PROXY_FORCE else 12

    bot = None
    consecutive_tunnel_fails = 0  # fast-fail after consecutive dead connections

    for attempt in range(max_tries):
        if not _running:
            state["status"] = "stopped"
            if bot: await bot.close()
            return

        # ── Pick a session for this attempt (never TOR in force mode) ──
        if proxy is None:
            proxy = await _next_proxy(force=PROXY_FORCE)
        if PROXY_FORCE and proxy is None:
            _log(f"[{wid}] [Proxy] No proxy sessions (forced mode) — refreshing and waiting...", level="warn")
            state["proxy"] = "waiting-for-proxy"
            await asyncio.sleep(5)
            continue
        state["proxy"] = proxy.get("key", "tor") if proxy else "tor"

        label = state["proxy"]

        # ── Launch or reuse browser (fresh domain each attempt) ──
        domain = _pick_domain(cfg)
        if bot is None:
            bot = DiscordAutomation(
                headless=cfg.get("headless", True),
                proxy=proxy,  # dict = sticky session; None = TOR in _build_context
                worker_id=wid,
                domain=domain,
                email=cfg.get("custom_email") or "",
            )
            state["bot"] = bot
            try:
                await bot.initialize()
            except Exception as e:
                state["status"] = "error"
                _log(f"[{wid}] Browser launch failed: {e}", level="error")
                if proxy:
                    proxy_pool.release(proxy, ok=False)
                    proxy = None
                await asyncio.sleep(3)
                continue
        else:
            # Reuse browser: rotate to a fresh session / TOR circuit
            # Custom email (if set) always wins; otherwise blank = fresh inbox.
            bot._email = cfg.get("custom_email") or ""
            bot._domain = domain
            bot.phone_verify_detected = False
            if not await bot.switch_proxy(proxy):
                _log(f"[{wid}] Context rebuild failed", level="warn")
                if proxy:
                    proxy_pool.release(proxy, ok=False)
                    proxy = None
                await asyncio.sleep(2)
                continue

        # ── Run signup ──
        try:
            state["status"] = "running"
            stagger = int(wid[1:]) - 1
            cam_task = asyncio.create_task(_worker_capture_loop(wid, cfg, stagger * int(cfg.get("camera_interval", 3))))
            ok = await bot.start_discord_signup()
            cam_task.cancel()

            # ── Phone verification hit → burn this domain + rotate everything ──
            if not ok and getattr(bot, "phone_verify_detected", False):
                _burn_domain(domain)
                _log(f"[{wid}] [Phone] Domain {domain} burned - proxy+fingerprint+domain will rotate", level="warn")

            # ── Clean up temp-mail session between attempts to prevent
            # aiohttp connector leaks (each failed attempt creates a new
            # cybertemp inbox that must be closed).
            if bot._mail is not None:
                try:
                    await bot._mail.close()
                except Exception:
                    pass
                bot._mail = None

            acc = bot.get_account()
            state["email"] = acc["email"]
            state["username"] = acc["username"]
            state["password"] = acc["password"]
            state["token"] = acc["token"]
            if ok and acc["token"]:
                state["status"] = "done"
                if _db_available and db is not None:
                    await db.save_account(
                        email=acc["email"], username=acc["username"],
                        password=acc["password"], token=acc["token"],
                        proxy=label, worker_id=wid,
                        user_id=acc.get("user_id", ""),
                        avatar=acc.get("avatar", ""),
                        bio=acc.get("bio", ""),
                        humanized=bool(acc.get("humanized")),
                    )
                _log(f"[{wid}] Done - token {len(acc['token'])} chars ({label})")
                if proxy:
                    proxy_pool.release(proxy, ok=True)
                _proxy_stats_line(wid)
                if bot: await bot.close()
                return
            elif ok:
                state["status"] = "done"
                _log(f"[{wid}] Signup ok (no token yet)")
                if bot: await bot.close()
                return

            # ── Track consecutive tunnel failures ──
            err_tag = state.get("proxy", "").lower()
            if not ok and any(k in err_tag for k in ("tor", "proxy")):
                consecutive_tunnel_fails += 1
                if consecutive_tunnel_fails >= 4:
                    _log(f"[{wid}] {consecutive_tunnel_fails} consecutive tunnel failures — aborting (all sessions appear dead)", level="error")
                    break
            else:
                consecutive_tunnel_fails = 0

            _log(f"[{wid}] Failed (attempt {attempt+1}/{max_tries}, {label})", level="warn")
        except Exception as e:
            state["status"] = "error"
            _log(f"[{wid}] error: {e}")
        # Session failed — release it so the next attempt rotates to a new one
        if proxy:
            proxy_pool.release(proxy, ok=False)
            proxy = None
        _proxy_stats_line(wid)
        await asyncio.sleep(2)

    state["status"] = "error"
    state["step"] = "retries exhausted - all proxy/TOR attempts failed"
    if bot:
        await bot.close()
        state["bot"] = None

async def _proxy_validate_loop() -> None:
    """Re-test proxies in the background so 'valid' is a real number (not an
    assumption) and dead sessions stop being handed to workers. Retests only
    proxies that never passed or previously failed; runs every 3 minutes."""
    while True:
        try:
            if _proxies_available and proxy_pool is not None and proxy_pool.count:
                n = await proxy_pool.validate_all()
                _log(f"[Proxy] Live validation: {n} working of {proxy_pool.count} loaded")
        except Exception as e:
            _log(f"[Proxy] validation error: {e}", level="warn")
        await asyncio.sleep(180)


async def _start_all_async(cfg: dict) -> None:
    global _running, _start_time
    if _running:
        return
    _running = True
    _start_time = time.time()

    for wid in WORKER_IDS:
        _workers[wid] = _init_worker(wid)

    # ── Proxy pool: load free + residential sessions (retry a few times) ──
    n_sessions = 0
    try:
        if _proxies_available and proxy_pool is not None:
            for _r in range(3):
                await proxy_pool.refresh()
                n_sessions = proxy_pool.count
                if n_sessions:
                    break
                await asyncio.sleep(2)
    except Exception as e:
        _log(f"[Proxy] pool refresh error: {e}", level="warn")
    if n_sessions:
        _log(f"[Proxy] {n_sessions} proxy sessions loaded — one IP per account (forced mode)" if PROXY_FORCE
             else f"[Proxy] {n_sessions} proxy sessions loaded — one IP per account")
    elif PROXY_FORCE:
        _log("[Proxy] [ERROR] PROXY FORCE MODE but 0 sessions loaded — workers will keep retrying, TOR is DISABLED", level="error")
    else:
        _log("[Proxy] No proxy sessions — TOR-only fallback (fresh circuit per attempt)")

    if n_sessions and _proxies_available and proxy_pool is not None:
        asyncio.create_task(_proxy_validate_loop())

    for i, wid in enumerate(WORKER_IDS):
        _log(f"[{wid}] Starting worker...")
        asyncio.create_task(_run_worker(wid, cfg, None))


async def _stop_all_async() -> None:
    global _running
    _running = False
    for wid, state in list(_workers.items()):
        bot = state.get("bot")
        if bot is not None:
            try:
                await bot.close()
            except Exception:
                pass
            state["bot"] = None
        if state["status"] in ("starting", "running"):
            state["status"] = "stopped"
    _log("[App] All workers stopped")


def _run_in_loop(coro) -> Optional[object]:
    if not _loop:
        _log("[Loop] Event loop not running!", level="error")
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, _loop)
        return fut.result(timeout=120)
    except Exception as e:
        _log(f"[Loop] Error running coroutine: {e}", level="error")
        import traceback
        traceback.print_exc()
        return None


# ── Flask app ─────────────────────────────────────────────

app = Flask(__name__)


@app.route('/')
def handle_root():
    return Response(DASHBOARD_HTML, content_type='text/html')


@app.route('/start', methods=['POST'])
def handle_start():
    global _workers
    if _running:
        return jsonify({"ok": False, "msg": "Already running"})
    try:
        cfg = load_config()
        threading.Thread(
            target=lambda: _run_in_loop(_start_all_async(cfg)),
            daemon=True,
        ).start()
        return jsonify({"ok": True, "msg": "Started — 1 browser launching"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Start error: {e}"})


@app.route('/stop', methods=['POST'])
def handle_stop():
    _run_in_loop(_stop_all_async())
    return "Stopped"


@app.route('/proxies/refresh', methods=['POST'])
def handle_proxy_refresh():
    if _proxies_available and proxy_pool is not None:
        _run_in_loop(proxy_pool.refresh())
        return jsonify(proxy_pool.stats())
    return jsonify({"error": "proxies module not loaded"})

@app.route('/status')
def handle_status():
    workers = []
    for wid in WORKER_IDS:
        s = _workers.get(wid) or _init_worker(wid)
        workers.append({
            "id": wid,
            "status": s["status"],
            "step": s["step"],
            "email": s["email"],
            "username": s["username"],
            "token": s["token"],
            "proxy": s["proxy"],
            "screenshots": s["screenshots"],
            "started_at": s["started_at"],
        })
    try:
        cfg_now = load_config()
        _mail_domains = cfg_now.get("mail_domains", []) or []
    except Exception:
        _mail_domains = []
    return jsonify({
        "running": _running,
        "uptime": int(time.time() - _start_time) if _start_time else 0,
        "workers": workers,
        "mail_domains": _mail_domains,
        "custom_email": cfg_now.get("custom_email", ""),
        "proxies": proxy_pool.stats() if (_proxies_available and proxy_pool is not None) else {},
    })


@app.route('/latest')
def handle_latest_screenshot():
    wid = request.args.get("worker", "B1")
    s = _workers.get(wid)
    if s and s.get("last_shot_b64"):
        try:
            return Response(base64.b64decode(s["last_shot_b64"]),
                            content_type='image/png')
        except Exception:
            pass
    return Response(status=404)


@app.route('/worker/<wid>/logs')
def handle_worker_logs(wid):
    s = _workers.get(wid)
    if not s:
        return jsonify({"logs": [], "status": "unknown"})
    bot = s.get("bot")
    bot_logs = bot.get_activity_log() if bot else []
    # Merge app-level lines ([Proxy] stats, [B1] Done/Failed, errors) with the
    # bot's internal activity log so the terminal shows everything.
    merged = list(bot_logs)
    seen = {(e.get("time"), e.get("message")) for e in bot_logs}
    for e in _APP_LOGS:
        k = (e.get("time"), e.get("message"))
        if k not in seen:
            seen.add(k)
            merged.append(dict(e))
    merged.sort(key=lambda e: e.get("timestamp", 0))
    return jsonify({
        "id": wid,
        "status": s["status"],
        "email": s.get("email", ""),
        "username": s.get("username", ""),
        "proxy": s.get("proxy", ""),
        "screenshots": s.get("screenshots", 0),
        "started_at": s.get("started_at", 0),
        "logs": merged[-200:],  # last 200 entries
    })


@app.route('/tokens')
def handle_tokens():
    if not _db_available or db is None:
        return jsonify({"count": 0, "valid": 0, "expired": 0, "pending": 0,
                        "accounts": [], "stats": {"total": 0, "valid": 0,
                        "expired": 0, "pending": 0}, "error": "DB not available"})
    accounts = _run_in_loop(db.list_accounts(limit=500)) or []
    expired = sum(1 for a in accounts if a.get("status") == "invalid")
    valid = sum(1 for a in accounts if a.get("status") == "valid")
    pending = len(accounts) - expired - valid
    return jsonify({
        "count": len(accounts),
        "valid": valid,
        "expired": expired,
        "pending": pending,
        "accounts": accounts,
        "stats": {"total": len(accounts), "valid": valid,
                   "expired": expired, "pending": pending},
    })


@app.route('/validate', methods=['POST'])
def handle_validate():
    if not _db_available or db is None:
        return jsonify({"error": "DB not available"})
    # Cap at 200 so the synchronous validate stays inside the 120s loop budget
    accounts = _run_in_loop(db.list_accounts(limit=200)) or []
    valid = _run_in_loop(db.validate_all_tokens(accounts)) if accounts else 0
    accounts = _run_in_loop(db.list_accounts(limit=200)) or []
    expired = sum(1 for a in accounts if a.get("status") == "invalid")
    return jsonify({"count": len(accounts), "valid": valid, "expired": expired,
                    "accounts": accounts})


@app.route('/export', methods=['POST'])
def handle_export():
    """Preview the next N accounts for export (does NOT delete)."""
    if not _db_available or db is None:
        return jsonify({"error": "DB not available"})
    data = request.get_json(silent=True) or {}
    try:
        count = max(1, min(int(data.get('count', 5)), 100))
    except Exception:
        count = 5
    mode = 'full' if data.get('mode') == 'full' else 'tokens'
    accounts = _run_in_loop(db.list_accounts(limit=500)) or []
    chosen = [a for a in accounts if a.get('token')][:count]
    out = []
    for a in chosen:
        if mode == 'full':
            if mode == 'full':
                text = "\n".join([
                    a.get('token') or '',
                    "Email: " + str(a.get('email') or ''),
                    "Password: " + str(a.get('password') or ''),
                    "Username: " + str(a.get('username') or ''),
                ])
        else:
            text = a.get('token') or ''
        out.append({
            "id": a.get("id"),
            "text": text,
            "token": a.get('token'),
            "email": a.get('email'),
            "username": a.get('username'),
        })
    return jsonify({"count": len(out), "accounts": out})


@app.route('/export/delete', methods=['POST'])
def handle_export_delete():
    """Delete exported accounts after the user confirms the copy."""
    if not _db_available or db is None:
        return jsonify({"error": "DB not available"})
    data = request.get_json(silent=True) or {}
    ids = []
    for i in (data.get('ids') or []):
        try:
            ids.append(int(i))
        except Exception:
            pass
    if not ids:
        return jsonify({"ok": False, "msg": "no ids"})
    deleted = _run_in_loop(db.delete_accounts(ids)) or 0
    return jsonify({"ok": True, "deleted": deleted})


@app.route('/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        cfg = load_config()
        if 'headless' in data:
            cfg['headless'] = bool(data['headless'])
        if 'worker_count' in data:
            cfg['worker_count'] = int(data['worker_count'])
        if 'mail_domains' in data:
            domains = [str(d).strip().lower() for d in data['mail_domains'] if str(d).strip()]
            cfg['mail_domains'] = domains or ["andrewslife.tattoo"]
        if 'custom_email' in data:
            cfg['custom_email'] = str(data.get('custom_email') or '').strip().lower()
        save_config(cfg)
        return jsonify({"ok": True, "config": cfg})
    cfg = load_config()
    avail = [d for d in CYBERTEMP_DISCORD_DOMAINS if d not in _BURNED_DOMAINS]
    return jsonify({"headless": cfg.get("headless", True),
                    "worker_count": cfg.get("worker_count", WORKER_COUNT),
                    "mail_domains": cfg.get("mail_domains", ["andrewslife.tattoo"]),
                    "custom_email": cfg.get("custom_email", ""),
                    "burned_domains": sorted(_BURNED_DOMAINS),
                    "available_domains": avail})


# ── Background event loop ─────────────────────────────────

def _run_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    global _loop
    _load_burned()
    config = load_config()
    web_port = config.get("web_port", 8080)

    if not os.path.exists(_config_path):
        save_config(config)

    _loop = asyncio.new_event_loop()
    t = threading.Thread(target=_run_event_loop, args=(_loop,), daemon=True)
    t.start()

    # Auto-migrate DB (DATABASE_URL from env)
    if _db_available and db is not None:
        _run_in_loop(db.init_db())

    print("=" * 56, flush=True)
    print("  EYES GEN - multi-browser Discord token generator", flush=True)
    print(f"  Browsers per Start: {WORKER_COUNT}", flush=True)
    print(f"  Dashboard: http://0.0.0.0:{web_port}", flush=True)
    print("=" * 56, flush=True)

    app.run(host='0.0.0.0', port=web_port, debug=False,
            use_reloader=False, threaded=True)


# ═══════════════════════════════════════════════════════════
# EYES GEN DASHBOARD — mobile-first
# ═══════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a0a0b">
<meta charset="utf-8">
<title>EY3 - Token Forge</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Space+Grotesk:wght@400;500;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#0a0a0b;--panel:#131316;--panel2:#1a1a1e;--line:#26262b;--line2:#34343a;
  --txt:#e7e7ea;--dim:#8a8a92;--dim2:#5c5c64;
  --ok:#34d399;--bad:#f87171;--warn:#fbbf24;
}
html{background:var(--bg)}
body{font-family:'Space Grotesk',-apple-system,'Segoe UI',Roboto,sans-serif;
  background:
    radial-gradient(900px 400px at 85% -10%, rgba(255,255,255,.045), transparent 60%),
    radial-gradient(700px 380px at -10% 0%, rgba(255,255,255,.03), transparent 55%),
    var(--bg);
  color:var(--txt);min-height:100vh;max-width:980px;margin:0 auto;padding:18px 16px 90px}
h1{font-family:'JetBrains Mono',monospace;font-size:30px;font-weight:700;letter-spacing:2px;
  display:flex;align-items:center;gap:12px}
h1 .tag{font-size:11px;font-weight:500;letter-spacing:2px;color:var(--dim2);
  border:1px solid var(--line);border-radius:99px;padding:4px 10px;background:var(--panel)}
.sub{color:var(--dim);font-size:12px;margin:4px 0 16px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dim2);display:inline-block;
  box-shadow:0 0 10px currentColor}
.dot.on{background:var(--ok);color:var(--ok)}
.dot.err{background:var(--bad);color:var(--bad)}
nav{display:flex;gap:6px;margin-bottom:18px;position:sticky;top:0;z-index:40;
  background:rgba(10,10,11,.92);backdrop-filter:blur(10px);padding:10px 0;border-bottom:1px solid var(--line)}
nav button{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;letter-spacing:2px;
  padding:10px 22px;border-radius:10px;border:1px solid var(--line);background:var(--panel);
  color:var(--dim);cursor:pointer;transition:all .15s}
nav button.on{background:#e7e7ea;color:#0a0a0b;border-color:#e7e7ea}
nav button:not(.on):hover{border-color:var(--line2);color:var(--txt)}
.tab{display:none}.tab.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:14px}
.card h3{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:2px;color:var(--dim);
  text-transform:uppercase;margin-bottom:12px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 12px;text-align:center}
.stat .num{font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:700}
.stat .lbl{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);margin-top:4px}
.num.g{color:var(--ok)}.num.r{color:var(--bad)}.num.a{color:var(--txt)}
button{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;letter-spacing:1.5px;
  padding:11px 18px;border-radius:10px;border:1px solid var(--line2);background:var(--panel2);
  color:var(--txt);cursor:pointer;transition:all .15s}
button:active{transform:scale(.97)}
button.primary{background:#e7e7ea;color:#0a0a0b;border-color:#e7e7ea}
button.danger{background:#2a1212;color:#fca5a5;border-color:#5a2323}
button.ok{background:#0f2e24;color:#6ee7b7;border-color:#1d4a3a}
button:disabled{opacity:.45;cursor:not-allowed}
.btnrow{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.btnrow .grow{flex:1;min-width:120px}
.badge{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;padding:3px 9px;border-radius:99px;letter-spacing:.5px;white-space:nowrap}
.b-pending{background:#3a2c12;color:var(--warn)}
.b-valid{background:#0f2e24;color:var(--ok)}
.b-invalid{background:#3a1212;color:var(--bad)}
.b-dim{background:#1e1e22;color:var(--dim)}
.dom{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.dom input{flex:1;font-family:'JetBrains Mono',monospace;font-size:13px;background:var(--panel2);
  border:1px solid var(--line);border-radius:9px;color:var(--txt);padding:10px 12px;outline:none}
.dom input:focus{border-color:var(--dim)}
.dom .x{background:none;border:none;color:var(--dim);font-size:16px;padding:4px 8px;cursor:pointer}
.dom .x:hover{color:var(--bad)}
.pick{display:flex;flex-wrap:wrap;gap:8px}
.chip{font-size:11px;letter-spacing:.5px;padding:8px 13px;border-radius:99px;background:var(--panel2);color:var(--dim);border:1px solid var(--line2);cursor:pointer}
.chip.on{background:#e7e7ea;color:#0a0a0b;border-color:#e7e7ea}
.hint{color:var(--dim2);font-size:11px;margin-top:8px}
.tog{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--dim)}
.sw{width:44px;height:24px;border-radius:99px;background:var(--line2);position:relative;cursor:pointer;transition:.2s}
.sw::after{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.2s}
.sw.on{background:var(--dim)}.sw.on::after{left:23px}
.term{background:#050506;border:1px solid var(--line);border-radius:14px;overflow:hidden;
  font-family:'JetBrains Mono',monospace;box-shadow:0 18px 50px rgba(0,0,0,.5)}
.term-head{display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--panel2);
  border-bottom:1px solid var(--line);font-size:11px;letter-spacing:1px;color:var(--dim)}
.term-head .cd{width:10px;height:10px;border-radius:50%;background:#3a3a40;display:inline-block}
.term-head .cd.r{background:var(--bad)}.term-head .cd.y{background:var(--warn)}.term-head .cd.g{background:var(--ok)}
.term-head .t{flex:1;text-align:center;letter-spacing:3px;color:var(--dim2)}
.pxline{padding:7px 14px;background:#0a0a0c;border-bottom:1px solid var(--line);font-size:11px;letter-spacing:.5px;color:var(--dim2)}
.chk{display:inline-flex;align-items:center;gap:6px;font-size:11px;letter-spacing:1px;color:var(--dim2);cursor:pointer;user-select:none}
.chk input{accent-color:var(--ok)}
.term-body{height:430px;overflow-y:auto;padding:14px;font-size:12px;line-height:1.65}
.tl{display:flex;gap:10px;white-space:pre-wrap;word-break:break-word;padding:2px 0;border-bottom:1px solid rgba(38,38,43,.25)}
.tl .tt{color:var(--dim2);min-width:58px}
.tl.info .tm{color:#c9c9cf}
.tl.ok .tm{color:#6ee7b7}
.tl.warn .tm{color:var(--warn)}
.tl.error .tm{color:var(--bad)}
.acc{display:flex;align-items:center;gap:12px;background:var(--panel2);border:1px solid var(--line);
  border-radius:12px;padding:11px 12px;margin-bottom:8px}
.av{width:42px;height:42px;border-radius:50%;object-fit:cover;background:#222;flex:none;
  border:1px solid var(--line2)}
.av.ph{display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;
  font-weight:700;color:var(--dim);font-size:14px}
.meta{flex:1;min-width:0}
.meta .u{font-weight:700;font-size:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.meta .u .id{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--dim2)}
.meta .e{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--dim);margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta .b{font-size:10px;color:var(--dim2);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tok{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:#9fb0c8;word-break:break-all;
  background:#07080b;border:1px solid var(--line);border-radius:8px;padding:7px 9px;margin-top:6px}
.acc .acts{display:flex;gap:6px;flex:none}
.acc .acts button{font-size:9.5px;padding:7px 9px;letter-spacing:1px}
.empty{color:var(--dim2);text-align:center;padding:34px 0;font-size:13px;font-family:'JetBrains Mono',monospace}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:90;
  justify-content:center;align-items:center;padding:16px}
.overlay.on{display:flex}
.modal{background:var(--panel);border:1px solid var(--line2);border-radius:16px;width:94vw;max-width:560px;
  max-height:86vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 30px 80px rgba(0,0,0,.6)}
.modal-head{display:flex;justify-content:space-between;align-items:center;padding:15px 18px;
  border-bottom:1px solid var(--line)}
.modal-head h2{font-family:'JetBrains Mono',monospace;font-size:15px;letter-spacing:2px}
.modal-close{background:none;border:none;color:var(--dim);font-size:22px;cursor:pointer;padding:0 4px}
.modal-body{padding:16px 18px;overflow-y:auto;flex:1}
label{display:block;font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:1.5px;
  color:var(--dim);margin-bottom:6px}
input[type=number],input[type=text]{font-family:'JetBrains Mono',monospace;font-size:15px;
  background:var(--panel2);border:1px solid var(--line2);border-radius:9px;color:var(--txt);
  padding:11px 12px;outline:none;width:100%}
input:focus{border-color:var(--dim)}
.rad{display:flex;gap:16px;margin:14px 0}
.rad label{display:flex;align-items:center;gap:8px;color:var(--txt);font-size:13px;cursor:pointer;margin:0}
.exp{background:#050506;border:1px solid var(--line);border-radius:10px;padding:12px;margin:12px 0;
  font-size:11px;line-height:1.6;max-height:240px;overflow-y:auto;color:#9fb0c8;white-space:pre-wrap;
  word-break:break-all;display:none}
#viewImg{width:100%;border-radius:10px;border:1px solid var(--line);background:#000;min-height:240px;object-fit:contain}
.vph{display:flex;align-items:center;justify-content:center;min-height:240px;color:var(--dim2);
  font-family:'JetBrains Mono',monospace;font-size:12px;border:1px dashed var(--line);border-radius:10px}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);z-index:120;
  background:#e7e7ea;color:#0a0a0b;font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;
  padding:10px 18px;border-radius:99px;opacity:0;pointer-events:none;transition:opacity .25s}
.toast.on{opacity:1}
.footer{color:#3a3a40;font-size:10px;text-align:center;margin-top:20px;font-family:'JetBrains Mono',monospace;letter-spacing:1px}
@media(max-width:600px){.stats{gap:6px}.stat{padding:10px 6px}.stat .num{font-size:20px}
  .acc{flex-wrap:wrap}.acc .acts{width:100%;justify-content:flex-end}}
</style></head><body>

<h1>EY3<span class="tag">TOKEN FORGE</span></h1>
<div class="sub"><span class="dot" id="dDot"></span> <span id="stLine">idle</span> - discord token gen - draxon+cybertemp <span id="domLine">@andrewslife.tattoo</span></div>

<nav>
  <button id="nvMain" class="on" onclick="showTab('Main')">MAIN</button>
  <button id="nvTerm" onclick="showTab('Term')">TERMINAL</button>
  <button id="nvMan" onclick="showTab('Manage')">MANAGE</button>
</nav>

<div id="tabMain" class="tab on">
  <div class="stats">
    <div class="stat"><div class="num a" id="stTotal">0</div><div class="lbl">Generated</div></div>
    <div class="stat"><div class="num g" id="stValid">0</div><div class="lbl">Valid</div></div>
    <div class="stat"><div class="num r" id="stExpired">0</div><div class="lbl">Expired</div></div>
  </div>

  <div class="card">
    <h3>Settings</h3>
    <div style="margin-bottom:10px;display:flex;align-items:center;justify-content:space-between;gap:10px">
      <span class="tog">Headless browsers
        <span class="sw" id="swHeadless" onclick="toggleHeadless()"></span>
      </span>
      <span class="badge b-dim" id="stPending2">0 pending</span>
    </div>
    <div style="margin-bottom:6px">
      <label style="display:block;font-size:12px;opacity:.7;margin-bottom:4px">Custom email (optional - leave empty to auto-generate)</label>
      <input type="text" id="inpEmail" placeholder="your@email.com">
      <button style="margin-top:6px" onclick="saveCustomEmail()">Save email</button>
    </div>
    <h3 style="margin-top:14px">Mail domains (discord-friendly on cybertemp)</h3>
    <div id="domPick" class="pick"></div>
    <div class="dom" style="margin-top:10px">
      <input type="text" id="domCustom" placeholder="custom domain e.g. mysite.cc">
      <button onclick="addCustomDomain()">Add custom</button>
    </div>
    <div class="btnrow" style="margin-top:12px">
      <button class="primary" onclick="saveDomains()">Save domains</button>
    </div>
    <div class="hint">Pick ONE discord-friendly domain - choosing one replaces the current and saves immediately. Domains that trigger phone verification get burned automatically.</div>
  </div>
</div>

<div id="tabTerm" class="tab">
  <div class="btnrow">
    <button class="ok grow" id="btnStart" onclick="start()">START</button>
    <button class="danger grow" onclick="stop()">STOP</button>
    <button onclick="openView()">VIEW</button>
    <label class="chk"><input type="checkbox" id="showAllChk" onchange="showAll=this.checked;refreshLogs()"> ALL LOGS</label>
  </div>
  <div class="term">
    <div class="term-head">
      <span class="cd" id="cd1"></span><span class="cd" id="cd2"></span><span class="cd" id="cd3"></span>
      <span class="t">EY3 - WORKER B1</span><span class="badge b-dim" id="termState">idle</span>
    </div>
    <div class="pxline" id="pxLine">proxies: checking...</div>
    <div class="term-body" id="termBody"><div class="empty">No activity yet - hit START.</div></div>
  </div>
</div>

<div id="tabManage" class="tab">
  <div class="btnrow">
    <button class="primary" id="btnMode" onclick="toggleMode()">SHOW TOKENS</button>
    <button onclick="doValidate()">VALIDATE</button>
    <button class="ok" onclick="openExport()">EXPORT</button>
    <span class="badge b-dim" style="align-self:center">Total: <span id="manCount">0</span></span>
  </div>
  <div id="accList"><div class="empty">No accounts yet - run the generator first.</div></div>
</div>

<div class="overlay" id="viewOverlay" onclick="if(event.target===this)closeView()">
  <div class="modal">
    <div class="modal-head"><h2>LIVE BROWSER</h2>
      <button class="modal-close" onclick="closeView()">X</button></div>
    <div class="modal-body">
      <div id="viewWrap"><div class="vph">connecting...</div></div>
    </div>
  </div>
</div>

<div class="overlay" id="expOverlay" onclick="if(event.target===this)closeExp()">
  <div class="modal">
    <div class="modal-head"><h2>EXPORT TOKENS</h2>
      <button class="modal-close" onclick="closeExp()">X</button></div>
    <div class="modal-body">
      <label>How many tokens you need?</label>
      <input type="number" id="expCount" value="5" min="1" max="100">
      <div class="rad">
        <label><input type="radio" name="expMode" value="tokens" checked> Tokens alone</label>
        <label><input type="radio" name="expMode" value="full"> Full tokens</label>
      </div>
      <div class="btnrow">
        <button class="primary" onclick="exportGen()">Generate</button>
        <button onclick="exportCopyAll()">Copy all</button>
        <button class="danger" onclick="exportConfirm()">Confirm and delete</button>
      </div>
      <div class="exp" id="expList"></div>
      <div class="hint">Confirm and delete copies the tokens to your clipboard, then removes them from Manage permanently.</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>
<div class="footer">EY3 - grey iron build</div>

<script>
var ACCOUNTS=[], MODE='accounts', EXPORT=[], VIEWINT=null, HEADLESS=true, VALIDATED_ONCE=false;
var NL2 = String.fromCharCode(10,10);
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function toast(m){var t=$('toast');t.textContent=m;t.classList.add('on');clearTimeout(toast._t);toast._t=setTimeout(function(){t.classList.remove('on');},2200);}
function api(p,o){return fetch(p,o);}

function showTab(n){
  var tabs=['Main','Term','Manage'];
  for(var i=0;i<tabs.length;i++){
    $('tab'+tabs[i]).classList.toggle('on',tabs[i]===n);
    $('nv'+tabs[i]).classList.toggle('on',tabs[i]===n);
  }
  if(n==='Manage') refreshTokens();
  if(n==='Term') refreshLogs();
}

function refreshStatus(){
  api('/status').then(function(r){return r.json();}).then(function(x){
    var st = x.running ? 'on' : ((x.workers||[]).some(function(w){return w.status==='error';}) ? 'err' : '');
    var d=$('dDot');d.className='dot'+(st?' '+st:'');
    var running=(x.workers||[]).filter(function(w){return w.status==='running'||w.status==='starting';}).length;
    $('stLine').textContent = x.running ? ('running - '+running+'/'+(x.workers||[]).length+' browsers - '+Math.floor(x.uptime/60)+'m') : 'idle';
    var dm=(x.mail_domains&&x.mail_domains.length)?x.mail_domains[0]:'';
    if(dm&&$('domLine'))$('domLine').textContent='@'+dm;
    var px=x.proxies;
    if(px&&$('pxLine')){
      $('pxLine').textContent='proxies: '+px.available+' loaded | '+px.valid+' valid | '+px.used+' used | '+px.working+' working | '+px.failed+' failed';
    }
  }).catch(function(){});
}

var FILTERS=['[Proxy]','Fingerprint','Discord site rendered','is in Discord and confirmed','[Account] Email=',
  'Inbox ready','Verification link found','Challenge iframe fully loaded','[Accessibility] [OK]',
  'solved:','Humanized','[Captcha] [READY]','[Captcha]','[FAIL]','[Form]','[Nav] Page:','[Mail] No verification',
  'No verification link','No email available','DEAD','BLOCKED','rate limit','Starting worker',
  '[Phone]','[Fingerprint]','draxon','Draxon'];
var showAll=false;
var OKWORDS=['[ok]','confirmed','solved','ready','rendered','humanized','verification link found'];
function refreshLogs(){
  api('/worker/B1/logs').then(function(r){return r.json();}).then(function(x){
    $('termState').textContent = x.status||'idle';
    var lines=(x.logs||[]).filter(function(l){
      if(showAll) return true;
      var m=l.message||'';
      for(var i=0;i<FILTERS.length;i++){ if(m.indexOf(FILTERS[i])!==-1) return true; }
      return false;
    }).slice(-150);
    if(!lines.length) return;
    var html='';
    for(var i=0;i<lines.length;i++){
      var l=lines[i];
      var m=l.message||'';
      if(m.indexOf('[B1]')===0)m=m.substring(4);
      var cls='info', lv=(l.level||'').toLowerCase();
      if(lv==='error')cls='error'; else if(lv==='warn')cls='warn';
      else{
        var low=m.toLowerCase();
        for(var k=0;k<OKWORDS.length;k++){ if(low.indexOf(OKWORDS[k])!==-1){cls='ok';break;} }
      }
      html+='<div class="tl '+cls+'"><span class="tt">'+(l.time||'')+'</span><span class="tm">'+esc(m)+'</span></div>';
    }
    var tb=$('termBody');
    var atBottom = tb.scrollHeight - tb.scrollTop - tb.clientHeight < 80;
    tb.innerHTML=html;
    if(atBottom) tb.scrollTop=tb.scrollHeight;
  }).catch(function(){});
}

function start(){
  var b=$('btnStart');b.disabled=true;b.textContent='LAUNCHING...';
  api('/start',{method:'POST'}).then(function(r){return r.json();}).then(function(x){
    toast(x.msg||'Started');
  }).catch(function(e){toast('Start error: '+e.message);})
    .finally(function(){b.disabled=false;b.textContent='START';});
}
function stop(){
  api('/stop',{method:'POST'}).then(function(r){return r.text();}).then(function(t){toast(t);})
    .catch(function(e){toast('Stop error: '+e.message);});
}

function openView(){
  $('viewOverlay').classList.add('on');
  $('viewWrap').innerHTML='<div class="vph">connecting...</div>';
  var snap=function(){
    api('/latest?worker=B1&t='+Date.now()).then(function(r){
      if(!r.ok){ $('viewWrap').innerHTML='<div class="vph">no feed yet - browser not started</div>'; return; }
      return r.blob().then(function(b){
        var u=URL.createObjectURL(b);
        $('viewWrap').innerHTML='<img id="viewImg" src="'+u+'">';
        setTimeout(function(){URL.revokeObjectURL(u);},3000);
      });
    }).catch(function(){});
  };
  snap();
  VIEWINT=setInterval(snap,2000);
}
function closeView(){if(VIEWINT)clearInterval(VIEWINT);VIEWINT=null;$('viewOverlay').classList.remove('on');}

function toggleMode(){
  MODE = MODE==='accounts' ? 'creds' : 'accounts';
  $('btnMode').textContent = MODE==='accounts' ? 'SHOW TOKENS' : 'SHOW ACCOUNTS';
  render();
}
function badge(st){
  var c = st==='valid' ? 'valid' : (st==='invalid' ? 'invalid' : 'pending');
  return '<span class="badge b-'+c+'">'+(st||'pending')+'</span>';
}
function render(){
  var el=$('accList');
  if(!ACCOUNTS.length){el.innerHTML='<div class="empty">No accounts yet - run the generator first.</div>';return;}
  var html='';
  if(MODE==='accounts'){
    for(var i=0;i<ACCOUNTS.length;i++){
      var a=ACCOUNTS[i];
      var user=a.username||'?';
      var av = a.avatar ? '<img class="av" src="'+esc(a.avatar)+'">'
        : '<div class="av ph">'+esc((user||'?').charAt(0).toUpperCase())+'</div>';
      html+='<div class="acc">'+av+
        '<div class="meta"><div class="u">@'+esc(user)+(a.user_id?'<span class="id">ID '+esc(a.user_id)+'</span>':'')+'</div>'+
        '<div class="e">'+esc(a.email||'')+'</div>'+
        '<div class="b">'+((a.bio?'bio: '+esc(a.bio):'')+(a.humanized?' - humanized':''))+'</div></div>'+
        badge(a.status)+
        '<div class="acts">'+
        '<button data-copy="'+esc(user)+'" data-label="USER" onclick="copyBtn(this)">USER</button>'+
        '<button data-copy="'+esc(a.user_id||'')+'" data-label="ID" onclick="copyBtn(this)">ID</button></div></div>';
    }
  }else{
    for(var j=0;j<ACCOUNTS.length;j++){
      var b=ACCOUNTS[j];
      var tok=b.token||'';
      html+='<div class="acc"><div class="meta" style="flex:1;min-width:0">'+
        '<div class="u">@'+esc(b.username||'?')+'</div>'+
        '<div class="tok">'+esc(tok)+'</div>'+
        '<div class="e" style="margin-top:4px">'+esc((b.email||'')+' : '+(b.password||''))+'</div></div>'+
        badge(b.status)+
        '<div class="acts">'+
        '<button data-copy="'+esc(tok)+'" data-label="TOKEN" onclick="copyBtn(this)">TOKEN</button>'+
        '<button data-copy="'+esc((b.email||'')+' : '+(b.password||''))+'" data-label="CREDS" onclick="copyBtn(this)">CREDS</button></div></div>';
    }
  }
  el.innerHTML=html;
}
function refreshTokens(){
  api('/tokens').then(function(r){return r.json();}).then(function(x){
    ACCOUNTS=x.accounts||[];
    $('stTotal').textContent=(x.stats&&x.stats.total)||0;
    $('stValid').textContent=(x.stats&&x.stats.valid)||0;
    $('stExpired').textContent=(x.stats&&x.stats.expired)||0;
    $('stPending2').textContent=((x.stats&&x.stats.pending)||0)+' pending';
    $('manCount').textContent=ACCOUNTS.length;
    render();
    if(!VALIDATED_ONCE && (x.stats&&x.stats.pending)>0){ VALIDATED_ONCE=true; doValidate(); }
  }).catch(function(){});
}
function doValidate(){
  toast('Validating tokens...');
  api('/validate',{method:'POST'}).then(function(r){return r.json();}).then(function(x){
    ACCOUNTS=x.accounts||ACCOUNTS;
    $('stValid').textContent=x.valid||0;
    $('stExpired').textContent=x.expired||0;
    render();
    toast('Validation done - '+x.valid+' valid');
  }).catch(function(e){toast('Validate error: '+e.message);});
}

function openExport(){EXPORT=[];$('expList').style.display='none';$('expOverlay').classList.add('on');}
function closeExp(){$('expOverlay').classList.remove('on');}
function exportGen(){
  var count=parseInt($('expCount').value||'5',10);
  var modeEl=document.querySelector('input[name="expMode"]:checked');
  var mode=modeEl?modeEl.value:'tokens';
  api('/export',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({count:count,mode:mode})}).then(function(r){return r.json();}).then(function(x){
    EXPORT=x.accounts||[];
    if(!EXPORT.length){$('expList').style.display='block';$('expList').textContent='nothing to export - no tokens in Manage';}
    else{
      var parts=[];
      for(var i=0;i<EXPORT.length;i++)parts.push(EXPORT[i].text);
      $('expList').style.display='block';
      $('expList').textContent=parts.join(NL2);
    }
  }).catch(function(e){toast('Export error: '+e.message);});
}
function exportCopyAll(){
  var parts=[];
  for(var i=0;i<EXPORT.length;i++)parts.push(EXPORT[i].text);
  var txt=parts.join(NL2);
  if(!txt)return toast('nothing to copy yet');
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(function(){toast('Copied '+EXPORT.length+' to clipboard');});
  }else{
    var ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');}catch(e){}
    document.body.removeChild(ta);
    toast('Copied '+EXPORT.length+' to clipboard');
  }
}
function exportConfirm(){
  if(!EXPORT.length)return toast('Generate first');
  exportCopyAll();
  var ids=[];
  for(var i=0;i<EXPORT.length;i++){ if(EXPORT[i].id!=null) ids.push(EXPORT[i].id); }
  api('/export/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:ids})}).then(function(r){return r.json();}).then(function(x){
    toast('Copied and deleted '+ids.length+' from Manage');
    EXPORT=[];$('expList').style.display='none';closeExp();refreshTokens();
  }).catch(function(e){toast('Delete error: '+e.message);});
}
function copyBtn(btn){
  var t=btn.getAttribute('data-copy')||'';
  if(!t)return toast('nothing to copy');
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(function(){btn.textContent='COPIED';setTimeout(function(){btn.textContent=btn.getAttribute('data-label')||'COPY';},1200);});
  }else{
    var ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');}catch(e){}
    document.body.removeChild(ta);
    btn.textContent='COPIED';setTimeout(function(){btn.textContent=btn.getAttribute('data-label')||'COPY';},1200);
  }
}

var DOMAINS=[], AVAIL=[];
function loadConfig(){
  api('/config').then(function(r){return r.json();}).then(function(x){
    AVAIL=(x.available_domains&&x.available_domains.length)?x.available_domains:['andrewslife.tattoo'];
    DOMAINS=(x.mail_domains&&x.mail_domains.length)?x.mail_domains.slice():['andrewslife.tattoo'];
    HEADLESS=x.headless!==false;
    $('swHeadless').classList.toggle('on',HEADLESS);
    if(x.custom_email&&$('inpEmail'))$('inpEmail').value=x.custom_email;
    renderDomains();
  }).catch(function(){AVAIL=['andrewslife.tattoo'];DOMAINS=['andrewslife.tattoo'];renderDomains();});
}
function renderDomains(){
  var html='';
  for(var i=0;i<AVAIL.length;i++){
    var d=AVAIL[i];
    var sel=DOMAINS.indexOf(d)!==-1;
    html+='<button type="button" class="chip'+(sel?' on':'')+'" data-d="'+esc(d)+'" onclick="pickDomain(this)">'+esc(d)+'</button>';
  }
  for(var j=0;j<DOMAINS.length;j++){
    if(AVAIL.indexOf(DOMAINS[j])===-1){
      html+='<button type="button" class="chip on" data-d="'+esc(DOMAINS[j])+'" onclick="pickDomain(this)">'+esc(DOMAINS[j])+'</button>';
    }
  }
  $('domPick').innerHTML=html;
}
function pickDomain(btn){
  var d=btn.getAttribute('data-d')||'';
  if(!d)return;
  if(DOMAINS.length===1 && DOMAINS[0]===d)return;
  DOMAINS=[d];
  saveDomains();
}
function addCustomDomain(){
  var v=$('domCustom').value.trim().toLowerCase();
  $('domCustom').value='';
  if(!v)return;
  if(DOMAINS.indexOf(v)!==-1)return toast('already in the pool');
  DOMAINS.push(v);renderDomains();
}
function toggleHeadless(){$('swHeadless').classList.toggle('on');}
function saveCustomEmail(){
  var v=$('inpEmail').value.trim().toLowerCase();
  api('/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({custom_email:v})})
    .then(function(r){return r.json();}).then(function(x){
      toast(x.ok?(v?('Custom email set: '+v):'Custom email cleared - auto-generate on'):'save failed');
    }).catch(function(e){toast('Save error: '+e.message);});
}
function saveDomains(){
  var cleaned=[];
  for(var i=0;i<DOMAINS.length;i++){
    var d=DOMAINS[i].trim().toLowerCase();
    if(d && cleaned.indexOf(d)===-1) cleaned.push(d);
  }
  DOMAINS=cleaned;
  renderDomains();
  api('/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mail_domains:DOMAINS,headless:$('swHeadless').classList.contains('on')})})
    .then(function(r){return r.json();}).then(function(x){
      toast(x.ok?('Domains saved - '+((x.config&&x.config.mail_domains)||[]).join(', ')):'save failed');
    }).catch(function(e){toast('Save error: '+e.message);});
}

loadConfig();
refreshStatus();
setInterval(refreshStatus,5000);
setInterval(refreshLogs,2200);
setInterval(function(){refreshTokens();},12000);
refreshLogs();
refreshTokens();
</script></body></html>
"""

if __name__ == "__main__":
    main()
