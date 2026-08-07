import asyncio
import base64
import json
import os
import threading
import time
from typing import Dict, List, Optional

from flask import Flask, jsonify, request, Response

try:
    import db
    _db_available = True
except ImportError:
    db = None
    _db_available = False
    print("[app] db.py not found - token saving disabled", flush=True)

try:
    from proxies import pool as proxy_pool
    _proxies_available = True
except ImportError:
    proxy_pool = None
    _proxies_available = False
    print("[app] proxies.py not found - direct connections only", flush=True)

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

DEFAULT_CONFIG = {
    "headless": True,
    "web_port": 8080,
    "camera_interval": 3,
    "worker_count": WORKER_COUNT,
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


def _log(msg: str, level: str = "info"):
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


async def _run_worker(wid: str, cfg: dict, proxy=None) -> None:
    state = _workers[wid]
    state["status"] = "starting"
    state["proxy"] = proxy.get("key", "direct") if (proxy and isinstance(proxy, dict)) else (proxy or "direct")
    state["started_at"] = time.time()
    max_tries = 3
    current_proxy = proxy

    for attempt in range(max_tries):
        if not _running:
            state["status"] = "stopped"
            return
        bot = DiscordAutomation(
            headless=cfg.get("headless", True),
            proxy=current_proxy,
            worker_id=wid,
        )
        state["bot"] = bot
        try:
            await bot.initialize()
            state["status"] = "running"
            stagger = int(wid[1:]) - 1
            cam_task = asyncio.create_task(_worker_capture_loop(wid, cfg, stagger * int(cfg.get("camera_interval", 3))))
            ok = await bot.start_discord_signup()
            cam_task.cancel()
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
                        proxy=state["proxy"], worker_id=wid,
                    )
                _log(f"[{wid}] Done - token {len(acc['token'])} chars")
                return
            elif ok:
                state["status"] = "done"
                _log(f"[{wid}] Signup ok (no token yet)")
                return
            _log(f"[{wid}] Failed (attempt {attempt+1}/{max_tries})", level="warn")
            if attempt < max_tries - 1:
                try:
                    ctx = bot._context
                    if ctx:
                        await ctx.clear_cookies()
                except Exception:
                    pass
                await asyncio.sleep(3)
        except Exception as e:
            state["status"] = "error"
            _log(f"[{wid}] error: {e}")
            if attempt < max_tries - 1:
                _log(f"[{wid}] Retry {attempt+2}/{max_tries}")
                await asyncio.sleep(3)
        finally:
            try:
                await bot.close()
            except Exception:
                pass
            state["bot"] = None

    state["status"] = "error"
    state["step"] = "retries exhausted"
    if _proxies_available and proxy_pool is not None and current_proxy:
        proxy_pool.release(current_proxy, ok=False)


async def _start_all_async(cfg: dict) -> None:
    global _running, _start_time
    if _running:
        return
    _running = True
    _start_time = time.time()

    for wid in WORKER_IDS:
        _workers[wid] = _init_worker(wid)

    # Refresh proxy pool (fetch + validate free proxies)
    if _proxies_available and proxy_pool is not None:
        _log("[Proxy] Refreshing free proxy pool...")
        try:
            await proxy_pool.refresh()
            _log(f"[Proxy] {proxy_pool.valid_count} working proxies available")
        except Exception as e:
            _log(f"[Proxy] refresh error: {e}")

    for i, wid in enumerate(WORKER_IDS):
        proxy = proxy_pool.take() if (_proxies_available and proxy_pool is not None) else None
        if not proxy:
            _log(f"[{wid}] No proxy available - using direct connection")
        asyncio.create_task(_run_worker(wid, cfg, proxy))


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
    return jsonify({
        "running": _running,
        "uptime": int(time.time() - _start_time) if _start_time else 0,
        "workers": workers,
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
    logs = bot.get_activity_log() if bot else []
    return jsonify({
        "id": wid,
        "status": s["status"],
        "email": s.get("email", ""),
        "username": s.get("username", ""),
        "proxy": s.get("proxy", ""),
        "screenshots": s.get("screenshots", 0),
        "started_at": s.get("started_at", 0),
        "logs": logs[-200:],  # last 200 entries
    })


@app.route('/tokens')
def handle_tokens():
    if not _db_available or db is None:
        return jsonify({"count": 0, "valid": 0, "accounts": [], "error": "DB not available"})
    accounts = _run_in_loop(db.list_accounts(limit=300)) or []
    valid = _run_in_loop(db.validate_all_tokens(accounts)) if accounts else 0
    return jsonify({
        "count": len(accounts),
        "valid": valid,
        "accounts": accounts,
    })


@app.route('/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        cfg = load_config()
        if 'headless' in data:
            cfg['headless'] = bool(data['headless'])
        if 'worker_count' in data:
            cfg['worker_count'] = int(data['worker_count'])
        save_config(cfg)
        return jsonify({"ok": True, "config": cfg})
    cfg = load_config()
    return jsonify({"headless": cfg.get("headless", True),
                    "worker_count": cfg.get("worker_count", WORKER_COUNT)})


# ── Background event loop ─────────────────────────────────

def _run_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    global _loop
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

DASHBOARD_HTML = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#05060f">
<title>Eyes GEN</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#05060f;--card:#0b0e1c;--card2:#10142a;--line:#1c2240;
  --txt:#e8ecff;--dim:#7c85a8;--acc:#22d3ee;--acc2:#8b5cf6;
  --good:#34d399;--bad:#f87171;--warn:#fbbf24;
}
body{position:relative;z-index:1;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:radial-gradient(1200px 600px at 80% -10%,#101a3a 0%,var(--bg) 55%);
  color:var(--txt);min-height:100vh;max-width:560px;margin:0 auto;padding:14px 12px 110px}
h1{font-size:26px;font-weight:900;letter-spacing:.5px;
  background:linear-gradient(90deg,var(--acc),var(--acc2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;display:flex;align-items:center;gap:8px}
h1 .dot{width:10px;height:10px;border-radius:50%;background:var(--good);display:inline-block;
  -webkit-text-fill-color:initial;box-shadow:0 0 12px var(--good);animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.sub{color:var(--dim);font-size:12px;margin:2px 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:12px}
.row{display:flex;align-items:center;justify-content:space-between;gap:8px}
button{font-size:14px;font-weight:700;padding:12px 18px;border-radius:12px;border:0;cursor:pointer;
  transition:transform .1s,filter .15s}
button:active{transform:scale(.96)}
.btn-start{background:linear-gradient(90deg,var(--acc),#06b6d4);color:#03141c;flex:1}
.btn-stop{background:#3b1020;color:#fca5a5;border:1px solid #7f1d3a;flex:1}
.btn-sm{background:var(--card2);color:var(--txt);border:1px solid var(--line);padding:8px 12px;font-size:12px}
.badge{font-size:11px;font-weight:700;padding:3px 9px;border-radius:99px}
.b-idle{background:#1e2a4a;color:var(--dim)}
.b-starting{background:#3a2c12;color:var(--warn)}
.b-running{background:#0f2e28;color:var(--good)}
.b-done{background:#122a3a;color:var(--acc)}
.b-error{background:#3a1212;color:var(--bad)}
.b-stopped{background:#241c38;color:var(--dim)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px;text-align:center}
.stat .num{font-size:22px;font-weight:900}
.stat .lbl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.num.acc{color:var(--acc)}.num.good{color:var(--good)}.num.prox{color:var(--acc2)}
h3{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin:16px 0 8px}
.cams{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.cam{background:#000;border:1px solid var(--line);border-radius:14px;overflow:hidden;position:relative;aspect-ratio:16/12}
.cam img{width:100%;height:100%;object-fit:contain;display:block}
.cam .tag{position:absolute;top:6px;left:6px;background:rgba(5,6,15,.75);color:var(--txt);
  font-size:10px;font-weight:800;padding:2px 8px;border-radius:99px;backdrop-filter:blur(4px)}
.cam .st{position:absolute;bottom:6px;right:6px;font-size:9px;padding:2px 7px;border-radius:99px;
  background:rgba(5,6,15,.75);color:var(--dim)}
.cam .ph{display:flex;align-items:center;justify-content:center;height:100%;color:#2c3560;font-size:11px}
.nav{position:fixed;bottom:0;left:0;right:0;height:64px;background:rgba(7,9,20,.92);backdrop-filter:blur(12px);
  border-top:1px solid var(--line);display:flex;z-index:50;max-width:560px;margin:0 auto}
.nav button{flex:1;background:none;color:var(--dim);font-size:13px;padding:14px 6px;border-radius:0;font-weight:600}
.nav button.on{color:var(--acc)}
.tab{display:none}.tab.on{display:block}
.tok{background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:10px;margin-bottom:8px}
.tok .top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}
.tok .user{font-weight:800;font-size:14px}
.tok .mail{color:var(--dim);font-size:11px}
.tok .line{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:#9fb4d8;word-break:break-all;
  background:#070a18;border:1px solid var(--line);border-radius:8px;padding:7px 8px;margin-top:5px}
.copy{background:var(--acc);color:#03141c;font-size:11px;padding:5px 10px;border-radius:8px}
.tok-badge{font-size:10px;padding:2px 8px;border-radius:99px;font-weight:700}
.t-valid{background:#0f2e28;color:var(--good)}
.t-invalid{background:#3a1212;color:var(--bad)}
.t-pending{background:#1e2a4a;color:var(--dim)}
.empty{color:var(--dim);text-align:center;padding:30px 0;font-size:13px}
.footer{color:#3a4368;font-size:10px;text-align:center;margin-top:18px}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:99;justify-content:center;align-items:center}
.modal-overlay.on{display:flex}
.modal{background:var(--card);border:1px solid var(--line);border-radius:16px;width:92vw;max-width:500px;max-height:80vh;overflow:hidden;display:flex;flex-direction:column}
.modal-head{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line)}
.modal-head h2{font-size:16px;margin:0}
.modal-close{background:none;border:none;color:var(--dim);font-size:22px;cursor:pointer;padding:0 4px}
.modal-body{padding:12px 16px;overflow-y:auto;flex:1}
.modal .log-line{font-family:ui-monospace,Menlo,monospace;font-size:11px;padding:3px 0;border-bottom:1px solid rgba(28,34,64,.5);display:flex;gap:6px}
.modal .log-time{color:var(--dim);white-space:nowrap;min-width:52px}
.modal .log-msg{color:var(--txt);word-break:break-all}
.cam{cursor:pointer}
</style></head><body>

<h1><span class="dot"></span>EYES GEN</h1>
<div class="sub">Multi-browser Discord token generator &middot; free proxies &middot; auto-save</div>

<div class="stats">
  <div class="stat"><div class="num acc" id="stRunning">0</div><div class="lbl">Running</div></div>
  <div class="stat"><div class="num good" id="stTokens">0</div><div class="lbl">Tokens</div></div>
  <div class="stat"><div class="num prox" id="stProxies">0</div><div class="lbl">Proxies</div></div>
</div>

<div id="tabHome" class="tab on">
  <div class="card">
    <div class="row">
      <button class="btn-start" onclick="start()">START</button>
      <button class="btn-stop" onclick="stop()">STOP</button>
    </div>
    <div id="statusLine" class="sub" style="margin:10px 0 0">Idle - press START to launch all browsers</div>
  </div>
  <h3>Live Cams</h3>
  <div class="cams" id="cams"></div>
  <div class="footer">Eyes GEN &middot; each browser: fresh proxy + duckmail inbox + full token capture</div>
</div>

<div id="tabTokens" class="tab">
  <div class="card">
    <div class="row">
      <button class="btn-sm" onclick="refreshTokens()">Refresh</button>
      <span id="tokCount" class="badge b-idle">--</span>
    </div>
  </div>
  <div id="tokList"></div>
</div>

<div class="modal-overlay" id="logModal" onclick="if(event.target===this)closeLogModal()">
  <div class="modal">
    <div class="modal-head">
      <h2 id="logModalTitle">Worker Logs</h2>
      <button class="modal-close" onclick="closeLogModal()">&times;</button>
    </div>
    <div class="modal-body" id="logModalBody">Loading...</div>
  </div>
</div>

<div class="nav">
  <button id="navHome" class="on" onclick="showTab('Home')">Home</button>
  <button id="navTokens" onclick="showTab('Tokens')">Tokens</button>
</div>

<script>
let workers = {};
async function api(path, opts){ return fetch(path, opts); }

function showTab(name){
  document.getElementById('tabHome').classList.toggle('on', name==='Home');
  document.getElementById('tabTokens').classList.toggle('on', name==='Tokens');
  document.getElementById('navHome').classList.toggle('on', name==='Home');
  document.getElementById('navTokens').classList.toggle('on', name==='Tokens');
  if(name==='Tokens') refreshTokens();
}

async function start(){
  let btn = document.querySelector('.btn-start');
  btn.textContent = '...'; btn.disabled = true;
  try{
    let r = await api('/start', {method:'POST'});
    let x = await r.json();
    document.getElementById('statusLine').textContent = x.msg || 'Unknown';
    document.getElementById('statusLine').style.color = x.ok ? '#34d399' : '#f87171';
  }catch(e){
    document.getElementById('statusLine').textContent = 'Error: '+e.message;
    document.getElementById('statusLine').style.color = '#f87171';
  }
  btn.textContent = 'START'; btn.disabled = false;
}
async function stop(){
  try{
    let r = await api('/stop', {method:'POST'});
    document.getElementById('statusLine').textContent = await r.text();
  }catch(e){ document.getElementById('statusLine').textContent = 'Error: '+e.message; }
}

async function refresh(){
  try{
    let r = await api('/status'); let x = await r.json();
    let running = x.workers.filter(w=>w.status==='running'||w.status==='starting').length;
    document.getElementById('stRunning').textContent = x.running ? running+'/'+x.workers.length : '0';
    document.getElementById('stProxies').textContent = x.proxies ? x.proxies.available : 0;
    if(x.running){
      let s = document.getElementById('statusLine');
      s.textContent = '> Running ('+Math.floor(x.uptime/60)+'m) - '+running+' browsers active';
      s.style.color = '#34d399';
    }
    workers = {};
    x.workers.forEach(w=>{ workers[w.id]=w; });
    renderCams();
  }catch(e){}
}

function camStatus(w){
  if(w.status==='done') return 'done';
  if(w.status==='error') return 'error';
  if(w.status==='starting') return 'starting';
  if(w.status==='running') return 'live';
  return w.status;
}

function renderCams(){
  let ids = ['B1'];
  let html = '';
  ids.forEach(id=>{
    let w = workers[id] || {status:'idle', email:'', proxy:''};
    let st = camStatus(w);
    let proxy = w.proxy ? w.proxy.replace('://',' ').split(':')[1]||'' : '';
    html += '<div class="cam" id="cam'+id+'" onclick="openLogModal(\''+id+'\')">'
      + '<div class="tag">'+id+(proxy?' &middot; '+proxy:'')+'</div>'
      + (st==='live'||st==='done'
        ? '<img src="/latest?worker='+id+'&t='+Date.now()+'" onerror="this.style.display=&#39;none&#39;">'
        : '<div class="ph">'+ (st==='starting'?'starting…':(st==='done'?'finished':'idle')) +'</div>')
      + '<div class="st">'+st+'</div></div>';
  });
  document.getElementById('cams').innerHTML = html;
}

async function openLogModal(wid){
  document.getElementById('logModalTitle').textContent = wid + ' Logs';
  document.getElementById('logModalBody').innerHTML = '<div class="ph">Loading...</div>';
  document.getElementById('logModal').classList.add('on');
  try{
    let r = await api('/worker/'+wid+'/logs');
    let x = await r.json();
    document.getElementById('logModalTitle').textContent = wid + ' Logs (' + x.status + ')';
    if(!x.logs || !x.logs.length){
      document.getElementById('logModalBody').innerHTML = '<div class="empty">No logs yet</div>';
      return;
    }
    let html = '';
    x.logs.forEach(l=>{
      let cls = l.level==='error'?'log-error':(l.level==='warn'?'log-warn':'log-msg');
      html += '<div class="log-line"><span class="log-time">'+l.time+'</span><span class="'+cls+'">'+l.message+'</span></div>';
    });
    document.getElementById('logModalBody').innerHTML = html;
  }catch(e){
    document.getElementById('logModalBody').innerHTML = '<div class="empty">Error: '+e.message+'</div>';
  }
}
function closeLogModal(){
  document.getElementById('logModal').classList.remove('on');
}

async function refreshTokens(){
  try{
    let r = await api('/tokens'); let x = await r.json();
    document.getElementById('tokCount').textContent = x.valid+' / '+x.count+' valid';
    document.getElementById('tokCount').className = 'badge '+(x.valid>0?'b-done':'b-idle');
    if(!x.accounts || !x.accounts.length){
      document.getElementById('tokList').innerHTML = '<div class="empty">No tokens yet. Press START.</div>';
      return;
    }
    let html = '';
    x.accounts.forEach(a=>{
      let line = a.token;
      let st = a.status || 'pending';
      let badge = st==='valid'?'t-valid':(st==='invalid'?'t-invalid':'t-pending');
      html += '<div class="tok">'
        + '<div class="top"><div><div class="user">@'+(a.username||'?')+'</div>'
        + '<div class="mail">'+(a.email||'')+'</div></div>'
        + '<div style="display:flex;gap:6px;align-items:center">'
        + '<span class="tok-badge '+badge+'">'+st+'</span>'
        + '<button class="copy" data-token="'+jsEscape(a.token)+'" onclick="copyLine(this.dataset.token,this)">COPY</button>'
        + '</div></div>'
        + '<div class="line">'+a.token+'</div>'
        + '<div class="line" style="color:#6b7aa0">'+(a.email||'')+' : '+(a.password||'')+'</div>'
        + '</div>';
    });
    document.getElementById('tokList').innerHTML = html;
  }catch(e){}
}

function jsEscape(s){ return String(s).replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'"); }

function copyLine(val, btn){
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(val).then(()=>{btn.textContent='COPIED';setTimeout(()=>btn.textContent='COPY',1200);});
  } else {
    let ta = document.createElement('textarea'); ta.value = val; document.body.appendChild(ta);
    ta.select(); try{document.execCommand('copy');}catch(e){}
    document.body.removeChild(ta);
    btn.textContent='COPIED'; setTimeout(()=>btn.textContent='COPY',1200);
  }
}

setInterval(refresh, 2500);
refresh();
setInterval(refreshTokens, 15000);
</script></body></html>"""


if __name__ == "__main__":
    main()
