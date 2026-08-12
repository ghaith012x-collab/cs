"""
stealth.py — "best human stealth" browser layer.

Everything modern anti-bot stacks (hCaptcha, Cloudflare, Akamai, DataDome)
fingerprint, patched in the way that actually survives their checks:

  · navigator.webdriver hidden at the PROTOTYPE level (survives
    Object.getOwnPropertyDescriptor integrity checks, not just `== false`)
  · navigator.userAgentData (Client Hints) rebuilt to match the UA we set —
    high-entropy values included, because hCaptcha reads them
  · plugins / mimeTypes as real PluginArray-like objects with item()/
    namedItem()/refresh() and Symbol.iterator-style access
  · a rich `window.chrome` object (runtime, csi, loadTimes, app, webstore)
  · WebGL UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL spoofed to match
    a plausible real GPU for the spoofed OS
  · optional canvas noise (STEALTH_CANVAS_NOISE=1..3) so canvas readbacks
    don't match the clean headless hash
  · permissions.query returns a stable non-suspicious state for every name
  · outer/inner window sizing fixed (headless reports outer==inner, humans
    don't) and media codec probing made realistic
  · every known automation trace scrubbed (cdc_, __playwright, selenium…)

Launch-side hardening (launch args + CDP) is also here so the whole
fingerprint is applied consistently from one module.
"""
import json
import os
import random
import re

from browser_engine import ENGINE

# ─────────────────────────────────────────────────────────────────────────
# Launch arguments
# ─────────────────────────────────────────────────────────────────────────

# Minimal baseline — always needed (Docker runs as root, containers have a
# small /dev/shm). These are NOT detection signals.
_BASE_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--mute-audio",
    "--password-store=basic",
    "--use-mock-keychain",
]

# Full hardening for stock Playwright. Patchright already patches the CDP
# layer itself, so we keep its flag set small (passing extra automation
# flags to Patchright can actually reintroduce signals).
_STEALTH_ARGS = _BASE_ARGS + [
    "--disable-blink-features=AutomationControlled",
    "--disable-component-extensions-with-background-pages",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-default-apps",
    "--disable-translate",
    "--disable-component-update",
    "--metrics-recording-only",
    "--no-pings",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-notifications",
    "--lang=en-US",
]


def launch_args(headless: bool = True) -> list:
    """Launch args for the active engine.

    clearcote = minimal baseline only: the SDK already drops
    ``--enable-automation`` and derives a coherent headless persona/window
    geometry in the engine; extra flags (and ``--headless=new``) would fight
    its machinery and can reintroduce signals.
    shardx = minimal baseline + ``--incognito``: the engine owns the whole
    identity (TLS / WebGL / UA-CH / fonts) and adds its own ``--headless=new``;
    incognito keeps every session a clean, disk-less identity.
    patchright = minimal set (it patches the CDP layer itself).
    stock = full hardening set.
    """
    if ENGINE == "clearcote":
        return list(_BASE_ARGS)
    if ENGINE == "shardx":
        return list(_BASE_ARGS) + ["--incognito"]
    args = _BASE_ARGS if ENGINE == "patchright" else _STEALTH_ARGS
    if headless:
        # New headless mode: visually identical to headed, no "HeadlessChrome"
        # in the UA by default. Explicit new mode avoids old-mode signals.
        args = list(args) + ["--headless=new"]
    return args


# ─────────────────────────────────────────────────────────────────────────
# UA parsing helpers
# ─────────────────────────────────────────────────────────────────────────

_CHROME_RE = re.compile(r"Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)")
_SAFARI_RE = re.compile(r"Version/(\d+)[._](\d+)")


def ua_chrome_version(ua: str) -> str:
    m = _CHROME_RE.search(ua or "")
    if m:
        return m.group(1)
    m = _SAFARI_RE.search(ua or "")
    if m:
        return m.group(1)
    return "130"


def ua_platform(ua: str) -> str:
    """Map a UA string to {nav_platform, ch_platform, arch, bitness,
    platform_version, wow64} — all values must be mutually consistent."""
    u = ua or ""
    if "Windows" in u:
        return {
            "nav_platform": "Win32",
            "ch_platform": "Windows",
            "arch": "x86",
            "bitness": "64",
            "platform_version": "10.0.0",
            "wow64": False,
        }
    if "Macintosh" in u or "Mac OS X" in u:
        return {
            "nav_platform": "MacIntel",
            "ch_platform": "macOS",
            "arch": "arm",
            "bitness": "64",
            "platform_version": "15.6.0",
            "wow64": False,
        }
    return {
        "nav_platform": "Linux x86_64",
        "ch_platform": "Linux",
        "arch": "x86",
        "bitness": "64",
        "platform_version": "",
        "wow64": False,
    }


def sec_ch_ua_header(ua: str) -> str:
    """sec-ch-ua header that matches the UA's Chrome version."""
    v = ua_chrome_version(ua)
    return f'"Chromium";v="{v}", "Google Chrome";v="{v}", "Not?A_Brand";v="99"'


# ─────────────────────────────────────────────────────────────────────────
# Locale / timezone / geolocation coherence
# ─────────────────────────────────────────────────────────────────────────

_LOCALE_PROFILES = [
    {"locale": "en-US", "languages": ["en-US", "en"], "tz": "America/New_York",
     "geo": {"latitude": 40.7128, "longitude": -74.0060}},
    {"locale": "en-US", "languages": ["en-US", "en"], "tz": "America/Chicago",
     "geo": {"latitude": 41.8781, "longitude": -87.6298}},
    {"locale": "en-US", "languages": ["en-US", "en"], "tz": "America/Los_Angeles",
     "geo": {"latitude": 34.0522, "longitude": -118.2437}},
    {"locale": "en-GB", "languages": ["en-GB", "en"], "tz": "Europe/London",
     "geo": {"latitude": 51.5074, "longitude": -0.1278}},
    {"locale": "en-CA", "languages": ["en-CA", "en"], "tz": "America/Toronto",
     "geo": {"latitude": 43.6532, "longitude": -79.3832}},
    {"locale": "en-AU", "languages": ["en-AU", "en"], "tz": "Australia/Sydney",
     "geo": {"latitude": -33.8688, "longitude": 151.2093}},
]

# Plausible GPU pairs per platform (vendor, renderer)
_GPU_WINDOWS = [
    ("Google Inc.", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc.", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc.", "ANGLE (Intel, Intel(R) Iris Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc.", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]
_GPU_MAC = [
    ("Google Inc.", "ANGLE (Apple, Apple M2, OpenGL 4.1)"),
    ("Google Inc.", "ANGLE (Apple, Apple M1, OpenGL 4.1)"),
    ("Google Inc.", "ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)"),
]
_GPU_LINUX = [
    ("Google Inc.", "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92), OpenGL 4.5 core)"),
    ("Google Inc.", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 (0x00002183), OpenGL 4.6 core)"),
]


def pick_gpu(platform: str, seed: int) -> dict:
    table = {"Windows": _GPU_WINDOWS, "macOS": _GPU_MAC, "Linux": _GPU_LINUX}
    pairs = table.get(platform, _GPU_WINDOWS)
    vendor, renderer = pairs[seed % len(pairs)]
    return {"webgl_vendor": vendor, "webgl_renderer": renderer}


# ─────────────────────────────────────────────────────────────────────────
# Fingerprint init script
# ─────────────────────────────────────────────────────────────────────────

_INIT_TEMPLATE = r"""
(() => {
  const NAV = navigator, WIN = window;
  const SAFE = (fn) => { try { fn(); } catch (e) {} };

  // 1. webdriver — prototype-level so getOwnPropertyDescriptor checks pass
  SAFE(() => {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => undefined, configurable: true,
    });
    Object.defineProperty(NAV, 'webdriver', {
      get: () => undefined, configurable: true,
    });
  });

  // 2. Client Hints — rebuild userAgentData to match the UA we set
  SAFE(() => {
    const brands = __CH_BRANDS__;
    const platform = __CH_PLATFORM__;
    const uaData = {
      brands: brands, mobile: false, platform: platform,
      getHighEntropyValues(keys) {
        const r = { brands: brands, mobile: false, platform: platform };
        if (keys.includes('architecture')) r.architecture = '__CH_ARCH__';
        if (keys.includes('bitness')) r.bitness = '__CH_BITNESS__';
        if (keys.includes('model')) r.model = '';
        if (keys.includes('platformVersion')) r.platformVersion = '__CH_PLATFORM_VERSION__';
        if (keys.includes('uaFullVersion')) r.uaFullVersion = '__CH_UA_FULL__';
        if (keys.includes('fullVersionList')) r.fullVersionList = brands;
        if (keys.includes('wow64')) r.wow64 = __CH_WOW64__;
        return Promise.resolve(r);
      },
      toJSON() { return { brands: brands, mobile: false, platform: platform }; },
    };
    Object.defineProperty(NAV, 'userAgentData', {
      get: () => uaData, configurable: true,
    });
  });

  // 3. navigator basics — one consistent identity
  SAFE(() => {
    Object.defineProperty(NAV, 'languages', {
      get: () => Object.freeze(__NAV_LANGS__), configurable: true,
    });
    Object.defineProperty(NAV, 'platform', {
      get: () => '__NAV_PLATFORM__', configurable: true,
    });
    Object.defineProperty(NAV, 'hardwareConcurrency', {
      get: () => __NAV_CORES__, configurable: true,
    });
    Object.defineProperty(NAV, 'deviceMemory', {
      get: () => __NAV_MEMORY__, configurable: true,
    });
    Object.defineProperty(NAV, 'maxTouchPoints', {
      get: () => __NAV_TOUCH__, configurable: true,
    });
    Object.defineProperty(NAV, 'vendor', {
      get: () => 'Google Inc.', configurable: true,
    });
  });

  // 4. plugins + mimeTypes — real PluginArray-like objects
  SAFE(() => {
    const mkMime = (type, suffixes, description) => {
      const m = { type: type, suffixes: suffixes, description: description };
      m.enabledPlugin = null;
      return m;
    };
    const pdf = mkMime('application/pdf', 'pdf', 'Portable Document Format');
    const txtPdf = mkMime('text/pdf', 'pdf', 'Portable Document Format');
    const mkPlugin = (name, filename, description, mimes) => {
      const p = { name: name, filename: filename, description: description, length: mimes.length };
      for (let i = 0; i < mimes.length; i++) p[i] = mimes[i];
      p.item = (i) => p[i] || null;
      p.namedItem = (n) => mimes.find((m) => m.type === n) || null;
      return p;
    };
    const plugins = [
      mkPlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format', [pdf]),
      mkPlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', [pdf]),
      mkPlugin('Native Client', 'internal-nacl-plugin', '', []),
    ];
    plugins.item = (i) => plugins[i] || null;
    plugins.namedItem = (n) => plugins.find((p) => p.name === n) || null;
    plugins.refresh = () => {};
    Object.defineProperty(plugins, 'length', { get: () => 3 });
    Object.defineProperty(NAV, 'plugins', { get: () => plugins, configurable: true });

    const mimeTypes = [pdf, txtPdf];
    mimeTypes.item = (i) => mimeTypes[i] || null;
    mimeTypes.namedItem = (n) => mimeTypes.find((m) => m.type === n) || null;
    Object.defineProperty(mimeTypes, 'length', { get: () => 2 });
    Object.defineProperty(NAV, 'mimeTypes', { get: () => mimeTypes, configurable: true });
  });

  // 5. rich window.chrome object
  SAFE(() => {
    const evt = () => ({
      addListener: () => {}, removeListener: () => {},
      hasListener: () => false, hasListeners: () => false,
    });
    const chromeObj = {
      runtime: {
        id: undefined, connect: () => {}, sendMessage: () => {},
        onMessage: evt(), onConnect: evt(), onInstalled: evt(),
      },
      csi: () => ({ startE: Date.now(), onloadT: Date.now(), pageT: Date.now(), tran: 0 }),
      loadTimes: () => ({
        requestTime: 0, startLoadTime: 0, commitLoadTime: 0,
        finishDocumentLoadTime: 0, finishLoadTime: 0,
        firstPaintTime: 0, firstPaintAfterLoadTime: 0,
        navigationType: 'Other', wasFetchedViaSpdy: true,
        wasNpnNegotiated: true, npnNegotiatedProtocol: 'h2',
        wasAlternateProtocolAvailable: false, connectionInfo: 'h2',
      }),
      app: {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      },
      webstore: { onInstallStageChanged: evt(), onDownloadProgress: evt() },
    };
    Object.defineProperty(WIN, 'chrome', { get: () => chromeObj, set: () => {}, configurable: true });
  });

  // 6. WebGL unmasked strings — hCaptcha reads these directly
  SAFE(() => {
    const patch = (proto) => {
      if (!proto || !proto.prototype) return;
      const orig = proto.prototype.getParameter;
      proto.prototype.getParameter = function (param) {
        if (param === 0x9245) return '__WEBGL_VENDOR__';
        if (param === 0x9246) return '__WEBGL_RENDERER__';
        if (param === 0x1F00) return 'WebKit';
        if (param === 0x1F01) return '__WEBGL_RENDERER__';
        return orig.call(this, param);
      };
    };
    patch(WebGLRenderingContext);
    patch(WebGL2RenderingContext);
  });

  // 7. canvas noise (opt-in via STEALTH_CANVAS_NOISE) — avoid headless hash
  SAFE(() => {
    if (__CANVAS_NOISE__ > 0) {
      const n = __CANVAS_NOISE__;
      const origData = HTMLCanvasElement.prototype.toDataURL;
      const origGI = CanvasRenderingContext2D.prototype.getImageData;
      const jitter = (d) => {
        for (let i = 0; i < d.length; i += 4) {
          d[i] += (Math.random() * 2 - 1) * n;
          d[i + 1] += (Math.random() * 2 - 1) * n;
          d[i + 2] += (Math.random() * 2 - 1) * n;
        }
      };
      HTMLCanvasElement.prototype.toDataURL = function (...a) {
        try {
          const ctx = this.getContext('2d');
          if (ctx && !this.__stealthNoised) {
            const img = ctx.getImageData(0, 0, this.width, this.height);
            jitter(img.data);
            ctx.putImageData(img, 0, 0);
            this.__stealthNoised = true;
          }
        } catch (e) {}
        return origData.apply(this, a);
      };
      CanvasRenderingContext2D.prototype.getImageData = function (...a) {
        const res = origGI.apply(this, a);
        try { jitter(res.data); } catch (e) {}
        return res;
      };
    }
  });

  // 8. permissions — stable non-suspicious state for every query
  SAFE(() => {
    const orig = NAV.permissions.query.bind(NAV.permissions);
    NAV.permissions.query = (params) => {
      if (params && params.name) {
        const state = { state: 'prompt', onchange: null };
        state.addEventListener = () => {};
        state.removeEventListener = () => {};
        return Promise.resolve(state);
      }
      return orig(params);
    };
  });

  // 9. window sizing — headless reports outer==inner, humans don't
  SAFE(() => {
    if (WIN.outerWidth === WIN.innerWidth && WIN.outerWidth !== 0) {
      const dx = 14 + Math.floor(Math.random() * 4);
      const dy = 118 + Math.floor(Math.random() * 6);
      Object.defineProperty(WIN, 'outerWidth', {
        get: () => WIN.innerWidth + dx, configurable: true,
      });
      Object.defineProperty(WIN, 'outerHeight', {
        get: () => WIN.innerHeight + dy, configurable: true,
      });
    }
  });

  // 10. media codecs — plausible answers for silent checks
  SAFE(() => {
    const orig = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = function (type) {
      const t = String(type);
      if (t.includes('video/mp4') || t.includes('audio/mp4')) return 'probably';
      return orig.call(this, t);
    };
  });

  // 11. scrub every known automation trace
  SAFE(() => {
    delete WIN.__playwright;
    delete WIN.__pw_manual;
    delete WIN.__pw_init;
    delete WIN.__nightmare;
    delete WIN._phantom;
    delete WIN.callPhantom;
    delete WIN.domAutomation;
    delete WIN.domAutomationController;
    try { delete document.$cdc_asdjflasutopfhvcZLmcfl_; } catch (e) {}
    try { delete document.__selenium_unwrapped; } catch (e) {}
    try { delete document.__webdriver_evaluate; } catch (e) {}
    try { delete document.__driver_evaluate; } catch (e) {}
    try { delete document.__webdriver_script_fn; } catch (e) {}
    try { delete document.__driver_unwrapped; } catch (e) {}
  });
})();
"""


def build_init_script(fingerprint: dict, ua: str) -> str:
    """Build the init script with one consistent identity baked in.

    clearcote / shardx: returns a no-op script — the engine's C++ persona
    already owns the whole identity (webdriver, UA-CH, WebGL, fonts, canvas,
    TLS). Injecting these JS overrides on top would re-introduce the
    self-revealing shims (toString returns the shim source, realm
    re-acquisition, descriptor checks) the engines exist to remove."""
    if ENGINE in ("clearcote", "shardx"):
        return f"// {ENGINE}: engine-level persona — no JS shims needed"
    pl = ua_platform(ua)
    version = ua_chrome_version(ua)
    full_version = _CHROME_RE.search(ua)
    ua_full = full_version.group(0) if full_version else f"{version}.0.0.0"
    brands = json.dumps([
        {"brand": "Chromium", "version": version},
        {"brand": "Google Chrome", "version": version},
        {"brand": "Not?A_Brand", "version": "99"},
    ])
    locale = fingerprint.get("locale", "en-US")
    languages = fingerprint.get("languages") or [locale, "en"]
    if locale not in languages:
        languages = [locale] + languages
    gpu = fingerprint.get("gpu") or pick_gpu(pl["ch_platform"], fingerprint.get("seed", 0))

    canvas_noise = int(os.environ.get("STEALTH_CANVAS_NOISE", "0") or "0")
    try:
        canvas_noise = max(0, min(3, canvas_noise))
    except Exception:
        canvas_noise = 0

    return (
        _INIT_TEMPLATE
        .replace("__CH_BRANDS__", brands)
        .replace("__CH_PLATFORM__", pl["ch_platform"])
        .replace("__CH_ARCH__", pl["arch"])
        .replace("__CH_BITNESS__", pl["bitness"])
        .replace("__CH_PLATFORM_VERSION__", pl["platform_version"])
        .replace("__CH_UA_FULL__", ua_full)
        .replace("__CH_WOW64__", "true" if pl["wow64"] else "false")
        .replace("__NAV_LANGS__", json.dumps(languages))
        .replace("__NAV_PLATFORM__", pl["nav_platform"])
        .replace("__NAV_CORES__", str(fingerprint.get("cores", 8)))
        .replace("__NAV_MEMORY__", str(fingerprint.get("device_memory", 8)))
        .replace("__NAV_TOUCH__", str(fingerprint.get("touch_points", 0)))
        .replace("__WEBGL_VENDOR__", gpu["webgl_vendor"])
        .replace("__WEBGL_RENDERER__", gpu["webgl_renderer"])
        .replace("__CANVAS_NOISE__", str(canvas_noise))
    )


# Legacy alias — server.py previously defined its own INIT_SCRIPT; keep the
# name available so any external imports don't break.
def INIT_SCRIPT():
    """Old API shim. Real code should call build_init_script()."""
    return build_init_script(
        {"cores": 8, "device_memory": 8, "touch_points": 0,
         "gpu": pick_gpu("Windows", 0), "locale": "en-US"},
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    )


# ─────────────────────────────────────────────────────────────────────────
# Context options
# ─────────────────────────────────────────────────────────────────────────

def build_context_options(fingerprint: dict, ua: str, proxy=None,
                          viewport=None) -> dict:
    """Context options with a fully coherent identity:
    locale ↔ languages ↔ timezone ↔ geolocation ↔ devicePixelRatio.
    proxy is a dict {proto, host, port, username, password} or None.

    clearcote / shardx: the persona lives in the ENGINE (C++), so the
    context only carries functional options. No user_agent / timezone /
    locale / headers / proxy — the persona owns the identity and the proxy
    rides on browser launch (Playwright rejects a context-level proxy when
    the browser was launched with one)."""
    if ENGINE in ("clearcote", "shardx"):
        vp = viewport or {"width": 1920, "height": 1080}
        opts = {
            "viewport": vp,
            "ignore_https_errors": True,
            "bypass_csp": True,
        }
        return opts
    profile = fingerprint.get("locale_profile") or random.choice(_LOCALE_PROFILES)
    vp = viewport or {"width": 1920, "height": 1080}
    opts = {
        "viewport": vp,
        "user_agent": ua,
        "timezone_id": profile["tz"],
        "locale": profile["locale"],
        "geolocation": profile["geo"],
        "permissions": ["geolocation"],
        "device_scale_factor": fingerprint.get("pixel_ratio", 1.0),
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": random.choice(["light", "light", "dark"]),
        "bypass_csp": True,
        "ignore_https_errors": True,
        "storage_state": None,
        "no_viewport": False,
        "reduced_motion": "no-preference",
        "forced_colors": "none",
        "extra_http_headers": {
            "sec-ch-ua": sec_ch_ua_header(ua),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": _sec_ch_ua_platform(ua),
            "accept-language": f"{profile['locale']},{profile['locale'].split('-')[0]};q=0.9,en;q=0.8",
            "upgrade-insecure-requests": "1",
        },
    }
    if proxy:
        p = proxy
        proto = p.get("proto", "http")
        server = f"{proto}://{p.get('host')}:{p.get('port')}"
        proxy_cfg = {"server": server}
        if p.get("username"):
            proxy_cfg["username"] = p.get("username")
            proxy_cfg["password"] = p.get("password", "")
        opts["proxy"] = proxy_cfg
    return opts


def _sec_ch_ua_platform(ua: str) -> str:
    u = ua or ""
    if "Windows" in u:
        return '"Windows"'
    if "Macintosh" in u or "Mac OS X" in u:
        return '"macOS"'
    return '"Linux"'


# ─────────────────────────────────────────────────────────────────────────
# CDP-level hardening (runs before any page script)
# ─────────────────────────────────────────────────────────────────────────

_CDP_WEBDRIVER_SRC = (
    "Object.defineProperty(Navigator.prototype, 'webdriver', "
    "{ get: () => undefined, configurable: true });"
)


async def apply_cdp_stealth(context, page) -> None:
    """CDP-level patches that run before init scripts / page JS.
    Works for both Playwright and Patchright (new_cdp_session exists on both).

    clearcote / shardx: no-op — the engine's C++ layer already hides
    webdriver and the launch defaults hold back CDP side-effects. Injecting
    JS here would create the exact self-revealing shim tells the engines
    remove."""
    if ENGINE in ("clearcote", "shardx"):
        return
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": _CDP_WEBDRIVER_SRC,
        })
    except Exception:
        pass

