import asyncio
import base64
import json
import os
import sys
from typing import Optional

from aiohttp import web
from server import DiscordAutomation, run_discord_automation
from database import KnowledgeDB
from captcha_solver import SolverAPI
from solver_api import SERVICE_ENV_VARS, SERVICE_FREE_CREDITS


class AppHost:
    def __init__(self):
        self._automation: Optional[DiscordAutomation] = None
        self._running = False
        self._config_path = "config.json"
        self._web_server = None
        self._web_port = 8080
        self._db: Optional[KnowledgeDB] = None
        self._solver: Optional[SolverAPI] = None

    def load_config(self, path: str = "config.json") -> dict:
        default_config = {
            "email": "test@example.com",
            "username": "",
            "password": "Password123!",
            "headless": True,
            "camera_interval": 3,
            "web_port": 8080,
            "run_automation": False,
            "solver_service": "capsolver",
        }
        if os.path.exists(path):
            with open(path, 'r') as f:
                config = json.load(f)
                for key, value in default_config.items():
                    if key not in config:
                        config[key] = value
                return config
        return default_config

    def save_config(self, config: dict, path: str = "config.json") -> None:
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)

    def _log(self, msg: str, level: str = "info"):
        print(f"[{level.upper()}] {msg}", flush=True)

    async def init_db(self):
        if self._db is None:
            self._db = await KnowledgeDB.create(log=self._log)
        return self._db

    async def start_automation(self) -> None:
        if self._automation and self._running:
            print("Automation already running")
            return
        config = self.load_config(self._config_path)
        self._automation = DiscordAutomation(headless=config.get('headless', True))
        self._running = True
        try:
            await self._automation.initialize()
            self._automation.load_config(self._config_path)
            screenshot_task = asyncio.create_task(
                self._capture_periodic_screenshots(config.get('camera_interval', 3))
            )
            success = await self._automation.start_discord_signup()
            if success:
                print("✓ Automation completed successfully")
            else:
                print("✗ Automation failed")
            await screenshot_task
        except Exception as e:
            print(f"Error during automation: {e}")
        finally:
            await self._cleanup()

    async def _capture_periodic_screenshots(self, interval: int) -> None:
        while self._running:
            try:
                await self._automation.capture_screenshot()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Screenshot error: {e}")
                await asyncio.sleep(interval)

    async def stop_automation(self) -> None:
        self._running = False
        if self._automation:
            await self._automation.close()
            self._automation = None
        print("Automation stopped")

    async def _cleanup(self) -> None:
        self._running = False
        if self._automation:
            await self._automation.close()
            self._automation = None

    async def start_web_server(self, port: int = 8080) -> None:
        self._web_port = port
        await self.init_db()

        async def handle_status(request):
            if self._automation:
                return web.json_response({
                    "running": self._running,
                    "screenshots": len(self._automation.get_screenshots()),
                    "email": self._automation._email if self._automation else "",
                    "username": self._automation._username if self._automation else ""
                })
            return web.json_response({"running": False, "screenshots": 0})

        async def handle_screenshot(request):
            if self._automation:
                b64 = self._automation.get_latest_screenshot()
                if b64:
                    return web.Response(text=b64, content_type='text/plain')
            return web.Response(status=404)

        async def handle_latest_screenshot(request):
            if self._automation:
                b64 = self._automation.get_latest_screenshot()
                if b64:
                    try:
                        return web.Response(body=base64.b64decode(b64), content_type='image/png')
                    except:
                        pass
            return web.Response(status=404)

        async def handle_activity_log(request):
            if self._automation:
                return web.json_response(self._automation.get_activity_log())
            return web.json_response([])

        async def handle_root(request):
            return web.Response(text=DASHBOARD_HTML, content_type='text/html')

        async def handle_start(request):
            if self._running:
                return web.Response(text="Automation is already running")
            try:
                data = await request.json()
                email = data.get('email', '').strip()
                config = self.load_config(self._config_path)
                if email: config['email'] = email
                config['run_automation'] = True
                self.save_config(config, self._config_path)
                asyncio.create_task(self.start_automation())
                return web.Response(text="Automation started")
            except Exception as e:
                return web.Response(status=400, text=f"Start failed: {e}")

        async def handle_stop(request):
            await self.stop_automation()
            return web.Response(text="Automation stopped")

        # ── Token API Routes ──────────────────────────────

        async def handle_tokens(request):
            if self._db and not self._db._noop:
                tokens = await self._db.get_all_tokens(limit=100)
                return web.json_response(tokens)
            return web.json_response([])

        async def handle_token_save(request):
            if not self._db or self._db._noop:
                return web.json_response({"status": "db_not_configured"}, status=400)
            try:
                data = await request.json()
                ok = await self._db.save_token(
                    token=data.get("token", ""),
                    service=data.get("service", "manual"),
                    site=data.get("site", ""),
                    account_email=data.get("email", ""),
                    account_username=data.get("username", ""),
                    account_password=data.get("password", ""),
                    sitekey=data.get("sitekey", ""),
                    pageurl=data.get("pageurl", ""),
                    expires_in_hours=data.get("expires_in_hours", 24),
                )
                return web.json_response({"status": "saved" if ok else "failed"})
            except Exception as e:
                return web.json_response({"status": "error", "error": str(e)}, status=500)

        async def handle_token_expire(request):
            if not self._db or self._db._noop:
                return web.json_response({"status": "db_not_configured"}, status=400)
            try:
                count = await self._db.expire_old_tokens(max_age_hours=48)
                return web.json_response({"status": "ok", "expired": count})
            except Exception as e:
                return web.json_response({"status": "error", "error": str(e)}, status=500)

        # ── Solver API Routes ─────────────────────────────

        async def handle_solver_balance(request):
            svc = SolverAPI(service="capsolver")
            info = svc.get_balance_info()
            await svc.close()
            return web.json_response(info)

        async def handle_solver_status(request):
            svc = SolverAPI(service="capsolver")
            stats = svc.get_stats()
            await svc.close()
            return web.json_response(stats)

        # ── Router ────────────────────────────────────────

        app = web.Application()
        app.router.add_get('/', handle_root)
        app.router.add_post('/start', handle_start)
        app.router.add_post('/stop', handle_stop)
        app.router.add_get('/status', handle_status)
        app.router.add_get('/screenshot', handle_screenshot)
        app.router.add_get('/latest', handle_latest_screenshot)
        app.router.add_get('/activity', handle_activity_log)
        # Token and Solver routes
        app.router.add_get('/api/tokens', handle_tokens)
        app.router.add_post('/api/tokens/save', handle_token_save)
        app.router.add_post('/api/tokens/expire', handle_token_expire)
        app.router.add_get('/api/solver/balance', handle_solver_balance)
        app.router.add_get('/api/solver/status', handle_solver_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"Web server started on 0.0.0.0:{port}", flush=True)
        return runner


def create_sample_config() -> None:
    config = {
        "email": "test@example.com",
        "username": "discord_user_1234",
        "password": "SecurePassword123!",
        "headless": True,
        "camera_interval": 3,
        "web_port": 8080,
        "solver_service": "capsolver",
    }
    with open("config.json", 'w') as f:
        json.dump(config, f, indent=2)
    print("Created config.json with sample values")


async def main():
    config = {}
    try:
        with open("config.json", 'r') as f:
            config = json.load(f)
    except:
        pass
    web_port = int(os.environ.get('PORT', config.get('web_port', 5000)))
    headless = config.get('headless', True)
    app = AppHost()
    await app.start_web_server(web_port)
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == '--init':
            create_sample_config()
            return
        if arg == '--single':
            await run_discord_automation()
            return
    if headless:
        if config.get('run_automation', False):
            await app.start_automation()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
    else:
        await app.run_shell()


# ═══════════════════════════════════════════════════════════
# DASHBOARD HTML — Solver & Token Dashboard
# ═══════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAPTCHA Solver Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui;background:#0a0f1e;color:#f1f5f9;max-width:960px;margin:0 auto;padding:20px}
h1{font-size:22px;font-weight:700;background:linear-gradient(135deg,#f59e0b,#d97706);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}
h2{font-size:16px;color:#94a3b8;font-weight:600;margin:18px 0 10px}
h3{font-size:13px;color:#94a3b8;font-weight:600;margin:10px 0 6px;text-transform:uppercase;letter-spacing:.5px}
.card{background:#131a2e;border-radius:14px;padding:18px;margin-bottom:14px;border:1px solid #1e2a45}
label{font-size:13px;font-weight:600;color:#94a3b8;display:block;margin-bottom:5px}
input,button{font-size:14px;padding:10px 14px;border-radius:9px;border:0}
input{width:100%;background:#1e2a45;color:#f1f5f9;outline:0;transition:border .2s}
input:focus{border:1px solid #f59e0b}
button{cursor:pointer;transition:all .15s;font-weight:600}
.btn-primary{background:#6366f1;color:white;margin-right:6px}
.btn-primary:hover{background:#4f46e5}
.btn-stop{background:#ef4444;color:white}
.btn-stop:hover{background:#dc2626}
.btn-success{background:#22c55e;color:white}
.btn-success:hover{background:#16a34a}
.btn-group{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
#status{margin:10px 0;color:#a7f3d0;font-size:14px;font-weight:500}
.cam-wrap{background:#000;border-radius:12px;overflow:hidden;min-height:180px;position:relative}
.cam-wrap img{width:100%;display:block;min-height:160px;object-fit:contain}
.cam-placeholder{display:flex;align-items:center;justify-content:center;min-height:160px;color:#475569;font-size:13px}
#log{background:#0d1326;border-radius:10px;padding:12px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:12px;line-height:1.6}
#log .entry{padding:3px 0;border-bottom:1px solid #1a2440}
#log .time{color:#4b5563;margin-right:8px}
#log .info{color:#a7f3d0}
#log .error{color:#fca5a5}
#log .warn{color:#fde68a}
.token-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:8px 0}
@media(max-width:600px){.token-grid{grid-template-columns:1fr}}
.token-stat{background:#0d1326;border-radius:10px;padding:14px;text-align:center}
.token-stat .num{font-size:32px;font-weight:800}
.token-stat .num.green{color:#22c55e}
.token-stat .num.red{color:#ef4444}
.token-stat .label{font-size:11px;color:#64748b;text-transform:uppercase;margin-top:2px}
.token-list{max-height:250px;overflow-y:auto}
.token-item{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #1a2440;font-size:12px}
.token-item:last-child{border:0}
.token-service{background:#1e2a45;color:#818cf8;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700}
.token-active{color:#22c55e;font-weight:700;font-size:11px}
.token-expired{color:#ef4444;font-weight:700;font-size:11px}
.token-site{color:#94a3b8;font-size:11px}
.api-key-list{display:grid;gap:8px}
.api-key-row{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#0d1326;border-radius:8px;font-size:13px}
.api-key-name{font-weight:600;color:#e2e8f0}
.api-key-status{font-size:12px}
.api-key-status.configured{color:#22c55e}
.api-key-status.missing{color:#ef4444}
.api-key-credits{font-size:11px;color:#64748b}
small{color:#64748b;font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
.badge-yellow{background:#78350f;color:#fbbf24}
.copy-btn{background:0;border:1px solid #373;color:#22c55e;padding:3px 8px;border-radius:4px;font-size:10px;cursor:pointer}
.copy-btn:hover{background:#22c55e22}
</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:4px">
  <h1>🧩 CAPTCHA Solver</h1>
  <small id="dbStatus" style="color:#64748b">DB: checking...</small>
</div>
<p><small>API-based solving · Auto-rotate between services · Free credits</small></p>

<!-- Token Status -->
<div class="card">
  <h3>🔑 Token Status</h3>
  <div class="token-grid">
    <div class="token-stat"><div class="num green" id="activeTokens">0</div><div class="label">Active Token</div></div>
    <div class="token-stat"><div class="num red" id="expiredTokens">0</div><div class="label">Expired Token</div></div>
  </div>
  <div class="token-list" id="tokenList">
    <div style="color:#475569;font-size:12px;text-align:center;padding:12px">Loading tokens...</div>
  </div>
  <div style="margin-top:8px">
    <button class="btn-primary" onclick="refreshTokens()">⟳ Refresh Tokens</button>
    <button class="btn-stop" onclick="expireOld()">🗑 Expire Old</button>
  </div>
</div>

<!-- API Keys / Solver Services -->
<div class="card">
  <h3>⚙️ Solver Services</h3>
  <p><small>Set API keys via environment variables or the API Keys tab in your project settings</small></p>
  <div class="api-key-list" id="apiKeyList">
    <div style="color:#475569;font-size:12px;text-align:center;padding:12px">Loading...</div>
  </div>
</div>

<!-- Discord Automation -->
<div class="card">
  <h3>🤖 Discord Automation</h3>
  <label>Email</label>
  <input id="email" type="email" placeholder="your email">
  <div class="btn-group">
    <button class="btn-success" onclick="start()">▶ Start</button>
    <button class="btn-stop" onclick="stop()">■ Stop</button>
  </div>
  <div id="status">Checking status...</div>
  <div class="cam-wrap" id="camWrap">
    <div class="cam-placeholder" id="camPlaceholder">Waiting for screenshot...</div>
    <img id="shot" alt="Live view" style="display:none">
  </div>
  <h2 style="margin-top:12px;font-size:13px;color:#94a3b8;font-weight:600">Activity Log</h2>
  <div id="log"><div class="entry"><span class="time">--:--:--</span><span class="info">Waiting...</span></div></div>
</div>

<script>
async function api(path,opts){return fetch(path,opts)}

// ── Token Management ──────────────────────────────────
async function refreshTokens(){
  try{
    let r=await api('/api/tokens');let tokens=await r.json();
    let active=0,expired=0;
    for(let t of tokens){
      if(!t.expired&&t.is_active)active++;
      else expired++;
    }
    document.getElementById('activeTokens').textContent=active;
    document.getElementById('expiredTokens').textContent=expired;
    let html='';
    if(tokens.length===0){
      html='<div style="color:#475569;font-size:12px;text-align:center;padding:12px">No tokens saved yet</div>';
    }else{
      for(let t of tokens.slice(0,30)){
        let status=t.expired||!t.is_active?'expired':'active';
        let time=new Date(t.created_at).toLocaleString();
        let svc=t.service||'manual';
        html+='<div class="token-item">';
        html+='<div><span class="token-service">'+svc.toUpperCase()+'</span> ';
        html+='<span class="token-site">'+(t.site||'?')+'</span>';
        html+='<div style="color:#64748b;font-size:10px">'+time+'</div></div>';
        html+='<div><span class="token-'+status+'">'+status.toUpperCase()+'</span></div>';
        html+='</div>';
      }
    }
    document.getElementById('tokenList').innerHTML=html;
  }catch(e){console.log('Token refresh error:',e)}
}

async function expireOld(){
  await api('/api/tokens/expire',{method:'POST'});
  refreshTokens();
}

// ── Solver Services ───────────────────────────────────
async function refreshSolvers(){
  try{
    let r=await api('/api/solver/balance');let services=await r.json();
    let html='';
    for(let s of services){
      let cls=s.configured?'configured':'missing';
      let txt=s.configured?'✅ Configured':'❌ Missing API Key';
      let keyHint=s.configured?' Key: '+s.key_preview:'';
      html+='<div class="api-key-row">';
      html+='<div><span class="api-key-name">'+s.name+'</span><br>';
      html+='<span class="api-key-credits">'+s.free_credits+'</span></div>';
      html+='<div><span class="api-key-status '+cls+'">'+txt+'</span>';
      html+='<div style="font-size:10px;color:#64748b">'+s.env_var+keyHint+'</div></div>';
      html+='</div>';
    }
    document.getElementById('apiKeyList').innerHTML=html;
  }catch(e){console.log('Solver refresh error:',e)}
}

// ── Discord Automation ────────────────────────────────
async function start(){
  let email=document.getElementById('email').value;
  let r=await api('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
  document.getElementById('status').textContent=await r.text();
}
async function stop(){
  let r=await api('/stop',{method:'POST'});
  document.getElementById('status').textContent=await r.text();
}
async function refresh(){
  try{
    let r=await api('/status');let x=await r.json();
    let s=document.getElementById('status');
    if(x.running){
      s.textContent=x.screenshots?'▶ Running · '+x.screenshots+' screenshot(s)':'▶ Running · waiting';
      s.style.color='#a7f3d0';
    }else{s.textContent='■ Stopped';s.style.color='#fca5a5'}
    let img=document.getElementById('shot');let ph=document.getElementById('camPlaceholder');
    if(x.screenshots){img.src='/latest?'+Date.now();img.style.display='block';ph.style.display='none'}
    else{img.style.display='none';ph.style.display='flex'}
  }catch(e){document.getElementById('status').textContent='Unable to reach service'}
}
async function refreshLog(){
  try{
    let r=await api('/activity');let logs=await r.json();
    if(logs.length===0)return;
    let html='';let recent=logs.slice(-50).reverse();
    for(let e of recent){let cls=e.level||'info';html+='<div class="entry"><span class="time">'+e.time+'</span><span class="'+cls+'">'+e.message+'</span></div>'}
    document.getElementById('log').innerHTML=html;
  }catch(e){}
}
async function checkDB(){
  try{
    let r=await api('/api/tokens');await r.json();
    document.getElementById('dbStatus').textContent='✅ Database connected';
    refreshTokens();
  }catch(e){
    document.getElementById('dbStatus').textContent='⚠️ No database (set DATABASE_URL to save tokens)';
    document.getElementById('activeTokens').textContent='--';
    document.getElementById('expiredTokens').textContent='--';
    document.getElementById('tokenList').innerHTML='<div style="color:#fca5a5;font-size:12px;text-align:center;padding:12px">Database not configured — set DATABASE_URL</div>';
  }
}
setInterval(refresh,3000);
setInterval(refreshLog,2000);
refresh();refreshLog();checkDB();refreshSolvers();
</script></body></html>"""


if __name__ == "__main__":
    asyncio.run(main())
