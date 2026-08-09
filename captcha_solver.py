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


# ═══════════════════════════════════════════════════════════════
# Animal word list — every known animal name (lowercase)
# Used for hCaptcha "pick the animal" accessibility challenges
# ═══════════════════════════════════════════════════════════════
ANIMAL_WORDS = frozenset([
    # Baby animals (frequently used in pick-the-animal challenges)
    "piglet","kitten","puppy","calf","chick","lamb","foal","fawn","duckling",
    "gosling","kid","cub","joey","cygnet","eaglet","owlet","leveret","pullet",
    "heifer","hatchling","fry","bunny","kit","filly","colt","sprat","parr",
    "aardvark","abalone","agouti","albatross","alligator","alpaca","anaconda",
    "angelfish","angelshark","ant","anteater","antelope","ape","aphid",
    "armadillo","asp","axolotl","baboon","badger","bandicoot","barnacle",
    "barracuda","basilisk","bass","bat","bear","beaver","bee","beetle",
    "bilby","binturong","bison","blackbird","blowfish","bluebird","boa",
    "bobcat","bongo","bonobo","buffalo","bull","bullfrog","bumblebee",
    "butterfly","caiman","camel","canary","capybara","caracal","cardinal",
    "caribou","carp","caterpillar","cat","catfish","cattle","centipede",
    "chameleon","cheetah","chickadee","chicken","chimpanzee","chinchilla",
    "chipmunk","cicada","clam","clownfish","coati","cobra","cockatoo",
    "cockroach","cod","collie","conch","condor","coral","cougar","cow",
    "coyote","coypu","crab","crane","crayfish","cricket","crocodile",
    "crow","cuckoo","cuttlefish","deer","dingo","dodo","dog","dolphin",
    "donkey","dove","dragon","dragonfly","dromedary","duck","dugong",
    "eagle","earthworm","earwig","echidna","eel","egret","eland",
    "elephant","elk","emu","ermine","falcon","ferret","finch","firefly",
    "flamingo","flea","flounder","fly","fossa","fox","frog","gar",
    "gazelle","gecko","gerbil","gibbon","giraffe","gnat","gnu","goat",
    "goldfinch","goldfish","goose","gopher","gorilla","grasshopper",
    "grouper","grouse","gull","guppy","haddock","halibut","hamster",
    "hare","hawk","hedgehog","hen","heron","herring","hippopotamus","hornet",
    "horse","hummingbird","husky","hyena","hyrax","ibis","iguana",
    "impala","indri","jackal","jackrabbit","jaguar","jay","jellyfish",
    "jerboa","kangaroo","katydid","kinkajou","kiwi","koala","kookaburra",
    "krill","kudu","ladybug","lamprey","lark","leech","lemming","lemur",
    "leopard","lion","lizard","llama","lobster","locust","loon","loris",
    "louse","lynx","macaque","macaw","mackerel","maggot","mallard",
    "mamba","manatee","mandrill","mantis","marmot","marten","meerkat",
    "mink","minnow","mockingbird","mole","mongoose","monkey","moose",
    "mosquito","moth","mouse","mule","muskrat","mussel","narwhal",
    "nautilus","newt","nightingale","numbat","nuthatch","nutria",
    "ocelot","octopus","okapi","opossum","orangutan","orca","oriole",
    "ostrich","otter","owl","ox","oyster","panda","pangolin","panther",
    "parakeet","parrot","peacock","peafowl","pelican","penguin",
    "pheasant","phoenix","pig","pigeon","pika","piranha","platypus",
    "pony","porcupine","porpoise","pronghorn","puffin","pug","puma",
    "python","quail","quetzal","quokka","quoll","rabbit","raccoon",
    "rat","rattlesnake","raven","reindeer","rhinoceros","robin",
    "rooster","salamander","salmon","sandpiper","sardine","sawfish",
    "scallop","scorpion","seahorse","seal","serval","shark","sheep",
    "shrew","shrimp","silkworm","silverfish","skink","skunk","sloth",
    "slug","snail","snake","sparrow","spider","sponge","squid",
    "squirrel","starfish","starling","stingray","stoat","stork",
    "sturgeon","swan","swordfish","tadpole","tamarin","tapir",
    "tarantula","tarpon","tarsier","termite","tern","thrush","tiger",
    "toad","tortoise","toucan","trout","tuatara","tuna","turkey",
    "turtle","unicorn","vaquita","vicuna","viper","vole","vulture",
    "wallaby","walrus","warthog","wasp","weasel","whale","wildebeest",
    "wolf","wolverine","wombat","woodchuck","woodpecker","worm","wren",
    "yak","zebra","zebu","zorse",
    # Dinosaurs
    "allosaurus","ankylosaurus","apatosaurus","brachiosaurus",
    "brontosaurus","diplodocus","iguanodon","megalodon","plesiosaur",
    "pterodactyl","pterosaur","stegosaurus","triceratops",
    "tyrannosaurus","velociraptor",
    # Mythical/extinct (some captchas include these)
    "dragon","griffin","phoenix","pegasus","centaur","hydra",
    "kraken","leviathan","manticore","minotaur","wyvern",
    "werewolf","yeti","bigfoot","sasquatch","nessie","chupacabra",
    # Sea creatures / marine
    "anemone","coral","jellyfish","manowar","nautilus","urchin",
    "barnacle","limpet","abalone","conch","whelk","cuttlefish",
    # Birds - additional
    "albatross","booby","budgerigar","budgie","bustard","cassowary",
    "cockatiel","cormorant","curlew","dodo","dunlin","falcon",
    "flamingo","frigatebird","gannet","godwit","guineafowl",
    "hoopoe","hornbill","jacana","kestrel","kingfisher","kiwi",
    "lapwing","magpie","martin","merlin","moorhen","myna","oriole",
    "osprey","owl","oystercatcher","partridge","pelican","penguin",
    "petrel","plover","puffin","quail","rail","razorbill",
    "roadrunner","rook","ruff","sanderling","shearwater","shrike",
    "skua","skylark","snipe","spoonbill","stilt","stint","swallow",
    "swift","tanager","titmouse","towhee","turnstone","vireo",
    "vulture","wagtail","warbler","waxwing","weaver","whimbrel",
    "whipbird","willet","yellowhammer",
    # Fish - additional
    "anchovy","anglerfish","arowana","barracuda","blenny","bream",
    "burbot","butterflyfish","carp","catla","char","chub","cichlid",
    "coelacanth","damselfish","darter","dory","dragonet","eel",
    "filefish","flatfish","flounder","goby","grouper","grunion",
    "gudgeon","guitarfish","gunnel","gurnard","hagfish","hake",
    "halfbeak","hamlet","hogfish","icefish","jawfish","killifish",
    "lamprey","ling","lionfish","loach","mackerel","marlin",
    "mooneye","mudskipper","mullet","needlefish","opah","parrotfish",
    "perch","pickerel","pike","pilchard","pipefish","plaice",
    "pompano","pufferfish","pupfish","rattail","remora","roach",
    "rockfish","rudderfish","sailfish","salmon","scorpionfish",
    "sculpin","shad","skate","smelt","snapper","snook","sole",
    "sprat","stickleback","stingray","stonefish","sturgeon",
    "sunfish","surgeonfish","swordfish","tang","tarpon","tench",
    "tetra","tilapia","triggerfish","trout","tuna","turbot",
    "wahoo","walleye","weakfish","whitefish","whiting","wolffish",
    "wrasse","yellowtail","zander",
    # Amphibians & Reptiles - additional
    "adder","agamid","alligator","anole","axolotl","bullfrog",
    "caiman","caecilian","chameleon","cobra","copperhead",
    "cottonmouth","dab","frog","gecko","gharial","gila",
    "iguana","krait","leopardfrog","mamba","monitor","mudpuppy",
    "newt","racer","racerunner","rattler","salamander","skink",
    "snake","springpeeper","taipan","terrapin","toad","tortoise",
    "treefrog","tuatara","turtle","viper","whiptail",
    # Insects / Bugs - additional
    "antlion","aphid","backswimmer","bedbug","bee","beetle",
    "borer","bristletail","bug","bumblebee","caddisfly","chafer",
    "chigger","cicada","cockroach","crane","cricket","damselfly",
    "dobsonfly","dragonfly","earwig","fireant","firefly","flea",
    "fly","fruitfly","gnat","grasshopper","grub","hornet",
    "horsefly","hoverfly","katydid","lacewing","ladybug",
    "lanternfly","leafcutter","leafhopper","lice","locust",
    "longhorn","louse","mantis","mayfly","midge","mite",
    "mosquito","moth","planthopper","potatobeetle","psyllid",
    "roach","robberfly","sawfly","scarab","silkworm","silverfish",
    "springtail","stinkbug","stonefly","termite","thrips","tick",
    "tsetse","walkingstick","wasp","weevil","whitefly","yellowjacket",
    # Arachnids
    "harvestman","mite","scorpion","spider","tarantula","tick",
    "vinegaroon","whipscorpion","whipspider",
    # Mollusks
    "abalone","arkclam","clam","conch","cowrie","cuttlefish",
    "geoduck","limpet","mussel","nautilus","octopus","oyster",
    "periwinkle","quahog","razorclam","scallop","slug","snail",
    "squid","triton","whelk",
    # Crustaceans
    "amphipod","barnacle","copepod","crab","crayfish","isopod",
    "krill","langoustine","lobster","prawn","sandhopper",
    "shrimp","sowbug","woodlouse",
    # Mammals - additional (bats, rodents, primates etc)
    "agouti","alpaca","anteater","armadillo","ayeaye","baboon",
    "badger","bandicoot","bat","bear","beaver","bilby","binturong",
    "bison","bobcat","bonobo","buffalo","bushbaby","camel",
    "capybara","caracal","caribou","cheetah","chimp","chinchilla",
    "chipmunk","coati","colobus","colugo","cougar","cow",
    "coyote","coypu","deer","dhole","dingo","dog","dolphin",
    "donkey","dormouse","dugong","echidna","eland","elephant",
    "elk","ermine","fennec","ferret","fisher","fossa","fox",
    "galago","gazelle","genet","gerbil","gibbon","giraffe",
    "goat","gopher","gorilla","grysbok","guanaco","hamster",
    "hare","hedgehog","hippo","hippopotamus","horse","human",
    "hutia","hyena","hyrax","ibex","impala","indri","jackal",
    "jaguar","jerboa","kangaroo","kinkajou","koala","kudu",
    "lemming","lemur","leopard","lion","llama","loris","lynx",
    "macaque","mammoth","manatee","mandrill","margay","marmoset",
    "marmot","marten","mastodon","meerkat","mink","mole",
    "mongoose","monkey","moose","mouse","mule","muntjac","muskox",
    "muskrat","narwhal","numbat","nutria","nyala","ocelot",
    "okapi","opossum","orangutan","orca","oryx","otter","panda",
    "pangolin","panther","peccary","pika","platypus","polecat",
    "pony","porcupine","possum","potoroo","pronghorn","pudu",
    "puma","quokka","quoll","rabbit","raccoon","rat","reindeer",
    "rhino","rhinoceros","sable","saiga","seal","serval",
    "sheep","shrew","siamang","skunk","sloth","solenodon",
    "springbok","springhare","squirrel","stoat","sugarglider",
    "sunbear","tamarin","tapir","tarsier","tiger","topi",
    "uakari","vicuna","vole","wallaby","walrus","warthog",
    "waterbuck","weasel","whale","wildebeest","wolf","wolverine",
    "wombat","woodchuck","yak","zebra","zebu","zorilla","zorro",
    # Dog breeds (sometimes captchas use these)
    "beagle","boxer","bulldog","chihuahua","collie","dalmatian",
    "doberman","greyhound","hound","husky","labrador","mastiff",
    "poodle","pug","retriever","rottweiler","shepherd","spaniel",
    "terrier","whippet",
    # Cat breeds
    "bengal","birman","burmese","calico","persian","siamese",
    "sphynx","tabby",
    # Horse breeds / equines
    "appaloosa","bronco","clydesdale","colt","filly","gelding",
    "mare","mustang","palomino","pony","stallion",
    # Collective / generic
    "amphibian","animal","arachnid","beast","bird","bovine",
    "bug","canine","cetacean","crustacean","dinosaur","equine",
    "feline","finch","fish","fowl","insect","invertebrate",
    "mammal","marsupial","mollusk","primate","raptor","reptile",
    "rodent","serpent","ungulate","vertebrate",
])
# ── Knowledge-base solver for hCaptcha accessibility NL questions ──
# Covers: rooms, colors, animal sounds, counting/legs, calendar, nature,
# object function, opposites, and "which of these is a/an X" pickers.

KNOWLEDGE_QUESTIONS = [
    # ── Rooms ──
    (r"room.*(?:has|with) a sink|sink for washing dishes|wash.*dishes", "kitchen"),
    (r"room.*cook|room.*prepar(e|ing) food", "kitchen"),
    (r"room.*refrigerator|room.*fridge", "kitchen"),
    (r"room.*(?:has|with) a bed|room.*sleep", "bedroom"),
    (r"room.*(?:shower|bath|bathtub|bath tub)|room.*brush.*teeth", "bathroom"),
    (r"room.*(?:sofa|couch|watch tv|watch television)", "living room"),
    (r"room.*(?:eat dinner|dining table|dining)", "dining room"),
    (r"room.*(?:laundry|washing machine)", "laundry room"),
    (r"room.*(?:read|books)", "library"),
    (r"room.*(?:work|desk|office)", "office"),
    # ── Colors ──
    (r"color.*sky|color.*ocean|color.*sea|color.*water", "blue"),
    (r"color.*grass|color.*(?:leaf|leaves)", "green"),
    (r"color.*snow|color.*cloud|color.*milk", "white"),
    (r"color.*banana|color.*sun|color.*lemon", "yellow"),
    (r"color.*blood|color.*strawberr|color.*stop sign", "red"),
    (r"color.*orange|color.*carrot|color.*pumpkin", "orange"),
    (r"color.*chocolate|color.*(?:tree|trunk)|color.*brown", "brown"),
    (r"color.*coal|color.*night sky|color.*crow", "black"),
    (r"color.*elephant", "gray"),
    (r"color.*apple", "red"),
    (r"color.*grape|color.*eggplant|color.*plum", "purple"),
    (r"color.*pink|color.*flamingo|color.*pig", "pink"),
    (r"what color.*sky", "blue"),
    (r"what color.*grass", "green"),
    (r"what color.*snow", "white"),
    (r"what color.*banana|what color.*sun", "yellow"),
    (r"what color.*blood|what color.*stop sign", "red"),
    # ── Animal sounds → animal ──
    (r"animal.*moo|says moo|makes.*moo", "cow"),
    (r"animal.*(?:barks|bark)|says woof|makes.*woof", "dog"),
    (r"animal.*(?:meows|meow)|says meow|makes.*meow", "cat"),
    (r"animal.*(?:quacks|quack)|says quack|makes.*quack", "duck"),
    (r"animal.*(?:oinks|oink)|says oink|makes.*oink", "pig"),
    (r"animal.*(?:neighs|neigh)|says neigh|makes.*neigh", "horse"),
    (r"animal.*(?:baas|baa)|says baa|makes.*baa", "sheep"),
    (r"animal.*(?:roars|roar)|says roar|makes.*roar", "lion"),
    (r"animal.*(?:howls|howl)|says howl|makes.*howl", "wolf"),
    (r"animal.*(?:chirps|chirp|tweets|tweet|sings)", "bird"),
    (r"animal.*(?:ribbits|ribbit|croaks|croak)", "frog"),
    (r"animal.*(?:hisses|hiss)", "snake"),
    (r"animal.*(?:gobbles|gobble)", "turkey"),
    (r"animal.*(?:hoots|hoot)", "owl"),
    (r"animal.*(?:buzzes|buzz)", "bee"),
    (r"animal.*(?:clucks|cluck)", "chicken"),
    (r"animal.*(?:caws|caw)", "crow"),
    (r"animal.*(?:growls|growl)", "bear"),
    # ── Counting / legs / wheels ──
    (r"how many legs.*(?:dog|cat|horse|cow|goat|sheep|pig|rabbit)", "4"),
    (r"how many legs.*spider", "8"),
    (r"how many legs.*(?:insect|ant|bee|beetle|fly|bug|grasshopper)", "6"),
    (r"how many legs.*(?:bird|chicken|duck|person|human|man|woman)", "2"),
    (r"how many legs.*(?:snake|worm)", "0"),
    (r"how many wheels.*car", "4"),
    (r"how many wheels.*(?:bicycle|bike)", "2"),
    (r"how many wheels.*tricycle", "3"),
    (r"how many wheels.*(?:motorcycle|motorbike)", "2"),
    (r"how many wheels.*bus", "4"),
    (r"how many days.*week", "7"),
    (r"how many months.*year", "12"),
    (r"how many seasons", "4"),
    (r"how many eyes", "2"),
    (r"how many fingers.*(?:one|single)? ?hand", "5"),
    (r"how many toes.*(?:one|single)? ?foot", "5"),
    (r"how many colors.*rainbow|colors in a rainbow", "7"),
    (r"how many sides.*triangle", "3"),
    (r"how many sides.*square", "4"),
    (r"how many sides.*(?:pentagon|star)", "5"),
    (r"how many sides.*hexagon", "6"),
    (r"how many hours.*(?:day|in a day)", "24"),
    (r"how many (?:minutes|mins).*hour", "60"),
    (r"how many (?:letters|alphabet).*alphabet", "26"),
    (r"how many (?:planets)", "8"),
    (r"how many (?:wings).*bird", "2"),
    (r"how many ears", "2"),
    (r"how many nose", "1"),
    (r"how many heads", "1"),
    (r"how many teeth", "32"),
    # ── Calendar ──
    (r"first month.*year|month.*first.*year", "january"),
    (r"last month.*year|month.*last.*year", "december"),
    (r"month.*after june", "july"),
    (r"month.*after july", "august"),
    (r"month (?:that|with).*28 (?:or 29 )?days|month.*february", "february"),
    (r"season.*after winter", "spring"),
    (r"season.*after spring", "summer"),
    (r"season.*after summer", "autumn"),
    (r"season.*after (?:autumn|fall)", "winter"),
    (r"first day of the week", "sunday"),
    (r"day.*after tuesday", "wednesday"),
    (r"day.*after monday", "tuesday"),
    (r"day.*after sunday", "monday"),
    (r"day.*before friday", "thursday"),
    (r"day.*before monday", "sunday"),
    (r"day between saturday and monday|day between sunday and tuesday", "sunday"),
    # ── Nature / food chain ──
    (r"frozen water", "ice"),
    (r"bees make|bee.*make|made by bees", "honey"),
    (r"chickens lay|chicken.*lay", "eggs"),
    (r"cow.*(?:produce|give)", "milk"),
    (r"falls.*sky.*(?:raining|rain)|comes.*sky.*rain", "rain"),
    (r"shines.*(?:night)", "moon"),
    (r"shines.*day|shines during the day", "sun"),
    (r"clouds produce|produced by clouds", "rain"),
    (r"what do plants need to grow", "water"),
    (r"do bees make", "honey"),
    (r"what do hens lay", "eggs"),
        # ── Instruments ──
    (r"string instrument.*six strings|six strings.*instrument|what.*six strings", "guitar"),
    (r"instrument.*(?:6|six) strings", "guitar"),
    (r"instrument.*(?:4|four) strings|violin", "violin"),
    (r"instrument.*(?:88|eighty.eight) keys|how many keys.*piano", "88"),
    (r"instrument.*keys|what.*has keys.*black.*white|piano", "piano"),
    (r"instrument.*(?:blow|wind).*flute", "flute"),
    (r"instrument.*(?:blow|brass).*trumpet", "trumpet"),
    (r"instrument.*(?:hit|percussion|drum)", "drums"),
    (r"instrument.*(?:sax|jazz)", "saxophone"),
    (r"instrument.*(?:large|string|orchestra).*harp", "harp"),
    (r"instrument.*(?:cello|violoncello)", "cello"),
    (r"instrument.*(?:bass guitar|electric bass)", "bass"),
    (r"instrument.*(?:ukulele|small.*strings)", "ukulele"),
    (r"instrument.*(?:banjo|five strings)", "banjo"),
    (r"(?:how many|number of) strings.*(?:guitar|acoustic)", "6"),
    (r"(?:how many|number of) strings.*violin", "4"),
    (r"(?:how many|number of) keys.*piano", "88"),

# ── Objects / function ──
    (r"use.*eat soup|eat soup.*with", "spoon"),
    (r"use.*cut (?:food|meat|bread)", "knife"),
    (r"use.*cut paper|cut paper.*with", "scissors"),
    (r"use.*write|write.*with", "pen"),
    (r"use.*tell time|tell time.*with", "clock"),
    (r"\buse\w*\s+.*\bread\b|\bread\b.*\bwith\b", "book"),
    (r"use.*take pictures|take (?:photos|pictures).*with", "camera"),
    (r"use.*call.*(?:someone|person)|call.*with", "phone"),
    (r"use.*light.*(?:room|dark)|light.*room.*with", "lamp"),
    (r"use.*clean.*teeth|clean.*teeth.*with", "toothbrush"),
    (r"use.*dry.*hands|dry.*hands.*with", "towel"),
    (r"use.*brush.*hair|brush.*hair.*with", "brush"),
    (r"ride.*school", "bus"),
    (r"what do you drive", "car"),
    (r"fly.*sky", "plane"),
    (r"type.*(?:computer|laptop)|keyboard.*type", "keyboard"),
    (r"listen.*music", "headphones"),
    (r"watch.*(?:movies|films)", "tv"),
    # ── Opposites ──
    (r"opposite of up", "down"),
    (r"opposite of hot", "cold"),
    (r"opposite of day", "night"),
    (r"opposite of left", "right"),
    (r"opposite of right", "left"),
    (r"opposite of big", "small"),
    (r"opposite of open", "closed"),
    (r"opposite of fast", "slow"),
    (r"opposite of wet", "dry"),
    (r"opposite of full", "empty"),
    (r"opposite of black", "white"),
    (r"opposite of white", "black"),
    (r"opposite of old", "young"),
    (r"opposite of happy", "sad"),
    (r"opposite of cold", "hot"),
    (r"opposite of down", "up"),
    (r"opposite of dark", "light"),
    (r"opposite of tall", "short"),
    (r"opposite of front", "back"),
    # ── Single fact ──
    (r"capital of (?:france|french)", "paris"),
    (r"capital of (?:england|united kingdom|uk|britain)", "london"),
    (r"capital of (?:spain|spainish)", "madrid"),
    (r"capital of (?:italy)", "rome"),
    (r"capital of (?:japan)", "tokyo"),
    (r"capital of (?:usa|america|united states)", "washington"),
    (r"capital of (?:germany)", "berlin"),
    (r"capital of (?:egypt)", "cairo"),
    (r"color of a (?:stop|stop sign) sign", "red"),
    (r"what (?:animal|creature).*milk.*(?:cow|cows)", "cow"),    # ── Instruments ──
    (r"string instrument.*six strings|six strings.*instrument|what.*six strings", "guitar"),
    (r"instrument.*(?:6|six) strings", "guitar"),
    (r"instrument.*(?:4|four) strings", "violin"),
    (r"instrument.*(?:88|eighty.eight) keys|how many keys.*piano", "88"),
    (r"instrument.*keys|what.*has keys.*black.*white", "piano"),
    (r"instrument.*(?:blow|wind).*(?:flute|recorder)", "flute"),
    (r"instrument.*(?:blow|brass).*trumpet", "trumpet"),
    (r"instrument.*(?:hit|percussion|drum)", "drums"),
    (r"instrument.*(?:sax|jazz)", "saxophone"),
    (r"instrument.*(?:large|string|orchestra).*harp", "harp"),
    (r"instrument.*(?:cello|violoncello)", "cello"),
    (r"instrument.*(?:bass|low.*notes)", "bass"),
    (r"instrument.*(?:ukulele|small.*string)", "ukulele"),
    (r"instrument.*(?:banjo|five string)", "banjo"),
    (r"(?:how many|number of) strings.*(?:guitar|acoustic)", "6"),
    (r"(?:how many|number of) strings.*violin", "4"),
    (r"(?:how many|number of) keys.*piano", "88"),
    # ── Science / Body ──
    (r"organ.*pumps blood|what.*pumps.*blood|pumps blood", "heart"),
    (r"organ.*breathe|breathe.*organ|what.*use.*breathe", "lungs"),
    (r"organ.*think|think.*organ|controls.*body.*organ", "brain"),
    (r"organ.*digest.*food|digest.*organ", "stomach"),
    (r"organ.*filter.*blood", "kidney"),
    (r"organ.*(?:see|sight|eye)", "eye"),
    (r"organ.*(?:hear|sound|ear)", "ear"),
    (r"(?:largest|biggest) organ.*(?:body|human)", "skin"),
    (r"(?:longest|biggest) bone.*(?:body|human)", "femur"),
    (r"(?:smallest|tiniest) bone.*(?:body|human)", "stapes"),
    (r"how many bones.*(?:adult|human) body", "206"),
    (r"how many bones.*baby", "300"),
    (r"(?:what|which) bone.*(?:skull|head|protect.*brain)", "skull"),
    (r"(?:what|which) bone.*(?:rib|chest|protect.*heart)", "rib"),
    (r"(?:how many|number of) chambers.*heart", "4"),
    (r"(?:how many|number of) lobes.*brain", "4"),
    (r"(?:what|which) (?:element|gas).*(?:breathe|air|oxygen)", "oxygen"),
    (r"(?:what|which) (?:element|gas).*(?:plant|photosynthesis)", "carbon dioxide"),
    (r"(?:what|which) (?:planet|body).*closest to (?:the )?sun", "mercury"),
    (r"(?:what|which) (?:planet|body).*largest.*(?:solar system|sun)", "jupiter"),
    (r"(?:what|which) (?:planet).*red planet", "mars"),
    (r"(?:what|which) (?:planet).*red.*spot", "jupiter"),
    (r"(?:what|which) (?:planet).*rings", "saturn"),
    (r"(?:what|which) (?:planet).*blue.*(?:planet|color)", "neptune"),
    (r"(?:what|which) (?:planet).*(?:life|we live|our planet)", "earth"),
    (r"(?:what|which) (?:planet).*red.*(?:surface|mars)", "mars"),
    (r"(?:what|which) (?:planet).*hottest", "venus"),
    (r"how many planets.*(?:solar system|sun)", "8"),
    (r"(?:what|which) (?:planet|dwarf).*pluto", "pluto"),
    # ── Geography ──
    (r"(?:what|which|name).*largest ocean|ocean.*largest|biggest ocean", "pacific"),
    (r"(?:what|which|name).*smallest ocean|ocean.*smallest", "arctic"),
    (r"(?:what|which|name).*largest continent|continent.*largest|biggest continent", "asia"),
    (r"(?:what|which|name).*smallest continent|continent.*smallest", "australia"),
    (r"(?:what|which|name).*longest river|river.*longest", "nile"),
    (r"(?:what|which|name).*highest mountain|mountain.*highest|tallest.*mountain", "everest"),
    (r"(?:what|which|name).*largest country|country.*largest.*area", "russia"),
    (r"(?:what|which|name).*most populous.*country|country.*most.*people", "india"),
    (r"(?:what|which|name).*largest desert|desert.*largest", "sahara"),
    (r"(?:what|which|name).*coldest.*continent", "antarctica"),
    (r"(?:what|which|name).*hottest.*continent", "africa"),
    (r"how many continents|number of continents", "7"),
    (r"how many oceans|number of oceans", "5"),
    (r"(?:what|which).*ocean.*(?:usa|america|united states).*west", "pacific"),
    (r"(?:what|which).*ocean.*(?:usa|america|united states).*east", "atlantic"),
    # ── Sports ──
    (r"how many players.*(?:soccer team|football.*team)", "11"),
    (r"how many players.*basketball.*team", "5"),
    (r"how many players.*baseball.*team", "9"),
    (r"how many players.*(?:hockey|ice hockey).*team", "6"),
    (r"how many players.*volleyball.*team", "6"),
    (r"(?:what|which) sport.*(?:racket|racquet).*net", "tennis"),
    (r"(?:what|which) sport.*(?:bat|ball).*diamond|baseball.*sport", "baseball"),
    (r"(?:what|which) sport.*(?:hoop|dribble|basket)", "basketball"),
    (r"(?:what|which) sport.*(?:goal.*net|kick.*ball)", "soccer"),
    (r"(?:what|which) sport.*(?:pool|cue|table)", "billiards"),
    (r"(?:what|which) sport.*(?:racket|court|shuttlecock)", "badminton"),
    (r"(?:what|which) sport.*(?:wicket|bat.*ball)", "cricket"),
    (r"how many holes.*golf course", "18"),
    (r"how many quarters.*basketball", "4"),
    (r"how many quarters.*(?:football|soccer)", "2"),
    (r"how many periods.*hockey", "3"),
    (r"how many innings.*baseball", "9"),
    # ── Everyday objects extended ──
    (r"(?:what|which).*use.*(?:open.*door|door.*open|unlock)", "key"),
    (r"(?:what|which).*use.*(?:see.*dark|light.*dark)", "flashlight"),
    (r"(?:what|which).*use.*(?:keep food cold|refrigerate)", "refrigerator"),
    (r"(?:what|which).*use.*(?:heat food|microwave|reheat)", "microwave"),
    (r"(?:what|which).*use.*(?:wash clothes|laundry)", "washing machine"),
    (r"(?:what|which).*use.*(?:iron|remove wrinkles|press)", "iron"),
    (r"(?:what|which).*use.*(?:vacuum|clean.*floor|sweep)", "vacuum"),
    (r"(?:what|which).*use.*(?:measure.*length|ruler)", "ruler"),
    (r"(?:what|which).*use.*(?:calculate|math.*device)", "calculator"),
    (r"(?:what|which).*use.*(?:sit|chair)", "chair"),
    (r"(?:what|which).*use.*(?:sleep|bed)", "bed"),
    (r"(?:what|which).*use.*(?:protect.*rain|umbrella)", "umbrella"),
    (r"(?:what|which).*use.*(?:carry.*groceries|shopping)", "bag"),
    (r"(?:what|which).*use.*(?:drink.*hot|liquid.*hot)", "cup"),
    (r"(?:what|which).*use.*(?:cut.*meat|steak)", "knife"),
    (r"(?:what|which).*use.*(?:dig.*hole|garden)", "shovel"),
    (r"(?:what|which).*use.*(?:hammer|nail|pound)", "hammer"),
    (r"(?:what|which).*use.*(?:paint|color.*wall)", "paintbrush"),
    (r"(?:what|which).*use.*(?:sew|stitch|thread)", "needle"),
    (r"(?:what|which).*use.*(?:lock|unlock)", "key"),
    (r"(?:what|which).*use.*(?:erase|rub out|remove.*writing)", "eraser"),
    (r"(?:what|which).*use.*(?:sharpen|pencil)", "sharpener"),
    (r"(?:what|which).*use.*(?:staple|attach.*paper)", "stapler"),
    (r"(?:what|which).*use.*(?:carry.*books|school.*bag)", "backpack"),
    (r"(?:what|which).*wear.*(?:feet|foot)", "shoes"),
    (r"(?:what|which).*wear.*(?:head|cold)", "hat"),
    (r"(?:what|which).*wear.*(?:eyes|sun|vision)", "glasses"),
    (r"(?:what|which).*wear.*(?:hands|cold.*hand)", "gloves"),
    (r"(?:what|which).*wear.*(?:wrist|time)", "watch"),
    # ── Time / measurements ──
    (r"how many (?:seconds|secs).*minute", "60"),
    (r"how many (?:minutes|mins).*hour", "60"),
    (r"how many hours.*day", "24"),
    (r"how many days.*(?:february|feb)", "28"),
    (r"how many days.*leap year", "366"),
    (r"how many weeks.*year", "52"),
    (r"(?:what|which) month.*(?:first|january|new year)", "january"),
    (r"(?:what|which) month.*(?:last|december|christmas)", "december"),
    (r"(?:what|which) month.*(?:valentine|february|love)", "february"),
    (r"(?:what|which) month.*(?:halloween|october|spooky)", "october"),
    (r"(?:what|which) month.*(?:thanksgiving|november|turkey)", "november"),
    (r"(?:what|which) month.*(?:independence|july|fireworks)", "july"),
    # ── Materials / substances ──
    (r"(?:what|which) (?:material|substance).*window|glass.*transparent", "glass"),
    (r"(?:what|which) (?:material|substance).*paper|paper.*made.*wood", "wood"),
    (r"(?:what|which) (?:material|substance).*(?:metal|iron|steel).*car", "metal"),
    (r"(?:what|which) (?:material|substance).*plastic|plastic.*bottle", "plastic"),
    (r"(?:what|which) (?:material|substance).*cloth|clothes.*made", "cotton"),
    (r"(?:what|which) (?:material|substance).*rubber|tire.*made", "rubber"),
    (r"(?:what|which) (?:material|substance).*leather|shoe.*leather", "leather"),
    (r"(?:what|which) (?:material|substance).*(?:gold|jewelry|ring)", "gold"),
    (r"(?:what|which) (?:material|substance).*(?:diamond|gem)", "diamond"),
    # ── Comparison / which is bigger/larger ──
    (r"which is (?:larger|bigger).*mouse.*horse|which is (?:larger|bigger).*horse.*mouse", "horse"),
    (r"which is (?:larger|bigger).*cat.*elephant|which is (?:larger|bigger).*elephant.*cat", "elephant"),
    (r"which is (?:larger|bigger).*(?:golf|tennis).*(?:tennis|golf).*ball", "tennis"),
    (r"which is (?:larger|bigger).*bus.*car|which is (?:larger|bigger).*car.*bus", "bus"),
    (r"which is (?:larger|bigger).*(?:airplane|plane).*(?:bicycle|bike)", "airplane"),
    (r"which is (?:taller|higher).*mountain.*hill", "mountain"),
    (r"which is (?:faster|quicker).*plane.*car", "plane"),
    (r"which is (?:faster|quicker).*cheetah.*turtle", "cheetah"),
    (r"which is (?:faster|quicker).*rabbit.*snail", "rabbit"),
    (r"which is (?:heavier|weighs more).*elephant.*mouse", "elephant"),
    (r"which is (?:heavier|weighs more).*(?:ton|truck).*(?:feather|gram)", "ton"),
    (r"which is (?:colder|freez).*ice.*fire", "ice"),
    (r"which is (?:hotter|warm).*(?:sun|fire).*ice", "sun"),
    # ── Spelling / word recognition ──
    (r"(?:which|what) (?:word|letter|number).*spelled|spell.*correctly", None),
    (r"how (?:do you|to) (?:write|spell).*word.*orange", "orange"),
    # ── Skip patterns: these are handled by Ollama ──



    # ── Food / Pets ──
    (r"(?:food|pet food).*cans.*cats?|cats?.*(?:food|pet food).*cans", "cat food"),
    (r"(?:food|pet food).*cans.*dogs?|dogs?.*(?:food|pet food).*cans", "dog food"),
    (r"pet food.*cans|food.*comes in cans|food.*cans", "dog food"),
    (r"pet food.*cats|cat.*food.*bowl", "cat food"),
    (r"food.*(?:purr|meow|cats)", "cat food"),
    (r"food.*(?:bark|dogs|pupp)", "dog food"),
    (r"food.*(?:whiskers|kitten)", "cat food"),
    (r"(?:what|which) (?:color|colour).*dog.*(?:food|can)", "brown"),
    # ── Holidays / months / seasons ──
    (r"(?:holiday|celebration).*trick.or.treat|trick.or.treat.*holiday", "halloween"),
    (r"(?:holiday|celebration).*(?:turkey|thanks)", "thanksgiving"),
    (r"(?:holiday|celebration).*(?:gifts|santa|christmas)", "christmas"),
    (r"(?:holiday|celebration).*(?:fireworks|independence|july)", "july 4th"),
    (r"(?:holiday|celebration).*(?:eggs|bunny|easter)", "easter"),
    (r"(?:holiday|celebration).*(?:love|valentine|hearts)", "valentines day"),
    (r"(?:holiday|celebration).*(?:green|shamrock|irish)", "st patricks day"),
    (r"(?:holiday|celebration).*(?:pumpkin|lantern)", "halloween"),
    # ── More counting ──
    (r"how many (?:legs|feet).*(?:insect|ant|bee|beetle|bug|fly)", "6"),
    (r"how many (?:legs|feet).*lobster|crab", "10"),
    (r"how many (?:legs|feet).*centipede", "100"),
    (r"how many (?:legs|feet).*millipede", "750"),
    (r"how many (?:wings).*butterfly", "4"),
    (r"how many (?:wings).*mosquito|fly", "2"),
    (r"how many (?:wings).*bird", "2"),
    (r"how many (?:wheels).*(?:motorcycle|motorbike)", "2"),
    (r"how many (?:wheels).*(?:train|locomotive)", "18"),
    (r"how many (?:fingers).*two hands", "10"),
    (r"how many (?:toes).*two feet", "10"),
    (r"how many (?:legs).*octopus", "8"),
    (r"how many (?:arms).*octopus", "8"),
    (r"how many (?:tentacles).*octopus", "8"),
    (r"how many (?:eyes).*spider", "8"),
    (r"how many (?:legs).*crab", "10"),
    (r"how many (?:humps).*camel", "1"),
    (r"how many (?:humps).*(?:bactrian|two.humped)", "2"),
    # ── Direction / position ──
    (r"(?:what|which) (?:direction|side).*sun.*(?:rise|rises)", "east"),
    (r"(?:what|which) (?:direction|side).*sun.*(?:set|sets)", "west"),
    (r"(?:what|which) (?:direction).*north.*(?:point|arrow)", "north"),
    (r"(?:what|which) (?:direction).*(?:up|above)", "north"),
    (r"(?:what|which) (?:direction).*(?:down|below)", "south"),
    (r"opposite of (?:north)", "south"),
    (r"opposite of (?:south)", "north"),
    (r"opposite of (?:east)", "west"),
    (r"opposite of (?:west)", "east"),
    (r"opposite of (?:on)", "off"),
    (r"opposite of (?:near)", "far"),
    (r"opposite of (?:wide)", "narrow"),
    (r"opposite of (?:long)", "short"),
    (r"opposite of (?:quiet|loud)", "quiet"),
    (r"opposite of (?:loud)", "quiet"),
    (r"opposite of (?:sweet)", "sour"),
    (r"opposite of (?:sour)", "sweet"),
    (r"opposite of (?:clean)", "dirty"),
    (r"opposite of (?:dirty)", "clean"),
    (r"opposite of (?:hard)", "soft"),
    (r"opposite of (?:soft)", "hard"),
    (r"opposite of (?:rough)", "smooth"),
    (r"opposite of (?:smooth)", "rough"),
    (r"opposite of (?:new)", "old"),
    (r"opposite of (?:young)", "old"),
    (r"opposite of (?:early)", "late"),
    (r"opposite of (?:late)", "early"),
    (r"opposite of (?:always)", "never"),
    (r"opposite of (?:never)", "always"),
    (r"opposite of (?:true)", "false"),
    (r"opposite of (?:false)", "true"),
    (r"opposite of (?:win)", "lose"),
    (r"opposite of (?:lose)", "win"),
    (r"opposite of (?:push)", "pull"),
    (r"opposite of (?:pull)", "push"),
    (r"opposite of (?:shallow)", "deep"),
    (r"opposite of (?:deep)", "shallow"),
    # ── Weather / nature ──
    (r"frozen.*water.*(?:walk|solid)", "ice"),
    (r"(?:what|which).*(?:boiling point).*water", "100"),
    (r"(?:what|which).*(?:freezing point).*water", "0"),
    (r"(?:what|which).*rainbow.*colors|colors.*rainbow", "7"),
    (r"(?:what|which).*(?:first|primary) color.*rainbow", "red"),
    (r"(?:what|which).*(?:last|violet) color.*rainbow", "violet"),
    (r"(?:what|which).*(?:gas|substance).*plants.*(?:breathe|absorb)", "carbon dioxide"),
    (r"(?:what|which).*(?:gas).*humans.*(?:breathe|inhale)", "oxygen"),
    (r"(?:what|which).*(?:gas).*humans.*(?:exhale|breathe out)", "carbon dioxide"),
    (r"(?:what|which).*trees.*(?:release|give off)", "oxygen"),
    (r"(?:what|which).*(?:heavenly body).*(?:night|moonlight)", "moon"),
    (r"(?:what|which).*(?:shines|star).*(?:day|morning)", "sun"),
    (r"(?:what|which).*frozen.*(?:lake|pond)", "ice"),
    (r"(?:what|which).*cold.*(?:water|drink).*summer", "ice"),
    (r"(?:what|which).*burns.*(?:oxygen|fire)", "fire"),
    # ── Grains / flour ──
    (r"grain.*flour|flour.*grain|grain.*bread|make flour|used to make.*flour|made into flour", "wheat"),
    (r"what grain|which grain", "wheat"),
    # ── Food questions ──
    (r"(?:what|which).*yellow.*(?:fruit|banana)", "banana"),
    (r"(?:what|which).*(?:round|red).*fruit.*apple", "apple"),
    (r"(?:what|which).*(?:orange).*(?:citrus|fruit)", "orange"),
    (r"(?:what|which).*made.*(?:grapes|wine)", "wine"),
    (r"(?:what|which).*made.*(?:milk|cheese|yogurt|butter)", "dairy"),
    (r"(?:what|which).*made.*(?:wheat|bread|flour)", "bread"),
    (r"(?:what|which).*made.*(?:cocoa|cacao|chocolate)", "chocolate"),
    (r"(?:what|which).*made.*(?:rice)", "rice"),
    (r"(?:what|which).*made.*(?:apples|cider)", "cider"),
    (r"(?:what|which).*made.*(?:potatoes|fries)", "potato"),
    (r"(?:what|which).*made.*(?:bees|honey)", "honey"),
    # ── Buildings / places ──
    (r"(?:what|which).*place.*(?:borrow.*book|read.*book)", "library"),
    (r"(?:what|which).*place.*(?:watch.*(?:film|movie))", "cinema"),
    (r"(?:what|which).*place.*(?:buy.*(?:medicine|drugs))", "pharmacy"),
    (r"(?:what|which).*place.*(?:buy.*food|grocery)", "supermarket"),
    (r"(?:what|which).*place.*(?:workout|exercise|gym)", "gym"),
    (r"(?:what|which).*place.*(?:sleep.*night|stay.*hotel)", "hotel"),
    (r"(?:what|which).*place.*(?:swim|pool)", "pool"),
    (r"(?:what|which).*place.*(?:park.*car)", "parking lot"),
    (r"(?:what|which).*place.*(?:send.*mail|post.*letter)", "post office"),
    (r"(?:what|which).*place.*(?:eat.*restaurant)", "restaurant"),
    # ── Occupations ──
    (r"(?:what|which).*(?:doctor|treats.*sick)", "doctor"),
    (r"(?:what|which).*(?:teacher|teaches.*students)", "teacher"),
    (r"(?:what|which).*(?:nurse|helps.*doctor)", "nurse"),
    (r"(?:what|which).*(?:police|catch.*criminals)", "police"),
    (r"(?:what|which).*(?:firefighter|puts out fires)", "firefighter"),
    (r"(?:what|which).*(?:pilot|flies.*plane)", "pilot"),
    (r"(?:what|which).*(?:chef|cooks.*food)", "chef"),
    (r"(?:what|which).*(?:farmer|grows.*crops)", "farmer"),
    (r"(?:what|which).*(?:lawyer|defends.*court)", "lawyer"),
    (r"(?:what|which).*(?:engineer|builds.*(?:bridges|machines))", "engineer"),
    (r"(?:what|which).*(?:scientist|does.*experiments)", "scientist"),
    (r"(?:what|which).*(?:artist|paints.*pictures)", "artist"),
    (r"(?:what|which).*(?:plumber|fixes.*pipes)", "plumber"),
    (r"(?:what|which).*(?:electrician|fixes.*wires)", "electrician"),
    # ── Tools / objects ──
    (r"(?:what|which).*use.*(?:cut.*grass|lawn)", "lawn mower"),
    (r"(?:what|which).*use.*(?:trim.*(?:hedge|bush))", "shears"),
    (r"(?:what|which).*use.*(?:hang.*picture|level)", "hammer"),
    (r"(?:what|which).*use.*(?:screw.*(?:screw|bolt))", "screwdriver"),
    (r"(?:what|which).*use.*(?:tighten.*(?:nut|bolt))", "wrench"),
    (r"(?:what|which).*use.*(?:drill.*hole)", "drill"),
    (r"(?:what|which).*use.*(?:saw.*wood|cut.*wood)", "saw"),
    (r"(?:what|which).*use.*(?:measure.*(?:temperature|fever))", "thermometer"),
    (r"(?:what|which).*use.*(?:weigh.*(?:food|things))", "scale"),
    (r"(?:what|which).*use.*(?:look.*(?:stars|microscope))", "microscope"),
    (r"(?:what|which).*use.*(?:see.*(?:far|distance))", "telescope"),
    (r"(?:what|which).*use.*(?:magnif.*(?:small|text))", "magnifying glass"),
    (r"(?:what|which).*use.*(?:type.*computer)", "keyboard"),
    (r"(?:what|which).*use.*(?:point.*computer.*click)", "mouse"),
    # ── Animals extended ──
    (r"animal.*(?:biggest|largest).*(?:land|elephant)", "elephant"),
    (r"animal.*(?:biggest|largest).*(?:sea|ocean|whale)", "blue whale"),
    (r"animal.*(?:tallest|giraffe)", "giraffe"),
    (r"animal.*(?:fastest|cheetah)", "cheetah"),
    (r"animal.*(?:slowest|snail|tortoise)", "snail"),
    (r"animal.*(?:longest|giraffe).*(?:neck)", "giraffe"),
    (r"animal.*(?:stripes|tiger|zebra)", "tiger"),
    (r"animal.*(?:spots|leopard|cheetah)", "leopard"),
    (r"animal.*(?:monkey|swings.*trees)", "monkey"),
    (r"animal.*(?:kangaroo|jumps.*(?:pouch))", "kangaroo"),
    (r"animal.*(?:penguin|cannot fly.*(?:cold|antarctica))", "penguin"),
    (r"animal.*(?:polar.*bear|white.*bear)", "polar bear"),
    (r"animal.*(?:panda|black.*white.*(?:bamboo))", "panda"),
    (r"animal.*(?:lion|king.*jungle)", "lion"),
    (r"animal.*(?:snake|no legs)", "snake"),
    (r"animal.*(?:fish|swims.*water)", "fish"),
    (r"animal.*(?:bird|has.*wings.*feathers)", "bird"),
    (r"animal.*(?:bat|flies.*night.*wings)", "bat"),
    (r"animal.*(?:kangaroo|australia)", "kangaroo"),
    (r"animal.*(?:koala|eucalyptus)", "koala"),
    (r"animal.*(?:rabbit|long ears)", "rabbit"),
    (r"animal.*(?:elephant|trunk)", "elephant"),
    (r"animal.*(?:rhino|horn.*nose)", "rhinoceros"),
    (r"animal.*(?:camel|desert.*hump)", "camel"),
    (r"animal.*(?:giraffe|long neck)", "giraffe"),
    # ── Space extended ──
    (r"(?:what|which).*star.*(?:closest.*earth|sun)", "sun"),
    (r"(?:what|which).*(?:galaxy).*(?:milky way|home)", "milky way"),
    (r"(?:what|which).*(?:spacecraft|rocket).*(?:moon|landing)", "rocket"),
    (r"(?:what|which).*(?:space station|orbit)", "iss"),
    (r"(?:what|which).*(?:dwarf planet)", "pluto"),
    (r"(?:what|which).*(?:red planet)", "mars"),
    (r"(?:what|which).*(?:blue planet)", "earth"),
    (r"(?:what|which).*(?:gas giant)", "jupiter"),
    (r"(?:what|which).*(?:planet.*(?:biggest|largest))", "jupiter"),
    # ── Misc common knowledge ──
    (r"(?:what|which).*language.*(?:most spoken|spoken.*world)", "english"),
    (r"(?:what|which).*language.*(?:china|chinese)", "chinese"),
    (r"(?:what|which).*(?:currency).*(?:usa|america|dollar)", "dollar"),
    (r"(?:what|which).*(?:currency).*(?:europe|euro)", "euro"),
    (r"(?:what|which).*(?:currency).*(?:japan|yen)", "yen"),
    (r"(?:what|which).*(?:currency).*(?:uk|britain|pound)", "pound"),
    (r"(?:what|which).*(?:color).*(?:banana|yellow)", "yellow"),
    (r"(?:what|which).*(?:color).*(?:sky)", "blue"),
    (r"(?:what|which).*(?:color).*(?:grass|leaf|leaves)", "green"),
    (r"(?:what|which).*(?:color).*(?:snow|milk)", "white"),
    (r"(?:what|which).*(?:color).*(?:blood)", "red"),
    (r"(?:what|which).*(?:color).*(?:pumpkin|carrot|orange)", "orange"),
    (r"(?:what|which).*(?:color).*(?:chocolate|coffee|brown)", "brown"),
    (r"(?:what|which).*(?:color).*(?:coal|night)", "black"),
    (r"(?:what|which).*(?:color).*(?:pink|flamingo)", "pink"),
    (r"(?:what|which).*(?:color).*(?:purple|grape|eggplant)", "purple"),
    (r"(?:what|which).*(?:shape).*(?:3 sides)", "triangle"),
    (r"(?:what|which).*(?:shape).*(?:4 sides)", "square"),
    (r"(?:what|which).*(?:shape).*(?:5 sides)", "pentagon"),
    (r"(?:what|which).*(?:shape).*(?:6 sides)", "hexagon"),
    (r"(?:what|which).*(?:shape).*(?:round|circle)", "circle"),
    (r"(?:what|which).*(?:shape).*(?:8 sides)", "octagon"),
    (r"(?:what|which).*(?:shape).*(?:3d.*ball)", "sphere"),
    (r"(?:what|which).*(?:shape).*(?:3d.*box)", "cube"),
    (r"(?:what|which).*(?:shape).*(?:3d.*pyramid)", "pyramid"),
    (r"(?:what|which).*(?:shape).*(?:3d.*cylinder)", "cylinder"),
    (r"(?:what|which).*(?:sport).*(?:football|soccer)", "soccer"),
    (r"(?:what|which).*(?:sport).*(?:basketball)", "basketball"),
    (r"(?:what|which).*(?:sport).*(?:baseball)", "baseball"),
    (r"(?:what|which).*(?:sport).*(?:tennis)", "tennis"),
    (r"(?:what|which).*(?:sport).*(?:hockey)", "hockey"),
    (r"(?:what|which).*(?:sport).*(?:swimming)", "swimming"),
    (r"(?:what|which).*(?:sport).*(?:boxing)", "boxing"),
    (r"(?:what|which).*(?:sport).*(?:golf)", "golf"),
    (r"(?:what|which).*(?:sport).*(?:volleyball)", "volleyball"),
]
# Category word sets for "which of these is a/an X" pickers
CATEGORY_WORDS = {
    "fruit": frozenset([
        "apple", "apricot", "avocado", "banana", "blackberry", "blueberry",
        "cherry", "coconut", "fig", "grape", "grapefruit", "kiwi", "lemon",
        "lime", "mango", "melon", "nectarine", "orange", "papaya", "peach",
        "pear", "pineapple", "plum", "pomegranate", "raspberry", "strawberry",
        "watermelon", "cantaloupe",
    ]),
    "vegetable": frozenset([
        "asparagus", "beet", "broccoli", "cabbage", "carrot", "cauliflower",
        "celery", "corn", "cucumber", "eggplant", "garlic", "kale", "lettuce",
        "onion", "pea", "pepper", "potato", "pumpkin", "radish", "spinach",
        "squash", "tomato", "turnip", "zucchini",
    ]),
    "color": frozenset([
        "red", "blue", "green", "yellow", "orange", "purple", "pink",
        "brown", "black", "white", "gray", "grey", "cyan", "magenta", "teal",
    ]),
    "room": frozenset([
        "kitchen", "bedroom", "bathroom", "living room", "dining room",
        "garage", "basement", "attic", "hallway", "office", "laundry room",
        "bath room",
    ]),
    "insect": frozenset([
        "ant", "bee", "beetle", "butterfly", "caterpillar", "cockroach",
        "cricket", "dragonfly", "fly", "grasshopper", "ladybug", "mantis",
        "mosquito", "moth", "wasp", "termite",
    ]),
    "bird": frozenset([
        "eagle", "hawk", "owl", "penguin", "parrot", "peacock", "pigeon",
        "robin", "sparrow", "swallow", "swan", "turkey", "duck", "goose",
        "chicken", "raven", "crow", "flamingo", "ostrich", "woodpecker",
    ]),
    "fish": frozenset([
        "salmon", "tuna", "trout", "shark", "goldfish", "cod", "halibut",
        "sardine", "anchovy", "eel", "catfish", "bass",
    ]),
    "clothing": frozenset([
        "shirt", "pants", "dress", "skirt", "jacket", "coat", "sweater",
        "hat", "socks", "shoes", "boots", "gloves", "scarf", "belt", "tie",
    ]),
    "body": frozenset([
        "arm", "leg", "head", "hand", "foot", "eye", "ear", "nose", "mouth",
        "finger", "toe", "knee", "elbow", "shoulder", "hair", "tongue",
    ]),
    "vehicle": frozenset([
        "car", "truck", "bus", "bicycle", "motorcycle", "train", "plane",
        "boat", "ship", "helicopter", "taxi", "van",
    ]),
    "musical instrument": frozenset([
        "guitar", "piano", "drums", "violin", "flute", "trumpet", "saxophone",
        "cello", "harp", "accordion", "clarinet", "trombone",
    ]),
    "season": frozenset(["spring", "summer", "autumn", "fall", "winter"]),
    "month": frozenset([
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
    ]),
    "day": frozenset([
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday",
    ]),
    "planet": frozenset([
        "mercury", "venus", "earth", "mars", "jupiter", "saturn",
        "uranus", "neptune",
    ]),
    "profession": frozenset([
        "doctor", "teacher", "nurse", "police", "firefighter", "lawyer",
        "engineer", "chef", "pilot", "farmer", "scientist", "artist",
    ]),
    "tool": frozenset([
        "hammer", "screwdriver", "wrench", "saw", "drill", "pliers",
        "axe", "shovel", "rake",
    ]),
    "weather": frozenset([
        "rain", "snow", "sun", "wind", "cloud", "storm", "fog", "hail",
    ]),
}


# ── Semantic answer table: topic keywords -> answer ──
# Matches ANY phrasing that contains the topic keywords, catching
# question variations the regex patterns miss. Checked after patterns.
SEMANTIC_ANSWERS = [
    # (required keywords ALL present, answer)
    (["pet food", "cans"], "dog food"),
    (["dog food", "cans"], "dog food"),
    (["cat food", "cans"], "cat food"),
    (["string instrument", "strings"], "guitar"),
    (["instrument", "six", "strings"], "guitar"),
    (["instrument", "four", "strings"], "violin"),
    (["instrument", "eighty", "keys"], "piano"),
    (["instrument", "black", "white", "keys"], "piano"),
    (["instrument", "keys"], "piano"),
    (["organ", "pumps", "blood"], "heart"),
    (["organ", "breathe"], "lungs"),
    (["organ", "think"], "brain"),
    (["organ", "digest"], "stomach"),
    (["organ", "filter", "blood"], "kidney"),
    (["largest", "organ"], "skin"),
    (["planet", "closest", "sun"], "mercury"),
    (["planet", "rings"], "saturn"),
    (["planet", "red"], "mars"),
    (["planet", "hottest"], "venus"),
    (["planet", "largest"], "jupiter"),
    (["planet", "live"], "earth"),
    (["planet", "rings"], "saturn"),
    (["ocean", "largest"], "pacific"),
    (["ocean", "smallest"], "arctic"),
    (["continent", "largest"], "asia"),
    (["continent", "smallest"], "australia"),
    (["river", "longest"], "nile"),
    (["mountain", "highest"], "everest"),
    (["country", "largest", "area"], "russia"),
    (["desert", "largest"], "sahara"),
    (["continent", "coldest"], "antarctica"),
    (["soccer", "players"], "11"),
    (["basketball", "players"], "5"),
    (["baseball", "players"], "9"),
    (["hockey", "players"], "6"),
    (["golf", "holes"], "18"),
    (["seconds", "minute"], "60"),
    (["minutes", "hour"], "60"),
    (["hours", "day"], "24"),
    (["days", "week"], "7"),
    (["months", "year"], "12"),
    (["planets", "solar"], "8"),
    (["continents"], "7"),
    (["oceans"], "5"),
    (["bones", "body"], "206"),
    (["chambers", "heart"], "4"),
    (["colors", "rainbow"], "7"),
    (["sides", "triangle"], "3"),
    (["sides", "square"], "4"),
    (["sides", "pentagon"], "5"),
    (["sides", "hexagon"], "6"),
    (["sides", "octagon"], "8"),
    (["string", "six"], "guitar"),
    (["grain", "flour"], "wheat"),
    (["grain", "bread"], "wheat"),
    (["flour", "bread"], "wheat"),
    (["animal", "moo"], "cow"),
    (["animal", "barks"], "dog"),
    (["animal", "meows"], "cat"),
    (["animal", "quacks"], "duck"),
    (["animal", "oinks"], "pig"),
    (["animal", "neighs"], "horse"),
    (["animal", "baa"], "sheep"),
    (["animal", "roars"], "lion"),
    (["animal", "howls"], "wolf"),
    (["animal", "chirps"], "bird"),
    (["animal", "ribbit"], "frog"),
    (["animal", "hisses"], "snake"),
    (["animal", "hoots"], "owl"),
    (["animal", "gobbles"], "turkey"),
    (["animal", "buzzes"], "bee"),
    (["animal", "clucks"], "chicken"),
    (["bees", "make"], "honey"),
    (["chickens", "lay"], "eggs"),
    (["cow", "produce"], "milk"),
    (["frozen", "water"], "ice"),
    (["opposite", "up"], "down"),
    (["opposite", "hot"], "cold"),
    (["opposite", "day"], "night"),
    (["opposite", "left"], "right"),
    (["opposite", "big"], "small"),
    (["opposite", "open"], "closed"),
    (["opposite", "fast"], "slow"),
    (["opposite", "wet"], "dry"),
    (["opposite", "full"], "empty"),
    (["capital", "france"], "paris"),
    (["capital", "england"], "london"),
    (["capital", "spain"], "madrid"),
    (["capital", "italy"], "rome"),
    (["capital", "japan"], "tokyo"),
    (["capital", "germany"], "berlin"),
    (["capital", "egypt"], "cairo"),
    (["room", "sink", "dishes"], "kitchen"),
    (["room", "cook"], "kitchen"),
    (["room", "bed"], "bedroom"),
    (["room", "shower"], "bathroom"),
    (["room", "bathtub"], "bathroom"),
    (["room", "sofa"], "living room"),
    (["room", "tv"], "living room"),
    (["room", "dining"], "dining room"),
    (["color", "sky"], "blue"),
    (["color", "grass"], "green"),
    (["color", "banana"], "yellow"),
    (["color", "snow"], "white"),
    (["color", "blood"], "red"),
    (["color", "stop sign"], "red"),
    (["color", "pumpkin"], "orange"),
    (["color", "chocolate"], "brown"),
    (["color", "coal"], "black"),
    (["color", "sun"], "yellow"),
    (["use", "eat soup"], "spoon"),
    (["use", "cut paper"], "scissors"),
    (["use", "write"], "pen"),
    (["use", "tell time"], "clock"),
    (["use", "read"], "book"),
    (["use", "take pictures"], "camera"),
    (["use", "call"], "phone"),
    (["use", "light", "room"], "lamp"),
    (["use", "clean", "teeth"], "toothbrush"),
    (["use", "dry", "hands"], "towel"),
    (["use", "brush", "hair"], "brush"),
    (["use", "open", "door"], "key"),
    (["use", "see", "dark"], "flashlight"),
    (["use", "keep food cold"], "refrigerator"),
    (["use", "wash clothes"], "washing machine"),
    (["wear", "feet"], "shoes"),
    (["wear", "head"], "hat"),
    (["wear", "eyes"], "glasses"),
    (["wear", "hands"], "gloves"),
    (["wear", "wrist"], "watch"),
    (["fly", "sky"], "plane"),
    (["ride", "school"], "bus"),
    (["drink", "soup"], "spoon"),
    (["material", "windows"], "glass"),
    (["material", "paper"], "wood"),
    (["month", "after", "june"], "july"),
    (["month", "after", "july"], "august"),
    (["month", "first", "year"], "january"),
    (["month", "last", "year"], "december"),
    (["season", "after", "winter"], "spring"),
    (["season", "after", "spring"], "summer"),
    (["season", "after", "summer"], "autumn"),
    (["day", "after", "tuesday"], "wednesday"),
    (["day", "after", "monday"], "tuesday"),
    (["day", "before", "friday"], "thursday"),
    (["first", "day", "week"], "sunday"),
    (["which", "larger", "mouse", "horse"], "horse"),
    (["which", "larger", "cat", "elephant"], "elephant"),
    (["which", "faster", "cheetah"], "cheetah"),
    (["which", "faster", "plane", "car"], "plane"),
    (["which", "colder", "ice", "fire"], "ice"),
    (["which", "heavier", "elephant", "mouse"], "elephant"),
    (["legs", "spider"], "8"),
    (["legs", "dog"], "4"),
    (["legs", "cat"], "4"),
    (["legs", "horse"], "4"),
    (["legs", "insect"], "6"),
    (["legs", "ant"], "6"),
    (["legs", "bird"], "2"),
    (["legs", "person"], "2"),
    (["wheels", "car"], "4"),
    (["wheels", "bicycle"], "2"),
    (["eyes", "human"], "2"),
    (["fingers", "hand"], "5"),
    (["toes", "foot"], "5"),
]


def _solve_semantic(text: str) -> Optional[str]:
    """Answer via topic-keyword table — matches ANY phrasing containing the topics."""
    if not text:
        return None
    t = text.lower()
    for keywords, answer in SEMANTIC_ANSWERS:
        if all(kw in t for kw in keywords):
            return answer
    return None


def _solve_knowledge_question(text: str) -> Optional[str]:
    """Answer natural-language knowledge questions locally (no API).
    Returns the answer string or None."""
    if not text:
        return None
    t = text.lower()

    # ── Category pickers: "Which of these is a/an X?" / "...is not a X?" ──
    # Robust against "is a fruit: apple, car, tree" and "comes after" phrasing.
    for _cat_re in (
        r"which (?:one )?of (?:these|the following|the) "
        r"(?:words )?(?:is not|are not|is|are)? ?(?:an? |the )?([a-z][a-z ]{2,20}?)\s*(?::|\?|\.|$)",
        r"(?:pick the one (?:that|which)? ?(?:represents|is)|represents|is) (?:an? |the )?([a-z][a-z ]{2,20}?)\s*(?::|\?|\.|,|$)",
    ):
        cat_match = re.search(_cat_re, t)
        if not cat_match:
            continue
        cat = cat_match.group(1).strip()
        cat_key = None
        for key in CATEGORY_WORDS:
            if cat == key or cat.startswith(key) or key.startswith(cat) or cat in key:
                cat_key = key
                break
        if cat_key:
            words = re.findall(r"[a-z]+", t)
            candidates = [w for w in words if w in CATEGORY_WORDS[cat_key]]
            negated = bool(re.search(r"\bnot\b", t))
            if negated:
                # "which is not a X" → pick the word NOT in category
                stop = ("which", "these", "following", "words", "one", "that",
                        "with", "from", "the", "and", "this", "them", "their",
                        "they", "are", "not", "animal", "fruit", "vegetable",
                        "color", "colour", "room", "is", "a", "an", "of")
                all_choice = [w for w in words if len(w) >= 3 and w not in stop]
                for w in all_choice:
                    if w not in CATEGORY_WORDS[cat_key]:
                        return w
            elif candidates:
                return candidates[0]

    # ── Direct pattern match ──
    for pattern, answer in KNOWLEDGE_QUESTIONS:
        if re.search(pattern, t, re.IGNORECASE):
            return answer

    return None


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

    async def _ollama_answer_text(question: str, timeout: float = 30.0) -> str:
        """Ask Ollama (text-only chat) for a single-word answer to a question.
        Used as fallback when the local knowledge base has no answer."""
        try:
            import aiohttp
            payload = {
                "model": ollama_model,
                "stream": False,
                "messages": [{
                    "role": "user",
                    "content": (
                        "You are solving a CAPTCHA accessibility question. "
                        "Answer with exactly ONE word or number. No punctuation, "
                        "no explanation, lowercase.\n\nQuestion: " + question
                    ),
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
            log(f"[Accessibility] Ollama text error: {e}", level="warn")
            return ""

    def _clean_llm_answer(raw: str) -> str:
        """Normalize an LLM answer: lowercase, strip punctuation,
        keep up to 3 words (captcha answers can be phrases like
        'dog food' or 'living room'). Returns '' if empty."""
        if not raw:
            return ""
        # Lowercase, drop quotes/brackets/periods but keep word separators
        s = re.sub(r"[\"'`\[\](){}<>]", "", raw)
        s = s.replace(".", " ").replace(",", " ").replace(";", " ").replace(":", " ")
        s = s.replace("\n", " ").replace("\t", " ").replace("-", " ")
        words = [w for w in s.lower().split() if re.search(r"[a-z0-9]", w)]
        if not words:
            return ""
        # Drop filler words that sometimes leak out
        stop = {"the", "a", "an", "is", "are", "it", "of", "to", "in", "for",
                "answer", "with", "and", "or", "be", "please"}
        cleaned = [w for w in words if w not in stop]
        if not cleaned:
            return ""
        return " ".join(cleaned[:3])

    async def _llm_answer_question(question: str, timeout: float = 40.0) -> str:
        """Layer 3: ask ANY LLM for the answer to an unknown question.
        Tries in order (each with up to 2 retries):
        1. Ollama (OLLAMA_URL env)
        2. OpenAI-compatible endpoint (LLM_API_URL + LLM_API_KEY + LLM_MODEL env)
        Returns the cleaned answer or empty string."""
        import asyncio

        question = (question or "").strip()[:500]
        if not question:
            return ""

        api_url = os.environ.get("LLM_API_URL") or os.environ.get("OPENAI_BASE_URL") or ""
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"

        # Log once if NOTHING is configured — user needs to know Layer 3 is inert
        if not ollama_url and not api_url:
            log("[Accessibility] [WARN] Unknown question and NO LLM configured — "
                "set OLLAMA_URL or LLM_API_URL/LLM_API_KEY/LLM_MODEL to solve any question",
                level="warn")
            return ""

        for attempt in range(1, 3):  # up to 2 attempts per provider round
            # ── Option 1: Ollama ──
            if ollama_url:
                ans = await _ollama_answer_text(question, timeout=timeout)
                cleaned = _clean_llm_answer(ans)
                if cleaned:
                    return cleaned

            # ── Option 2: OpenAI-compatible endpoint ──
            if api_url:
                try:
                    import aiohttp
                    endpoint = api_url.rstrip("/")
                    if not endpoint.endswith("/chat/completions"):
                        endpoint += "/chat/completions"
                    headers = {"Content-Type": "application/json"}
                    if api_key:
                        headers["Authorization"] = "Bearer " + api_key
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": (
                                "You are solving a CAPTCHA accessibility question. "
                                "Answer with exactly ONE word, number, or short phrase. "
                                "No punctuation, no explanation, no quotes, lowercase."
                            )},
                            {"role": "user", "content": "Question: " + question},
                        ],
                        "temperature": 0,
                        "max_tokens": 20,
                    }
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as session:
                        async with session.post(endpoint, json=payload, headers=headers) as resp:
                            if resp.status != 200:
                                log(f"[Accessibility] LLM API error {resp.status}", level="warn")
                            else:
                                data = await resp.json()
                                raw = data["choices"][0]["message"]["content"]
                                cleaned = _clean_llm_answer(raw)
                                if cleaned:
                                    return cleaned
                except Exception as e:
                    log(f"[Accessibility] LLM API error: {e}", level="warn")

            if attempt == 1:
                log("[Accessibility] LLM returned nothing — retrying once...", level="warn")
                await asyncio.sleep(1.0)

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
        # FrameLocator doesn't have .screenshot() -- use locator("body") instead
        try:
            img = await target.screenshot(type="png")
        except AttributeError:
            img = await target.locator("body").screenshot(type="png")
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

    def _find_options_line(best_source: str, all_texts, best_line: str) -> str:
        """For 'pick the one that represents an X' challenges the candidate words
        are rendered on their OWN line (e.g. 'oar, glass, piglet'). Find a short
        comma-separated list of words near the question."""
        sources = ([t for s, t in all_texts if s == best_source]
                   + [t for s, t in all_texts if s != best_source])
        for text in sources:
            for line in re.split(r'[\n|]', text):
                line = line.strip()
                if not line or len(line) > 60:
                    continue
                if line in best_line or best_line in line:
                    continue
                if not re.match(r"^[a-zA-Z][a-zA-Z ,-]{3,55}$", line):
                    continue
                parts = [p.strip() for p in line.split(',')]
                if 2 <= len(parts) <= 4 and all(2 <= len(p) <= 18 for p in parts):
                    return line
        return ''

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

        # ── Now scan all collected texts and SCORE lines ──
        # The instruction line ("Read and answer with 1 word") matches weak
        # keywords too, so we must pick the line with the MOST question
        # keywords, not the first line with any keyword.
        best_line = None
        best_score = 0
        best_source = None

        for source, text in all_texts:
            lines = text.split(chr(10))
            for line in lines:
                line = line.strip()
                if len(line) < 8 or len(line) > 500:
                    continue
                score = 0
                # STRONG keywords (the actual question uses these):
                # jar/coins math
                if re.search(r'\bjar\b|\bcoins?\b|\bhow many\b|\baltogether\b|\bin all\b', line, re.IGNORECASE):
                    score += 4
                if re.search(r'\badd\b|\bput\b|\btotal\b|\bhas\b|\bstart with\b', line, re.IGNORECASE):
                    score += 2
                # word puzzles
                if re.search(r'\bremove\b|\bdelet\w*\b|\bdrop\b|\bstrip\b', line, re.IGNORECASE):
                    score += 4
                if re.search(r'\bfirst\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\blast\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bletter\w*\b|\bcharacter\w*\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\breverse\b|\bbackward\w*\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bword\b', line, re.IGNORECASE):
                    score += 2
                # Animal challenge: "pick the word that is an animal"
                if re.search(r'\banimal\b|\bcreature\b|\bbeast\b|\bliving\b.*\bthing\b|\bwhich\b.*\banimal\b', line, re.IGNORECASE):
                    score += 5
                # Knowledge questions (rooms, colors, counting, calendar...)
                if re.search(r'\broom\b|\bsink\b|\bkitchen\b|\bbedroom\b|\bbathroom\b', line, re.IGNORECASE):
                    score += 4
                if re.search(r'what (?:color|colour|room)|which (?:room|color)|color of|colour of', line, re.IGNORECASE):
                    score += 4
                if re.search(r'\blegs\b|\bwheels\b|\bhow many\b|\bhow much\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bmonth\b|\bseason\b|\bday\b|\bweek\b|\byear\b', line, re.IGNORECASE):
                    score += 2
                if re.search(r'\bsink\b|\bdishes\b|\bmoos?\b|\bquacks?\b|\bmeows?\b|\bbarks?\b|\bneighs?\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bcapital\b|\bfruit\b|\bvegetable\b|\binsect\b|\bwhich of these\b', line, re.IGNORECASE):
                    score += 3
                if re.search(r'\bused to\b|\buse .* to\b|\bwhat do you\b', line, re.IGNORECASE):
                    score += 3
                # numbers reinforce a real math question
                if re.search(r'\b\d+\b', line):
                    score += 2
                # Penalize pure instruction lines
                if re.search(r'^\s*(?:read|answer|respond|type|please|question)\b', line, re.IGNORECASE):
                    score -= 2
                if re.search(r'read and answer|answer with|respond with|single word', line, re.IGNORECASE):
                    score -= 4
                # Heavily penalize signup-form / non-captcha UI text
                # (happens when the accessibility iframe fails to load and
                #  the solver reads the parent page's signup form instead)
                if re.search(r'\b(?:email\*?|password\*?|username\*?|display\s+name|create\s+(?:an\s+)?account|sign\s*(?:up|in)|log\s*in|this is how others see you)\b', line, re.IGNORECASE):
                    score -= 15
                if re.search(r'\b(?:available|nice!|special characters|emoji)\b', line, re.IGNORECASE):
                    score -= 8

                if score > best_score:
                    best_score = score
                    best_line = line
                    best_source = source

        if best_line and best_score >= 4:
            # Picker questions ("pick the one that represents an animal") render
            # the candidate words on a SEPARATE line ("oar, glass, piglet").
            # Append that options line so the solver sees the candidates.
            if re.search(r'pick the one|pick the word|words below|represents|which of these|which one', best_line, re.IGNORECASE):
                extra = _find_options_line(best_source, all_texts, best_line)
                if extra:
                    best_line = best_line + ' : ' + extra
            log(f"[Accessibility] Scored question ({best_score}) from {best_source}: '{best_line[:160]}'")
            return best_line

        # Priority fallback: concatenated text from any source
        # BUT reject text that looks like a signup form or generic page UI
        # (not a captcha question). This prevents infinite-loop on pages
        # where the accessibility iframe fails to load a real challenge.
        _FORM_SIGNALS = re.compile(
            r'(?:email\*?|password\*?|username\*?|display\s+name|'
            r'create\s+(?:an\s+)?account|sign\s*(?:up|in)|log\s*in|'
            r'this is how others see you|nice!|special characters)',
            re.IGNORECASE
        )
        for source, text in all_texts:
            if len(text) > 10:
                if _FORM_SIGNALS.search(text):
                    log(f"[Accessibility] Raw text from {source} looks like a signup form — skipping",
                        level="warn")
                    continue
                log(f"[Accessibility] No scored question — returning raw text from {source}")
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
            # SMART: split by sentences, only sum numbers from sentences
            # about the jar owner (you/your/jar/put/add). Ignores numbers
            # from other people (friend/they/he/she).
            sentences = re.split(r'[.!?]', orig)
            own_nums = []
            for sent in sentences:
                s_lower = sent.lower().strip()
                if any(w in s_lower for w in ('you', 'your', 'jar', "you're", 'put', 'add', 'placed')):
                    own_nums.extend(re.findall(r'(\d+)', sent))
                elif any(w in s_lower for w in ('friend', 'they', 'he', 'she', 'them', 'brother', 'sister')):
                    continue
                else:
                    own_nums.extend(re.findall(r'(\d+)', sent))
            if not own_nums:
                own_nums = re.findall(r'(\d+)', orig)
            if len(own_nums) >= 1:
                total = sum(int(n) for n in own_nums)
                log(f"[Accessibility] Coin/jar smart sum: {'+'.join(own_nums)} = {total}")
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
        # TIGHTER: require that "first" AND "last" are followed by
        # "letter"/"character" within a few words. Prevents matching
        # "remove the first item from the list and the last" (no letter).
        word_pat = re.compile(
            r'(?:remov(?:e|es|ed|ing)?|delet(?:e|es|ed|ing)?|drop|strip|take)\s+(?:out\s+)?(?:the\s+)?'
            r'(?:first|1st)\s+(?:letter|character|char)s?\s+(?:and|&)\s+(?:the\s+)?'
            r'(?:last)\s+(?:letter|character|char)s?',
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

        # ── ANIMAL WORD puzzle: given 3 words, pick the animal ──
        # "seal, trash, bucket" → seal is the animal
        animal_pat = re.search(
            r'(?:animal|creature|beast|living\s+thing|which\s+one\s+is)',
            t, re.IGNORECASE
        )
        if animal_pat:
            # Extract all words from the text (3+ letters)
            words = re.findall(r'\b([a-zA-Z]{3,})\b', orig)
            # Generic category words ("animal", "creature", "one"...) are NOT
            # answers — the real candidates are the option words. Exclude them.
            generic = {'animal', 'creature', 'beast', 'living', 'thing', 'which',
                       'one', 'pick', 'the', 'that', 'from', 'words', 'below',
                       'represents', 'an', 'and', 'are', 'is', 'of', 'these',
                       'them', 'with', 'you', 'your', 'following', 'any'}
            candidates = [w for w in words if w.lower() in ANIMAL_WORDS and w.lower() not in generic]
            if candidates:
                log(f"[Accessibility] Animal challenge: candidates={candidates} from {words}")
                return candidates[0]
            # Broader: normalize and check against ANIMAL_WORDS
            for w in words:
                if w.lower() in ANIMAL_WORDS and w.lower() not in generic:
                    return w

        # ── KNOWLEDGE QUESTIONS (rooms, colors, animal sounds, counting...) ──
        # Runs before number extraction so "how many legs" etc. hit the KB.
        knowledge_ans = _solve_knowledge_question(text)
        if knowledge_ans is not None:
            log(f"[Accessibility] Knowledge answer: {knowledge_ans}")
            return knowledge_ans

        # ── Semantic fallback: topic keywords match any phrasing ──
        semantic_ans = _solve_semantic(text)
        if semantic_ans is not None:
            log(f"[Accessibility] Semantic answer: {semantic_ans}")
            return semantic_ans

        # ── PURE NUMBER extraction (e.g. "type the number 42") ──
        num_pat = re.search(r'(?:number|digit|num)\s+[iof]*\s*(\d+)', t)
        if num_pat:
            return num_pat.group(1)

        # ── Just a number? ──
        lone_num = re.search(r'^\s*(\d+)\s*$', t)
        if lone_num:
            return lone_num.group(1)

        return None

    async def _vision_answer(hcaptcha, question_text: str = "") -> str:
        """Screenshot the challenge and ask the VISION model (moondream).
        moondream is a vision model — it reads questions from images far
        better than from raw text. This is the path that actually works."""
        try:
            b64 = await _screenshot_question(hcaptcha)
            if not b64:
                log("[Accessibility] Vision: empty screenshot", level="warn")
                return ""
            prompt = (
                "This is an hCaptcha accessibility challenge. Read the question "
                "shown in this image and answer it with exactly one word, number, "
                "or short phrase. No punctuation, no explanation, lowercase."
            )
            if question_text:
                prompt += "\nThe question is about: " + question_text[:200]
            raw = await _ollama_chat(b64, prompt, timeout=60.0)
            cleaned = _clean_llm_answer(raw)
            if cleaned:
                log(f"[Accessibility] Vision model answered: {cleaned}")
            else:
                log(f"[Accessibility] Vision model returned nothing (raw='{raw[:60]}')", level="warn")
            return cleaned
        except Exception as e:
            log(f"[Accessibility] Vision error: {e}", level="warn")
            return ""

    async def _get_answer(hcaptcha, q: int) -> Optional[str]:
        """Get the answer with 3 layers:
        Layer 1: regex patterns (513)   — exact phrasings
        Layer 2: semantic topic table   — any phrasing containing topics
        Layer 3: LLM fallback           — ANY unknown question → Ollama / OpenAI-compatible"""
        text = await _read_question_text()
        log(f"[Accessibility] Q{q} text: '{text[:200]}'")
        if text:
            # ── Layers 1+2: local KB ──
            local = _solve_text_question(text)
            if local is not None:
                log(f"[Accessibility] Q{q} solved locally: {local}")
                return local

            # ── Layer 3: unknown question → LLM ──
            log(f"[Accessibility] Q{q} UNKNOWN question (no local match) — calling Layer 3 LLM", level="warn")
            # moondream is a VISION model — try the screenshot first (it reads
            # the question from the image), then text chat as backup.
            if ollama_url:
                vans = await _vision_answer(hcaptcha, text)
                if vans:
                    log(f"[Accessibility] Q{q} Layer 3 vision answered: {vans}")
                    return vans
            ans = await _llm_answer_question(text)
            if ans:
                log(f"[Accessibility] Q{q} Layer 3 LLM answered: {ans}")
                return ans
            log(f"[Accessibility] Q{q} Layer 3 could not answer either", level="error")
        else:
            log(f"[Accessibility] Q{q} NO TEXT FOUND — trying vision directly", level="warn")
            if ollama_url:
                vans = await _vision_answer(hcaptcha)
                if vans:
                    log(f"[Accessibility] Q{q} vision answered without text: {vans}")
                    return vans
            log(f"[Accessibility] Q{q} NO TEXT FOUND anywhere", level="error")
        return None

    async def _type_answer(hcaptcha, answer: str) -> bool:
        """Type answer into the hCaptcha accessibility input.
        Uses JS injection (via _challenge_js which scans ALL frames) as primary
        approach - more reliable than Playwright locators on deeply nested iframes."""
        escaped = answer.replace("\\", "\\\\").replace("'", "\\'")

        # Primary: JS injection - find visible input, set value, fire events
        js_set = (
            "() => {"
            "const inputs = document.querySelectorAll("
            "'input:not([type=\"hidden\"]), textarea, [role=\"textbox\"], [contenteditable=\"true\"]'"
            ");"
            "for (const inp of inputs) {"
            "if (inp.offsetParent !== null) {"
            "inp.focus();"
            "inp.value = '';"
            "inp.value = '" + escaped + "';"
            "inp.dispatchEvent(new Event('input', { bubbles: true }));"
            "inp.dispatchEvent(new Event('change', { bubbles: true }));"
            "return 'ok:' + inp.tagName;"
            "}"
            "}"
            "return null;"
            "}"
        )
        try:
            result = await _challenge_js(js_set)
            if result and 'ok' in str(result):
                log(f"[Accessibility] JS-set '{answer}' ({result})")
                return True
        except Exception as e:
            log(f"[Accessibility] JS-set error: {e}")

        # Fallback 1: get_by_role (works on newer Playwright)
        try:
            inp = hcaptcha.get_by_role("textbox", name="Challenge Text Input").first
            await inp.wait_for(state="visible", timeout=3000)
            await inp.click()
            await inp.fill("")
            await inp.type(answer, delay=30)
            log(f"[Accessibility] Typed '{answer}' via get_by_role")
            return True
        except Exception:
            pass

        # Fallback 2: keyboard type (click visible input first)
        try:
            inp = hcaptcha.locator(
                "input:not([type='hidden']), textarea, [role='textbox']"
            ).first
            await inp.click()
            await asyncio.sleep(0.2)
            await page.keyboard.press("Control+a")
            await page.keyboard.type(answer, delay=50)
            log(f"[Accessibility] Typed '{answer}' via keyboard")
            return True
        except Exception:
            pass

        # Fallback 3: brute-force page-level keyboard
        try:
            await page.keyboard.type(answer, delay=50)
            log(f"[Accessibility] Typed '{answer}' via brute-force keyboard")
            return True
        except Exception:
            pass

        return False

    async def _submit_answer(hcaptcha) -> bool:
        """Click Next / Submit on the accessibility challenge.
        Primary: JS injection via _challenge_js (works on nested iframes).
        The 3s wait avoids the Skip button which shares coordinates."""
        log("[Accessibility] Waiting 3s before clicking Next (avoid Skip)")
        await asyncio.sleep(3)

        # ── Primary: JS injection — click the visible action button ──
        js_click = r"""() => {
            const names = ['Next', 'Submit', 'Verify', 'Continue', 'OK', 'Done'];
            const btns = document.querySelectorAll('button, [role="button"]');
            for (const b of btns) {
                const txt = (b.textContent || '').trim().toLowerCase();
                const label = ((b.getAttribute('aria-label') || '') + ' ' + txt).toLowerCase();
                for (const n of names) {
                    if (label.includes(n.toLowerCase()) && b.offsetParent !== null) {
                        b.click();
                        return 'clicked:' + n;
                    }
                }
            }
            // Fallback: any visible primary submit button
            for (const b of btns) {
                const t = (b.getAttribute('type') || '').toLowerCase();
                if (t === 'submit' && b.offsetParent !== null) {
                    b.click();
                    return 'clicked:submit';
                }
            }
            return null;
        }"""
        try:
            result = await _challenge_js(js_click)
            if result and 'clicked' in str(result):
                log(f"[Accessibility] Submitted via JS click ({result})")
                return True
        except Exception as e:
            log(f"[Accessibility] JS click error: {e}")

        # ── Fallback 1: get_by_role ──
        for name in ("Next", "Submit", "Verify", "Continue", "OK"):
            try:
                btn = hcaptcha.get_by_role("button", name=name).first
                await btn.wait_for(state="visible", timeout=2000)
                await btn.click(timeout=2000)
                log(f"[Accessibility] Submitted via get_by_role {name}")
                return True
            except Exception:
                continue

        # ── Fallback 2: selector-based (aria-label + type) ──
        for btn_sel in [
            'button[aria-label="Next"]',
            'button[aria-label="Submit"]',
            'button[aria-label="Verify"]',
            'button[type="button"]',
            'button[type="submit"]',
            'button:has-text("Next")',
            'button:has-text("Submit")',
            'button:has-text("Verify")',
            'button:has-text("OK")',
            'button:has-text("Continue")',
        ]:
            try:
                await hcaptcha.locator(btn_sel).first.click(timeout=2000)
                log(f"[Accessibility] Submitted via {btn_sel}")
                return True
            except Exception:
                pass

        # ── Fallback 3: Enter key ──
        try:
            inp = hcaptcha.locator("input:not([type='hidden']), textarea").first
            await inp.press("Enter", timeout=2000)
            log("[Accessibility] Submitted via Enter")
            return True
        except Exception:
            pass

        # ── Fallback 4: coordinate click on iframe body ──
        # Next button is reliably at the bottom-right of the iframe
        try:
            btn = hcaptcha.locator(
                'button[aria-label="Next"], button:has-text("Next")'
            ).first
            box = await btn.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await page.mouse.click(cx, cy)
                log(f"[Accessibility] Submitted via coordinate click ({int(cx)},{int(cy)})")
                return True
        except Exception:
            pass
        # Ultimate fallback: click frame body bottom-right
        try:
            box = await hcaptcha.locator("body").first.bounding_box()
            if box:
                cx = box["x"] + box["width"] - 60
                cy = box["y"] + box["height"] - 40
                await page.mouse.click(cx, cy)
                log("[Accessibility] Submitted via frame bottom-right click")
                return True
        except Exception:
            pass

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

            # ── Captcha-chain loop: keep solving until iframe disappears ──
            # hCaptcha can throw multiple challenges in a row after each
            # set of accessibility questions. Solve them all.
            chain_attempt = 0
            while True:
                chain_attempt += 1
                if chain_attempt > 4:
                    log("[Accessibility] Too many captcha chains — aborting",
                        level="warn")
                    break
                if chain_attempt > 1:
                    log(f"[Accessibility] NEW captcha detected — chain #{chain_attempt}")
                    # Re-open accessibility for the new captcha
                    if not await _open_accessibility_challenge(hcaptcha):
                        log("[Accessibility] Could not re-open accessibility for chain",
                            level="warn")
                        break
                    await asyncio.sleep(1.5)

                # ── Answer every question in this chain ──
                # Track recent Q texts & answers to detect infinite loops
                # (same non-question text being answered repeatedly)
                _prev_texts = []
                _prev_answers = []
                for q in range(1, max_questions + 1):
                    if await _token_present():
                        log("[Accessibility] Token appeared mid-chain!")
                        break

                    answer = await _get_answer(hcaptcha, q)
                    # ── Duplicate detection: if we get the same question
                    # text 3+ times in a row, the page isn't showing real
                    # captcha challenges — abort this chain.
                    if answer:
                        cur_text = await _read_question_text()
                        _prev_texts.append(cur_text[:200])
                        _prev_answers.append(answer)
                        if len(_prev_texts) >= 3:
                            unique_texts = set(_prev_texts[-3:])
                            unique_ans = set(a for a in _prev_answers[-3:] if a)
                            if len(unique_texts) <= 1 and len(unique_ans) <= 1:
                                log("[Accessibility] Same question+answer repeated 3x — "
                                    "page isn't showing real challenges, aborting chain",
                                    level="warn")
                                break
                    if answer is None:
                        log(f"[Accessibility] Q{q}: No answer", level="warn")
                        break

                    log(f"[Accessibility] Q{q} solved: {answer}")

                    if not await _type_answer(hcaptcha, answer):
                        log("[Accessibility] Could not type answer", level="warn")
                        break

                    await asyncio.sleep(0.8)

                    if not await _submit_answer(hcaptcha):
                        log("[Accessibility] Could not submit", level="warn")
                        break

                    # Wait for Next→new question transition
                    await asyncio.sleep(2.0)

                    # Check if token appeared (captcha complete)
                    if await _token_present():
                        log(f"[Accessibility] Token appeared after Q{q}!")
                        break

                # ── After answering all questions, check if captcha is gone ──
                await asyncio.sleep(2.0)
                if await _token_present():
                    log("[Accessibility] Token present — checking for new captcha...")
                    await asyncio.sleep(3.0)
                    if await _token_present():
                        # Still have token, but is there a NEW iframe?
                        try:
                            new_iframe = page.locator('iframe[src*="hcaptcha.com"]')
                            if await new_iframe.count() == 0:
                                log("[Accessibility] [OK] No more captchas — done!")
                                return True
                            log("[Accessibility] Captcha iframe still present — new challenge!")
                        except Exception:
                            log("[Accessibility] [OK] No captcha iframe found — done!")
                            return True
                    else:
                        # Token disappeared — maybe page transitioned
                        log("[Accessibility] Token disappeared — page may have advanced")
                        return True
                else:
                    log(f"[Accessibility] No token after Q{q} — more questions or retry")

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
