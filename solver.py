#!/usr/bin/env python3
"""
hCaptcha Universal Solver — Free Edition
=========================================
Combines all free tactics from reference implementations:
  · curl_cffi for TLS fingerprinting (mimics Chrome)
  · Playwright for HSW token generation
  · Synthetic motion data (no multibot.in)
  · ResNet18 classifier for tile grids
  · OpenCV template matcher for drag puzzles
  · Direct API calls — no browser clicking

Requirements:
  pip install curl_cffi playwright opencv-python numpy pillow torch torchvision
  python -m playwright install chromium

Usage:
  python solver.py --sitekey a9b5fb07-92ff-493f-86fe-352a2803b3df --host discord.com
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import time
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import cv2
import numpy as np
from curl_cffi import requests as cffi_requests
from PIL import Image
from playwright.async_api import async_playwright

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

# Realistic screen sizes for motion data
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

# ═══════════════════════════════════════════════════════════════
# TLS Session (curl_cffi — free Chrome fingerprint)
# ═══════════════════════════════════════════════════════════════

def make_session(proxy: Optional[str] = None) -> cffi_requests.Session:
    """Create a TLS session that mimics Chrome 130."""
    s = cffi_requests.Session()
    s.headers.update({
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
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
# Synthetic Motion Data Generator (free — no multibot.in)
# ═══════════════════════════════════════════════════════════════

class MotionData:
    """
    Generates realistic fake motion data in the exact JSON format
    hCaptcha expects.  No API.  Just math.
    """

    def __init__(self):
        self.base_ms = int(time.time() * 1000)
        self.screen_w, self.screen_h = random.choice(SCREEN_SIZES)
        self.color_depth = random.choice(COLOR_DEPTHS)
        self.cores = random.choice(CORE_COUNTS)
        self.lang, self.langs = random.choice(LANGUAGES)
        self.counter = 0

    def _tick(self, ms: int = 0) -> int:
        self.counter += ms or random.randint(8, 25)
        return self.base_ms + self.counter

    def _human_path(self, start: Tuple[int, int], end: Tuple[int, int],
                    points: int = 30) -> List[List[int]]:
        """Generate a curved, human-like mouse path between two points."""
        path = []
        sx, sy = start
        ex, ey = end
        for i in range(points):
            t = i / (points - 1)
            # Bezier-like curve with noise
            cx = sx + (ex - sx) * 0.4 + random.randint(-8, 8)
            cy = sy + (ey - sy) * 0.3 + random.randint(-6, 6)
            x = int((1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex)
            y = int((1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey)
            # Add micro-jitter
            x += random.randint(-1, 1)
            y += random.randint(-1, 1)
            ts = self._tick(6 + random.randint(0, 8))
            path.append([x, y, ts])
        return path

    def get_captcha_motion(self) -> dict:
        """Generate motion data for the 'get captcha' request."""
        # Widget positioned randomly in the viewport
        widget_x = random.randint(0, self.screen_w - 310)
        widget_y = random.randint(0, self.screen_h - 85)

        # Start position (somewhere on screen)
        start = (random.randint(300, self.screen_w - 300),
                 random.randint(100, self.screen_h - 200))
        # End position (center of the checkbox)
        end_center = (widget_x + 16 + 14, widget_y + 23 + 14)

        path = self._human_path(start, end_center)

        # Make coords relative to widget
        mm = [[x - widget_x, y - widget_y, t] for x, y, t in path]
        periods = [(mm[i + 1][2] - mm[i][2]) for i in range(len(mm) - 1)]
        avg_period = sum(periods) / len(periods) if periods else 0

        data = {
            "st": self.base_ms,
            "mm": mm,
            "mm-mp": avg_period,
            "md": [mm[-1][:2] + [self._tick(50)]],
            "md-mp": 0,
            "mu": [mm[-1][:2] + [self._tick(100)]],
            "mu-mp": 0,
            "v": 1,
            "topLevel": self._top_level(widget_x, widget_y, start),
            "session": [],
            "widgetList": ["0" + "".join(random.choices("abcdef0123456789", k=10))],
            "widgetId": "0" + "".join(random.choices("abcdef0123456789", k=10)),
            "href": "https://discord.com/",
            "prev": {
                "escaped": False,
                "passed": False,
                "expiredChallenge": False,
                "expiredResponse": False,
            },
        }
        return data

    def get_check_motion(self) -> dict:
        """Generate motion data for 'check captcha' (submission)."""
        widget_x = random.randint(0, self.screen_w - 310)
        widget_y = random.randint(0, self.screen_h - 85)
        start = (random.randint(300, self.screen_w - 300),
                 random.randint(100, self.screen_h - 200))
        end_center = (widget_x + 16 + 14, widget_y + 23 + 14)

        path = self._human_path(start, end_center)
        mm = [[x - widget_x, y - widget_y, t] for x, y, t in path]
        periods = [(mm[i + 1][2] - mm[i][2]) for i in range(len(mm) - 1)]
        avg_period = sum(periods) / len(periods) if periods else 0

        data = {
            "st": self.base_ms,
            "mm": mm,
            "mm-mp": avg_period,
            "md": [mm[-1][:2] + [self._tick(50)]],
            "md-mp": 0,
            "mu": [mm[-1][:2] + [self._tick(100)]],
            "mu-mp": 0,
            "v": 1,
            "topLevel": self._top_level(widget_x, widget_y, start),
            "session": [],
            "widgetList": [],
            "widgetId": "",
            "href": "https://discord.com/",
            "prev": {
                "escaped": False,
                "passed": False,
                "expiredChallenge": False,
                "expiredResponse": False,
            },
        }
        return data

    def _top_level(self, widget_x, widget_y, start) -> dict:
        """Browser fingerprint data (navigator, screen, plugins)."""
        taskbar = random.choice([0, 30, 40, 48])
        avail_h = max(1, self.screen_h - taskbar)

        # Path from edge of screen to widget vicinity
        start = (0, random.randint(100, self.screen_h - 200))
        end = (widget_x + random.randint(10, 280),
               widget_y + random.randint(10, 60))
        mm = self._human_path(start, end, 20)

        return {
            "inv": False,
            "st": self.base_ms - random.randint(200, 800),
            "sc": {
                "availWidth": self.screen_w,
                "availHeight": avail_h,
                "width": self.screen_w,
                "height": self.screen_h,
                "colorDepth": self.color_depth,
                "pixelDepth": self.color_depth,
                "top": 0, "left": 0,
                "availTop": 0, "availLeft": 0,
            },
            "nv": {
                "vendor": "Google Inc.",
                "vendorSub": "",
                "cookieEnabled": True,
                "webdriver": False,
                "hardwareConcurrency": self.cores,
                "userAgent": CHROME_UA,
                "language": self.lang,
                "languages": self.langs,
                "onLine": True,
                "doNotTrack": None,
                "maxTouchPoints": 0,
                "pdfViewerEnabled": True,
                "plugins": ["internal-pdf-viewer"] if random.random() > 0.3 else [],
            },
            "dr": "",
            "exec": False,
            "wn": [[self.screen_w, self.screen_h, 1, self.base_ms - 500]],
            "wn-mp": 0,
            "xy": [[0, 0, 1, self.base_ms - 500]],
            "xy-mp": 0,
            "mm": mm,
            "mm-mp": sum((mm[i+1][2]-mm[i][2]) for i in range(len(mm)-1)) / max(len(mm)-1, 1),
        }


# ═══════════════════════════════════════════════════════════════
# HSW Token Generator (Playwright — free, no Camoufox needed)
# ═══════════════════════════════════════════════════════════════

class HSWGenerator:
    """
    Generates the HSW (proof-of-work) token hCaptcha requires.
    Uses Playwright to evaluate hCaptcha's JavaScript in a real browser context.
    """

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

        # Decode the req token to get the hsw.js URL
        # The req token is a JWT with an "l" claim pointing to the hsw.js location
        try:
            import base64
            payload = req_token.split(".")[1]
            # Add padding
            payload += "=" * (4 - len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            hsw_url = f"https://newassets.hcaptcha.com{decoded['l']}/hsw.js"
        except Exception:
            hsw_url = f"https://newassets.hcaptcha.com/c/{self.version}/hsw.js"

        resp = session.get(hsw_url)
        self._hsw_js = resp.text

    async def _get_page(self):
        """Launch Playwright browser if not already running."""
        if self._browser is None:
            pw = await async_playwright().start()
            launch_args = {
                "headless": True,
                "args": [
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    # Disable CSP enforcement entirely so hsw.js WASM runs
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
        """Generate an HSW token for the given req token."""
        await self._ensure_js(session, req_token)
        await self._get_page()

        page = await self._context.new_page()

        try:
            # Navigate to a BLANK page on the target host.
            # Intercept EVERY request (Discord redirects / → /login, so a
            # single-path pattern misses) and serve empty HTML. Combined with
            # --disable-web-security, this guarantees no CSP blocks hsw.js.
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<html><head></head><body></body></html>",
                ),
            )
            await page.goto(f"https://{self.host}/", wait_until="domcontentloaded", timeout=10000)

            # Inject hsw.js
            await page.evaluate(self._hsw_js)

            # Verify hsw function exists
            max_wait = 30
            for _ in range(max_wait):
                try:
                    has = await page.evaluate("typeof hsw === 'function'")
                    if has:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.02)

            # Call hsw(req)
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
# Tile Grid Classifier (ResNet18 — your trained model)
# ═══════════════════════════════════════════════════════════════

class TileClassifier:
    """
    Loads model.pth from training and classifies tiles.
    If no model.pth found, falls back to a heuristic edge-detection approach.
    """

    CLASSES = ["bicycle", "bus", "motorcycle", "truck", "train",
               "cat", "dog", "bird", "car", "airplane", "boat", "traffic light"]

    def __init__(self, model_path: Optional[str] = None):
        self.model = None
        self.use_model = False
        if model_path and Path(model_path).exists():
            try:
                import torch
                from torchvision import models, transforms
                raw = torch.load(model_path, map_location="cpu", weights_only=False)
                # Support both raw state_dict and the mega-trainer's
                # {"state_dict": ..., "classes": [...]} wrapped format
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
                    transforms.Resize(256),
                    transforms.CenterCrop(224),
                    transforms.ToTensor(),
                    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
                ])
                self.use_model = True
                print(f"  Loaded model from {model_path} ({len(self.CLASSES)} classes)")
            except Exception as e:
                print(f"  Model load failed ({e}) — using heuristic fallback")

    def classify(self, img_bytes: bytes) -> str:
        """Classify a single tile image. Returns class name."""
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

# Need io for BytesIO
import io


# ═══════════════════════════════════════════════════════════════
# Drag Solver (OpenCV template matching — from drag_solver.py)
# ═══════════════════════════════════════════════════════════════

def solve_drag(piece_bytes: bytes, bg_bytes: bytes) -> Tuple[int, int, float]:
    """
    Given puzzle piece and background images, return (target_x, target_y, confidence).
    Multi-scale, multi-method, edge-aware matching.
    """
    piece = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_COLOR)
    bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)

    if piece is None or bg is None:
        return 0, 0, 0.0

    ph, pw = piece.shape[:2]
    bh, bw = bg.shape[:2]

    if ph < 8 or pw < 8 or ph > bh or pw > bw:
        return bw // 2, bh // 2, 0.0

    # Edge maps for robustness
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

        for method_name, method_val, tmpl, tgt in [
            ("color", cv2.TM_CCOEFF_NORMED, sp, bg),
            ("color2", cv2.TM_CCORR_NORMED, sp, bg),
            ("edge", cv2.TM_CCOEFF_NORMED, sp_edges, bg_edges),
        ]:
            result = cv2.matchTemplate(tgt, tmpl, method_val)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            conf = max_val * 100
            if conf > best_conf:
                best_conf = conf
                best_x, best_y = max_loc
    return best_x, best_y, best_conf


# ═══════════════════════════════════════════════════════════════
# Main Solver
# ═══════════════════════════════════════════════════════════════

class HCaptchaSolver:
    """
    Universal hCaptcha solver.
    Handles: tile grid, drag puzzle, area select.
    No paid APIs.  No browser clicking.  Direct API calls.
    """

    def __init__(self, sitekey: str, host: str, proxy: Optional[str] = None,
                 model_path: Optional[str] = None):
        self.sitekey = sitekey
        self.host = host.split("//")[-1].split("/")[0]
        self.proxy = proxy
        self.session = make_session(proxy)
        self.motion = MotionData()
        self.classifier = TileClassifier(model_path)

        resp = self.session.get("https://hcaptcha.com/1/api.js",
                                params={"render": "explicit"})
        versions = re.findall(r"v1/([A-Za-z0-9]+)/static", resp.text)
        self.version = versions[1] if len(versions) > 1 else DEFAULT_VERSION
        print(f"  hCaptcha v{self.version[:8]}...")

    def get_config(self) -> Optional[dict]:
        """Step 1: checksiteconfig"""
        params = {
            "v": self.version,
            "sitekey": self.sitekey,
            "host": self.host,
            "sc": "1",
            "swa": "1",
            "spst": "1",
        }
        resp = self.session.post(f"{HCAPTCHA_API}/checksiteconfig", params=params)
        if resp.status_code != 200:
            print(f"  checksiteconfig failed: {resp.status_code}")
            return None
        return resp.json()

    async def fetch_challenge(self, config: dict,
                               hsw: HSWGenerator) -> Optional[dict]:
        """Step 3: getcaptcha"""
        req = config["c"]["req"]
        token = await hsw.generate(self.session, req)
        if not token:
            print("  HSW token failed")
            return None

        data = {
            "v": self.version,
            "sitekey": self.sitekey,
            "host": self.host,
            "hl": "en-US",
            "motionData": json.dumps(self.motion.get_captcha_motion()),
            "n": token,
            "c": json.dumps(config["c"]),
        }
        resp = self.session.post(
            f"{HCAPTCHA_API}/getcaptcha/{self.sitekey}", data=data)
        if resp.status_code != 200:
            print(f"  getcaptcha failed: {resp.status_code}")
            return None
        return resp.json()

    def solve_tile_grid(self, challenge: dict) -> dict:
        """Classify tiles using ResNet18 and return selected indices."""
        tasklist = challenge.get("tasklist", [])
        question = challenge.get("requester_question", {}).get("en", "")

        # Determine what we're looking for
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

            # Download tile image
            try:
                resp = self.session.get(img_url)
                img_bytes = resp.content
            except Exception:
                continue

            cls = self.classifier.classify(img_bytes)

            if target_class and cls == target_class:
                selected[task["task_key"]] = "true"
            elif not target_class:
                # No target found in text — use majority voting later
                selected[task["task_key"]] = "true" if i < 2 else "false"
            else:
                selected[task["task_key"]] = "false"

        print(f"  Target: {target_class or 'unknown'} → {sum(1 for v in selected.values() if v == 'true')} tiles selected")
        return selected

    def solve_drag(self, challenge: dict) -> dict:
        """Match puzzle piece to background and return coordinates."""
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
        """Step 5: checkcaptcha"""
        req = challenge["c"]["req"]
        token = await hsw.generate(self.session, req)
        if not token:
            return None

        endpoint = f"{HCAPTCHA_API}/checkcaptcha/{self.sitekey}/{challenge['key']}"
        payload = json.dumps({
            "v": self.version,
            "sitekey": self.sitekey,
            "serverdomain": self.host,
            "job_mode": challenge["request_type"],
            "motionData": json.dumps(self.motion.get_check_motion()),
            "n": token,
            "c": json.dumps(challenge["c"]),
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
        """Main solve loop."""
        config = self.get_config()
        if not config:
            return {"success": False, "error": "Config failed"}

        # Check for passive pass
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

                # Passive pass?
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
                    # Fallback: click center of each task area
                    answers = {}
                    for task in challenge.get("tasklist", []):
                        answers[task["task_key"]] = [{
                            "entity_name": 0,
                            "entity_type": "default",
                            "entity_coords": [200, 150],
                        }]
                else:
                    print(f"  ⚠️  Unsupported type: {req_type}")
                    continue

                result = await self.submit(challenge, answers, hsw)
                if result and result.get("success"):
                    elapsed = time.time() - start
                    print(f"  ✅ Solved! ({elapsed:.1f}s)")
                    await hsw.close()
                    return {"success": True, "token": result.get("token", ""),
                            "time": elapsed}
                else:
                    error = result.get("error", "unknown") if result else "none"
                    print(f"  ❌ Rejected: {error}")
                    # Refresh config between attempts
                    config = self.get_config()

            await hsw.close()
            return {"success": False, "error": f"Max {max_attempts} attempts",
                    "time": time.time() - start}
        except Exception as e:
            await hsw.close()
            return {"success": False, "error": str(e),
                    "time": time.time() - start}


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="hCaptcha Universal Solver")
    parser.add_argument("--sitekey", default="a9b5fb07-92ff-493f-86fe-352a2803b3df",
                        help="hCaptcha sitekey (default: Discord)")
    parser.add_argument("--host", default="discord.com", help="Target host")
    parser.add_argument("--proxy", help="HTTP proxy URL")
    parser.add_argument("--model", default="model.pth", help="Path to trained model.pth")
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
