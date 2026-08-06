#!/usr/bin/env python3
"""
hCaptcha Universal Solver — Free, Self-Contained Edition
=========================================================
Zero paid APIs. Uses your trained brains (models/):
  model_grid.pth      — 33-class tile classifier (ResNet18)
  model_drag.pth      — drag position regressor (ResNet18 fc=2)
  motion_params.json  — human mouse-behavior stats

Tactics:
  · curl_cffi for TLS fingerprinting (mimics Chrome)
  · Playwright for HSW proof-of-work token generation
  · Synthetic motion data (no multibot.in)
  · ResNet18 classifier for tile grids (trained brain)
  · OpenCV template matching for drag puzzles (in-browser + API)
  · Offline pixel-similarity for FunCAPTCHA / Arkose tiles
  · Direct API calls + in-browser drag solving

Requirements:
  pip install curl_cffi playwright opencv-python numpy pillow torch torchvision
  python -m playwright install chromium

Usage:
  # Standalone solve:
  python captcha_solver.py --sitekey a9b5fb07-92ff-493f-86fe-352a2803b3df --host discord.com

  # In a browser script (import helpers):
  from captcha_solver import (
      solve_hcaptcha_drag, solve_funcaptcha_pixels,
      extract_hcaptcha_sitekey, read_hcaptcha_token, set_hcaptcha_token_on_page,
  )
"""

import argparse
import asyncio
import io
import json
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Lazy / optional imports — not all environments have these.
# They are imported properly inside the functions that need them.
try:
    import cv2
except ImportError:
    cv2 = None  # type: ignore
try:
    import numpy as np
except ImportError:
    np = None  # type: ignore
try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None  # type: ignore
try:
    from PIL import Image, ImageChops
except ImportError:
    Image = ImageChops = None  # type: ignore

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
CHROME_VERSION = "130"
HCAPTCHA_API = "https://api2.hcaptcha.com"
DEFAULT_VERSION = "c3663008fb8d8104807d55045f8251cbe96a2f84"

MODELS_DIR = Path(__file__).resolve().parent / "models"

SCREEN_SIZES = [
    (1920, 1080), (1366, 768), (1536, 864),
    (1440, 900), (1280, 720), (1600, 900),
]
CORE_COUNTS = [4, 8, 6, 12]
COLOR_DEPTHS = [24, 30]
LANGUAGES = [
    ("en-US", ["en-US", "en"]),
    ("en-GB", ["en-GB", "en"]),
]

_SITEKEY_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════
# TLS Session (curl_cffi — free Chrome fingerprint)
# ═══════════════════════════════════════════════════════════════

def make_session(proxy: Optional[str] = None) -> cffi_requests.Session:
    s = cffi_requests.Session()
    s.headers.update({
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache", "pragma": "no-cache",
        "sec-ch-ua": f'"Chromium";v="{CHROME_VERSION}", "Google Chrome";v="{CHROME_VERSION}", "Not?A_Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": CHROME_UA,
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


# ═══════════════════════════════════════════════════════════════
# Synthetic Motion Data Generator
# ═══════════════════════════════════════════════════════════════

class MotionData:
    """Generates realistic fake motion data in hCaptcha's exact JSON format."""

    def __init__(self):
        self.base_ms = int(time.time() * 1000)
        self.screen_w, self.screen_h = random.choice(SCREEN_SIZES)
        self.color_depth = random.choice(COLOR_DEPTHS)
        self.cores = random.choice(CORE_COUNTS)
        self.lang, self.langs = random.choice(LANGUAGES)
        self.counter = 0
        self.params = {}
        mp = MODELS_DIR / "motion_params.json"
        if mp.exists():
            try:
                with open(mp) as f:
                    self.params = json.load(f)
            except Exception:
                pass

    def _tick(self, ms: int = 0) -> int:
        if ms:
            self.counter += ms
        else:
            mean_pause = self.params.get("mean_pause") or 16
            lo = max(1, int(mean_pause * 0.6))
            hi = max(lo + 1, int(mean_pause * 1.6))
            self.counter += random.randint(lo, hi)
        return self.base_ms + self.counter

    def _human_path(self, start: Tuple[int, int], end: Tuple[int, int],
                    points: int = 30) -> List[List[int]]:
        if self.params.get("mean_points"):
            points = max(8, min(60, int(self.params["mean_points"])))
        path = []
        sx, sy = start
        ex, ey = end
        for i in range(points):
            t = i / (points - 1)
            cx = sx + (ex - sx) * 0.4 + random.randint(-8, 8)
            cy = sy + (ey - sy) * 0.3 + random.randint(-6, 6)
            x = int((1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex)
            y = int((1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey)
            x += random.randint(-1, 1)
            y += random.randint(-1, 1)
            path.append([x, y, self._tick(6 + random.randint(0, 8))])
        return path

    def get_captcha_motion(self) -> dict:
        widget_x = random.randint(0, self.screen_w - 310)
        widget_y = random.randint(0, self.screen_h - 85)
        start = (random.randint(300, self.screen_w - 300),
                 random.randint(100, self.screen_h - 200))
        end_center = (widget_x + 30, widget_y + 37)
        path = self._human_path(start, end_center)
        mm = [[x - widget_x, y - widget_y, t] for x, y, t in path]
        periods = [(mm[i + 1][2] - mm[i][2]) for i in range(len(mm) - 1)]
        avg_period = sum(periods) / len(periods) if periods else 0
        return {
            "st": self.base_ms, "mm": mm, "mm-mp": avg_period,
            "md": [mm[-1][:2] + [self._tick(50)]], "md-mp": 0,
            "mu": [mm[-1][:2] + [self._tick(100)]], "mu-mp": 0,
            "v": 1,
            "topLevel": self._top_level(widget_x, widget_y, start),
            "session": [],
            "widgetList": ["0" + "".join(random.choices("abcdef0123456789", k=10))],
            "widgetId": "0" + "".join(random.choices("abcdef0123456789", k=10)),
            "href": f"https://{self.host}/",
            "prev": {"escaped": False, "passed": False,
                     "expiredChallenge": False, "expiredResponse": False},
        }

    def get_check_motion(self) -> dict:
        widget_x = random.randint(0, self.screen_w - 310)
        widget_y = random.randint(0, self.screen_h - 85)
        start = (random.randint(300, self.screen_w - 300),
                 random.randint(100, self.screen_h - 200))
        end_center = (widget_x + 30, widget_y + 37)
        path = self._human_path(start, end_center)
        mm = [[x - widget_x, y - widget_y, t] for x, y, t in path]
        periods = [(mm[i + 1][2] - mm[i][2]) for i in range(len(mm) - 1)]
        avg_period = sum(periods) / len(periods) if periods else 0
        return {
            "st": self.base_ms, "mm": mm, "mm-mp": avg_period,
            "md": [mm[-1][:2] + [self._tick(50)]], "md-mp": 0,
            "mu": [mm[-1][:2] + [self._tick(100)]], "mu-mp": 0,
            "v": 1,
            "topLevel": self._top_level(widget_x, widget_y, start),
            "session": [], "widgetList": [], "widgetId": "",
            "href": f"https://{self.host}/",
            "prev": {"escaped": False, "passed": False,
                     "expiredChallenge": False, "expiredResponse": False},
        }

    host = "discord.com"  # default; overwritten by HCaptchaSolver

    def _top_level(self, widget_x, widget_y, start) -> dict:
        taskbar = random.choice([0, 30, 40, 48])
        avail_h = max(1, self.screen_h - taskbar)
        start = (0, random.randint(100, self.screen_h - 200))
        end = (widget_x + random.randint(10, 280),
               widget_y + random.randint(10, 60))
        mm = self._human_path(start, end, 20)
        return {
            "inv": False,
            "st": self.base_ms - random.randint(200, 800),
            "sc": {
                "availWidth": self.screen_w, "availHeight": avail_h,
                "width": self.screen_w, "height": self.screen_h,
                "colorDepth": self.color_depth, "pixelDepth": self.color_depth,
                "top": 0, "left": 0, "availTop": 0, "availLeft": 0,
            },
            "nv": {
                "vendor": "Google Inc.", "vendorSub": "",
                "cookieEnabled": True, "webdriver": False,
                "hardwareConcurrency": self.cores,
                "userAgent": CHROME_UA, "language": self.lang,
                "languages": self.langs, "onLine": True,
                "doNotTrack": None, "maxTouchPoints": 0,
                "pdfViewerEnabled": True,
                "plugins": ["internal-pdf-viewer"] if random.random() > 0.3 else [],
            },
            "dr": "", "exec": False,
            "wn": [[self.screen_w, self.screen_h, 1, self.base_ms - 500]],
            "wn-mp": 0,
            "xy": [[0, 0, 1, self.base_ms - 500]], "xy-mp": 0,
            "mm": mm,
            "mm-mp": sum((mm[i+1][2]-mm[i][2]) for i in range(len(mm)-1)) / max(len(mm)-1, 1),
        }


# ═══════════════════════════════════════════════════════════════
# HSW Token Generator (Playwright)
# ═══════════════════════════════════════════════════════════════

class HSWGenerator:
    """Generates the HSW proof-of-work token hCaptcha requires."""

    def __init__(self, sitekey: str, host: str, version: str, proxy: Optional[str] = None):
        self.sitekey = sitekey
        self.host = host
        self.version = version
        self.proxy = proxy
        self._hsw_js: Optional[str] = None
        self._browser = None
        self._context = None

    async def _ensure_js(self, session: cffi_requests.Session, req_token: str):
        if self._hsw_js is not None:
            return
        try:
            import base64
            payload = req_token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            hsw_url = f"https://newassets.hcaptcha.com{decoded['l']}/hsw.js"
        except Exception:
            hsw_url = f"https://newassets.hcaptcha.com/c/{self.version}/hsw.js"
        resp = session.get(hsw_url)
        self._hsw_js = resp.text

    async def _get_page(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            launch_args = {
                "headless": True,
                "args": [
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--window-size=1920,1080",
                ],
            }
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}
            self._browser = await pw.chromium.launch(**launch_args)
            self._pw = pw
            self._context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=CHROME_UA,
            )

    async def generate(self, session: cffi_requests.Session,
                       req_token: str) -> Optional[str]:
        await self._ensure_js(session, req_token)
        await self._get_page()
        page = await self._context.new_page()
        try:
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200, content_type="text/html",
                    body="<html><head></head><body></body></html>",
                ),
            )
            await page.goto(f"https://{self.host}/", wait_until="domcontentloaded", timeout=10000)
            await page.evaluate(self._hsw_js)
            for _ in range(30):
                try:
                    if await page.evaluate("typeof hsw === 'function'"):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.02)
            result = await page.evaluate("(req) => hsw(req)", req_token)
            return result
        finally:
            await page.close()

    async def close(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if hasattr(self, '_pw'):
            await self._pw.stop()


# ═══════════════════════════════════════════════════════════════
# Tile Classifier (ResNet18 — trained brain)
# ═══════════════════════════════════════════════════════════════

class TileClassifier:
    """Loads model_grid.pth and classifies tile images."""

    CLASSES = ["bicycle", "bus", "motorcycle", "truck", "train",
               "cat", "dog", "bird", "car", "airplane", "boat", "traffic light"]

    def __init__(self, model_path: Optional[str] = None):
        self.model = None
        self.use_model = False
        if not model_path:
            for candidate in (MODELS_DIR / "model_grid.pth", Path("model.pth")):
                if candidate.exists():
                    model_path = str(candidate)
                    break
        if model_path and Path(model_path).exists():
            try:
                import torch
                from torchvision import models, transforms
                raw = torch.load(model_path, map_location="cpu", weights_only=False)
                if isinstance(raw, dict) and "state_dict" in raw:
                    state = raw["state_dict"]
                    saved_classes = raw.get("classes")
                    if saved_classes:
                        self.CLASSES = list(saved_classes)
                else:
                    state = raw
                self.model = models.resnet18(weights=None)
                self.model.fc = torch.nn.Linear(self.model.fc.in_features, len(self.CLASSES))
                self.model.load_state_dict(state)
                self.model.eval()
                self.transform = transforms.Compose([
                    transforms.Resize(256), transforms.CenterCrop(224),
                    transforms.ToTensor(),
                    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
                ])
                self.use_model = True
                print(f"  🧠 Loaded tile classifier ({len(self.CLASSES)} classes)")
            except Exception as e:
                print(f"  ⚠️  Tile classifier load failed ({e}) — heuristic fallback")

    def classify(self, img_bytes: bytes) -> str:
        if self.use_model and self.model is not None:
            import torch
            try:
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                x = self.transform(img).unsqueeze(0)
                with torch.no_grad():
                    pred = self.model(x).argmax(1).item()
                return self.CLASSES[pred]
            except Exception:
                pass
        # Heuristic fallback
        arr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return "unknown"
        edges = cv2.Canny(img, 50, 150)
        return f"object_{edges.sum() % 12}"


# ═══════════════════════════════════════════════════════════════
# Drag Brain (ResNet18 regressor — trained model_drag.pth)
# ═══════════════════════════════════════════════════════════════

class DragBrain:
    """Loads model_drag.pth and regresses normalized (x,y) drag target."""

    def __init__(self, model_path: Optional[str] = None):
        self.model = None
        self.use_model = False
        if not model_path:
            model_path = MODELS_DIR / "model_drag.pth"
        if model_path and Path(model_path).exists():
            try:
                import torch
                from torchvision import models, transforms
                raw = torch.load(model_path, map_location="cpu", weights_only=False)
                state = raw.get("state_dict", raw) if isinstance(raw, dict) else raw
                self.model = models.resnet18(weights=None)
                self.model.fc = torch.nn.Linear(self.model.fc.in_features, 2)
                self.model.load_state_dict(state)
                self.model.eval()
                self.transform = transforms.Compose([
                    transforms.Resize((224, 224)), transforms.ToTensor(),
                    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
                ])
                self.use_model = True
                print("  🧠 Loaded drag brain")
            except Exception as e:
                print(f"  ⚠️  Drag brain load failed ({e}) — OpenCV only")

    def predict(self, bg_bgr: np.ndarray) -> Optional[Tuple[float, float]]:
        if not self.use_model or self.model is None:
            return None
        try:
            import torch
            rgb = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2RGB)
            x = self.transform(Image.fromarray(rgb)).unsqueeze(0)
            with torch.no_grad():
                out = self.model(x)[0].tolist()
            return float(out[0]), float(out[1])
        except Exception:
            return None


_DRAG_BRAIN: Optional[DragBrain] = None

def get_drag_brain() -> DragBrain:
    global _DRAG_BRAIN
    if _DRAG_BRAIN is None:
        _DRAG_BRAIN = DragBrain()
    return _DRAG_BRAIN


# ═══════════════════════════════════════════════════════════════
# Drag Solver (OpenCV template matching + brain fallback)
# ═══════════════════════════════════════════════════════════════

def solve_drag(piece_bytes: bytes, bg_bytes: bytes) -> Tuple[int, int, float]:
    """Match puzzle piece to background. Returns (x, y, confidence%)."""
    piece = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_COLOR)
    bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if piece is None or bg is None:
        return 0, 0, 0.0

    ph, pw = piece.shape[:2]
    bh, bw = bg.shape[:2]
    if ph < 8 or pw < 8 or ph > bh or pw > bw:
        return bw // 2, bh // 2, 0.0

    piece_gray = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)
    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    piece_edges = cv2.Canny(piece_gray, 50, 150)
    bg_edges = cv2.Canny(bg_gray, 50, 150)

    best_x, best_y, best_conf = 0, 0, -999.0
    for scale in np.linspace(0.6, 1.4, 12):
        spw, sph = int(pw * scale), int(ph * scale)
        if spw < 8 or sph < 8 or spw > bw or sph > bh:
            continue
        sp = cv2.resize(piece, (spw, sph))
        sp_edges = cv2.resize(piece_edges, (spw, sph), interpolation=cv2.INTER_NEAREST)
        for tmpl, tgt in [
            (sp, bg), (sp_edges, bg_edges),
            (sp, bg),  # TM_CCORR_NORMED
        ]:
            result = cv2.matchTemplate(tgt, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            conf = max_val * 100
            if conf > best_conf:
                best_conf = conf
                best_x, best_y = max_loc

    # Brain fallback when OpenCV is unsure
    if best_conf < 55:
        brain = get_drag_brain()
        if brain.use_model:
            pred = brain.predict(bg)
            if pred:
                nx, ny = pred
                bx, by = int(nx * bw), int(ny * bh)
                if 0 <= bx < bw and 0 <= by < bh:
                    return bx, by, 70.0
    return best_x, best_y, best_conf


# ═══════════════════════════════════════════════════════════════
# DOM helpers
# ═══════════════════════════════════════════════════════════════

def _is_valid_sitekey(value: str) -> bool:
    v = (value or "").strip()
    return bool(_SITEKEY_RE.match(v))


async def extract_hcaptcha_sitekey(page) -> str:
    """Pull the hCaptcha sitekey from DOM, iframe src, or hcaptcha global."""
    # Strategy 1: [data-sitekey]
    try:
        sk = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            return el ? el.getAttribute('data-sitekey') : '';
        }""")
        if _is_valid_sitekey(str(sk)):
            return str(sk).strip()
    except Exception:
        pass
    # Strategy 2: hcaptcha iframe src
    try:
        src = await page.evaluate("""() => {
            const f = document.querySelector('iframe[src*="hcaptcha.com"]');
            return f ? f.src : '';
        }""")
        m = re.search(r"sitekey=([^&]+)", src or "")
        if m and _is_valid_sitekey(m.group(1)):
            return m.group(1)
    except Exception:
        pass
    # Strategy 3: scan all iframes
    try:
        sitekey = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            for (const f of iframes) {
                const m = (f.src || '').match(/sitekey=([^&#]+)/);
                if (m) return m[1];
            }
            return '';
        }""")
        if _is_valid_sitekey(sitekey):
            return sitekey.strip()
    except Exception:
        pass
    # Strategy 4: hcaptcha global
    try:
        sk = await page.evaluate("""() => {
            if (window.hcaptcha && window.hcaptcha.getSitekey) {
                try { return window.hcaptcha.getSitekey(); } catch(e) {}
            }
            return '';
        }""")
        if _is_valid_sitekey(str(sk)):
            return str(sk).strip()
    except Exception:
        pass
    return ""


async def read_hcaptcha_token(page) -> Optional[str]:
    """Read the current h-captcha-response token from the page."""
    try:
        token = await page.evaluate("""() => {
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            if (ta && ta.value && ta.value.length > 20) return ta.value;
            if (window.hcaptcha && window.hcaptcha.getResponse) {
                const r = window.hcaptcha.getResponse();
                if (r && r.length > 20) return r;
            }
            return '';
        }""")
        if token:
            return token
    except Exception:
        pass
    return None


async def set_hcaptcha_token_on_page(page, token: str) -> bool:
    """Inject a solved token into the hCaptcha textarea."""
    try:
        result = await page.evaluate(f""""() => {{
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            if (ta) {{
                ta.value = '{token}';
                ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                ta.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }}
            return false;
        }}""")
        return bool(result)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# Offline tile similarity (FunCAPTCHA / Arkose)
# ═══════════════════════════════════════════════════════════════

def _tile_signature(img: Image.Image) -> List[float]:
    small = img.resize((32, 32), Image.LANCZOS)
    gray = small.convert('L')
    avg_brightness = sum(gray.getdata()) / (32 * 32)
    pixels = list(small.getdata())
    n = len(pixels)
    r_avg = sum(p[0] for p in pixels) / n
    g_avg = sum(p[1] for p in pixels) / n
    b_avg = sum(p[2] for p in pixels) / n
    variance = sum((p[0] - r_avg)**2 + (p[1] - g_avg)**2 + (p[2] - b_avg)**2
                   for p in pixels) / n
    edge_sum = 0
    for y in range(32):
        for x in range(31):
            edge_sum += abs(gray.getpixel((x + 1, y)) - gray.getpixel((x, y)))
    edge_density = edge_sum / (32 * 31)
    return [avg_brightness / 255, r_avg / 255, g_avg / 255, b_avg / 255,
            variance / 50000, edge_density / 50]


def _signature_distance(sig1: List[float], sig2: List[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(sig1, sig2)))


def find_matching_tiles_by_similarity(tiles: List[Image.Image],
                                      threshold: float = 0.15) -> List[int]:
    if len(tiles) < 3:
        return list(range(len(tiles)))
    sigs = [_tile_signature(t) for t in tiles]
    n_dims = len(sigs[0])
    median_sig = []
    for dim in range(n_dims):
        vals = sorted(s[dim] for s in sigs)
        median_sig.append(vals[len(vals) // 2])
    distances = [_signature_distance(s, median_sig) for s in sigs]
    avg_dist = sum(distances) / len(distances)
    adaptive = max(threshold, avg_dist * 1.2)
    matching = [i for i, d in enumerate(distances) if d > adaptive]
    if len(matching) > len(tiles) * 0.7:
        return []
    return matching


def split_grid_screenshot(screenshot_bytes: bytes,
                          grid_size: int = 3) -> List[Image.Image]:
    img = Image.open(io.BytesIO(screenshot_bytes))
    w, h = img.size
    margin_x = int(w * 0.02)
    margin_y = int(h * 0.02)
    tile_w = (w - 2 * margin_x) // grid_size
    tile_h = (h - 2 * margin_y) // grid_size
    if tile_w < 20 or tile_h < 20:
        return []
    tiles = []
    for row in range(grid_size):
        for col in range(grid_size):
            left = margin_x + col * tile_w
            top = margin_y + row * tile_h
            tile = img.crop((left, top, left + tile_w, top + tile_h))
            tiles.append(tile.resize((128, 128), Image.LANCZOS))
    return tiles


# ── FunCAPTCHA solver ───────────────────────────────────────

FUNCAPTCHA_SELECTORS = [
    'iframe[src*="funcaptcha"]', 'iframe[src*="arkose"]',
    'iframe[title*="captcha"]', 'iframe[src*="captcha"]',
    '[id*="funcaptcha"]', '[class*="funcaptcha"]',
    '[class*="Challenge"]',
]


async def extract_funcaptcha_task(page, iframe=None) -> str:
    try:
        if iframe:
            text = await iframe.evaluate("""() => {
                const els = document.querySelectorAll('[class*="challenge"], [class*="prompt"], [class*="instruction"], [class*="header"], h1, h2, [class*="title"]');
                for (const el of els) {
                    const t = (el.textContent || '').trim();
                    if (t.length > 6 && t.length < 200) return t;
                }
                return document.body ? document.body.innerText.slice(0, 300) : '';
            }""")
            if text and len(str(text).strip()) > 5:
                return str(text).strip()
    except Exception:
        pass
    try:
        text = await page.evaluate("""() => {
            const el = document.querySelector('[class*="challenge"], [class*="prompt"], [class*="instruction"], [class*="header"]');
            return el ? el.textContent.trim().slice(0, 200) : '';
        }""")
        if text and len(str(text).strip()) > 5:
            return str(text).strip()
    except Exception:
        pass
    return ""


async def solve_funcaptcha_pixels(page, iframe=None,
                                  log: Optional[Callable] = None) -> bool:
    """Solve a FunCAPTCHA/Arkose tile challenge offline via pixel similarity."""
    log = log or (lambda msg, level="info": None)
    if iframe is None:
        for sel in FUNCAPTCHA_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    iframe = el
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)
    if not iframe:
        log("[FunCAPTCHA] No challenge element found", level="warn")
        return False
    await asyncio.sleep(1)

    clicked = 0

    # Try DOM tile boxes
    tile_boxes = []
    try:
        data = await iframe.evaluate("""() => {
            const selectors = '.task-image, [class*="image"], [role="button"] > div, ' +
                              '.grid-item, .cell, td, img[class*="task"], ' +
                              '.image-grid > div, [class*="tile"]';
            const els = document.querySelectorAll(selectors);
            const out = [];
            els.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 30 && r.height > 30 && r.width < 500 && r.height < 500) {
                    out.push({x: r.x, y: r.y, w: r.width, h: r.height});
                }
            });
            return JSON.stringify(out);
        }""")
        if data:
            boxes = json.loads(data)
            if len(boxes) >= 2:
                ibox = await iframe.bounding_box()
                if ibox:
                    tile_boxes = [{'x': ibox['x'] + b['x'], 'y': ibox['y'] + b['y'],
                                   'w': b['w'], 'h': b['h']} for b in boxes]
    except Exception:
        pass

    if len(tile_boxes) >= 2:
        tiles = []
        valid = []
        for i, b in enumerate(tile_boxes):
            try:
                clip = {'x': b['x'], 'y': b['y'], 'width': b['w'], 'height': b['h']}
                shot = await page.screenshot(clip=clip)
                tiles.append(Image.open(io.BytesIO(shot)).resize((128, 128), Image.LANCZOS))
                valid.append(i)
            except Exception:
                tiles.append(None)
        if len(valid) >= 2:
            sig_tiles = [tiles[i] for i in valid]
            local = find_matching_tiles_by_similarity(sig_tiles)
            matching = [valid[i] for i in local] if local else []
            if not matching:
                matching = valid
            for idx in matching:
                b = tile_boxes[idx]
                try:
                    await page.mouse.click(b['x'] + b['w'] / 2, b['y'] + b['h'] / 2)
                    clicked += 1
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            log(f"[FunCAPTCHA] Clicked {clicked} tiles (DOM boxes)")

    # Grid split fallback
    if clicked == 0:
        try:
            box = await iframe.bounding_box()
            if box and box['width'] >= 100 and box['height'] >= 100:
                clip = {'x': box['x'], 'y': box['y'],
                        'width': box['width'], 'height': box['height']}
                shot = await page.screenshot(clip=clip)
                img = Image.open(io.BytesIO(shot))
                w, h = img.size
                grid = 4 if (w / h) > 1.5 else 3
                tiles = split_grid_screenshot(shot, grid)
                if len(tiles) >= 2:
                    matching = find_matching_tiles_by_similarity(tiles) or list(range(len(tiles)))
                    margin_x = int(box['width'] * 0.02)
                    margin_y = int(box['height'] * 0.02)
                    tile_w = (box['width'] - 2 * margin_x) / grid
                    tile_h = (box['height'] - 2 * margin_y) / grid
                    for idx in matching:
                        row, col = divmod(idx, grid)
                        x = box['x'] + margin_x + col * tile_w + tile_w / 2
                        y = box['y'] + margin_y + row * tile_h + tile_h / 2
                        try:
                            await page.mouse.click(x, y)
                            clicked += 1
                        except Exception:
                            pass
                        await asyncio.sleep(0.25)
                    log(f"[FunCAPTCHA] Clicked {clicked} tiles (grid split)")
        except Exception as e:
            log(f"[FunCAPTCHA] grid split error: {e}", level="warn")

    if clicked == 0:
        return False

    # Submit button
    try:
        await iframe.evaluate("""() => {
            const btns = document.querySelectorAll('button, [role="button"], [type="submit"]');
            for (const b of btns) {
                const t = (b.textContent || '').toLowerCase();
                if (b.offsetParent !== null &&
                    (t.includes('verify') || t.includes('submit') ||
                     t.includes('continue') || t.includes('done'))) {
                    b.click();
                    return;
                }
            }
        }""")
    except Exception:
        pass
    await asyncio.sleep(2.5)

    # Check solved
    try:
        solved = await page.evaluate("""() => {
            const fc = document.querySelector('textarea[name="fc-token"]');
            if (fc && fc.value && fc.value.length > 10) return 'fc-token';
            const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
            if (ta && ta.value && ta.value.length > 10) return 'recaptcha';
            const ch = document.querySelector('[class*="challenge" i], [class*="Challenge"]');
            if (ch && getComputedStyle(ch).display === 'none') return 'hidden';
            return '';
        }""")
    except Exception:
        solved = ""
    if solved:
        log(f"[FunCAPTCHA] SOLVED ({solved})")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# hCaptcha Puzzle Drag — In-Browser OpenCV Solver
# ═══════════════════════════════════════════════════════════════

def _puzzle_edge_profile(img: Image.Image) -> List[float]:
    gray = img.convert('L')
    w, h = gray.size
    px = gray.load()
    best: List[float] = []
    for band_top, band_bot in ((int(h * 0.08), int(h * 0.85)),
                               (int(h * 0.05), int(h * 0.95))):
        prof = [0.0] * w
        for y in range(band_top, band_bot):
            prev = px[0, y]
            for x in range(1, w):
                cur = px[x, y]
                prof[x] += abs(cur - prev)
                prev = cur
        n = max(1, band_bot - band_top)
        prof = [p / n for p in prof]
        if not best or max(prof) > max(best):
            best = prof
    return best


def _find_outline_pairs(img: Image.Image, max_pairs: int = 16) -> List[tuple]:
    prof = _puzzle_edge_profile(img)
    w = len(prof)
    if w < 40:
        return []
    mean = sum(prof) / len(prof)
    peaks = []
    for threshold in (mean * 2.0 + 1.5, mean * 1.3 + 0.8, mean * 1.05 + 0.4):
        found = []
        for x in range(1, w - 1):
            if prof[x] >= threshold and prof[x] >= prof[x - 1] and prof[x] >= prof[x + 1]:
                found.append((x, prof[x]))
        if len(found) >= 2:
            peaks = found
            break
    if not peaks:
        return []
    peaks.sort(key=lambda p: -p[1])
    kept = []
    for x, s in peaks:
        if all(abs(x - kx) > 3 for kx, _ in kept):
            kept.append((x, s))
        if len(kept) >= 12:
            break
    kept.sort()
    min_w = int(w * 0.22)
    max_w = int(w * 0.85)
    pairs = []
    for i in range(len(kept)):
        for j in range(i + 1, len(kept)):
            lx, ls = kept[i]
            rx, rs = kept[j]
            if min_w <= rx - lx <= max_w:
                pairs.append((lx, rx, ls + rs))
    pairs.sort(key=lambda p: -p[2])
    return pairs[:max_pairs]


def _puzzle_deltas(img: Image.Image) -> List[int]:
    w = img.size[0]
    pairs = _find_outline_pairs(img)
    if len(pairs) < 2:
        return []
    clusters = []
    for p in pairs:
        pw = p[1] - p[0]
        for cl in clusters:
            if abs(cl['w'] - pw) <= int(w * 0.05):
                cl['pairs'].append(p)
                n = len(cl['pairs'])
                cl['w'] = (cl['w'] * (n - 1) + pw) / n
                break
        else:
            clusters.append({'w': float(pw), 'pairs': [p]})
    clusters.sort(key=lambda c: (-len(c['pairs']), -max(p[2] for p in c['pairs'])))
    members = sorted(clusters[0]['pairs'], key=lambda p: -p[2])
    keep = []
    for p in members:
        if all(abs(p[0] - k[0]) > 15 and abs(p[1] - k[1]) > 15 for k in keep):
            keep.append(p)
        if len(keep) >= 3:
            break
    if len(keep) < 2:
        return []
    cands = []
    for i in range(len(keep)):
        for j in range(len(keep)):
            if i == j:
                continue
            d = keep[j][0] - keep[i][0]
            if d != 0 and abs(d) < w * 0.8:
                cands.append((-keep[i][2], abs(d), d))
    cands.sort()
    seen, out = set(), []
    for _s, _a, d in cands:
        if d not in seen:
            seen.add(d)
            out.append(d)
        if len(out) >= 5:
            break
    return out


def _find_piece_boxes(img: Image.Image, max_boxes: int = 4) -> list:
    gray = img.convert('L')
    w, h = gray.size
    if w < 60 or h < 60:
        return []
    px = gray.load()
    band_top = int(h * 0.08)
    band_bot = int(h * 0.85)
    prof = [0.0] * w
    for y in range(band_top, band_bot):
        prev = px[0, y]
        for x in range(1, w):
            cur = px[x, y]
            prof[x] += abs(cur - prev)
            prev = cur
    n = max(1, band_bot - band_top)
    prof = [p / n for p in prof]
    mean = sum(prof) / len(prof)
    thresh = max(mean * 2.0 + 1.0, 2.0)
    peaks = []
    for x in range(1, w - 1):
        if prof[x] >= thresh and prof[x] >= prof[x - 1] and prof[x] >= prof[x + 1]:
            peaks.append(x)
    if len(peaks) < 2:
        return []
    clusters = []
    for p in peaks:
        if clusters and p - clusters[-1][-1] <= 5:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    scored = [(c[len(c) // 2], sum(prof[x] for x in c)) for c in clusters]
    min_sep = int(w * 0.15)
    pairs = []
    for i in range(len(scored)):
        for j in range(i + 1, len(scored)):
            a, ea = scored[i]
            b, eb = scored[j]
            if a > b:
                a, b = b, a
            bw2 = b - a
            if bw2 < min_sep or bw2 > int(w * 0.8):
                continue
            pairs.append((a, b, ea + eb))
    pairs.sort(key=lambda t: -t[2])
    boxes = []
    for a, b, _s in pairs:
        dup = any(abs(a - bx) < 8 and abs(b - (bx + bw_)) < 8
                  for (bx, _by, bw_, _bh) in boxes)
        if dup:
            continue
        pw = b - a
        row_prof = []
        for y in range(band_top, band_bot):
            e = sum(abs(px[x, y] - px[x - 1, y]) for x in range(max(1, a), b))
            row_prof.append(e)
        rmean = sum(row_prof) / max(1, len(row_prof))
        rvar = sum((e - rmean) ** 2 for e in row_prof) / max(1, len(row_prof))
        rstd = rvar ** 0.5
        rthresh = rmean + 1.5 * rstd
        best_run, cur_run = [], []
        for i, e in enumerate(row_prof):
            if e >= rthresh:
                cur_run.append(i)
            else:
                if len(cur_run) > len(best_run):
                    best_run = cur_run
                cur_run = []
        if len(cur_run) > len(best_run):
            best_run = cur_run
        y_top, y_bot = band_top, band_bot
        if len(best_run) >= 15:
            y_top = band_top + best_run[0]
            y_bot = band_top + best_run[-1] + 1
        ph = y_bot - y_top
        if ph < 20:
            y_top, ph = band_top, band_bot - band_top
        boxes.append((a, y_top, pw, ph))
        if len(boxes) >= max_boxes:
            break
    return boxes


def _template_match_hole(img: Image.Image, piece_box: tuple,
                         scale: int = 4) -> Optional[tuple]:
    try:
        x, y, w, h = piece_box
        if w < 24 or h < 24:
            return None
        inset = max(2, int(min(w, h) * 0.06))
        tpl = img.crop((x + inset, y + inset, x + w - inset, y + h - inset))
        if tpl.width < 8 or tpl.height < 8:
            return None
        tw = max(6, tpl.width // scale)
        th = max(6, tpl.height // scale)
        tpl_s = tpl.convert('L').resize((tw, th), Image.BILINEAR)
        band = img.crop((0, y + inset, img.width, y + h - inset))
        bw = max(6, band.width // scale)
        bh = max(6, band.height // scale)
        band_s = band.convert('L').resize((bw, bh), Image.BILINEAR)
        if bw - tw < 10:
            return None
        piece_cx_s = int(((x + inset) + (x + w - inset)) / 2 / scale)
        best_score = float('inf')
        best_x = None
        for sx in range(0, bw - tw + 1):
            if abs(sx + tw / 2 - piece_cx_s) < tw * 0.75:
                continue
            diff = ImageChops.difference(band_s.crop((sx, 0, sx + tw, th)), tpl_s)
            score = sum(diff.getdata()) / (tw * th)
            if score < best_score:
                best_score = score
                best_x = sx
        if best_x is None:
            return None
        coarse = best_x * scale
        tpl_f = tpl.convert('L')
        band_f = band.convert('L')
        twf, thf = tpl_f.size
        best_f = None
        lo = max(0, coarse - scale * 2)
        hi = min(band_f.width - twf, coarse + scale * 2)
        for fx in range(lo, hi + 1):
            region = band_f.crop((fx, 0, fx + twf, thf))
            diff = ImageChops.difference(region, tpl_f)
            score = sum(diff.getdata()) / (twf * thf)
            if best_f is None or score < best_f[0]:
                best_f = (score, fx)
        if best_f is not None:
            return best_f[1], best_f[0]
        return coarse, best_score
    except Exception:
        return None


async def _drag_handle(page, start_x: float, start_y: float, delta: int,
                       steps: int = 16) -> None:
    await page.mouse.move(start_x, start_y)
    await asyncio.sleep(random.uniform(0.06, 0.14))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.05, 0.10))
    for i in range(1, steps + 1):
        await page.mouse.move(
            start_x + delta * i / steps,
            start_y + random.uniform(-0.8, 0.8),
            steps=2,
        )
        await asyncio.sleep(random.uniform(0.004, 0.02))
    await asyncio.sleep(random.uniform(0.06, 0.14))
    await page.mouse.up()


async def _challenge_solved(page, iframe) -> bool:
    try:
        if await read_hcaptcha_token(page):
            return True
    except Exception:
        pass
    try:
        frame = await iframe.content_frame()
        if frame:
            hidden = await frame.evaluate("""() => {
                const chal = document.querySelector('[class*="challenge"], [class*="Challenge"]');
                if (!chal) return false;
                const cs = getComputedStyle(chal);
                return cs.display === 'none' || cs.visibility === 'hidden';
            }""")
            if hidden:
                return True
    except Exception:
        pass
    return False


async def _probe_drag_dom(iframe) -> dict:
    try:
        frame = await iframe.content_frame()
        if not frame:
            return {}
        handle = await frame.evaluate("""() => {
            const cands = [];
            const ch = window.innerHeight || 400;
            for (const el of document.querySelectorAll('*')) {
                if (el.children.length > 4) continue;
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                if (r.width < 18 || r.width > 700 || r.height < 12 || r.height > 200) continue;
                const isKnob = ['grab', 'grabbing', 'move', 'ew-resize', 'col-resize', 'pointer'].includes(cs.cursor) ||
                               el.getAttribute('role') === 'slider' ||
                               el.getAttribute('aria-valuenow') !== null;
                if (!isKnob) continue;
                cands.push({x: r.x + r.width / 2, y: r.y + r.height / 2, area: r.width * r.height, y0: r.y});
            }
            if (!cands.length) return null;
            const bottom = cands.filter(c => c.y0 > ch * 0.55);
            const pool = bottom.length ? bottom : cands;
            pool.sort((a, b) => b.y0 - a.y0 || a.area - b.area);
            return {x: pool[0].x, y: pool[0].y};
        }""")
        area = await frame.evaluate("""() => {
            let best = null, bestArea = 0;
            for (const el of document.querySelectorAll('canvas, img[src], [class*="puzzle" i], [class*="task-image" i], [style*="background-image"]')) {
                const r = el.getBoundingClientRect();
                const a = r.width * r.height;
                if (r.width >= 80 && r.height >= 80 && a > bestArea) {
                    bestArea = a;
                    best = {x: r.x, y: r.y, w: r.width, h: r.height};
                }
            }
            return best;
        }""")
        return {"handle": handle, "area": area}
    except Exception:
        return {}


async def solve_hcaptcha_drag(page, iframe, log=None,
                              max_attempts: int = 6) -> bool:
    """Solve the hCaptcha puzzle drag challenge directly in the browser.

    Screenshots the puzzle, finds the piece with edge analysis, template-matches
    its pixels to locate the hole, then drags the slider. Returns True only when
    hCaptcha actually accepts the solve.
    """
    log = log or (lambda msg, level="info": None)
    try:
        iframe_box = await iframe.bounding_box()
        if not iframe_box or iframe_box['width'] < 60 or iframe_box['height'] < 60:
            log("[Drag] Challenge iframe too small", level="error")
            return False

        probe = await _probe_drag_dom(iframe)
        area = probe.get("area")
        handle = probe.get("handle")
        if not handle and not area:
            return False
        shot_box = iframe_box
        if area and area.get("w", 0) >= 40:
            shot_box = {
                'x': iframe_box['x'] + area['x'],
                'y': iframe_box['y'] + area['y'],
                'width': area['w'],
                'height': area['h'],
            }
        if handle:
            hx = iframe_box['x'] + handle['x']
            hy = iframe_box['y'] + handle['y']
        else:
            hx = iframe_box['x'] + iframe_box['width'] * 0.85
            hy = iframe_box['y'] + iframe_box['height'] * 0.85

        try:
            dpr = float(await page.evaluate("() => window.devicePixelRatio || 1"))
        except Exception:
            dpr = 1.0
        if not dpr or dpr <= 0:
            dpr = 1.0

        for attempt in range(1, max_attempts + 1):
            shot = await page.screenshot(clip=shot_box)
            img = Image.open(io.BytesIO(shot))

            delta_img = None
            best_match = None
            for box in _find_piece_boxes(img):
                found = _template_match_hole(img, box)
                if not found:
                    continue
                hole_x, score = found
                piece_cx = box[0] + box[2] / 2
                d = int(round(hole_x - piece_cx))
                if best_match is None or score < best_match[0]:
                    best_match = (score, d)
            if best_match:
                delta_img = best_match[1]
                log(f"[Drag] Attempt {attempt}: template offset {delta_img:+d}px")
            if delta_img is None:
                deltas = _puzzle_deltas(img)
                if deltas:
                    delta_img = deltas[0]
                    log(f"[Drag] Attempt {attempt}: edge offset {delta_img:+d}px (candidates {deltas})")
            if delta_img is None:
                log(f"[Drag] Attempt {attempt}: no piece/hole detected", level="warn")
                await asyncio.sleep(1.2)
                continue

            delta = int(round(delta_img / dpr))
            for adjust in (0, -4, 4):
                d = delta + adjust
                if d == 0:
                    continue
                await _drag_handle(page, hx, hy, d)
                for _ in range(2):
                    await asyncio.sleep(1.0)
                    if await _challenge_solved(page, iframe):
                        log("[Drag] ✅ Puzzle solved!")
                        return True
            log(f"[Drag] Attempt {attempt} did not pass", level="warn")
            probe = await _probe_drag_dom(iframe)
            handle = probe.get("handle")
            if handle:
                hx = iframe_box['x'] + handle['x']
                hy = iframe_box['y'] + handle['y']
            await asyncio.sleep(0.8)

        log("[Drag] ❌ Could not solve after retries", level="error")
        return False
    except Exception as e:
        log(f"[Drag] solver error: {e}", level="error")
        return False


# ═══════════════════════════════════════════════════════════════
# Brain-Based hCaptcha Solver (curl_cffi API flow)
# ═══════════════════════════════════════════════════════════════

class HCaptchaSolver:
    """Universal hCaptcha solver using trained brains + direct API calls."""

    def __init__(self, sitekey: str, host: str, proxy: Optional[str] = None,
                 model_path: Optional[str] = None):
        self.sitekey = sitekey
        self.host = host.split("//")[-1].split("/")[0]
        self.proxy = proxy
        self.session = make_session(proxy)
        self.motion = MotionData()
        self.motion.host = self.host
        self.classifier = TileClassifier(model_path)

        resp = self.session.get("https://hcaptcha.com/1/api.js",
                                params={"render": "explicit"})
        versions = re.findall(r"v1/([A-Za-z0-9]+)/static", resp.text)
        self.version = versions[1] if len(versions) > 1 else DEFAULT_VERSION
        print(f"  hCaptcha v{self.version[:8]}...")

    def get_config(self) -> Optional[dict]:
        params = {
            "v": self.version, "sitekey": self.sitekey,
            "host": self.host, "sc": "1", "swa": "1", "spst": "1",
        }
        resp = self.session.post(f"{HCAPTCHA_API}/checksiteconfig", params=params)
        if resp.status_code != 200:
            return None
        return resp.json()

    async def fetch_challenge(self, config: dict,
                               hsw: HSWGenerator) -> Optional[dict]:
        req = config["c"]["req"]
        token = await hsw.generate(self.session, req)
        if not token:
            return None
        data = {
            "v": self.version, "sitekey": self.sitekey,
            "host": self.host, "hl": "en-US",
            "motionData": json.dumps(self.motion.get_captcha_motion()),
            "n": token, "c": json.dumps(config["c"]),
        }
        resp = self.session.post(
            f"{HCAPTCHA_API}/getcaptcha/{self.sitekey}", data=data)
        if resp.status_code != 200:
            return None
        return resp.json()

    def solve_tile_grid(self, challenge: dict) -> dict:
        tasklist = challenge.get("tasklist", [])
        question = challenge.get("requester_question", {}).get("en", "")

        target_class = None
        for cls in self.classifier.CLASSES:
            if cls in question.lower():
                target_class = cls
                break

        selected = {}
        for i, task in enumerate(tasklist):
            img_url = task.get("datapoint_uri")
            if not img_url:
                continue
            try:
                resp = self.session.get(img_url)
                img_bytes = resp.content
            except Exception:
                continue
            cls = self.classifier.classify(img_bytes)
            if target_class and cls == target_class:
                selected[task["task_key"]] = "true"
            elif not target_class:
                selected[task["task_key"]] = "true" if i < 2 else "false"
            else:
                selected[task["task_key"]] = "false"

        total = sum(1 for v in selected.values() if v == "true")
        print(f"  Target: {target_class or 'unknown'} → {total} tiles selected")
        return selected

    def solve_drag(self, challenge: dict) -> dict:
        tasklist = challenge.get("tasklist", [])
        if not tasklist:
            return {}
        main_task = tasklist[0]
        task_key = main_task.get("task_key")
        entities = main_task.get("entities", [])
        bg_url = main_task.get("datapoint_uri")
        answers = []
        for entity in entities:
            piece_url = entity.get("datapoint_uri")
            if not piece_url or not bg_url:
                continue
            try:
                pg_resp = self.session.get(piece_url)
                bg_resp = self.session.get(bg_url)
            except Exception:
                continue
            tx, ty, conf = solve_drag(pg_resp.content, bg_resp.content)
            print(f"  Drag: entity={entity.get('entity_id')} → ({tx},{ty}) conf={conf:.1f}%")
            answers.append({
                "entity_name": entity.get("entity_id"),
                "entity_type": "default",
                "entity_coords": [tx, ty],
            })
        return {task_key: answers}

    async def submit(self, challenge: dict, answers: dict,
                      hsw: HSWGenerator) -> Optional[dict]:
        req = challenge["c"]["req"]
        token = await hsw.generate(self.session, req)
        if not token:
            return None
        endpoint = f"{HCAPTCHA_API}/checkcaptcha/{self.sitekey}/{challenge['key']}"
        payload = json.dumps({
            "v": self.version, "sitekey": self.sitekey,
            "serverdomain": self.host,
            "job_mode": challenge["request_type"],
            "motionData": json.dumps(self.motion.get_check_motion()),
            "n": token, "c": json.dumps(challenge["c"]),
            "answers": answers,
        })
        headers = {
            "content-type": "application/json;charset=UTF-8",
            "accept": "*/*",
            "origin": "https://newassets.hcaptcha.com",
            "referer": "https://newassets.hcaptcha.com/",
        }
        resp = self.session.post(endpoint, data=payload, headers=headers)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("pass"):
                return {"success": True, "token": result.get("generated_pass_UUID")}
            if result.get("success") is False:
                return {"success": False, "error": result.get("error-codes", [])}
        return {"success": False, "error": f"HTTP {resp.status_code}"}

    async def solve(self, max_attempts: int = 10) -> dict:
        config = self.get_config()
        if not config:
            return {"success": False, "error": "Config failed"}
        if "c" not in config:
            return {"success": False, "error": "No config['c']"}

        hsw = HSWGenerator(self.sitekey, self.host, self.version, self.proxy)
        start = time.time()

        try:
            for attempt in range(1, max_attempts + 1):
                print(f"\n── Attempt {attempt}/{max_attempts} ──")

                challenge = await self.fetch_challenge(config, hsw)
                if not challenge:
                    config = self.get_config()
                    if not config:
                        continue
                    challenge = await self.fetch_challenge(config, hsw)
                    if not challenge:
                        continue

                if challenge.get("generated_pass_UUID"):
                    elapsed = time.time() - start
                    print(f"  ✅ Passive pass! ({elapsed:.1f}s)")
                    await hsw.close()
                    return {"success": True, "token": challenge["generated_pass_UUID"],
                            "time": elapsed}

                req_type = challenge.get("request_type", "unknown")
                print(f"  Type: {req_type}")

                if req_type == "image_label_binary":
                    answers = self.solve_tile_grid(challenge)
                elif req_type == "image_drag_drop":
                    answers = self.solve_drag(challenge)
                elif req_type == "image_label_area_select":
                    answers = {}
                    for task in challenge.get("tasklist", []):
                        answers[task["task_key"]] = [{
                            "entity_name": 0,
                            "entity_type": "default",
                            "entity_coords": [200, 150],
                        }]
                else:
                    print(f"  ⚠️  Unsupported: {req_type}")
                    continue

                result = await self.submit(challenge, answers, hsw)
                if result and result.get("success"):
                    elapsed = time.time() - start
                    print(f"  ✅ Solved! ({elapsed:.1f}s)")
                    await hsw.close()
                    return {"success": True, "token": result.get("token", ""),
                            "time": elapsed}
                error = result.get("error", "unknown") if result else "none"
                print(f"  ❌ Rejected: {error}")
                config = self.get_config()

            await hsw.close()
            return {"success": False, "error": f"Max {max_attempts} attempts",
                    "time": time.time() - start}
        except Exception as e:
            await hsw.close()
            return {"success": False, "error": str(e),
                    "time": time.time() - start}


# ═══════════════════════════════════════════════════════════════
# hCaptcha Accessibility Challenge Solver (Ollama vision)
# ═══════════════════════════════════════════════════════════════

def _eval_arithmetic_chain(expr: str) -> Optional[str]:
    """Evaluate a pure integer arithmetic chain ('5 + 8 + 7', '12 × 3 ÷ 2',
    '10 - 2 - 3') with standard operator precedence. Accepts ASCII and unicode
    math symbols (× ÷ − x X). Returns the answer as a string, or None when the
    expression is not a safe integer expression."""
    s = expr.replace(" ", "")
    s = s.replace("×", "*").replace("÷", "/").replace("−", "-")
    s = s.replace("x", "*").replace("X", "*")
    if not re.fullmatch(r"[0-9+\-*/]+", s):
        return None
    tokens = re.findall(r"\d+|[+\-*/]", s)
    if len(tokens) < 3 or len(tokens) % 2 == 0:
        return None
    nums: List[int] = []
    ops: List[str] = []
    for tok in tokens:
        if tok.isdigit():
            nums.append(int(tok))
        else:
            ops.append(tok)
    if len(nums) != len(ops) + 1:
        return None
    # pass 1: * and / (left to right)
    i = 0
    while i < len(ops):
        if ops[i] in ("*", "/"):
            a, b = nums[i], nums[i + 1]
            if ops[i] == "*":
                nums[i] = a * b
            else:
                if b == 0:
                    return None
                nums[i] = a // b if a % b == 0 else a / b
            del nums[i + 1]
            del ops[i]
        else:
            i += 1
    # pass 2: + and -
    val = nums[0]
    for op, b in zip(ops, nums[1:]):
        val = val + b if op == "+" else val - b
    if isinstance(val, float) and val.is_integer():
        val = int(val)
    return str(val)


async def _dump_clickables(page, frame, iframe_box, log):
    """Log EVERY clickable element found (iframe + page) with coordinates.
    Used for debugging — shows exactly what the bot sees and where."""
    try:
        page_scroll = await page.evaluate("() => ({x: window.scrollX || 0, y: window.scrollY || 0})")
        log(f"[Accessibility] Page scroll: ({page_scroll['x']}, {page_scroll['y']})")
    except Exception:
        page_scroll = {"x": 0, "y": 0}

    # Dump iframe clickables
    try:
        items = await frame.evaluate("""() => {
            const out = [];
            const els = document.querySelectorAll('button, [role="button"], a, [aria-label], [title], [class*="dot"], [class*="menu"]');
            for (const el of els) {
                if (el.offsetParent === null) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                const t = (el.textContent || '').trim().slice(0, 25);
                const label = el.getAttribute('aria-label') || el.getAttribute('title') || '';
                const cls = (el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '';
                out.push({
                    x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                    w: Math.round(r.width), h: Math.round(r.height),
                    tag: el.tagName, text: t.slice(0, 20), label: label.slice(0, 30),
                    cls: String(cls).slice(0, 40),
                });
            }
            return out;
        }""")
        log(f"[Accessibility] IFRAME clickables ({len(items or [])}):")
        for it in (items or [])[:25]:
            log(f"[Accessibility]   iframe ({it['x']},{it['y']}) {it['w']}x{it['h']} "
                f"<{it['tag']}> label='{it['label']}' text='{it['text']}' cls='{it['cls']}'")
    except Exception as e:
        log(f"[Accessibility] iframe dump error: {e}", level="warn")

    # Dump page clickables (inside the hcaptcha widget container)
    try:
        items = await page.evaluate("""() => {
            const out = [];
            const scope = document.querySelector('[class*="hcaptcha"], [id*="hcaptcha"], iframe[src*="hcaptcha"]')
                          ? document : document.body;
            const els = scope.querySelectorAll('button, [role="button"], a, [aria-label], [class*="dot"], [class*="menu"]');
            for (const el of els) {
                if (el.offsetParent === null) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                const t = (el.textContent || '').trim().slice(0, 25);
                const label = el.getAttribute('aria-label') || el.getAttribute('title') || '';
                const cls = (el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '';
                out.push({
                    x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                    w: Math.round(r.width), h: Math.round(r.height),
                    tag: el.tagName, text: t.slice(0, 20), label: label.slice(0, 30),
                    cls: String(cls).slice(0, 40),
                });
            }
            return out;
        }""")
        log(f"[Accessibility] PAGE clickables ({len(items or [])}):")
        for it in (items or [])[:25]:
            log(f"[Accessibility]   page ({it['x']},{it['y']}) {it['w']}x{it['h']} "
                f"<{it['tag']}> label='{it['label']}' text='{it['text']}' cls='{it['cls']}'")
    except Exception as e:
        log(f"[Accessibility] page dump error: {e}", level="warn")


async def _click_at(page, x, y, log, desc=""):
    """Real mouse click at scroll-aware page coordinates."""
    try:
        scroll = await page.evaluate("() => window.scrollY || 0")
    except Exception:
        scroll = 0
    log(f"[Accessibility] Clicking {desc} at ({x:.0f},{y:.0f}) (scrollY={scroll:.0f})")
    await page.mouse.click(x, y)


async def solve_hcaptcha_accessibility(page, iframe, 
                                        ollama_model: str = "",
                                        ollama_url: str = "",
                                        log: Optional[Callable] = None,
                                        max_attempts: int = 6,
                                        max_questions: int = 10) -> bool:
    """Solve hCaptcha via the Accessibility Challenge using Playwright's
    frame_locator for reliable cross-origin iframe interaction.

    Flow:
      1. Use frame_locator('iframe[title="hCaptcha challenge"]') for iframe.
      2. Click #menu-info (the 3-dots) inside the hCaptcha iframe.
      3. Select "Accessibility Challenge".
      4. Screenshot and send to Ollama vision model.
      5. Type answer and submit.

    Requirements:
      - Ollama running with a vision model (e.g. `ollama pull minicpm-v`)
      - Fast GPU recommended for fast inference
    """
    log = log or (lambda msg, level="info": None)

    # Ollama endpoint/model come from env vars so the bot can reach
    # a server that actually hosts a vision model (localhost:11434
    # only works when Ollama runs on the same machine as the bot).
    if not ollama_url:
        ollama_url = os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_BASE") or "http://localhost:11434"
    if not ollama_model:
        ollama_model = os.environ.get("OLLAMA_MODEL") or os.environ.get("OLLAMA_VISION_MODEL") or "minicpm-v"
    ollama_url = ollama_url.rstrip("/")
    log(f"[Accessibility] Ollama endpoint: {ollama_url}  model: {ollama_model}")
    import asyncio
    import base64

    async def _discover_vision_model() -> str:
        """Ask Ollama /api/tags which models are actually loaded and pick a
        vision-capable one. The configured model may not exist on the server,
        which silently breaks every vision solve."""
        try:
            import aiohttp
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(f"{ollama_url}/api/tags") as resp:
                    if resp.status != 200:
                        return ollama_model
                    data = await resp.json()
            names = [m.get("name", "") for m in data.get("models", [])]
            if not names:
                log("[Accessibility] [CRITICAL] Ollama endpoint has NO models pulled — "
                    "vision cannot solve image questions. Pull a vision model "
                    "e.g. `ollama pull llava:7b` (or moondream) on the server, or set "
                    "OLLAMA_URL to a server that has one.", level="error")
                return ollama_model
            # Prefer known vision models, else the first one that supports images
            preferred = ["llava", "moondream", "minicpm-v", "bakllava",
                         "llava-llama3", "llava:13b", "llava:7b", "llava:34b"]
            for p in preferred:
                for n in names:
                    if n.split(":")[0].lower() == p or n == p:
                        return n
            for n in names:
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as session:
                        async with session.get(f"{ollama_url}/api/show",
                                               params={"name": n}) as resp:
                            if resp.status == 200:
                                d = await resp.json()
                                if "vision" in str(d.get("model_info", {})).lower() or \
                                   "clip" in str(d.get("model_info", {})).lower():
                                    return n
                except Exception:
                    continue
            return names[0]
        except Exception as e:
            log(f"[Accessibility] Model discovery error: {e}", level="warn")
            return ollama_model

    discovered = await _discover_vision_model()
    if discovered and discovered != ollama_model:
        log(f"[Accessibility] Using available vision model: {discovered}")
        ollama_model = discovered
    log(f"[Accessibility] Vision model in use: {ollama_model}")

    # ── Helpers ────────────────────────────────────────────

    async def _ollama_chat(image_b64: str, prompt: str, timeout: float = 45.0) -> str:
        """Send image + prompt to Ollama /api/chat (vision models)."""
        try:
            import aiohttp
            payload = {
                "model": ollama_model,
                "stream": False,
                "messages": [{
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64],
                }],
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.post(
                    f"{ollama_url}/api/chat", json=payload
                ) as resp:
                    if resp.status != 200:
                        return ""
                    data = await resp.json()
                    return data["message"]["content"].strip()
        except Exception as e:
            log(f"[Accessibility] Ollama error: {e}", level="error")
            return ""

    async def _screenshot_b64(target, selector: str | None = None) -> str:
        """Capture a screenshot as base64 PNG."""
        if selector:
            try:
                el = await target.locator(selector).first.element_handle(timeout=4000)
                if el:
                    img = await el.screenshot(type="png")
                    return base64.b64encode(img).decode()
            except Exception:
                pass
        img = await target.screenshot(type="png")
        return base64.b64encode(img).decode()

    async def _screenshot_question(hcaptcha) -> str:
        """Screenshot just the question area (bigger = better OCR).
        Tries selectors inside the frame, then falls back to full frame."""
        for sel in (
            '#prompt-text', '.challenge-prompt', '[class*="prompt"]',
            '[class*="challenge"] [class*="text"]', '[class*="question"]',
            '[class*="task"]', '[class*="instruction"]', '[class*="challenge-container"]',
        ):
            try:
                el = await hcaptcha.locator(sel).first.element_handle(timeout=1500)
                if el:
                    img = await el.screenshot(type="png")
                    b64 = base64.b64encode(img).decode()
                    if b64:
                        return b64
            except Exception:
                continue
        return await _screenshot_b64(hcaptcha)

    async def _token_present() -> bool:
        try:
            tok = await page.evaluate(
                """() => {
                    const ta = document.querySelector('textarea[name="h-captcha-response"]');
                    return !!(ta && ta.value && ta.value.length > 20);
                }"""
            )
            return bool(tok)
        except Exception:
            return False

    async def _challenge_js(js: str, arg=None):
        """Run JS in hCaptcha frames first, then every other frame."""
        try:
            for f in page.frames:
                try:
                    if f.url and "hcaptcha" in f.url.lower():
                        res = await f.evaluate(js, arg)
                        if res is not None and res != "":
                            return res
                except Exception:
                    continue
            for f in page.frames:
                try:
                    res = await f.evaluate(js, arg)
                    if res is not None and res != "":
                        return res
                except Exception:
                    continue
        except Exception:
            pass
        return None

    async def _accessibility_active(hcaptcha) -> bool:
        """True when the accessibility challenge UI is actually visible.
        Uses progressively broader selectors — from tight input selectors
        to container/text-based detection — to catch all hCaptcha
        accessibility variants (text input, start screen, cookie prompt).
        No page-level JS fallback (hidden hCaptcha token inputs false-match)."""
        # ── Tier 1: Direct input selectors (most specific) ──
        tier1 = [
            'input[type="text"]',
            'input[type="number"]',
            'textarea',
            '[role="textbox"]',
        ]
        for sel in tier1:
            try:
                await hcaptcha.locator(sel).first.wait_for(
                    state="visible", timeout=1000
                )
                return True
            except Exception:
                continue

        # ── Tier 2: Accessibility-specific containers & start elements ──
        tier2 = [
            '[class*="accessibility"]',
            '[class*="challenge-container"]',
            '#prompt-text',
            '[class*="prompt"]',
            'h2:has-text("Accessibility")',
            'button:has-text("Set Accessibility Cookie")',
            'button:has-text("Start")',
            '[class*="challenge-text"]',
            '[class*="task-text"]',
            '[class*="instruction"]',
            '[class*="question"]',
        ]
        for sel in tier2:
            try:
                await hcaptcha.locator(sel).first.wait_for(
                    state="visible", timeout=800
                )
                return True
            except Exception:
                continue

        # ── Tier 3: JS-based text content scan inside the hCaptcha frame ──
        # Look for any visible element containing accessibility-challenge
        # text patterns, even if the markup uses unexpected classes.
        try:
            result = await _challenge_js("""() => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT
                );
                let node;
                while ((node = walker.nextNode())) {
                    if (node.offsetParent === null) continue;
                    const t = (node.textContent || '').trim();
                    if (t.length < 3 || t.length > 200) continue;
                    if (/how many|jar|coins|add|put|total|remove|first|last|
                         letter|reverse|word|type|number|accessibility|
                         challenge|question|answer|submit/i.test(t)) {
                        return 'found';
                    }
                }
                return null;
            }""")
            if result:
                return True
        except Exception:
            pass

        return False

    async def _menu_visible(hcaptcha) -> bool:
        """True when the dropdown menu is open — has visible menu/listbox."""
        try:
            result = await _challenge_js("""() => {
                const els = document.querySelectorAll('[role="menu"], [role="listbox"], .menu, .dropdown, [class*="menu"]');
                for (const el of els) {
                    if (el.offsetParent !== null && el.children.length > 0) {
                        return 'menu_open';
                    }
                }
                return null;
            }""")
            return bool(result)
        except Exception:
            return False

    async def _click_three_dots(hcaptcha) -> bool:
        """Click the 3-dots menu button — tries 4 methods in rapid sequence:
        WAY 1: JS evaluate finds & clicks the button (most direct, bypasses intercept).
        WAY 2: Playwright aria-label / role-based click.
        WAY 3: CSS selector #menu-info + force-click.
        WAY 4: Dispatch click event as last resort.
        Only waits 1.2s between attempts; the widget has already loaded."""

        # ── WAY 1: JS evaluation (most reliable — bypasses intercept layers) ──
        try:
            js_result = await _challenge_js("""() => {
                const btn = document.querySelector('#menu-info')
                         || document.querySelector('[aria-label*="About hCaptcha"]')
                         || document.querySelector('[aria-label*="Extra menu"]')
                         || document.querySelector('.display-menu-btn');
                if (btn && btn.offsetParent !== null) {
                    btn.scrollIntoView({block: 'center'});
                    btn.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                    btn.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
                    btn.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                    return 'js_click_ok';
                }
                return null;
            }""")
            if js_result:
                log("[Accessibility] Clicked 3-dots via JS (way 1)")
                await asyncio.sleep(0.5)
                return True
        except Exception as e:
            log(f"[Accessibility] way 1 (JS) failed: {str(e)[:80]}", level="warn")

        await asyncio.sleep(1.2)

        # ── WAY 2: Playwright aria-label / role click ──
        try:
            for label in ("About hCaptcha & Accessibility Options",
                          "Extra menu", "More options", "Menu"):
                try:
                    btn = hcaptcha.get_by_role("button", name=label).first
                    await btn.wait_for(state="visible", timeout=3000)
                    await btn.click(timeout=2000)
                    log(f"[Accessibility] Clicked 3-dots via role '{label}' (way 2)")
                    await asyncio.sleep(0.5)
                    return True
                except Exception:
                    continue
            # generic label fallback
            btn = hcaptcha.get_by_label("Extra menu").first
            await btn.wait_for(state="visible", timeout=2000)
            await btn.click(timeout=2000)
            log("[Accessibility] Clicked 3-dots via aria-label (way 2)")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            log(f"[Accessibility] way 2 (aria-label) failed: {str(e)[:80]}", level="warn")

        await asyncio.sleep(1.2)

        # ── WAY 3: CSS selector + force-click ──
        try:
            btn = hcaptcha.locator("#menu-info").first
            await btn.wait_for(state="visible", timeout=3000)
            await btn.click(force=True, timeout=3000)
            log("[Accessibility] Force-clicked #menu-info (way 3)")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            log(f"[Accessibility] way 3 (force-click) failed: {str(e)[:80]}", level="warn")

        await asyncio.sleep(1.2)

        # ── WAY 4: Dispatch event on any matching element ──
        try:
            await hcaptcha.locator("#menu-info").first.dispatch_event("click")
            log("[Accessibility] Dispatched click event (way 4)")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            log(f"[Accessibility] way 4 (dispatch) failed: {str(e)[:80]}", level="warn")

        return False

    async def _click_accessibility_option(hcaptcha) -> bool:
        """Select 'Accessibility Challenge' from the already-open menu.
        Menu items in hCaptcha have child spans — don't skip containers.
        Just find any visible element with short text matching the label."""
        deadline = time.time() + 8
        poll = 0
        while time.time() < deadline:
            poll += 1
            # JS: find ANY visible element whose trimmed text is the menu label.
            # hCaptcha menu items look like <div role="menuitem"><span>The Label</span></div>
            # so textContent works but children.length > 0 would wrongly skip them.
            try:
                clicked = await _challenge_js("""() => {
                    // Match any element with short text matching the label
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (!t || t.length > 60 || t.length < 8) continue;
                        if (/^Accessibility Challenge$/i.test(t)) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return t;
                        }
                    }
                    // Fallback: partial match on short text
                    for (const el of all) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (t.length > 60 || t.length < 5) continue;
                        if (/accessibility.*challenge/i.test(t)) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return t;
                        }
                    }
                    return null;
                }""")
                if clicked:
                    log(f"[Accessibility] JS-clicked menu item: '{clicked}' (poll {poll})")
                    return True
            except Exception:
                pass
            # Playwright fallback: text match
            try:
                loc = hcaptcha.get_by_text("Accessibility Challenge", exact=False).first
                await loc.wait_for(state="visible", timeout=1500)
                await loc.click(timeout=2000)
                log(f"[Accessibility] Clicked via text locator (poll {poll})")
                return True
            except Exception:
                pass
            # Playwright fallback: role match
            for role in ("link", "button", "menuitem"):
                try:
                    loc = hcaptcha.get_by_role(role, name="Accessibility Challenge").first
                    await loc.wait_for(state="visible", timeout=1500)
                    await loc.click(timeout=2000)
                    log(f"[Accessibility] Clicked via role={role} (poll {poll})")
                    return True
                except Exception:
                    pass
            await asyncio.sleep(0.7)
        return False

    async def _open_accessibility_challenge(hcaptcha) -> bool:
        """Open accessibility challenge with detection at each step.
        Click one 3-dots method → wait 5s for menu → click option → detect input.
        If any step fails, retry with next method."""

        # ── Step A: Click 3-dots (one method at a time, detect menu open) ──
        menu_opened = False

        # WAY 1: JS click
        try:
            js_result = await _challenge_js("""() => {
                const btn = document.querySelector('#menu-info')
                         || document.querySelector('[aria-label*="About hCaptcha"]')
                         || document.querySelector('[aria-label*="Extra menu"]')
                         || document.querySelector('.display-menu-btn');
                if (btn && btn.offsetParent !== null) {
                    btn.scrollIntoView({block: 'center'});
                    btn.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                    btn.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
                    btn.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                    return 'ok';
                }
                return null;
            }""")
            if js_result:
                log("[Accessibility] Clicked 3-dots via JS (way 1)")
                for _ in range(10):  # 5 seconds
                    if await _menu_visible(hcaptcha):
                        menu_opened = True
                        log("[Accessibility] Menu opened (way 1)")
                        break
                    await asyncio.sleep(0.5)
        except Exception as e:
            log(f"[Accessibility] way 1 (JS) failed: {str(e)[:60]}", level="warn")

        if not menu_opened:
            # WAY 2: Playwright role click
            try:
                for label in ("About hCaptcha & Accessibility Options", "Extra menu", "More options", "Menu"):
                    try:
                        btn = hcaptcha.get_by_role("button", name=label).first
                        await btn.wait_for(state="visible", timeout=3000)
                        await btn.click(timeout=2000)
                        log(f"[Accessibility] Clicked 3-dots via role '{label}' (way 2)")
                        for _ in range(10):
                            if await _menu_visible(hcaptcha):
                                menu_opened = True
                                break
                            await asyncio.sleep(0.5)
                        if menu_opened:
                            break
                    except Exception:
                        continue
            except Exception as e:
                log(f"[Accessibility] way 2 (role) failed: {str(e)[:60]}", level="warn")

        if not menu_opened:
            # WAY 3: CSS force-click
            try:
                btn = hcaptcha.locator("#menu-info").first
                await btn.wait_for(state="visible", timeout=3000)
                await btn.click(force=True, timeout=3000)
                log("[Accessibility] Force-clicked #menu-info (way 3)")
                for _ in range(10):
                    if await _menu_visible(hcaptcha):
                        menu_opened = True
                        break
                    await asyncio.sleep(0.5)
            except Exception as e:
                log(f"[Accessibility] way 3 (force) failed: {str(e)[:60]}", level="warn")

        if not menu_opened:
            # WAY 4: dispatch event
            try:
                await hcaptcha.locator("#menu-info").first.dispatch_event("click")
                log("[Accessibility] Dispatched click (way 4)")
                for _ in range(10):
                    if await _menu_visible(hcaptcha):
                        menu_opened = True
                        break
                    await asyncio.sleep(0.5)
            except Exception as e:
                log(f"[Accessibility] way 4 (dispatch) failed: {str(e)[:60]}", level="warn")

        if not menu_opened:
            log("[Accessibility] Menu never opened after all 4 methods", level="warn")
            return False

        # ── Step B: Menu is open — click Accessibility Challenge option ──
        await asyncio.sleep(0.5)  # let animation finish
        clicked_opt = await _click_accessibility_option(hcaptcha)
        if not clicked_opt:
            log("[Accessibility] Could not click accessibility option", level="warn")
            return False

        # ── Step C: Wait for the challenge to render, then return ──
        # No detection — just wait. The caller will screenshot + AI solve.
        log("[Accessibility] Accessibility option clicked — waiting 10s for challenge to load")
        await asyncio.sleep(10)
        log("[Accessibility] Challenge wait complete — proceeding to screenshot + AI solve")
        return True

    async def _read_question_text() -> str:
        """EXTREME search: scan page.innerText, EVERY frame innerText,
        and the hCaptcha frame body for question text patterns."""
        all_texts = []

        # Method 0: JS scan for img[alt] and aria-* — accessibility challenges
        # render the question as an <img alt="You have a jar with 9 coins...">
        # which inner_text() does NOT capture!
        async def _scan_js(source, js_func):
            try:
                val = await js_func()
                if val and str(val).strip() and len(str(val).strip()) > 3:
                    return str(val).strip()
            except Exception:
                pass
            return None

        # JS that captures alt/aria text (the actual question for accessibility)
        aria_js = """() => {
            const parts = [];
            for (const el of document.querySelectorAll('img[alt], [aria-label], [aria-describedby]')) {
                if (el.offsetParent === null && el.tagName !== 'IMG') continue;
                const t = (el.getAttribute('alt') || el.getAttribute('aria-label') || '').trim();
                if (t && t.length > 8 && t.length < 600) parts.push(t);
            }
            // Also grab ALL visible text (includes headings, paragraphs)
            const bodyText = document.body ? (document.body.innerText || '') : '';
            if (bodyText.trim()) parts.push(bodyText.trim());
            return parts.join(' | ');
        }"""

        # Run aria scan on the hCaptcha frame first
        for frame_source in [lambda: hcaptcha.locator("body").evaluate(aria_js),
                             lambda: page.evaluate(aria_js)]:
            val = await _scan_js("aria-js", frame_source)
            if val:
                all_texts.insert(0, ("aria-alt", val))
                break

        # Method 1: hCaptcha frame body innerText
        try:
            t = await hcaptcha.locator("body").inner_text()
            if t and len(t.strip()) > 5:
                all_texts.append(("hcaptcha-body", t.strip()))
        except Exception:
            pass

        # Method 2: page.evaluate document.body.innerText
        try:
            t = await page.evaluate('() => document.body ? document.body.innerText : ""')
            if t and len(t.strip()) > 5:
                all_texts.append(("page-body", t.strip()))
        except Exception:
            pass

        # Method 3: iterate ALL frames and read innerText
        try:
            for i, frame in enumerate(page.frames):
                try:
                    t = await frame.evaluate('() => document.body ? document.body.innerText : ""')
                    if t and len(t.strip()) > 5:
                        all_texts.append((f"frame-{i}", t.strip()))
                except Exception:
                    continue
        except Exception:
            pass

        # Method 4: page.locator("body").inner_text()
        try:
            t = await page.locator("body").inner_text()
            if t and len(t.strip()) > 5:
                all_texts.append(("page-locator", t.strip()))
        except Exception:
            pass

        # ── Now scan all collected texts for question patterns ──
        # Priority 1: lines with digits AND jar/coins/add/put/remove/first/last
        for source, text in all_texts:
            lines = text.split(chr(10))
            for line in lines:
                line = line.strip()
                if len(line) < 8 or len(line) > 500:
                    continue
                # Coin/jar math
                if re.search(r'\d', line) and re.search(
                    r'jar|coins?|add|put|total|how many|altogether|start|has',
                    line, re.IGNORECASE
                ):
                    log(f"[Accessibility] Found question in {source}: '{line[:120]}'")
                    return line
                # Word puzzle
                if re.search(r'\bremove\b|\bdrop\b|\bdelete\b|\bstrip\b|\bfirst\b|\blast\b|\bletter\b|\breverse\b|\bbackwards\b|\bword\b',
                             line, re.IGNORECASE):
                    log(f"[Accessibility] Found question in {source}: '{line[:120]}'")
                    return line

        # Priority 2: any line with question keywords
        for source, text in all_texts:
            lines = text.split(chr(10))
            for line in lines:
                line = line.strip()
                if len(line) < 8 or len(line) > 500:
                    continue
                if re.search(r'jar|coins?|how many|add|put|remove|first|last|letter|reverse|backwards|number|type|word',
                             line, re.IGNORECASE):
                    log(f"[Accessibility] Found (loose) in {source}: '{line[:120]}'")
                    return line

        # Priority 3: just return the concatenated text from all sources
        for source, text in all_texts:
            if len(text) > 10:
                log(f"[Accessibility] No pattern match — returning raw text from {source}")
                return text[:500]

        return ''

    def _find_target_word(text: str) -> Optional[str]:
        """Given a word puzzle question, extract the target word.
        Looks for quoted words, ALL-CAPS, or the longest word in the question."""
        # Strategy 1: Word in quotes
        m = re.search('["“”](\w{3,})["“”]', text)
        if m:
            return m.group(1)
        # Strategy 2: Word after "from the word X" / "the word X" / "word X" / "word is X"
        m = re.search(r'(?:from\s+)?(?:the\s+)?word\s+(?:is\s+|:\s+|of\s+|mayor\s*)?(\w{3,})',
                      text, re.IGNORECASE)
        if m:
            candidate = m.group(1)
            # 'of' could be caught as the word if the "word of" form is used;
            # only accept real content words
            if candidate.lower() not in ('of', 'the', 'and', 'is', 'a', 'an'):
                return candidate
        # Strategy 3: ALL-CAPS word (often the target in these puzzles)
        caps = re.findall(r'\b([A-Z]{3,})\b', text)
        if caps:
            return caps[0]
        # Strategy 4: Find the longest word (likely the target)
        words = re.findall(r'\b([a-zA-Z]{3,})\b', text)
        if words:
            # Filter out common question words
            skip = {'what','the','and','remove','first','last','letter','write',
                    'backwards','reverse','type','this','that','with','your'}
            candidates = [w for w in words if w.lower() not in skip]
            if candidates:
                return max(candidates, key=len)
        return None

    def _solve_text_question(text: str) -> Optional[str]:
        """Try to answer a text question locally without Ollama.
        Handles: math chains (5+8+7), word puzzles (remove first/last letter
        and reverse), simple arithmetic, etc."""
        t = text.strip().lower()
        orig = text.strip()

        # ── COIN / JAR word problems: sum all numbers ──
        # "Your jar has 3 coins. On Sunday, you add 6 coins. Then on Saturday,
        #  you add 8 coins. How many coins are there?" → 3+6+8 = 17
        # Repeats DO count: "put in 5... put in 5" = +5 twice → 9+5+5=19.
        coin_jar = re.search(
            r'(?:jar|coins?|add|put|total|altogether|in\s+all)',
            t, re.IGNORECASE
        )
        if coin_jar:
            # Extract ALL numbers from the question and sum them.
            # Repeats DO count: "put in 5... put in 5" means +5 twice → 9+5+5=19.
            nums = re.findall(r'\b(\d+)\b', orig)
            if len(nums) >= 2:
                total = sum(int(n) for n in nums)
                log(f"[Accessibility] Coin/jar sum: {'+'.join(nums)} = {total}")
                return str(total)

        # ── MATH: robust chain detection ──
        # Match expressions like "5 + 8 + 7", "12 × 3 ÷ 2", "10 - 2 - 3"
        math_re = re.compile(
            r'(?:(?:what\s+is|calculate|compute|solve|evaluate|find)[:\s]*)?'
            r'(-?\d+)\s*([+\-×xX*÷/])\s*(-?\d+)(\s*([+\-×xX*÷/])\s*(-?\d+))*',
            re.IGNORECASE
        )
        m = math_re.search(orig)
        if m:
            # Reconstruct the full chain from the match
            full_expr = m.group(0)
            # Clean up any leading text like "what is "
            full_expr = re.sub(r'^[a-z\s]+[:\s]*', '', full_expr, flags=re.IGNORECASE)
            ans = _eval_arithmetic_chain(full_expr)
            if ans is not None:
                log(f"[Accessibility] Math chain: '{full_expr}' = {ans}")
                return ans

        # ── WORD PUZZLES: "remove/drop first and last letter, write backwards" ──
        # Loose detection: any of remove/drop/delete/strip/take + first + last
        # + letter/character. The "write it backwards/reverse" tail varies, so
        # it is optional. Target word = LAST word of the question sentence.
        word_pat = re.compile(
            r'(?:remov(?:e|es|ed|ing)?|delet(?:e|es|ed|ing)?|drop|strip|take)\s+(?:out\s+)?(?:the\s+)?'
            r'(?:first|1st|first\s+letter)\s+(?:and|&)\s+(?:the\s+)?'
            r'(?:last|last\s+letter)\s+(?:letter|character|char|letters|characters)?s?',
            re.IGNORECASE
        )
        if word_pat.search(orig) or (re.search(r'\bremove\b', t)
                                     and re.search(r'\bfirst\b', t)
                                     and re.search(r'\blast\b', t)
                                     and re.search(r'\bletter', t)):
            # Strategy A: quoted / "of the word X" / "word is X" / ALL-CAPS
            # (most reliable — hCaptcha always names the word explicitly)
            word = _find_target_word(orig)
            # Strategy B: LAST word of the sentence as fallback
            if not word or len(word) <= 2:
                words = re.findall(r'[A-Za-z]{2,}', orig)
                if words:
                    skip_tail = {'backwards', 'backward', 'reverse', 'reversed',
                                 'direction', 'remaining', 'them', 'it', 'the',
                                 'and', 'write', 'spell', 'type', 'put', 'word',
                                 'letter', 'letters', 'order', 'answer', 'please',
                                 'from', 'with', 'into', 'your', 'in'}
                    for w in reversed(words):
                        if w.lower() in skip_tail:
                            continue
                        word = w
                        break
            if word and len(word) > 2:
                # Remove first and last letter, then reverse
                result = word[1:-1][::-1]
                if result:
                    log(f"[Accessibility] Word puzzle: '{word}' -> remove '{word[0]}'+'{word[-1]}' -> '{word[1:-1]}' -> reverse -> '{result}'")
                    return result

        # ── SIMPLE ARITHMETIC (single operation, e.g. "3 + 5") ──
        simple_pat = re.compile(r'(-?\d+)\s*([+\-×xX*÷/])\s*(-?\d+)')
        sm = simple_pat.search(t)
        if sm:
            ans = _eval_arithmetic_chain(sm.group(0))
            if ans is not None:
                log(f"[Accessibility] Simple math: '{sm.group(0)}' = {ans}")
                return ans

        # ── PURE NUMBER extraction (e.g. "type the number 42") ──
        num_pat = re.search(r'(?:number|digit|num)\s+[iof]*\s*(\d+)', t)
        if num_pat:
            return num_pat.group(1)

        # ── Just a number? ──
        lone_num = re.search(r'^\s*(\d+)\s*$', t)
        if lone_num:
            return lone_num.group(1)

        return None

    async def _get_answer(hcaptcha, q: int) -> Optional[str]:
        """Get the answer: EXTREME DOM text scan -> local solver.
        No Ollama. No screenshots. Just read text and do math."""
        text = await _read_question_text()
        log(f"[Accessibility] Q{q} text: '{text[:200]}'")
        if text:
            local = _solve_text_question(text)
            if local is not None:
                log(f"[Accessibility] Q{q} solved: {local}")
                return local
            log(f"[Accessibility] Q{q} local solver returned None", level="warn")
        else:
            log(f"[Accessibility] Q{q} NO TEXT FOUND anywhere", level="error")
        return None

    async def _type_answer(hcaptcha, answer: str) -> bool:
        # Try 0: Accessible role-based locator (most reliable)
        try:
            inp = hcaptcha.get_by_role("textbox", name="Challenge Text Input").first
            await inp.wait_for(state="visible", timeout=3000)
            await inp.click()
            await inp.fill("")
            await inp.type(answer, delay=30)
            log(f"[Accessibility] Typed '{answer}' via get_by_role textbox")
            return True
        except Exception:
            pass
        # Try 1: Locate input field by selector (aria + fallbacks)
        for inp_sel in [
            'input[aria-label="Challenge Text Input"]',
            '[role="textbox"][name="Challenge Text Input"]',
            'input[name="captcha"]',
            'input[type="text"]', 'input[type="number"]',
            'input:not([type="hidden"])', 'textarea',
            '[role="textbox"]', '[contenteditable="true"]',
        ]:
            try:
                inp = hcaptcha.locator(inp_sel).first
                await inp.wait_for(state="visible", timeout=3000)
                await inp.click()
                await inp.fill("")
                await inp.type(answer, delay=30)
                log(f"[Accessibility] Typed '{answer}' into {inp_sel}")
                return True
            except Exception:
                continue
        # Try 2: Click center of frame to focus, then type
        try:
            box = await hcaptcha.locator("body").first.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await page.mouse.click(cx, cy)
                await asyncio.sleep(0.3)
                await page.keyboard.type(answer, delay=50)
                log(f"[Accessibility] Typed '{answer}' via page keyboard")
                return True
        except Exception:
            pass
        # Try 3: JS focus + keyboard
        try:
            await _challenge_js('''() => {
                const inp = document.querySelector('input:not([type="hidden"]), textarea, [role="textbox"]');
                if (inp) { inp.focus(); inp.value = ''; return 'ok'; }
                return null;
            }''')
            await asyncio.sleep(0.3)
            await page.keyboard.type(answer, delay=50)
            log("[Accessibility] Typed via JS focus + keyboard")
            return True
        except Exception:
            pass
        return False

    async def _submit_answer(hcaptcha) -> bool:
        # The Next button is the primary submit for accessibility challenges.
        # WAIT 3 seconds before clicking — Skip sits at the same coordinates
        # as Next and would be clicked instead if we act too fast.
        log("[Accessibility] Waiting 3s before clicking Next (avoid Skip)")
        await asyncio.sleep(3)
        # Try accessible role-based locator: "Next" first, then "Submit"
        for name in ("Next", "Submit", "Verify", "Continue", "OK"):
            try:
                btn = hcaptcha.get_by_role("button", name=name).first
                await btn.wait_for(state="visible", timeout=3000)
                await btn.click(timeout=3000)
                log(f"[Accessibility] Submitted via get_by_role button {name}")
                return True
            except Exception:
                continue
        # Fallback: selector-based submit buttons
        for btn_sel in [
            'button[type="submit"]',
            'button:has-text("Next")',
            'button:has-text("Submit")',
            'button:has-text("Verify")',
            'button:has-text("OK")',
            'button:has-text("Continue")',
            '#submit',
        ]:
            try:
                await hcaptcha.locator(btn_sel).first.click(timeout=3000)
                log(f"[Accessibility] Submitted via {btn_sel}")
                return True
            except Exception:
                pass
        try:
            await hcaptcha.locator("input").first.press("Enter", timeout=2000)
            log("[Accessibility] Submitted via Enter")
            return True
        except Exception:
            return False

    # ── Main flow ─────────────────────────────────────────

    try:
        # ── Step 1: Locate the hCaptcha challenge iframe via frame_locator ──
        # Use the passed iframe element to build a reliable frame_locator, or
        # fall back to searching the page for the challenge iframe.
        hcaptcha = None
        if iframe is not None:
            # Derive frame_locator from the iframe element that server.py found
            try:
                frame_url = await iframe.get_attribute("src")
                if frame_url and "hcaptcha.com" in frame_url:
                    hcaptcha = page.frame_locator(f'iframe[src="{frame_url}"]')
                    log("[Accessibility] Using passed iframe for frame_locator")
            except Exception:
                pass
        if hcaptcha is None:
            HCAPTCHA_FRAME = 'iframe[title="hCaptcha challenge"]'
            hcaptcha = page.frame_locator(HCAPTCHA_FRAME)

        # Verify the iframe body is attached (rendered)
        try:
            await hcaptcha.locator("body").first.wait_for(
                state="attached", timeout=15000
            )
            log("[Accessibility] hCaptcha challenge iframe located via frame_locator")
        except Exception:
            log("[Accessibility] hCaptcha challenge iframe not found via frame_locator — "
                "trying fallback selectors", level="warn")
            # Fallback: try other iframe selectors
            for fallback_sel in [
                'iframe[src*="hcaptcha.com/captcha"]',
                'iframe[src*="hcaptcha.com"]',
                'iframe[title*="hCaptcha"]',
            ]:
                try:
                    hcaptcha = page.frame_locator(fallback_sel)
                    await hcaptcha.locator("body").first.wait_for(
                        state="attached", timeout=5000
                    )
                    log(f"[Accessibility] Found via fallback: {fallback_sel}")
                    break
                except Exception:
                    continue
            else:
                log("[Accessibility] Cannot locate any hCaptcha iframe — aborting",
                    level="error")
                return False

        for attempt in range(1, max_attempts + 1):
            log(f"[Accessibility] Attempt {attempt}/{max_attempts}")

            if await _token_present():
                log("[Accessibility] [OK] Already solved — token present!")
                return True

            # Always open the accessibility challenge via the menu.
            # NOTE: removed the "already active" shortcut because
            # _accessibility_active was false-matching hidden inputs in
            # the hCaptcha token field. Always clicking 3-dots is safer.
            if not await _open_accessibility_challenge(hcaptcha):
                log("[Accessibility] Could not open accessibility challenge",
                    level="warn")
                await asyncio.sleep(1.5)
                continue

            # ── Answer every question hCaptcha asks, one after another ──
            for q in range(1, max_questions + 1):
                if await _token_present():
                    log("[Accessibility] [OK] hCaptcha passed!")
                    return True

                answer = await _get_answer(hcaptcha, q)
                if answer is None:
                    log("[Accessibility] No answer this round", level="warn")
                    break

                if not await _type_answer(hcaptcha, answer):
                    log("[Accessibility] Could not type answer", level="warn")
                    break

                await asyncio.sleep(0.8)

                if not await _submit_answer(hcaptcha):
                    log("[Accessibility] Could not submit", level="warn")
                    break

                # Wait for token (passed) or assume next question
                outcome = None
                for _ in range(12):
                    await asyncio.sleep(0.75)
                    if await _token_present():
                        outcome = "passed"
                        break
                if outcome == "passed":
                    log("[Accessibility] [OK] hCaptcha passed!")
                    return True
                await asyncio.sleep(1.0)
                if await _token_present():
                    log("[Accessibility] [OK] hCaptcha passed!")
                    return True
                log(f"[Accessibility] No token yet — continuing to Q{q + 1}")

            log(f"[Accessibility] Attempt {attempt} did not solve — retrying",
                level="warn")
            await asyncio.sleep(2.0)

        log("[Accessibility] [FAIL] Could not solve after all attempts", level="error")
        return False

    except Exception as e:
        log(f"[Accessibility] Fatal error: {e}", level="error")
        import traceback
        traceback.print_exc()
        return False

# Backward-compat: NoCaptchaAI class wrapping the brain solver
# (app.py / server.py still import this)
# ═══════════════════════════════════════════════════════════════

class NoCaptchaAI:
    """Drop-in replacement for the old NoCaptchaAI API client.
    Now uses the trained brains + curl_cffi API flow instead of paid tokens."""

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self.stats = {"calls": 0, "ok": 0, "failed": 0}

    @property
    def configured(self) -> bool:
        return True  # always ready — no API key needed

    async def solve_hcaptcha(self, sitekey: str, pageurl: str,
                             timeout: float = 85.0, poll: float = 1.0,
                             rqdata: Optional[str] = None) -> Optional[str]:
        """Solve hCaptcha using the brain-based solver. Returns token or None."""
        self.stats["calls"] += 1
        self._log(f"[Solver] hCaptcha (sitekey {sitekey[:12]}...)")
        host = pageurl
        try:
            parsed = __import__("urllib.parse", fromlist=[""]).urlparse(pageurl)
            host = parsed.netloc or parsed.path or pageurl
        except Exception:
            pass
        solver = HCaptchaSolver(sitekey=sitekey, host=host)
        result = await solver.solve()
        if result.get("success"):
            self.stats["ok"] += 1
            token = result.get("token", "")
            self._log(f"[Solver] [OK] Token after {result.get('time', 0):.0f}s")
            return token
        self.stats["failed"] += 1
        self._log(f"[Solver] Failed: {result.get('error')}", level="warn")
        return None

    async def get_balance(self) -> Optional[dict]:
        return {"balance": 0.0, "currency": "USD", "free": True}


async def extract_hcaptcha_rqdata(page) -> str:
    """Pull the hCaptcha Enterprise rqdata from the page (best effort).
    Still exported for backward compat — brain solver doesn't need it."""
    try:
        val = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            if (el) {
                const v = el.getAttribute('data-rqdata') || el.getAttribute('rqdata');
                if (v && v.length > 8) return v;
            }
            for (const s of document.querySelectorAll('script')) {
                const t = s.textContent || '';
                const m = t.match(/"rqdata"\\s*:\\s*"([^"]{8,})"/) ||
                          t.match(/'rqdata'\\s*:\\s*'([^']{8,})'/) ||
                          t.match(/rqdata\\s*[:=]\\s*["']([^"']{8,})["']/);
                if (m) return m[1];
            }
            return '';
        }""")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="hCaptcha Universal Solver")
    parser.add_argument("--sitekey", default="a9b5fb07-92ff-493f-86fe-352a2803b3df",
                        help="hCaptcha sitekey (default: Discord)")
    parser.add_argument("--host", default="discord.com", help="Target host")
    parser.add_argument("--proxy", help="HTTP proxy URL")
    parser.add_argument("--model", default=None,
                        help="Path to trained model (default: models/model_grid.pth)")
    args = parser.parse_args()

    print("═" * 50)
    print("  hCaptcha Universal Solver — Free Edition")
    print(f"  Sitekey: {args.sitekey}")
    print(f"  Host: {args.host}")
    print("═" * 50)

    solver = HCaptchaSolver(
        sitekey=args.sitekey,
        host=args.host,
        proxy=args.proxy,
        model_path=args.model,
    )
    result = await solver.solve()
    if result["success"]:
        print(f"\n✅ Token: {result['token'][:30]}...")
    else:
        print(f"\n❌ Failed: {result.get('error')}")
    print(f"   Time: {result.get('time', 0):.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
