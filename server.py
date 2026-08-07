import asyncio
import base64
import json
import os
import random
import socket
import time
from typing import Optional

from playwright.async_api import async_playwright

from captcha_solver import (
    NoCaptchaAI,
    extract_hcaptcha_sitekey,
    extract_hcaptcha_rqdata,
    extract_funcaptcha_task,
    read_hcaptcha_token,
    set_hcaptcha_token_on_page,
    solve_funcaptcha_pixels,
    solve_hcaptcha_drag,
    solve_hcaptcha_accessibility,
    _probe_drag_dom,
)
from duckmail import TempMail


# ── TOR Control ───────────────────────────────────────────

def _tor_newnym():
    """Signal TOR to switch to a new identity (fresh exit node)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect(("127.0.0.1", 9051))
        s.recv(1024)
        s.sendall(b"AUTHENTICATE\r\n")
        auth_resp = s.recv(1024).decode().strip()
        if "250" not in auth_resp:
            s.close()
            print(f"[TOR] auth failed: {auth_resp}", flush=True)
            return False
        s.sendall(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(1024).decode().strip()
        s.close()
        if "250" in resp:
            time.sleep(3)
            return True
        print(f"[TOR] newnym rejected: {resp}", flush=True)
    except Exception as e:
        print(f"[TOR] newnym error: {e}", flush=True)
    return False


def _tor_check():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 9050))
        s.close()
        return True
    except:
        return False


USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
]

PAST_CAPTCHA_KEYWORDS = ['/channels', '/verify', '/welcome', '/login', '@me', 'discord.com/app']

INIT_SCRIPT = """
// ==============================================
// Anti-detection: incognito + humanized fingerprints v2
// ==============================================
(function(){
  // --- Hide that we're in incognito ---
  Object.defineProperty(navigator, 'cookieEnabled', { get: () => true });

  // --- Canvas fingerprint noise ---
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type) {
    const ctx = this.getContext('2d');
    if (ctx) {
      const w = this.width, h = this.height;
      const imgData = ctx.getImageData(0, 0, Math.min(w, 10), Math.min(h, 10));
      for (let i = 0; i < Math.min(imgData.data.length, 20); i += 4) {
        imgData.data[i] = imgData.data[i] ^ (Math.floor(Math.random() * 3) - 1);
      }
      ctx.putImageData(imgData, 0, 0);
    }
    return origToDataURL.apply(this, arguments);
  };
  const origToBlob = HTMLCanvasElement.prototype.toBlob;
  HTMLCanvasElement.prototype.toBlob = function(cb, type) {
    const ctx = this.getContext('2d');
    if (ctx) {
      const w = this.width, h = this.height;
      const imgData = ctx.getImageData(0, 0, Math.min(w, 10), Math.min(h, 10));
      for (let i = 0; i < Math.min(imgData.data.length, 20); i += 4) {
        imgData.data[i] = imgData.data[i] ^ (Math.floor(Math.random() * 3) - 1);
      }
      ctx.putImageData(imgData, 0, 0);
    }
    return origToBlob.apply(this, arguments);
  };

  // --- WebGL fingerprint ---
  const vendors = ['Google Inc. (Intel)', 'Google Inc. (NVIDIA)', 'Google Inc. (AMD)', 'Google Inc.'];
  const renderers = [
    'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)',
    'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)',
    'ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0)',
    'ANGLE (Intel, Intel(R) Iris Xe Graphics Direct3D11 vs_5_0 ps_5_0)',
    'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0)',
  ];
  const pick = Math.floor(Math.random() * vendors.length);
  const vendor = vendors[pick];
  const renderer = renderers[pick];
  try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return vendor;   // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return renderer; // UNMASKED_RENDERER_WEBGL
      return getParam.call(this, p);
    };
    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return vendor;
      if (p === 37446) return renderer;
      return getParam2.call(this, p);
    };
  } catch(e) {}

  // --- AudioContext fingerprint ---
  try {
    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {
      const data = origGetChannelData.call(this, channel);
      for (let i = 0; i < Math.min(data.length, 10); i++) {
        data[i] = data[i] + (Math.random() * 1e-10 - 5e-11);
      }
      return data;
    };
    const origCreateAnalyser = AudioContext.prototype.createAnalyser;
    AudioContext.prototype.createAnalyser = function() {
      const analyser = origCreateAnalyser.call(this);
      const origGetFloatFreq = analyser.getFloatFrequencyData;
      analyser.getFloatFrequencyData = function(arr) {
        origGetFloatFreq.call(this, arr);
        for (let i = 0; i < Math.min(arr.length, 5); i++) {
          arr[i] = arr[i] + (Math.random() * 1e-10 - 5e-11);
        }
      };
      return analyser;
    };
  } catch(e) {}

  // --- Navigator properties ---
  const platforms = ['Win32', 'Win32', 'MacIntel', 'Linux x86_64'];
  const cores = [4, 8, 8, 12, 16, 16];
  const mem = [4, 8, 8, 16, 16, 32];
  const touches = [0, 0, 0, 0, 0, 0, 5];
  const p = platforms[Math.floor(Math.random() * platforms.length)];
  const c = cores[Math.floor(Math.random() * cores.length)];
  const m = mem[Math.floor(Math.random() * mem.length)];
  const t = touches[Math.floor(Math.random() * touches.length)];

  Object.defineProperty(navigator, 'webdriver', { get: () => false });
  Object.defineProperty(navigator, 'languages', { get: () => Object.freeze(['en-US', 'en']) });
  Object.defineProperty(navigator, 'platform', { get: () => p });
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => c });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => m });
  Object.defineProperty(navigator, 'maxTouchPoints', { get: () => t });
  Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
  Object.defineProperty(navigator, 'productSub', { get: () => '20030107' });
  Object.defineProperty(navigator, 'appVersion', { get: () => navigator.userAgent.replace('Mozilla/', '') });

  // --- Hide Chrome automation ---
  if (window.chrome) { window.chrome.runtime = {}; }
  delete window.__nightmare;
  delete window._phantom;
  delete window.callPhantom;
  delete window.Buffer;
  Object.defineProperty(window, 'chrome', { get: () => ({ runtime: {} }), set: () => {} });

  // --- Plugins array ---
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const arr = [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
      ];
      arr.item = (i) => arr[i];
      arr.namedItem = (n) => arr.find(x => x.name === n);
      arr.refresh = () => {};
      Object.defineProperty(arr, 'length', { get: () => 3 });
      return arr;
    }
  });

  // --- Mime types ---
  Object.defineProperty(navigator, 'mimeTypes', {
    get: () => {
      const arr = [
        { type: 'application/pdf', description: 'Portable Document Format', suffixes: 'pdf' },
        { type: 'text/pdf', description: 'Portable Document Format', suffixes: 'pdf' },
      ];
      arr.item = (i) => arr[i];
      arr.namedItem = (n) => arr.find(x => x.type === n);
      Object.defineProperty(arr, 'length', { get: () => 2 });
      return arr;
    }
  });

  // --- Permissions ---
  const origQuery = window.navigator.permissions.query;
  window.navigator.permissions.query = function(args) {
    if (args.name === 'notifications') return Promise.resolve({ state: 'prompt', onchange: null });
    if (args.name === 'camera') return Promise.resolve({ state: 'prompt', onchange: null });
    if (args.name === 'microphone') return Promise.resolve({ state: 'prompt', onchange: null });
    return origQuery.apply(this, arguments);
  };

  // --- Battery API spoof ---
  try {
    navigator.getBattery = function() {
      return Promise.resolve({
        charging: true,
        chargingTime: 0,
        dischargingTime: Infinity,
        level: 0.92 + Math.random() * 0.08,
        onchargingchange: null,
        onchargingtimechange: null,
        ondischargingtimechange: null,
        onlevelchange: null,
      });
    };
  } catch(e) {}

  // --- Screen (matches random viewport) ---
  const screenW = screen.width || 1920;
  const screenH = screen.height || 1080;
  Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
  Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
  Object.defineProperty(screen, 'availWidth', { get: () => screenW });
  Object.defineProperty(screen, 'availHeight', { get: () => screenH - 40 });

  // --- Connection type ---
  Object.defineProperty(navigator, 'connection', {
    get: () => ({
      effectiveType: '4g',
      rtt: 50,
      downlink: 10,
      saveData: false,
    })
  });
})();
"""

NAV_TIMEOUT_MS = 60000


class DiscordAutomation:
    def __init__(self, headless: bool = False, email: str = "",
                 proxy=None, worker_id: str = "B1"):
        self.headless = headless
        self.worker_id = worker_id
        # proxy: dict {proto, host, port, username, password, key} or None
        self.proxy = proxy
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._ua = ""
        self._tor_enabled = False
        self._screenshots: list = []
        self._activity_log: list = []
        self._email = (email or os.environ.get("ACCOUNT_EMAIL", "")).strip()
        self._username = ""
        self._password = ""
        self._token = ""
        self._solver = NoCaptchaAI(log=self._log)
        self._mail: Optional[TempMail] = None

    def _log(self, message: str, level: str = "info") -> None:
        tagged = f"[{self.worker_id}] {message}"
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "timestamp": time.time(),
            "level": level,
            "message": tagged
        }
        self._activity_log.append(entry)
        if len(self._activity_log) > 500:
            self._activity_log = self._activity_log[-500:]
        print(f"[{entry['time']}] [{level.upper()}] {tagged}", flush=True)

    def get_activity_log(self) -> list:
        return self._activity_log

    async def initialize(self) -> None:
        self._playwright = await async_playwright().start()

        args = [
            '--incognito',
            '--incognito',  # double-enforce private browsing
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-restore-session-state',
            '--disable-session-crashed-bubble',
            '--aggressive-cache-discard',
            '--disable-blink-features=AutomationDetected',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-extensions',
            '--disable-background-networking',
            '--mute-audio',
            '--disable-default-apps',
            '--disable-sync',
            '--disable-translate',
            '--disable-component-update',
            '--disable-features=IsolateOrigins,site-per-process,TranslateUI,OptimizationHints',
            '--password-store=basic',
            '--use-mock-keychain',
            '--js-flags=--max-old-space-size=256',
        ]

        self._ua = random.choice(USER_AGENTS)
        self._browser = await self._playwright.chromium.launch(headless=self.headless, args=args)

        timezones = ['America/New_York','America/Chicago','America/Denver','America/Los_Angeles',
                     'Europe/London','Europe/Berlin','Europe/Paris','Asia/Tokyo','Australia/Sydney']
        tz = random.choice(timezones)
        locales = ['en-US','en-GB','en-CA','en-AU']
        loc = random.choice(locales)
        # Random viewport — common desktop resolutions
        viewports = [
            {'width': 860, 'height': 640},
            {'width': 1024, 'height': 768},
            {'width': 1280, 'height': 720},
            {'width': 900, 'height': 700},
            {'width': 1366, 'height': 768},
        ]
        vp = random.choice(viewports)
        scales = [1, 1.25, 2]
        dsf = random.choice(scales)
        # Spoof geolocation (Discord sees this)
        geos = [
            {'latitude': 40.7128, 'longitude': -74.0060},   # NYC
            {'latitude': 34.0522, 'longitude': -118.2437},  # LA
            {'latitude': 41.8781, 'longitude': -87.6298},   # Chicago
            {'latitude': 51.5074, 'longitude': -0.1278},    # London
            {'latitude': 48.8566, 'longitude': 2.3522},     # Paris
        ]
        geo = random.choice(geos)
        ctx_opts = {
            'viewport': vp,
            'user_agent': self._ua,
            'timezone_id': tz,
            'locale': loc,
            'geolocation': geo,
            'permissions': ['geolocation'],
            'device_scale_factor': dsf,
            'is_mobile': False,
            'has_touch': False,
            'color_scheme': random.choice(['dark', 'light', 'no-preference']),
            'bypass_csp': True,
            'ignore_https_errors': True,
            # Ensure clean incognito — no storage/cookies/state ever persist
            'storage_state': None,
            'no_viewport': False,
            'reduced_motion': 'no-preference',
            'forced_colors': 'none',
        }
        if self.proxy:
            p = self.proxy
            server = f"{p.get('proto', 'http')}://{p.get('host')}:{p.get('port')}"
            proxy_cfg = {'server': server}
            if p.get('username'):
                proxy_cfg['username'] = p.get('username')
                proxy_cfg['password'] = p.get('password', '')
            ctx_opts['proxy'] = proxy_cfg
            self._log(f"Proxy: {server} (auth={'yes' if p.get('username') else 'no'})")
        elif _tor_check():
            self._log("[TOR] Using TOR SOCKS5 proxy (fallback)...")
            if _tor_newnym():
                self._log("[TOR] New identity requested")
            ctx_opts['proxy'] = {'server': 'socks5://127.0.0.1:9050'}
            await asyncio.sleep(2)
        else:
            self._log("[Proxy] [FAIL] No proxy available - cannot proceed", level="error")
            # Still create context but mark as unprotected
            pass

        self._context = await self._browser.new_context(**ctx_opts)
        self._log(f"User-Agent: {self._ua[:60]}...")

        await self._context.add_init_script(INIT_SCRIPT)
        self._page = await self._context.new_page()

    async def _rebuild_context_with_tor(self) -> bool:
        """Close the context and reopen WITH a fresh TOR circuit."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if not _tor_check():
                self._log("[Nav] TOR not available for rebuild", level="error")
                return False
            if _tor_newnym():
                self._log("[Nav] Fresh TOR circuit requested")
            await asyncio.sleep(2)
            timezones2 = random.choice(['America/New_York','America/Chicago','America/Denver','America/Los_Angeles','Europe/London','Europe/Berlin','Europe/Paris','Asia/Tokyo'])
            vp2 = random.choice([
                {'width': 860, 'height': 640},
                {'width': 1024, 'height': 768},
                {'width': 900, 'height': 700},
            ])
            self._context = await self._browser.new_context(
                viewport=vp2,
                user_agent=self._ua,
                timezone_id=timezones2,
                locale='en-US',
                proxy={'server': 'socks5://127.0.0.1:9050'},
                bypass_csp=True,
                ignore_https_errors=True,
                storage_state=None,
                no_viewport=False,
            )
            await self._context.add_init_script(INIT_SCRIPT)
            self._page = await self._context.new_page()
            self._log("[Nav] Rebuilt browser context WITH fresh TOR proxy")
            return True
        except Exception as e:
            self._log(f"[Nav] context rebuild failed: {e}", level="error")
            return False

    async def _goto_register(self) -> bool:
        """Navigate to Discord registration with multiple retry strategies.

        Free proxies are slow and Discord is a heavy SPA. We NEVER use
        'networkidle' because Discord makes constant background requests
        (websockets, analytics) that prevent it from ever settling.

        Strategy per attempt:
        - Attempt 1: 'commit' (fastest — just wait for HTTP response), then
          poll for the React form to render (up to 45s).
        - Attempt 2: 'domcontentloaded' (DOM parsed), then poll.
        - Attempt 3: 'load' (all resources), then poll.

        After ANY goto (even if it times out), check if Discord returned a
        block/error page and log it, then poll for the email input anyway.
        """
        wait_strategies = [
            ('commit',      NAV_TIMEOUT_MS),       # fastest — just need the HTML
            ('domcontentloaded', NAV_TIMEOUT_MS // 2),
            ('load',        NAV_TIMEOUT_MS // 2),
        ]
        for attempt, (wait_until, timeout_ms) in enumerate(wait_strategies, 1):
            self._log(f"[Nav] Opening Discord /register (attempt {attempt}, wait={wait_until}, timeout={timeout_ms}ms)...")
            try:
                await self._page.goto('https://discord.com/register',
                                      wait_until=wait_until,
                                      timeout=timeout_ms)
            except Exception as e:
                self._log(f"[Nav] goto error: {str(e)[:100]}", level="warn")

            # Always check what we got — even after a timeout, the page may
            # have loaded partially and be usable.
            try:
                page_title = await asyncio.wait_for(
                    self._page.title(), timeout=3.0)
                page_url = self._page.url
                self._log('[Nav] Current page: title="' + str(page_title)[:60] + '" url=' + str(page_url)[:60])
            except Exception:
                page_title = ""
                page_url = ""

            # Detect Cloudflare / blocks / error pages
            if any(w in (page_title or "").lower() for w in ('attention required', 'just a moment', 'blocked', '502', '503', 'cloudflare')):
                self._log('[Nav] [BLOCKED] Discord returned: "' + str(page_title)[:60] + '" - try next attempt', level="warn")
                if self._tor_enabled and attempt == 2:
                    self._log("[Nav] Rotating TOR circuit...")
                    if not await self._rebuild_context_with_tor():
                        continue
                await asyncio.sleep(2)
                continue

            # Poll for the registration form to actually render
            self._log("[Nav] Polling for Discord React form to render...")
            for poll_i in range(45):  # up to 45 seconds
                try:
                    # Check for email input — the definitive sign the form is ready
                    cnt = await asyncio.wait_for(
                        self._page.locator('input[name="email"]').count(), timeout=1.5)
                    if cnt > 0:
                        self._log(f"[Nav] [OK] Registration form rendered! (attempt {attempt}, poll {poll_i+1}s)")
                        return True
                except Exception:
                    pass
                # Also check if Discord redirected us somewhere else (login, etc)
                try:
                    cur = self._page.url
                    if 'discord.com/app' in cur or 'discord.com/channels' in cur:
                        self._log(f"[Nav] Redirected to {cur[:50]} - already logged in?")
                        return True
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            self._log(f"[Nav] Form never rendered after 45s of polling (attempt {attempt})", level="warn")

            # Rotate TOR before next attempt if available
            if self._tor_enabled and attempt < 3:
                self._log("[Nav] Rotating TOR circuit for next attempt...")
                if not await self._rebuild_context_with_tor():
                    continue
                await asyncio.sleep(2)
            elif attempt < 3:
                await asyncio.sleep(3)

        self._log("[Nav] [FAIL] Could not reach Discord registration after all attempts", level="error")
        return False

    async def capture_screenshot(self) -> str:
        if not self._page:
            return ""
        screenshot = await self._page.screenshot(full_page=True)
        b64 = base64.b64encode(screenshot).decode('utf-8')
        self._screenshots.append(b64)
        if len(self._screenshots) > 100:
            self._screenshots = self._screenshots[-50:]
        return b64

    async def start_discord_signup(self) -> bool:
        if not self._page:
            await self.initialize()

        # No hardcoded email - use the configured email, or fall back to a
        # fresh duckmail.sbs address (mail.tm fallback) when none is provided.
        if not self._email:
            self._log("[Mail] No email configured - creating duckmail.sbs inbox...")
            try:
                self._mail = TempMail(log=self._log)
                self._email = await self._mail.create_inbox()
            except Exception as e:
                self._log(f"[Mail] inbox creation error: {e}", level="error")
                self._email = ""

        if not self._email:
            self._log("[FAIL] No email available - aborting signup", level="error")
            return False

        self._log("=" * 40)
        self._log(f"Starting Discord signup with email: {self._email}")
        self._log("=" * 40)

        try:
            if not await self._goto_register():
                # Fallback: try Discord homepage first, then click to register
                self._log("[FAIL] Direct /register nav failed - trying Discord homepage fallback...", level="warn")
                try:
                    self._log("[Nav] Loading https://discord.com first (fallback)...")
                    await self._page.goto('https://discord.com',
                                          wait_until='commit',
                                          timeout=NAV_TIMEOUT_MS)
                    await asyncio.sleep(5)
                    # Try clicking the login/register link on homepage
                    try:
                        await self._page.click('a[href*="/register"]', timeout=5000)
                        self._log("[Nav] Clicked register link on homepage")
                    except Exception:
                        self._log("[Nav] No register link - trying direct /register again")
                        await self._page.goto('https://discord.com/register',
                                              wait_until='commit',
                                              timeout=NAV_TIMEOUT_MS)
                    await asyncio.sleep(5)
                    # Check if the form appeared after fallback
                    try:
                        cnt = await asyncio.wait_for(
                            self._page.locator('input[name="email"]').count(), timeout=5.0)
                        if cnt > 0:
                            self._log("[Nav] [OK] Fallback worked - form is visible!")
                        else:
                            self._log("[FAIL] Still no form after fallback - aborting", level="error")
                            return False
                    except Exception:
                        self._log("[FAIL] Page not usable after fallback", level="error")
                        return False
                except Exception as e:
                    self._log(f"[FAIL] Fallback also failed: {e}", level="error")
                    return False
            await asyncio.sleep(3)
            await self.capture_screenshot()

            # Fill the form
            form_ok = await self._fill_registration_form()
            if form_ok:
                self._log("[OK] Form filled - now solving captcha...")
                success = await self._solve_hcaptcha_if_present()
                if success:
                    self._log("[OK] CAPTCHA SOLVED! Registration submitted.")
                    # Auto-verify: complete Discord email verification via duckmail.sbs
                    await self._verify_account_email()
                    # Login + grab the FULL token from localStorage
                    self._token = await self._extract_token()
                    if self._token:
                        self._log("[Token] [OK] Full token captured")
                    else:
                        self._log("[Token] No token yet (account may still be pending)", level="warn")
                else:
                    self._log("[FAIL] Captcha solving failed", level="error")
            else:
                self._log("[FAIL] Form filling failed", level="error")
                success = False

        except Exception as e:
            self._log(f"Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            success = False

        await self.capture_screenshot()
        return success

    async def _verify_account_email(self) -> bool:
        """Wait for the Discord verification email and open its link (best effort)."""
        if not self._mail:
            return False
        try:
            link = await self._mail.wait_for_verification_link(timeout=240)
            if not link:
                self._log("[Mail] No verification link found yet - account may still be created", level="warn")
                return False
            self._log(f"[Mail] Opening verification link: {link[:80]}...")
            await self._page.goto(link, wait_until='domcontentloaded', timeout=NAV_TIMEOUT_MS)
            await asyncio.sleep(5)
            # Discord shows a verification success page (or redirects to login)
            try:
                page_text = await self._page.evaluate(
                    "() => document.body.innerText.substring(0, 300)")
            except Exception:
                page_text = ""
            if any(w in (page_text or "").lower()
                   for w in ('verified', 'success', 'confirmation', 'you\'re all set')):
                self._log("[Mail] [OK] Email verification completed")
            await self.capture_screenshot()
            self._log("[Mail] [OK] Verification link opened")
            return True
        except Exception as e:
            self._log(f"[Mail] verification error: {e}", level="warn")
            return False

    async def _past_captcha(self) -> bool:
        """True when the page has moved past the captcha into Discord."""
        try:
            cur_url = self._page.url
            return any(k in cur_url for k in PAST_CAPTCHA_KEYWORDS)
        except:
            return False

    async def _detect_challenge_mode(self, iframe_element) -> str:
        """Identify the hCaptcha state: 'checkbox' or 'drag'.

        A checkbox widget is solvable via the NoCaptchaAI token API. A drag
        puzzle (piece to position) must be solved in-browser with the mouse.
        When there is no checkbox, an active challenge is showing - classify
        it as a drag puzzle so the in-browser solver gets a chance (it
        self-verifies and fast-fails back to the API if nothing is found).
        """
        try:
            frame = await iframe_element.content_frame()
            if not frame:
                return "drag"
            try:
                await frame.wait_for_selector('#checkbox', state='visible', timeout=1500)
                return "checkbox"
            except Exception:
                return "drag"
        except Exception:
            return "drag"

    async def _try_solve_drag(self, iframe) -> Optional[bool]:
        """Try the in-browser drag solver.

        Returns True on a verified solve, False when a real drag puzzle was
        found but could not be solved, and None when nothing puzzle-like was
        present (widget still loading / not a drag challenge at all).
        """
        probe = await _probe_drag_dom(iframe)
        if not probe.get("handle") and not probe.get("area"):
            self._log("[Captcha] No puzzle found yet - will re-check after load",
                      level="warn")
            return None
        solved = await solve_hcaptcha_drag(self._page, iframe, log=self._log)
        if solved:
            self._log("[Captcha] [OK] Drag puzzle solved!")
            await self._click_form_submit()
            await asyncio.sleep(3)
            return True
        self._log("[Captcha] Drag solve failed - falling back to NoCaptchaAI API",
                  level="warn")
        return False

    async def _extract_sitekey_with_retry(self, timeout: float = 15.0,
                                          poll: float = 3.0) -> str:
        """Extract the hCaptcha sitekey, polling until it is valid.

        The captcha iframe mounts before its src carries the sitekey, so
        extracting too early returns a partial/garbage value. Poll every
        `poll` seconds for up to `timeout` seconds and only accept a
        well-formed UUID sitekey (extraction is validated upstream).
        """
        deadline = time.time() + timeout
        attempts = 0
        while True:
            attempts += 1
            sitekey = await extract_hcaptcha_sitekey(self._page)
            if sitekey:
                self._log(f"[Captcha] Sitekey ready (attempt {attempts}): {sitekey[:16]}...")
                return sitekey
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._log(f"[Captcha] Sitekey not ready yet (attempt {attempts}) - "
                      f"retrying in {int(min(poll, remaining))}s", level="warn")
            await asyncio.sleep(min(poll, remaining))
        self._log("[Captcha] Sitekey never appeared after retries", level="error")
        return ""

    async def _click_form_submit(self) -> bool:
        """Click Create Account / Continue after the captcha token is in place."""
        try:
            result = await self._page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (btn.offsetParent === null) continue;
                    const t = btn.textContent.toLowerCase().trim();
                    if (t.includes('create account') || t.includes('continue') || t.includes('sign up')) {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return t.slice(0, 24);
                    }
                }
                const submit = document.querySelector('[type="submit"]');
                if (submit && submit.offsetParent !== null) {
                    submit.click();
                    return 'submit_btn';
                }
                const form = document.querySelector('form');
                if (form) {
                    if (form.requestSubmit) { form.requestSubmit(); return 'requestSubmit'; }
                    form.submit();
                    return 'form_submit';
                }
                return '';
            }""")
            if result:
                self._log(f"[Captcha] [OK] Submit clicked: {result}")
                return True
        except Exception as e:
            self._log(f"[Captcha] submit click error: {e}", level="warn")
        return False

    async def _solve_hcaptcha_if_present(self) -> bool:
        """Detect and solve the hCaptcha challenge.

        Drag puzzles are solved in-browser with real mouse movement. Checkbox
        widgets are solved via the NoCaptchaAI token API (sitekey + pageurl).
        """
        try:
            self._log("[Captcha] Checking for hCaptcha...")

            if await self._past_captcha():
                self._log(f"[Captcha] Already past captcha - at {self._page.url[:50]}")
                return True

            # ── CRITICAL: Always wait 12s for the captcha widget to FULLY load ──
            self._log("[Captcha] Waiting 8 seconds for captcha widget to fully load...")
            await asyncio.sleep(8)

            # Check if already past captcha after waiting
            if await self._past_captcha():
                self._log(f"[Captcha] Page moved past captcha - at {self._page.url[:50]}")
                return True

            # Find the captcha iframe
            iframe = None
            for attempt in range(6):
                try:
                    iframe_el = await asyncio.wait_for(
                        self._page.query_selector('iframe[src*="hcaptcha.com"]'),
                        timeout=2.0
                    )
                except:
                    iframe_el = None
                if iframe_el:
                    self._log(f"[Captcha] hCaptcha iframe found (attempt {attempt+1})")
                    iframe = iframe_el
                    break
                if await self._past_captcha():
                    self._log(f"[Captcha] Page navigated to {self._page.url[:50]} - no captcha needed")
                    return True
                self._log(f"[Captcha] No hCaptcha iframe yet (attempt {attempt+1}/6)")
                await asyncio.sleep(2.0)

            if not iframe:
                # No hCaptcha iframe - check for FunCAPTCHA (Arkose) instead
                try:
                    if await self._past_captcha():
                        self._log(f"[Captcha] Registration went through - at {self._page.url[:50]}")
                        return True
                    page_text = await self._page.evaluate(
                        "() => document.body.innerText.substring(0, 500)")
                    has_captcha_text = ('captcha' in page_text.lower()
                                        or 'security' in page_text.lower()
                                        or 'verify' in page_text.lower())
                    if has_captcha_text:
                        self._log("[Captcha] FunCAPTCHA detected - pixel tile solver...")
                        return await self._solve_funcaptcha()
                    self._log(f"[Captcha] No captcha indicators on page: {self._page.url[:40]}", level="warn")
                    # DON'T claim solved - let the caller retry or report failure
                    return False
                except Exception as e:
                    self._log(f"[Captcha] Captcha check error: {e}", level="warn")
                return False

            # Which challenge is showing? (only used for logging now)
            mode = await self._detect_challenge_mode(iframe)
            self._log(f"[Captcha] Challenge mode: {mode}")

            # ── ACCESSIBILITY CHALLENGE — THE ONLY SOLVER ──
            # Opens the 3-dots menu and uses the Accessibility Challenge,
            # which gives a text/audio question that's solvable locally
            # (math, word puzzles) with Ollama vision as fallback.
            self._log("[Captcha] Trying accessibility challenge (only solver)...")
            acc_result = await solve_hcaptcha_accessibility(self._page, iframe, log=self._log)
            if acc_result:
                self._log("[Captcha] [OK] Accessibility challenge solved!")
                # Wait and verify the captcha iframe is truly gone before
                # clicking Create Account. hCaptcha can chain challenges.
                for check_i in range(5):
                    await asyncio.sleep(3)
                    if await self._past_captcha():
                        self._log("[Captcha] Page past captcha — clicking Create Account")
                        await self._click_form_submit()
                        return True
                    # Check if another captcha iframe appeared
                    try:
                        new_iframe = await self._page.query_selector(
                            'iframe[src*="hcaptcha.com"]'
                        )
                        if new_iframe:
                            self._log(
                                "[Captcha] NEW captcha detected! Solving again..."
                            )
                            iframe = new_iframe
                            acc_result = await solve_hcaptcha_accessibility(
                                self._page, iframe, log=self._log
                            )
                            if not acc_result:
                                self._log(
                                    "[Captcha] Chain captcha failed",
                                    level="error"
                                )
                                return False
                            continue  # captcha solved, re-check
                    except Exception:
                        pass
                    self._log(f"[Captcha] Waiting for page... ({check_i+1}/5)")
                # After all checks, try clicking Create Account anyway
                await self._click_form_submit()
                await asyncio.sleep(3)
                if await self._past_captcha():
                    return True
                return True
            else:
                self._log("[Captcha] [FAIL] Accessibility challenge did not solve",
                          level="error")
                await asyncio.sleep(2)
                return False

        except Exception as e:
            self._log(f"[Captcha] Flow error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _solve_funcaptcha(self) -> bool:
        """Solve FunCAPTCHA tile challenges with the offline pixel solver."""
        try:
            task = await extract_funcaptcha_task(self._page)
            if task:
                self._log(f"[FunCAPTCHA] Challenge: {task[:80]}")
            solved = await solve_funcaptcha_pixels(self._page, log=self._log)
            if solved:
                await self._click_form_submit()
                return True
            self._log("[FunCAPTCHA] [FAIL] Could not solve challenge", level="error")
            return False
        except Exception as e:
            self._log(f"[FunCAPTCHA] Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _select_dob(self, label: str, option_text: str) -> bool:
        """Select DOB dropdown. Discord uses custom React-Select components."""
        try:
            self._log(f"Selecting {label}: {option_text}")

            # Strategy 1: JS click on placeholder, then find and click option
            success = await self._page.evaluate(f"""
                async () => {{
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    let targetEl = null;
                    while (node = walker.nextNode()) {{
                        if (node.textContent.trim() === '{label}') {{
                            const parent = node.parentElement;
                            if (parent && parent.offsetParent !== null &&
                                !parent.querySelector('input[name="email"]')) {{
                                targetEl = parent;
                                break;
                            }}
                        }}
                    }}

                    if (!targetEl) return 'no_element';

                    let clickTarget = targetEl;
                    for (let i = 0; i < 5; i++) {{
                        clickTarget = clickTarget.parentElement;
                        if (!clickTarget) break;
                        const style = window.getComputedStyle(clickTarget);
                        if (style.cursor === 'pointer' ||
                            clickTarget.getAttribute('tabindex') !== null ||
                            clickTarget.className.includes('control') ||
                            clickTarget.className.includes('css-')) {{
                            break;
                        }}
                    }}

                    if (!clickTarget) clickTarget = targetEl;
                    clickTarget.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
                    await new Promise(r => setTimeout(r, 600));

                    const allOptions = document.querySelectorAll(
                        '[id*="option"], [role="option"], [class*="option"]'
                    );
                    for (const opt of allOptions) {{
                        const text = opt.textContent.trim();
                        if (text === '{option_text}') {{
                            opt.scrollIntoView({{block: 'nearest'}});
                            opt.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                            opt.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                            opt.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                            return 'selected';
                        }}
                    }}
                    return 'option_not_found';
                }}
            """)

            if success and 'selected' in str(success):
                self._log(f"Selected {label}: {option_text} ({success})")
                await asyncio.sleep(0.4)
                return True

            self._log(f"JS result for {label}: {success}")

            # Strategy 2: Click the placeholder text, then type to filter
            try:
                placeholder = self._page.get_by_text(label, exact=True)
                count = await asyncio.wait_for(placeholder.count(), timeout=2.0)
                if count > 0:
                    await placeholder.first.click()
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.type(option_text, delay=30)
                    await asyncio.sleep(0.4)
                    await self._page.keyboard.press('Enter')
                    await asyncio.sleep(0.4)
                    self._log(f"Selected {label} via text click")
                    return True
            except:
                pass

            # Strategy 3: Tab navigation
            try:
                idx = {"Month": 0, "Day": 1, "Year": 2}.get(label, 0)
                pw = self._page.locator('input[name="password"]')
                count = await asyncio.wait_for(pw.count(), timeout=2.0)
                if count > 0:
                    await pw.click()
                    await asyncio.sleep(0.2)
                    for _ in range(idx + 1):
                        await self._page.keyboard.press('Tab')
                        await asyncio.sleep(0.15)
                    await self._page.keyboard.press('Space')
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.type(option_text, delay=30)
                    await asyncio.sleep(0.3)
                    await self._page.keyboard.press('Enter')
                    await asyncio.sleep(0.4)
                    self._log(f"Selected {label} via tab")
                    return True
            except:
                pass

            self._log(f"All DOB strategies failed for {label}")
            return False

        except Exception as e:
            self._log(f"DOB error for {label}: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _fill_registration_form(self) -> bool:
        try:
            self._log("=" * 40)
            self._log("FILLING REGISTRATION FORM")
            self._log("=" * 40)
            self._log(f"Email: {self._email}")

            # ── Handle Discord age gate (birthday modal before form) ──
            for _ in range(6):
                try:
                    age_text = await self._page.evaluate(
                        "() => (document.body.innerText || '').substring(0, 300)")
                except Exception:
                    age_text = ""
                has_age_gate = any(w in age_text.lower() for w in
                                   ('birthday', 'date of birth', 'born', 'how old'))
                has_form = ('email' in age_text.lower() and 'username' in age_text.lower())

                if has_form:
                    self._log("[Form] Registration form detected — no age gate")
                    break
                if has_age_gate:
                    self._log("[Form] Age gate detected — filling DOB...")
                    # Discord age gate: pick adult DOB
                    try:
                        await self._page.evaluate("""() => {
                            const pickers = document.querySelectorAll('input, [role="combobox"], [class*="select"]');
                            for (const p of pickers) {
                                if (p.offsetParent === null) continue;
                                const label = (p.getAttribute('aria-label') || p.getAttribute('placeholder') || '').toLowerCase();
                                // Click and select month/day/year
                                p.click();
                                break;
                            }
                        }""")
                    except Exception:
                        pass
                    # Try typing DOB fields directly
                    try:
                        inputs = self._page.locator('input[type="text"], input:not([type])')
                        count = await inputs.count()
                        if count >= 3:
                            await inputs.nth(0).fill("January")
                            await inputs.nth(1).fill("15")
                            await inputs.nth(2).fill("1995")
                            await self._page.keyboard.press("Enter")
                    except Exception:
                        pass
                    try:
                        await self._page.keyboard.type("01", delay=30)
                        await self._page.keyboard.press("Tab")
                        await self._page.keyboard.type("15", delay=30)
                        await self._page.keyboard.press("Tab")
                        await self._page.keyboard.type("1995", delay=30)
                        await self._page.keyboard.press("Enter")
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    continue
                # Neither form nor age gate — page may still be loading
                self._log(f"[Form] Waiting for page content... ({_+1}/6)")
                await asyncio.sleep(1.5)

            # ── Wait for email input with multiple fallback selectors ──
            email_input = None
            for selector in (
                'input[name="email"]',
                'input[type="email"]',
                'input[id*="email" i]',
                'input[aria-label*="email" i]',
                'input[placeholder*="email" i]',
                'input[autocomplete="email"]',
            ):
                try:
                    await self._page.wait_for_selector(selector, timeout=8000)
                    email_input = self._page.locator(selector)
                    self._log(f"[Form] Email input found: {selector}")
                    break
                except Exception:
                    continue

            if not email_input:
                # Last resort: type into the first visible text input
                try:
                    all_inputs = self._page.locator('input:not([type="hidden"]):not([type="submit"])')
                    count = await all_inputs.count()
                    if count > 0:
                        email_input = all_inputs.first
                        self._log("[Form] Using first visible input as email", level="warn")
                except Exception:
                    pass

            if not email_input:
                self._log("[Form] No email input found on page", level="error")
                await self.capture_screenshot()
                return False

            await email_input.fill(self._email)
            await self._human_pause()

            # Generate username
            consonants = 'bcdfghjklmnpqrstvwxyz'
            vowels = 'aeiou'
            username = ''
            for _ in range(random.randint(8, 12)):
                username += random.choice(vowels if random.random() < 0.35 else consonants)
            self._username = username
            display_name = self._username[:15]

            self._log(f"Display name: {display_name}")
            try:
                await self._page.wait_for_selector('input[name="global_name"]', timeout=5000)
                await self._page.locator('input[name="global_name"]').fill(display_name)
            except:
                pass
            await self._human_pause()

            self._log(f"Username: {self._username}")
            await self._page.locator('input[name="username"]').fill(self._username)
            await self._human_pause()

            # Generate password
            first = random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')
            body = ''
            for _ in range(random.randint(8, 11)):
                body += random.choice(vowels if random.random() < 0.35 else consonants)
            specials = '!@#$%&*'
            self._password = first + body + random.choice(specials) + str(random.randint(1, 99))

            self._log("Filling password")
            await self._page.locator('input[name="password"]').fill(self._password)
            await self._human_pause()

            # DOB
            month_val = random.randint(1, 12)
            day_val = str(random.randint(1, 28))
            year_val = str(random.randint(1990, 1999))
            months = ['January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December']
            month_name = months[month_val - 1]
            self._log(f"DOB: {month_name} {day_val}, {year_val}")

            await self._select_dob("Month", month_name)
            await self._human_pause()
            await self._select_dob("Day", day_val)
            await self._human_pause()
            await self._select_dob("Year", year_val)
            await self._human_pause()

            # ── ToS Checkbox — FIND THE CORRECT ONE (Terms of Service, not newsletter) ──
            self._log("Checking ToS checkbox...")
            tos_checked = False

            try:
                tos_result = await self._page.evaluate("""() => {
                    // Find ANY text node containing ToS-like keywords
                    const tosKeywords = ['terms of service', 'terms of use', 'terms & conditions',
                                        'terms and conditions', 'i have read', 'read and agree',
                                        'agree to', 'by creating'];
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                    let node;
                    while (node = walker.nextNode()) {
                        const t = node.textContent.trim().toLowerCase();
                        if (!tosKeywords.some(k => t.includes(k))) continue;

                        // Walk UP to find a clickable container that has a checkbox nearby
                        let el = node.parentElement;
                        for (let i = 0; i < 10 && el; i++) {
                            // Look for checkbox inside this container
                            const cb = el.querySelector('input[type="checkbox"]');
                            if (cb) {
                                cb.scrollIntoView({block: 'center'});
                                cb.click();
                                cb.checked = true;
                                cb.dispatchEvent(new Event('change', { bubbles: true }));
                                cb.dispatchEvent(new Event('input', { bubbles: true }));
                                return 'found_tos_checkbox';
                            }
                            // Also try role=checkbox
                            const roleCb = el.querySelector('[role="checkbox"]');
                            if (roleCb) {
                                roleCb.click();
                                roleCb.setAttribute('aria-checked', 'true');
                                roleCb.dispatchEvent(new Event('change', { bubbles: true }));
                                return 'found_tos_role';
                            }
                            el = el.parentElement;
                        }
                    }

                    // FALLBACK: check ALL visible unchecked checkboxes (Discord has max 2)
                    const allCbs = document.querySelectorAll('input[type="checkbox"]');
                    let checked = 0;
                    for (const cb of allCbs) {
                        if (cb.offsetParent === null) continue;
                        if (cb.checked) continue;
                        cb.scrollIntoView({block: 'center'});
                        cb.click();
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                        cb.dispatchEvent(new Event('input', { bubbles: true }));
                        checked++;
                    }
                    if (checked > 0) return 'fallback_checked_' + checked;

                    // Also try role checkboxes
                    const roleCbs = document.querySelectorAll('[role="checkbox"]');
                    for (const rc of roleCbs) {
                        if (rc.offsetParent === null) continue;
                        if (rc.getAttribute('aria-checked') === 'true') continue;
                        rc.click();
                        rc.setAttribute('aria-checked', 'true');
                        rc.dispatchEvent(new Event('change', { bubbles: true }));
                        checked++;
                    }
                    if (checked > 0) return 'role_fallback_' + checked;

                    return 'not_found';
                }""")
                if tos_result and tos_result != 'not_found':
                    tos_checked = True
                    self._log(f"[OK] ToS checked via JS: {tos_result}")
                else:
                    self._log(f"[WARN] ToS checkbox not found by JS ({tos_result}) - trying Playwright locator...")
                    # Playwright fallback: click any visible checkbox input
                    try:
                        checkboxes = self._page.locator('input[type="checkbox"]:visible')
                        cb_count = await checkboxes.count()
                        for i in range(cb_count):
                            cb = checkboxes.nth(i)
                            is_checked = await cb.is_checked()
                            if not is_checked:
                                await cb.scroll_into_view_if_needed()
                                await cb.check(force=True)
                                self._log(f"[OK] ToS checkbox {i} checked via Playwright")
                                tos_checked = True
                    except Exception as pw_e:
                        self._log(f"Playwright checkbox fallback error: {pw_e}", level="warn")
            except Exception as e:
                self._log(f"ToS JS evaluate error: {e}", level="warn")

            if tos_checked:
                self._log("[OK] ToS checkbox checked")
            else:
                self._log("[WARN] No ToS checkbox found - the Create Account button may be disabled")

            # Wait for React to process the checkbox change
            await asyncio.sleep(2.0)  # Increased: React needs time to re-render enabled state

            # ── VERIFY ToS is actually checked before trying Create Account ──
            try:
                verify = await self._page.evaluate("""() => {
                    const cbs = document.querySelectorAll('input[type="checkbox"]');
                    let checked = 0;
                    for (const cb of cbs) {
                        if (cb.checked) checked++;
                    }
                    const roleCbs = document.querySelectorAll('[role="checkbox"][aria-checked="true"]');
                    return { native: checked, role: roleCbs.length };
                }""")
                self._log(f"[Form] Checkbox state: native={verify.get('native',0)} role={verify.get('role',0)}")
            except Exception:
                pass

            # ── Create Account Button — try multiple strategies ────────
            self._log("Clicking Create Account...")
            create_clicked = False

            for click_attempt in range(4):
                if create_clicked:
                    break
                if click_attempt > 0:
                    self._log(f"Retrying Create Account click (attempt {click_attempt+1}/4)...")
                    # Re-check ALL unchecked checkboxes on retry (React may have reset them)
                    try:
                        checkboxes = self._page.locator('input[type="checkbox"]:visible')
                        cb_count = await checkboxes.count()
                        rechecked = 0
                        for i in range(cb_count):
                            try:
                                if not await checkboxes.nth(i).is_checked():
                                    await checkboxes.nth(i).scroll_into_view_if_needed()
                                    await checkboxes.nth(i).check(force=True)
                                    rechecked += 1
                            except Exception:
                                pass
                        if rechecked:
                            self._log(f"Re-checked {rechecked} checkbox(es)")
                            # Also re-check role checkboxes
                            try:
                                await self._page.evaluate("""() => {
                                    const rcs = document.querySelectorAll('[role="checkbox"]');
                                    for (const rc of rcs) {
                                        if (rc.getAttribute('aria-checked') !== 'true') {
                                            rc.click();
                                            rc.setAttribute('aria-checked', 'true');
                                            rc.dispatchEvent(new Event('change', {bubbles: true}));
                                        }
                                    }
                                }""")
                            except Exception:
                                pass
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)  # Longer wait for React to process

                try:
                    result = await self._page.evaluate("""() => {
                        // Strategy 1: Find button by text content (most reliable)
                        const btns = document.querySelectorAll('button, [role="button"], [type="submit"]');
                        for (const btn of btns) {
                            if (btn.offsetParent === null) continue;
                            // Check if disabled
                            if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                            const t = (btn.textContent || '').toLowerCase().trim();
                            const v = (btn.value || '').toLowerCase().trim();
                            if (t.includes('create account') || t.includes('sign up') || t.includes('continue') ||
                                v.includes('create account') || v.includes('sign up')) {
                                btn.scrollIntoView({block: 'center'});
                                btn.click();
                                return 'btn_' + t.slice(0, 20);
                            }
                        }

                        // Strategy 2: Find any submit-type button
                        for (const btn of btns) {
                            if (btn.offsetParent === null) continue;
                            if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                            if (btn.getAttribute('type') === 'submit' || btn.tagName === 'BUTTON') {
                                const t = btn.textContent.toLowerCase().trim();
                                if (t.length > 4) {  // has meaningful text
                                    btn.scrollIntoView({block: 'center'});
                                    btn.click();
                                    return 'btntype_' + t.slice(0, 20);
                                }
                            }
                        }

                        // Strategy 3: Form submit
                        const forms = document.querySelectorAll('form');
                        for (const form of forms) {
                            if (form.offsetParent === null) continue;
                            if (form.requestSubmit) {
                                form.requestSubmit();
                                return 'form_requestSubmit';
                            }
                            form.submit();
                            return 'form_submit';
                        }

                        return 'failed';
                    }""")
                    if result and result != 'failed':
                        create_clicked = True
                        self._log(f"[OK] Account button clicked: {result}")
                        break
                except Exception as e:
                    self._log(f"Create Account JS attempt {click_attempt+1} error: {e}", level="warn")

                # Playwright fallback: click the button directly
                if not create_clicked:
                    try:
                        btn_selectors = [
                            'button:has-text("Create Account")',
                            'button:has-text("Sign Up")',
                            'button:has-text("Continue")',
                            'button[type="submit"]',
                        ]
                        for sel in btn_selectors:
                            try:
                                btn = self._page.locator(sel).first
                                if await btn.count() > 0:
                                    is_disabled = await btn.is_disabled()
                                    if not is_disabled:
                                        await btn.scroll_into_view_if_needed()
                                        await btn.click()
                                        self._log(f"[OK] Playwright click: {sel}")
                                        create_clicked = True
                                        break
                            except Exception:
                                continue
                    except Exception as pw_e:
                        self._log(f"Playwright button click error: {pw_e}", level="warn")

                # Last resort: Enter key on password field
                if not create_clicked:
                    try:
                        await self._page.locator('input[name="password"]').press('Enter')
                        self._log("Pressed Enter on password field")
                        create_clicked = True
                    except Exception:
                        pass

            if create_clicked:
                self._log("[OK] Create Account submitted - waiting for response...")
            else:
                self._log("[FAIL] Could not click Create Account after all attempts!", level="error")
                await self.capture_screenshot()
                return False

            await asyncio.sleep(3)
            await self.capture_screenshot()

            return True


        except Exception as e:
            self._log(f"Form filling error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _human_pause(self) -> None:
        await asyncio.sleep(random.uniform(0.1, 0.5))

    async def live_camera_loop(self, interval: int = 4) -> None:
        while True:
            await self.capture_screenshot()
            await asyncio.sleep(interval)

    async def _extract_token(self, attempts: int = 4) -> str:
        """Login to Discord with the created account and grab the FULL token
        from localStorage. Discord stores it under 'token'."""
        if not (self._email and self._password):
            return ""
        try:
            for i in range(attempts):
                try:
                    await self._page.goto("https://discord.com/login",
                                          wait_until="domcontentloaded",
                                          timeout=NAV_TIMEOUT_MS)
                    break
                except Exception:
                    await asyncio.sleep(3)
            await asyncio.sleep(3)
            try:
                email_input = self._page.locator('input[name="email"]').first
                await email_input.fill(self._email, timeout=8000)
                pw_input = self._page.locator('input[name="password"]').first
                await pw_input.fill(self._password, timeout=8000)
                await pw_input.press("Enter")
                self._log("[Token] Submitted login form")
            except Exception as e:
                self._log(f"[Token] Login fill error: {e}", level="warn")
                return ""
            # Wait for token to appear
            for _ in range(12):
                await asyncio.sleep(2.5)
                try:
                    token = await self._page.evaluate(
                        "() => localStorage.getItem('token') || ''"
                    )
                    if token and len(token) > 20:
                        return token.strip()
                except Exception:
                    pass
            return ""
        except Exception as e:
            self._log(f"[Token] extract error: {e}", level="warn")
            return ""

    def get_account(self) -> dict:
        """Return the generated account info (email, user, pass, full token)."""
        return {
            "email": self._email,
            "username": self._username,
            "password": self._password,
            "token": self._token,
            "proxy": self.proxy,
            "worker_id": self.worker_id,
        }

    async def close(self) -> None:
        if self._mail:
            try:
                await self._mail.close()
            except Exception:
                pass
            self._mail = None
        if self._page:
            try:
                await self._page.close()
            except:
                pass
            self._page = None
        if self._context:
            try:
                await self._context.close()
            except:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except:
                pass
            self._playwright = None

    def get_screenshots(self) -> list:
        return self._screenshots

    def get_latest_screenshot(self) -> str:
        if self._screenshots:
            return self._screenshots[-1]
        return ""


async def run_discord_automation():
    bot = DiscordAutomation(headless=True)
    try:
        await bot.initialize()
        success = await bot.start_discord_signup()
        if success:
            print("[OK] Discord automation completed")
        else:
            print("[FAIL] Discord automation failed")
        await asyncio.sleep(5)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(run_discord_automation())
