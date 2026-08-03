import asyncio
import base64
import json
import os
import sys
import threading
from typing import Optional

from flask import Flask, jsonify, request, Response

from server import DiscordAutomation
from captcha_solver import NoCaptchaAI

# ── Global state (shared between the Flask thread and the asyncio thread) ──

_loop: Optional[asyncio.AbstractEventLoop] = None
_automation: Optional[DiscordAutomation] = None
_running = False
_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "email": "",
    "headless": True,
    "web_port": 8080,
    "camera_interval": 3,
    "run_automation": False,
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
    # Environment always wins for the port
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


# ── Automation control (runs inside the asyncio thread) ──

async def _start_automation_async(config: dict) -> None:
    global _automation, _running
    if _automation and _running:
        _log("Automation already running")
        return
    _automation = DiscordAutomation(
        headless=config.get('headless', True),
        email=config.get('email', ''),
    )
    _running = True
    try:
        await _automation.initialize()
        screenshot_task = asyncio.create_task(
            _capture_periodic_screenshots(config.get('camera_interval', 3))
        )
        _log("Starting Discord signup "
             "(email from config — auto-generated via duckmail.sbs if empty)")
        success = await _automation.start_discord_signup()
        if success:
            _log("✓ Automation completed successfully")
        else:
            _log("✗ Automation failed", level="error")
        screenshot_task.cancel()
    except Exception as e:
        _log(f"Error during automation: {e}", level="error")
        import traceback
        traceback.print_exc()
    finally:
        await _cleanup_async()


async def _capture_periodic_screenshots(interval: int) -> None:
    global _running
    while _running:
        try:
            await _automation.capture_screenshot()
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(interval)


async def _stop_automation_async() -> None:
    global _running
    _running = False
    if _automation:
        try:
            await _automation.close()
        except Exception as e:
            _log(f"[STOP] close error (non-fatal): {e}")
    _log("Automation stopped")


async def _cleanup_async() -> None:
    global _running
    _running = False
    if _automation:
        try:
            await _automation.close()
        except Exception as e:
            _log(f"[CLEANUP] close error (non-fatal): {e}")


def _run_in_loop(coro):
    """Schedule a coroutine on the background event loop and wait for it."""
    if not _loop:
        return None
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=120)


# ── Flask app ─────────────────────────────────────────────

app = Flask(__name__)


@app.route('/')
def handle_root():
    return Response(DASHBOARD_HTML, content_type='text/html')


@app.route('/start', methods=['POST'])
def handle_start():
    if _running:
        return "Already running"
    config = load_config()
    threading.Thread(
        target=lambda: _run_in_loop(_start_automation_async(config)),
        daemon=True,
    ).start()
    return "Started"


@app.route('/stop', methods=['POST'])
def handle_stop():
    _run_in_loop(_stop_automation_async())
    return "Stopped"


@app.route('/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        config = load_config()
        if 'email' in data:
            config['email'] = str(data['email'] or '').strip()
        if 'headless' in data:
            config['headless'] = bool(data['headless'])
        save_config(config)
        return jsonify({"ok": True, "config": config})
    config = load_config()
    return jsonify({
        "email": config.get('email', ''),
        "headless": config.get('headless', True),
    })


@app.route('/status')
def handle_status():
    auto = _automation
    screenshots = len(auto.get_screenshots()) if auto else 0
    email = auto._email if auto else (load_config().get('email') or "")
    username = auto._username if auto else ""
    mail_provider = auto._mail.provider if auto and auto._mail else ""
    solver = {"configured": False, "stats": {}}
    if auto and hasattr(auto, '_solver'):
        solver = {
            "configured": auto._solver.configured,
            "stats": auto._solver.stats,
        }
    return jsonify({
        "running": _running,
        "screenshots": screenshots,
        "email": email,
        "username": username,
        "mail_provider": mail_provider,
        "solver": solver,
    })


@app.route('/latest')
def handle_latest_screenshot():
    if _automation:
        b64 = _automation.get_latest_screenshot()
        if b64:
            try:
                return Response(base64.b64decode(b64), content_type='image/png')
            except Exception:
                pass
    return Response(status=404)


@app.route('/activity')
def handle_activity_log():
    if _automation:
        return jsonify(_automation.get_activity_log())
    return jsonify([])


@app.route('/api_status')
def handle_api_status():
    solver_key_set = bool((os.environ.get("API_KEY") or "").strip())
    return jsonify({
        "api_key_set": solver_key_set,
    })


@app.route('/credits')
def handle_credits():
    """NoCaptchaAI account balance."""
    s = NoCaptchaAI()
    if not s.configured:
        return jsonify({"configured": False})
    result = _run_in_loop(s.get_balance())
    if result is None:
        return jsonify({"configured": True, "error": "unreachable"})
    return jsonify({
        "configured": True,
        "balance": result.get("balance", 0),
    })


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

    api_key = os.environ.get('API_KEY', '').strip()
    print("=" * 56, flush=True)
    if api_key:
        print("  NoCaptchaAI solver: READY — click Start", flush=True)
    else:
        print("  NoCaptchaAI solver: API_KEY not set — FunCAPTCHA offline solver only", flush=True)
    print("  Email: config.json or duckmail.sbs (auto)", flush=True)
    print(f"  Dashboard: http://0.0.0.0:{web_port}", flush=True)
    print("=" * 56, flush=True)

    # Flask dev server on 0.0.0.0 — no debug/reloader (threads + asyncio don't mix with it)
    app.run(host='0.0.0.0', port=web_port, debug=False, use_reloader=False, threaded=True)


# ═══════════════════════════════════════════════════════════
# DASHBOARD HTML
# ═══════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NoCaptchaAI — Discord GEN Control</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui;background:#0a0e1a;color:#e2e8f0;max-width:960px;margin:0 auto;padding:20px}
h1{font-size:24px;font-weight:800;background:linear-gradient(135deg,#06b6d4,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}
h2{font-size:16px;color:#94a3b8;font-weight:600;margin:18px 0 10px}
h3{font-size:13px;color:#94a3b8;font-weight:600;margin:10px 0 6px;text-transform:uppercase;letter-spacing:.5px}
.card{background:#111827;border-radius:14px;padding:18px;margin-bottom:14px;border:1px solid #1e293b}
.btn-group{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
button{font-size:14px;padding:10px 18px;border-radius:9px;border:0;cursor:pointer;font-weight:600;transition:all .15s}
.btn-primary{background:#6366f1;color:white}
.btn-primary:hover{background:#4f46e5}
.btn-stop{background:#ef4444;color:white}
.btn-stop:hover{background:#dc2626}
input[type=text],input[type=email]{background:#0f172a;border:1px solid #1e293b;color:#e2e8f0;border-radius:8px;padding:9px 12px;font-size:14px;width:100%;max-width:420px}
#status{margin:10px 0;color:#a7f3d0;font-size:14px;font-weight:500}
.cam-wrap{background:#000;border-radius:12px;overflow:hidden;min-height:200px;position:relative}
.cam-wrap img{width:100%;display:block;min-height:180px;object-fit:contain}
.cam-placeholder{display:flex;align-items:center;justify-content:center;min-height:180px;color:#475569;font-size:14px}
#log{background:#0f172a;border-radius:10px;padding:12px;max-height:400px;overflow-y:auto;font-family:monospace;font-size:12px;line-height:1.6}
#log .entry{padding:3px 0;border-bottom:1px solid #1e293b}
#log .time{color:#4b5563;margin-right:8px}
#log .info{color:#a7f3d0}
#log .error{color:#fca5a5}
#log .warn{color:#fde68a}
#log .vision{color:#818cf8}
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:8px 0}
@media(max-width:600px){.stats-grid{grid-template-columns:repeat(2,1fr)}}
.stat-card{background:#0f172a;border-radius:10px;padding:12px;text-align:center}
.stat-card .num{font-size:24px;font-weight:800}
.stat-card .num.cyan{color:#06b6d4}
.stat-card .num.green{color:#22c55e}
.stat-card .num.red{color:#ef4444}
.stat-card .label{font-size:11px;color:#64748b;text-transform:uppercase;margin-top:2px}
.model-status{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#0f172a;border-radius:8px;margin:8px 0;font-size:13px}
.model-dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.model-dot.loading{background:#f59e0b;animation:pulse 1s infinite}
.model-dot.loaded{background:#22c55e}
.model-dot.error{background:#ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.small{font-size:12px;color:#64748b}
.mt8{margin-top:8px}
</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:4px">
  <h1>⚡ NoCaptchaAI GEN</h1>
  <div class="model-status" id="modelStatus">
    <span class="model-dot loading" id="modelDot"></span>
    <span id="modelText">Checking solver...</span>
  </div>
</div>
<p class="small">Discord account generator · NoCaptchaAI solving (primary) · offline FunCAPTCHA solver · duckmail.sbs auto-verify</p>

<div class="card">
  <h3>🧪 Solver Status</h3>
  <div class="stats-grid">
    <div class="stat-card"><div class="num cyan" id="statChallenges">0</div><div class="label">Tasks</div></div>
    <div class="stat-card"><div class="num green" id="statSolved">0</div><div class="label">Solved</div></div>
    <div class="stat-card"><div class="num red" id="statFailed">0</div><div class="label">Failed</div></div>
    <div class="stat-card"><div class="num cyan" id="statSolverCalls">0</div><div class="label">API Calls</div></div>
  </div>
  <div id="creditLine" class="small mt8">Balance: --</div>
</div>

<div class="card">
  <h3>📧 Signup Email</h3>
  <p class="small">Leave empty to auto-generate via duckmail.sbs (mail.tm fallback). Set your own email to skip the mail service.</p>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;align-items:center">
    <input type="email" id="emailInput" placeholder="your@email.com (optional)">
    <button class="btn-primary" onclick="saveEmail()">Save</button>
  </div>
  <div id="emailSaved" class="small mt8" style="color:#22c55e;display:none">✓ Email saved</div>
</div>

<div class="card">
  <h3>🤖 Discord Automation</h3>
  <p class="small">Email: <strong id="emailLabel">loading...</strong></p>
  <div class="btn-group">
    <button class="btn-primary" onclick="start()">▶ Start</button>
    <button class="btn-stop" onclick="stop()">■ Stop</button>
  </div>
  <div id="status">Idle</div>
  <div class="cam-wrap">
    <div class="cam-placeholder" id="camPlaceholder">Waiting for screenshot...</div>
    <img id="shot" alt="Live view" style="display:none">
  </div>
  <h2 style="margin-top:12px;font-size:12px;color:#94a3b8;font-weight:600">📝 Activity Log</h2>
  <div id="log"><div class="entry"><span class="time">--:--:--</span><span class="info">Ready — set an API_KEY (nocaptchaai.com) to enable fast solving.</span></div></div>
</div>

<script>
async function api(path,opts){return fetch(path,opts)}

async function start(){
  let r=await api('/start',{method:'POST'});
  document.getElementById('status').textContent=await r.text();
}
async function stop(){
  let r=await api('/stop',{method:'POST'});
  document.getElementById('status').textContent=await r.text();
}

async function saveEmail(){
  let email=document.getElementById('emailInput').value.trim();
  let r=await api('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
  let x=await r.json();
  if(x.ok){
    document.getElementById('emailSaved').style.display='block';
    setTimeout(()=>document.getElementById('emailSaved').style.display='none',2500);
    document.getElementById('emailLabel').textContent=email||'auto (duckmail.sbs)';
  }
}

async function refresh(){
  try{
    let r=await api('/status');let x=await r.json();
    let s=document.getElementById('status');
    if(x.email) document.getElementById('emailLabel').textContent=x.email;
    if(x.mail_provider) document.getElementById('emailLabel').textContent+=' ('+x.mail_provider+')';
    if(x.username) document.getElementById('emailLabel').textContent+=' · @'+x.username;
    if(x.running){
      s.textContent=x.screenshots?'▶ Running · '+x.screenshots+' screenshot(s)':'▶ Running';
      s.style.color='#a7f3d0';
    }else{
      s.textContent='■ Stopped';
      s.style.color='#fca5a5';
    }
    let sol=x.solver||{};
    let st=sol.stats||{};
    document.getElementById('statChallenges').textContent=st.calls||0;
    document.getElementById('statSolved').textContent=st.ok||0;
    document.getElementById('statFailed').textContent=st.failed||0;
    document.getElementById('statSolverCalls').textContent=st.calls||0;
    let img=document.getElementById('shot');let ph=document.getElementById('camPlaceholder');
    if(x.screenshots&&x.screenshots>0){
      img.src='/latest?'+Date.now();
      img.style.display='block';
      ph.style.display='none';
    }else{
      img.style.display='none';
      ph.style.display='flex';
      ph.textContent='Waiting for screenshot...';
    }
  }catch(e){document.getElementById('status').textContent='Connection error'}
}

async function refreshLog(){
  try{
    let r=await api('/activity');let logs=await r.json();
    if(logs.length===0)return;
    let html='';let recent=logs.slice(-80).reverse();
    for(let e of recent){
      let cls=e.level||'info';
      let msg=e.message||'';
      if(msg.includes('[NoCaptchaAI]')) cls='vision';
      if(msg.includes('[FunCAPTCHA]')||msg.includes('[Captcha]')) cls='vision';
      if(msg.includes('SOLVED')||msg.includes('✓')) cls='info';
      html+='<div class="entry"><span class="time">'+e.time+'</span><span class="'+cls+'">'+msg+'</span></div>';
    }
    document.getElementById('log').innerHTML=html;
  }catch(e){}
}

async function checkModel(){
  try{
    let r=await api('/api_status');let st=await r.json();
    if(st.api_key_set){
      document.getElementById('modelDot').className='model-dot loaded';
      document.getElementById('modelText').textContent='NoCaptchaAI ready ⚡';
      document.getElementById('modelText').style.color='#22c55e';
    } else {
      document.getElementById('modelDot').className='model-dot error';
      document.getElementById('modelText').textContent='No solver key — set API_KEY (nocaptchaai.com)';
      document.getElementById('modelText').style.color='#ef4444';
    }
  }catch(e){}
}

async function loadCredits(){
  try{
    let r=await api('/credits');let x=await r.json();
    if(x.configured){
      document.getElementById('creditLine').innerHTML='NoCaptchaAI balance: <b>$'+Number(x.balance||0).toFixed(2)+'</b>';
    } else {
      document.getElementById('creditLine').textContent='Set API_KEY (nocaptchaai.com) to see balance';
    }
  }catch(e){}
}

async function loadEmail(){
  try{
    let r=await api('/config');let x=await r.json();
    if(x.email) document.getElementById('emailInput').value=x.email;
    document.getElementById('emailLabel').textContent=x.email||'auto (duckmail.sbs)';
  }catch(e){}
}

setInterval(refresh,3000);
setInterval(refreshLog,2000);
setInterval(checkModel,5000);
setInterval(loadCredits,30000);
refresh();
refreshLog();
checkModel();
loadCredits();
loadEmail();
</script></body></html>"""


if __name__ == "__main__":
    main()
