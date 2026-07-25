import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional

from aiohttp import web
from server import DiscordAutomation, run_discord_automation
from farm import FarmSession, KnowledgeDB


class AppHost:
    def __init__(self):
        self._automation: Optional[DiscordAutomation] = None
        self._running = False
        self._config_path = "config.json"
        self._web_server = None
        self._web_port = 8080
        self._db: Optional[KnowledgeDB] = None
        self._farm: Optional[FarmSession] = None

    def load_config(self, path: str = "config.json") -> dict:
        default_config = {
            "email": "test@example.com",
            "username": "",
            "password": "Password123!",
            "headless": True,
            "camera_interval": 3,
            "web_port": 8080,
            "run_automation": False
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

    def show_banner(self) -> None:
        print("=" * 50)
        print("  Discord Automation Suite")
        print("  Version 1.0.0")
        print("=" * 50)
        print()

    def show_help(self) -> None:
        print("Commands:")
        print("  start       - Start Discord automation")
        print("  stop        - Stop automation")
        print("  status      - Check status")
        print("  config      - Show current config")
        print("  screenshot  - Get latest screenshot")
        print("  help        - Show this help")
        print("  exit        - Exit the application")
        print()

    def display_screenshots(self, count: int = 5) -> None:
        if not self._automation:
            print("Automation not initialized")
            return
        
        screenshots = self._automation.get_screenshots()
        print(f"\nAvailable screenshots: {len(screenshots)}")
        for i, _ in enumerate(screenshots[-count:]):
            print(f"  [{len(screenshots) - count + i + 1}] Screenshot captured")

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
            
            # Start screenshot loop as background task DURING signup
            screenshot_task = asyncio.create_task(
                self._capture_periodic_screenshots(config.get('camera_interval', 3))
            )
            
            success = await self._automation.start_discord_signup()
            
            if success:
                print("✓ Automation completed successfully")
            else:
                print("✗ Automation failed")
            
            # Keep capturing after signup too
            await screenshot_task
            
        except Exception as e:
            print(f"Error during automation: {e}")
        finally:
            await self._cleanup()

    async def _capture_periodic_screenshots(self, interval: int) -> None:
        print(f"Capturing screenshots every {interval} seconds...")
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

    async def run_shell(self) -> None:
        self.show_banner()
        self.show_help()
        
        while True:
            try:
                cmd = input("\n> ").strip().lower()
                
                if cmd in ['exit', 'quit', 'q']:
                    if self._running:
                        await self.stop_automation()
                    print("Goodbye!")
                    break
                
                elif cmd == 'start':
                    await self.start_automation()
                
                elif cmd == 'stop':
                    await self.stop_automation()
                
                elif cmd == 'status':
                    if self._automation and self._running:
                        print("Status: Running")
                        screenshots = len(self._automation.get_screenshots())
                        print(f"Screenshots captured: {screenshots}")
                    else:
                        print("Status: Stopped")
                
                elif cmd == 'config':
                    config = self.load_config(self._config_path)
                    print(json.dumps(config, indent=2))
                
                elif cmd == 'screenshot':
                    if self._automation:
                        self.display_screenshots()
                    else:
                        print("Automation not running")
                
                elif cmd == 'help':
                    self.show_help()
                
                else:
                    print(f"Unknown command: {cmd}")
                    self.show_help()
            
            except KeyboardInterrupt:
                if self._running:
                    await self.stop_automation()
                print("\nGoodbye!")
                break
            
            except Exception as e:
                print(f"Error: {e}")

    async def init_db(self):
        """Initialize the knowledge database."""
        if self._db is None:
            self._db = await KnowledgeDB.create(log=self._log)
        return self._db

    def _log(self, msg: str, level: str = "info"):
        print(f"[{level.upper()}] {msg}", flush=True)

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
                    except Exception as e:
                        print(f"Screenshot decode error: {e}", flush=True)
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

        # ── Farm Routes ────────────────────────────────────

        async def handle_farm(request):
            return web.Response(text=FARM_HTML, content_type='text/html')

        async def handle_farm_start(request):
            if self._farm and self._farm.running:
                return web.json_response({"status": "already_running"})
            try:
                self._farm = FarmSession(db=self._db, log=self._log)
                ok = await self._farm.start()
                return web.json_response({"status": "started" if ok else "failed"})
            except Exception as e:
                return web.json_response({"status": "error", "error": str(e)}, status=500)

        async def handle_farm_stop(request):
            if self._farm:
                await self._farm.stop()
                return web.json_response({"status": "stopped"})
            return web.json_response({"status": "not_running"})

        async def handle_farm_status(request):
            if self._farm:
                return web.json_response(self._farm.get_status())
            return web.json_response({"running": False, "captchas_solved": 0, "captchas_failed": 0, "total_captchas": 0, "recognitions_count": 0})

        async def handle_farm_recognitions(request):
            if self._farm:
                recs = self._farm.get_recent_recognitions(count=50)
                return web.json_response(recs)
            return web.json_response([])

        async def handle_farm_cam(request):
            if self._farm:
                png = self._farm.get_latest_png()
                if png:
                    return web.Response(body=png, content_type='image/png')
            return web.Response(status=204)

        async def handle_farm_db_summary(request):
            if self._db and not self._db._noop:
                summary = await self._db.get_knowledge_summary()
                return web.json_response(summary)
            return web.json_response([])

        app = web.Application()
        app.router.add_get('/', handle_root)
        app.router.add_post('/start', handle_start)
        app.router.add_post('/stop', handle_stop)
        app.router.add_get('/status', handle_status)
        app.router.add_get('/screenshot', handle_screenshot)
        app.router.add_get('/latest', handle_latest_screenshot)
        app.router.add_get('/activity', handle_activity_log)
        # Farm routes
        app.router.add_get('/farm', handle_farm)
        app.router.add_post('/api/farm/start', handle_farm_start)
        app.router.add_post('/api/farm/stop', handle_farm_stop)
        app.router.add_get('/api/farm/status', handle_farm_status)
        app.router.add_get('/api/farm/recognitions', handle_farm_recognitions)
        app.router.add_get('/api/farm/cam', handle_farm_cam)
        app.router.add_get('/api/farm/knowledge', handle_farm_db_summary)
        
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
        "web_port": 8080
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


# ── Dashboard HTML ──────────────────────────────────────

DASHBOARD_HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Discord Automation</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui;background:#0a0f1e;color:#f1f5f9;max-width:800px;margin:0 auto;padding:24px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}
h1{font-size:22px;font-weight:700;background:linear-gradient(135deg,#818cf8,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.farm-btn{background:linear-gradient(135deg,#f59e0b,#d97706);color:#1a1a2e;padding:10px 20px;border-radius:12px;border:0;font-weight:700;font-size:14px;cursor:pointer;text-decoration:none;transition:transform .15s,box-shadow .15s}
.farm-btn:hover{transform:scale(1.05);box-shadow:0 4px 20px rgba(245,158,11,.35)}
.card{background:#131a2e;border-radius:14px;padding:20px;margin-bottom:16px;border:1px solid #1e2a45}
label{font-size:14px;font-weight:600;color:#94a3b8;display:block;margin-bottom:6px}
input,button{font-size:15px;padding:12px 16px;border-radius:10px;border:0}
input{width:100%;background:#1e2a45;color:#f1f5f9;outline:0;transition:border .2s}
input:focus{border:1px solid #6366f1}
button{cursor:pointer;transition:all .15s;font-weight:600}
.btn-primary{background:#6366f1;color:white;margin-right:8px}
.btn-primary:hover{background:#4f46e5;transform:translateY(-1px)}
.btn-stop{background:#ef4444;color:white}
.btn-stop:hover{background:#dc2626;transform:translateY(-1px)}
.btn-group{display:flex;gap:8px;flex-wrap:wrap}
#status{margin:12px 0;color:#a7f3d0;font-size:14px;font-weight:500}
.cam-wrap{background:#000;border-radius:12px;overflow:hidden;min-height:200px;position:relative}
.cam-wrap img{width:100%;display:block;min-height:180px;object-fit:contain}
.cam-placeholder{display:flex;align-items:center;justify-content:center;min-height:180px;color:#475569;font-size:14px}
#log{background:#0d1326;border-radius:12px;padding:14px;max-height:350px;overflow-y:auto;font-family:'JetBrains Mono','Courier New',monospace;font-size:12px;line-height:1.7}
#log .entry{padding:3px 0;border-bottom:1px solid #1a2440}
#log .time{color:#4b5563;margin-right:10px}
#log .info{color:#a7f3d0}
#log .error{color:#fca5a5}
#log .warn{color:#fde68a}
h2{margin-top:20px;font-size:16px;color:#94a3b8;font-weight:600}
small{color:#64748b}
.badge{display:inline-block;background:#1e2a45;color:#94a3b8;padding:4px 10px;border-radius:20px;font-size:12px;margin-left:8px}
</style></head><body>
<div class="header">
  <h1>Discord Automation</h1>
  <a href="/farm" class="farm-btn">⚗️ Farm</a>
</div>
<p><small>Live dashboard</small></p>
<div class="card">
  <label>Email</label>
  <input id="email" type="email" placeholder="your email">
  <div class="btn-group" style="margin-top:10px">
    <button class="btn-primary" onclick="start()">▶ Start</button>
    <button class="btn-stop" onclick="stop()">■ Stop</button>
  </div>
</div>
<div id="status">Checking status...</div>
<div class="cam-wrap" id="camWrap">
  <div class="cam-placeholder" id="camPlaceholder">Waiting for first screenshot...</div>
  <img id="shot" alt="Live view" style="display:none">
</div>
<h2>Activity Log</h2>
<div id="log"><div class="entry"><span class="time">--:--:--</span><span class="info">Waiting for activity...</span></div></div>
<script>
async function api(path,opts){return fetch(path,opts)}
async function start(){let email=document.getElementById('email').value;let r=await api('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});document.getElementById('status').textContent=await r.text()}
async function stop(){let r=await api('/stop',{method:'POST'});document.getElementById('status').textContent=await r.text()}
async function refresh(){
  try{
    let r=await api('/status');let x=await r.json();
    let s=document.getElementById('status');
    if(x.running){s.textContent=x.screenshots?'▶ Running · '+x.screenshots+' screenshot(s)':'▶ Running · waiting for first screenshot';s.style.color='#a7f3d0'}
    else{s.textContent='■ Stopped';s.style.color='#fca5a5'}
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
setInterval(refresh,3000);setInterval(refreshLog,2000);refresh();refreshLog();
</script></body></html>"""

# ── Farm Page HTML ───────────────────────────────────────

FARM_HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Recognition Farm</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui;background:#0a0f1e;color:#f1f5f9;min-height:100vh;display:flex;flex-direction:column}
.top-bar{display:flex;justify-content:space-between;align-items:center;padding:16px 24px;background:#131a2e;border-bottom:1px solid #1e2a45}
.top-bar h1{font-size:20px;font-weight:700;background:linear-gradient(135deg,#fbbf24,#f59e0b);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.back-btn{background:#1e2a45;color:#94a3b8;padding:8px 18px;border-radius:10px;border:0;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;transition:all .15s}
.back-btn:hover{background:#2a3a5a;color:#f1f5f9}
.main{flex:1;display:flex;gap:20px;padding:20px 24px;max-width:1400px;margin:0 auto;width:100%}
@media(max-width:900px){.main{flex-direction:column}}
.left-panel{flex:1;min-width:0}
.right-panel{width:380px;flex-shrink:0;display:flex;flex-direction:column;gap:16px}
@media(max-width:900px){.right-panel{width:100%}}
.card{background:#131a2e;border-radius:14px;padding:18px;border:1px solid #1e2a45}
.card-title{font-size:13px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.cam-wrap{background:#000;border-radius:12px;overflow:hidden;min-height:300px;position:relative}
.cam-wrap img{width:100%;display:block;min-height:300px;object-fit:contain}
.cam-placeholder{display:flex;align-items:center;justify-content:center;min-height:300px;color:#475569;font-size:14px}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.btn-start{background:linear-gradient(135deg,#22c55e,#16a34a);color:white;padding:12px 28px;border-radius:12px;border:0;font-weight:700;font-size:15px;cursor:pointer;transition:all .15s}
.btn-start:hover{transform:scale(1.03);box-shadow:0 4px 20px rgba(34,197,94,.3)}
.btn-start:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-stop-farm{background:linear-gradient(135deg,#ef4444,#dc2626);color:white;padding:12px 28px;border-radius:12px;border:0;font-weight:700;font-size:15px;cursor:pointer;transition:all .15s}
.btn-stop-farm:hover{transform:scale(1.03);box-shadow:0 4px 20px rgba(239,68,68,.3)}
.stats{display:flex;gap:12px;flex-wrap:wrap}
.stat{background:#0d1326;border-radius:10px;padding:12px 16px;text-align:center;flex:1;min-width:70px}
.stat-value{font-size:24px;font-weight:800;color:#fbbf24}
.stat-label{font-size:11px;color:#64748b;text-transform:uppercase;margin-top:2px}
.stat-value.solved{color:#22c55e}
.stat-value.failed{color:#ef4444}
.recog-list{flex:1;overflow-y:auto;max-height:400px;min-height:200px}
.recog-item{background:#0d1326;border-radius:8px;padding:10px 12px;margin-bottom:6px;border-left:3px solid #6366f1}
.recog-item.success{border-left-color:#22c55e}
.recog-item.failed{border-left-color:#ef4444}
.recog-time{font-size:11px;color:#4b5563}
.recog-type{font-size:11px;color:#818cf8;font-weight:600}
.recog-text{font-size:13px;color:#e2e8f0;margin-top:2px}
.recog-objects{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}
.recog-object{background:#1e2a45;color:#fbbf24;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.recog-empty{color:#475569;font-size:13px;text-align:center;padding:40px 0}
.knowledge-list{max-height:200px;overflow-y:auto}
.knowledge-item{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1a2440;font-size:13px}
.knowledge-item:last-child{border:0}
.knowledge-name{color:#e2e8f0;font-weight:500}
.knowledge-count{color:#fbbf24;font-weight:700}
.status-badge{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:20px;font-size:13px;font-weight:600}
.status-badge.running{background:#064e3b;color:#6ee7b7}
.status-badge.stopped{background:#27272a;color:#a1a1aa}
</style></head><body>
<div class="top-bar">
  <a href="/" class="back-btn">← Back</a>
  <h1>⚗️ Recognition Farm</h1>
  <div id="statusBadge" class="status-badge stopped">■ Stopped</div>
</div>
<div class="main">
  <div class="left-panel">
    <div class="card">
      <div class="card-title">Live Cam</div>
      <div class="cam-wrap" id="camWrap">
        <div class="cam-placeholder" id="camPlaceholder">Farm not started</div>
        <img id="camShot" alt="Live view" style="display:none">
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="controls" id="controls">
        <button class="btn-start" id="btnStart" onclick="farmStart()">▶ Start Farming</button>
        <button class="btn-stop-farm" id="btnStop" onclick="farmStop()" style="display:none">■ Stop Farming</button>
      </div>
      <div class="stats" style="margin-top:14px" id="stats">
        <div class="stat"><div class="stat-value solved" id="statSolved">0</div><div class="stat-label">Solved</div></div>
        <div class="stat"><div class="stat-value failed" id="statFailed">0</div><div class="stat-label">Failed</div></div>
        <div class="stat"><div class="stat-value" id="statTotal">0</div><div class="stat-label">Total</div></div>
        <div class="stat"><div class="stat-value" id="statLearned">0</div><div class="stat-label">Learned</div></div>
      </div>
    </div>
  </div>
  <div class="right-panel">
    <div class="card">
      <div class="card-title">🧠 Recent Recognitions</div>
      <div class="recog-list" id="recogList">
        <div class="recog-empty">Start farming to see recognitions...</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">📊 Knowledge Database</div>
      <div class="knowledge-list" id="knowledgeList">
        <div class="recog-empty">No data yet</div>
      </div>
    </div>
  </div>
</div>
<script>
let pollInterval;

async function api(path,opts){return fetch(path,opts)}

async function farmStart(){
  document.getElementById('btnStart').disabled=true;
  document.getElementById('btnStart').textContent='Starting...';
  let r=await api('/api/farm/start',{method:'POST'});
  let x=await r.json();
  if(x.status==='started'){
    document.getElementById('btnStart').style.display='none';
    document.getElementById('btnStop').style.display='inline-block';
    pollInterval=setInterval(pollStatus,2000);
  } else {
    document.getElementById('btnStart').disabled=false;
    document.getElementById('btnStart').textContent='\u25b6 Start Farming';
    alert('Failed to start: '+x.status);
  }
}

async function farmStop(){
  clearInterval(pollInterval);
  await api('/api/farm/stop',{method:'POST'});
  document.getElementById('btnStart').style.display='inline-block';
  document.getElementById('btnStop').style.display='none';
  document.getElementById('btnStart').disabled=false;
  document.getElementById('btnStart').textContent='\u25b6 Start Farming';
  document.getElementById('statusBadge').className='status-badge stopped';
  document.getElementById('statusBadge').innerHTML='\u25a0 Stopped';
}

async function pollStatus(){
  try{
    // Farm status
    let r=await api('/api/farm/status');
    let s=await r.json();
    document.getElementById('statSolved').textContent=s.captchas_solved||0;
    document.getElementById('statFailed').textContent=s.captchas_failed||0;
    document.getElementById('statTotal').textContent=s.total_captchas||0;
    
    let badge=document.getElementById('statusBadge');
    if(s.running){badge.className='status-badge running';badge.innerHTML='\u25b6 Running'}
    else{badge.className='status-badge stopped';badge.innerHTML='\u25a0 Stopped'}
    
    // Cam
    let img=document.getElementById('camShot');let ph=document.getElementById('camPlaceholder');
    if(s.captchas_solved+s.captchas_failed>0){
      img.src='/api/farm/cam?'+Date.now();
      img.style.display='block';ph.style.display='none'
    } else if(s.running){
      img.src='/api/farm/cam?'+Date.now();
      img.style.display='block';ph.style.display='none'
    }
    
    // Recognitions
    let rr=await api('/api/farm/recognitions');
    let recs=await rr.json();
    let rl=document.getElementById('recogList');
    if(recs.length===0){
      rl.innerHTML='<div class="recog-empty">Waiting for captchas...</div>';
    } else {
      let html='';
      for(let r of recs.slice(0,30)){
        let cls=r.success?'success':'failed';
        let icon=r.success?'\u2705':'\u274c';
        let type=r.challenge_type||'?';
        let objects=(r.objects_found||[]).join(', ');
        let time=new Date(r.timestamp*1000).toLocaleTimeString();
        html+='<div class="recog-item '+cls+'">';
        html+='<div><span class="recog-time">'+time+'</span> <span class="recog-type">'+type+'</span> <span>'+icon+'</span></div>';
        html+='<div class="recog-text">'+(r.challenge_text||'')+'</div>';
        if(objects)html+='<div class="recog-objects">'+objects.split(',').map(o=>'<span class="recog-object">'+o.trim()+'</span>').join('')+'</div>';
        html+='</div>';
      }
      rl.innerHTML=html;
    }
    
    // Knowledge
    let kr=await api('/api/farm/knowledge');
    let k=await kr.json();
    document.getElementById('statLearned').textContent=k.length;
    let kl=document.getElementById('knowledgeList');
    if(k.length===0){
      kl.innerHTML='<div class="recog-empty">No data yet</div>';
    } else {
      let html='';
      for(let item of k.slice(0,20)){
        html+='<div class="knowledge-item"><span class="knowledge-name">'+item.class_name+'</span><span class="knowledge-count">'+item.sample_count+'</span></div>';
      }
      kl.innerHTML=html;
    }
  }catch(e){console.log('Poll error:',e)}
}
</script></body></html>"""


if __name__ == "__main__":
    asyncio.run(main())