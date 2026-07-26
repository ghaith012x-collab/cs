import asyncio
import base64
import json
import os
import sys
from typing import Optional

from aiohttp import web
from server import DiscordAutomation
from captcha_solver import VisionSolver


class AppHost:
    def __init__(self):
        self._automation: Optional[DiscordAutomation] = None
        self._running = False
        self._config_path = "config.json"
        self._web_port = 8080

    def load_config(self, path: str = "config.json") -> dict:
        default_config = {
            "email": "alistra742@gmail.com",
            "headless": True,
            "web_port": 8080,                "run_automation": False,
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

    async def start_automation(self) -> None:
        if self._automation and self._running:
            print("Automation already running")
            return
        config = self.load_config(self._config_path)
        self._automation = DiscordAutomation(headless=config.get('headless', True))
        self._running = True
        try:
            await self._automation.initialize()
            screenshot_task = asyncio.create_task(
                self._capture_periodic_screenshots(config.get('camera_interval', 3))
            )
            # Auto-fill email and start
            email = config.get('email', 'alistra742@gmail.com')
            self._log(f"Auto-starting with email: {email}")
            success = await self._automation.start_discord_signup()
            if success:
                self._log("✓ Automation completed successfully")
            else:
                self._log("✗ Automation failed", level="error")
            screenshot_task.cancel()
        except Exception as e:
            self._log(f"Error during automation: {e}", level="error")
            import traceback
            traceback.print_exc()
        finally:
            await self._cleanup()

    async def _capture_periodic_screenshots(self, interval: int) -> None:
        while self._running:
            try:
                await self._automation.capture_screenshot()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except:
                await asyncio.sleep(interval)

    async def stop_automation(self) -> None:
        self._running = False
        if self._automation:
            await self._automation.close()
            self._automation = None
        self._log("Automation stopped")

    async def _cleanup(self) -> None:
        self._running = False
        if self._automation:
            await self._automation.close()
            self._automation = None

    async def start_web_server(self, port: int = 8080) -> None:
        self._web_port = port

        async def handle_status(request):
            if self._automation:
                return web.json_response({
                    "running": self._running,
                    "screenshots": len(self._automation.get_screenshots()),
                    "email": self._automation._email if self._automation else "",
                    "username": self._automation._username if self._automation else ""
                })
            return web.json_response({"running": False, "screenshots": 0})

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
                return web.Response(text="Already running")
            asyncio.create_task(self.start_automation())
            return web.Response(text="Started")

        async def handle_stop(request):
            await self.stop_automation()
            return web.Response(text="Stopped")

        # Router
        app = web.Application()
        app.router.add_get('/', handle_root)
        app.router.add_post('/start', handle_start)
        app.router.add_post('/stop', handle_stop)
        app.router.add_get('/status', handle_status)
        app.router.add_get('/latest', handle_latest_screenshot)
        app.router.add_get('/activity', handle_activity_log)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        self._log(f"Web server on 0.0.0.0:{port}")
        return runner


async def main():
    config = {}
    try:
        with open("config.json", 'r') as f:
            config = json.load(f)
    except:
        pass

    web_port = int(os.environ.get('PORT', config.get('web_port', 8080)))
    host = AppHost()

    # Auto-create config with email
    if not os.path.exists("config.json"):
        with open("config.json", 'w') as f:
            json.dump({
                "email": "alistra742@gmail.com",
                "headless": True,
                "web_port": web_port,
                "run_automation": False,
            }, f, indent=2)
        print("Created config.json with alistra742@gmail.com", flush=True)

    await host.start_web_server(web_port)

    # Manual start only — user clicks Start in dashboard
    print("=" * 50, flush=True)
    print("  CLIP Vision AI ready — click Start in dashboard", flush=True)
    print(f"  Email: alistra742@gmail.com", flush=True)
    print("=" * 50, flush=True)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass


# ═══════════════════════════════════════════════════════════
# DASHBOARD HTML
# ═══════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CLIP Vision AI - Captcha Solver</title>
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
.model-status{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#0f172a;border-radius:8px;margin:8px 0}
.model-dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.model-dot.loading{background:#f59e0b;animation:pulse 1s infinite}
.model-dot.loaded{background:#22c55e}
.model-dot.error{background:#ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:4px">
  <h1>🧠 CLIP Vision AI</h1>
  <div class="model-status" id="modelStatus">
    <span class="model-dot loading" id="modelDot"></span>
    <span id="modelText" style="font-size:13px">Loading CLIP model...</span>
  </div>
</div>
<p><small>Custom AI vision solver · No external APIs · Pure CPU inference</small></p>

<div class="card">
  <h3>📊 Solver Stats</h3>
  <div class="stats-grid">
    <div class="stat-card"><div class="num cyan" id="statChallenges">0</div><div class="label">Challenges</div></div>
    <div class="stat-card"><div class="num green" id="statSolved">0</div><div class="label">Solved</div></div>
    <div class="stat-card"><div class="num red" id="statFailed">0</div><div class="label">Failed</div></div>
    <div class="stat-card"><div class="num cyan" id="statTiles">0</div><div class="label">Tiles Classified</div></div>
  </div>
</div>

<div class="card">
  <h3>🤖 Discord Automation</h3>
  <p style="font-size:13px;color:#94a3b8;margin-bottom:8px">Email: <strong>alistra742@gmail.com</strong> · Manual start</p>
  <div class="btn-group">
    <button class="btn-primary" onclick="start()">▶ Start</button>
    <button class="btn-stop" onclick="stop()">■ Stop</button>
  </div>
  <div id="status">Starting...</div>
  <div class="cam-wrap">
    <div class="cam-placeholder" id="camPlaceholder">Waiting for screenshot...</div>
    <img id="shot" alt="Live view" style="display:none">
  </div>
  <h2 style="margin-top:12px;font-size:12px;color:#94a3b8;font-weight:600">📝 Activity Log</h2>
  <div id="log"><div class="entry"><span class="time">--:--:--</span><span class="info">Starting CLIP Vision AI...</span></div></div>
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

async function refresh(){
  try{
    let r=await api('/status');let x=await r.json();
    let s=document.getElementById('status');
    if(x.running){
      s.textContent=x.screenshots?'▶ Running · '+x.screenshots+' screenshot(s)':'▶ Running';
      s.style.color='#a7f3d0';
    }else{
      s.textContent='■ Stopped';
      s.style.color='#fca5a5';
    }
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
      if(msg.includes('[Vision')||msg.includes('CLIP')) cls='vision';
      html+='<div class="entry"><span class="time">'+e.time+'</span><span class="'+cls+'">'+msg+'</span></div>';
    }
    document.getElementById('log').innerHTML=html;
    // Check for success message
    if(logs.some(l=>l.message&&l.message.includes('SOLVED'))) {
      document.getElementById('status').textContent='✅ SOLVED!';
      document.getElementById('status').style.color='#22c55e';
    }
  }catch(e){}
}

async function checkModel(){
  try{
    let r=await api('/status');let x=await r.json();
    // Model status from activity log
    let logR=await api('/activity');let logs=await logR.json();
    if(logs.some(l=>l.message&&l.message.includes('CLIP model loaded'))){
      document.getElementById('modelDot').className='model-dot loaded';
      document.getElementById('modelText').textContent='CLIP model loaded ✅';
      document.getElementById('modelText').style.color='#22c55e';
    } else if(logs.some(l=>l.message&&l.message.includes('CLIP model failed'))){
      document.getElementById('modelDot').className='model-dot error';
      document.getElementById('modelText').textContent='CLIP model failed ❌';
      document.getElementById('modelText').style.color='#ef4444';
    }
  }catch(e){}
}

setInterval(refresh,3000);
setInterval(refreshLog,2000);
setInterval(checkModel,5000);
refresh();
refreshLog();
checkModel();
</script></body></html>"""


if __name__ == "__main__":
    asyncio.run(main())
