import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional

from aiohttp import web
from server import DiscordAutomation, run_discord_automation


class AppHost:
    def __init__(self):
        self._automation: Optional[DiscordAutomation] = None
        self._running = False
        self._config_path = "config.json"
        self._web_server = None
        self._web_port = 8080

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
            
            success = await self._automation.start_discord_signup()
            
            if success:
                print("✓ Automation completed successfully")
            else:
                print("✗ Automation failed")
            
            await self._capture_periodic_screenshots(config.get('camera_interval', 3))
            
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
            return web.Response(text="""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Discord Automation</title>
<style>
body{font-family:system-ui;background:#111827;color:#f9fafb;max-width:760px;margin:0 auto;padding:28px}
input,button{font-size:16px;padding:12px;border-radius:8px;border:0;margin:5px 0}
input{width:calc(100% - 24px)}
button{cursor:pointer;background:#5865f2;color:white;margin-right:8px}
.stop{background:#ef4444}
#status{margin:18px 0;color:#a7f3d0}
img{width:100%;min-height:180px;object-fit:contain;background:#000;border-radius:10px}
small{color:#9ca3af}
#log{margin-top:20px;background:#1f2937;border-radius:10px;padding:16px;max-height:400px;overflow-y:auto;font-family:'Courier New',monospace;font-size:13px;line-height:1.6}
#log .entry{padding:2px 0;border-bottom:1px solid #374151}
#log .time{color:#6b7280;margin-right:8px}
#log .info{color:#a7f3d0}
#log .error{color:#fca5a5}
#log .warn{color:#fde68a}
h2{margin-top:24px;font-size:18px;color:#d1d5db}
</style></head><body>
<h1>Discord Automation</h1>
<p><small>Railway live dashboard</small></p>
<label>Email</label>
<input id="email" type="email" placeholder="your email">
<div><button onclick="start()">Start</button><button class="stop" onclick="stop()">Stop</button></div>
<div id="status">Checking status...</div>
<img id="shot" alt="Live view will appear here">
<h2>Activity Log</h2>
<div id="log"><div class="entry"><span class="time">--:--:--</span><span class="info">Waiting for activity...</span></div></div>
<script>
async function api(path,opts){return fetch(path,opts)}
async function start(){let email=document.getElementById('email').value;let r=await api('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});document.getElementById('status').textContent=await r.text()}
async function stop(){let r=await api('/stop',{method:'POST'});document.getElementById('status').textContent=await r.text()}
async function refresh(){
  try{
    let r=await api('/status');let x=await r.json();
    document.getElementById('status').textContent=x.running?(x.screenshots?'Running \u00b7 '+x.screenshots+' screenshot(s)':'Running \u00b7 waiting for first screenshot'):'Stopped';
    if(x.screenshots)document.getElementById('shot').src='/latest?'+Date.now();
  }catch(e){document.getElementById('status').textContent='Unable to reach service'}
}
async function refreshLog(){
  try{
    let r=await api('/activity');
    let logs=await r.json();
    if(logs.length===0)return;
    let html='';
    // Show last 50 entries, newest first
    let recent=logs.slice(-50).reverse();
    for(let entry of recent){
      let cls=entry.level||'info';
      html+='<div class="entry"><span class="time">'+entry.time+'</span><span class="'+cls+'">'+entry.message+'</span></div>';
    }
    document.getElementById('log').innerHTML=html;
  }catch(e){}
}
setInterval(refresh,3000);
setInterval(refreshLog,2000);
refresh();refreshLog();
</script></body></html>""", content_type='text/html')

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

        app = web.Application()
        app.router.add_get('/', handle_root)
        app.router.add_post('/start', handle_start)
        app.router.add_post('/stop', handle_stop)
        app.router.add_get('/status', handle_status)
        app.router.add_get('/screenshot', handle_screenshot)
        app.router.add_get('/latest', handle_latest_screenshot)
        app.router.add_get('/activity', handle_activity_log)
        
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


if __name__ == "__main__":
    asyncio.run(main())