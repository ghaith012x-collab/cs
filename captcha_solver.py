"""
hCaptcha Master Solver — supports ALL challenge types:
- GRID: click matching image tiles (Moondream tile-by-tile YES/NO)
- DRAG: drag object to target (DOM extraction + Moondream vision + heuristics)
- SLIDER: puzzle slider (OpenCV template matching)

All queries use small images (max 224px) for fast CPU inference on Moondream.
"""

import asyncio
import base64
import io
import json
import math
import os
import random
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
from pathlib import Path
from PIL import Image
from playwright.async_api import Page
import aiohttp


@dataclass
class SolverConfig:
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = field(default_factory=lambda: os.environ.get(
        "OLLAMA_MODEL", "moondream"
    ))
    ollama_timeout: int = 30
    ollama_num_ctx: int = 2048
    ollama_temperature: float = 0.0
    max_rounds: int = 2


# ── Helpers ──────────────────────────────────────────────

def _resize(img: Image.Image, max_dim: int = 224) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    s = max_dim / max(w, h)
    return img.resize((int(w * s), int(h * s)), Image.LANCZOS)


def _b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


async def _iframe_b64(iframe, max_dim: int = 320) -> Optional[str]:
    try:
        raw = await iframe.screenshot()
        if not raw or len(raw) < 100:
            return None
        img = Image.open(io.BytesIO(raw))
        return _b64(_resize(img, max_dim))
    except Exception:
        return None


# ── Ollama Client ─────────────────────────────────────────

class OllamaClient:
    _session: Optional[aiohttp.ClientSession] = None

    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.base_url = config.ollama_base_url
        self.model = config.ollama_model
        self.timeout = config.ollama_timeout
        self.num_ctx = config.ollama_num_ctx
        self.temp = config.ollama_temperature
        self._log = log or (lambda msg, level="info": None)

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    async def ask(self, prompt: str, images: List[str], max_tokens: int = 300) -> str:
        s = await self._session_get()
        kb = sum(len(i) for i in images) // 1024
        self._log(f"[Ollama] {self.model} | {len(images)} img ~{kb}KB | asking...")
        try:
            async with s.post(f"{self.base_url}/api/generate", json={
                "model": self.model, "prompt": prompt, "images": images,
                "stream": False,
                "options": {"num_ctx": self.num_ctx, "temperature": self.temp, "num_predict": max_tokens}
            }) as r:
                if r.status == 200:
                    data = await r.json()
                    resp = (data.get("response", "") or "").strip()
                    if resp:
                        self._log(f"[Ollama] → {resp[:300]}")
                    return resp
                err = (await r.text())[:200]
                self._log(f"[Ollama] HTTP {r.status}: {err}", level="error")
                return ""
        except asyncio.TimeoutError:
            self._log(f"[Ollama] TIMEOUT {self.timeout}s", level="error")
            return ""
        except Exception as e:
            self._log(f"[Ollama] {e}", level="error")
            return ""

    async def ask_retry(self, prompt: str, images: List[str], max_tokens: int = 300) -> str:
        for a in range(2):
            r = await self.ask(prompt, images, max_tokens)
            if r:
                return r
            await asyncio.sleep(1)
        return ""

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ── Human Mouse ───────────────────────────────────────────

class Mouse:
    @staticmethod
    def _jerk(t): return 10*t**3 - 15*t**4 + 6*t**5

    @staticmethod
    def _path(sx, sy, ex, ey):
        d = math.hypot(ex - sx, ey - sy)
        n = max(18, min(80, int(d / 4)))
        p = []
        for i in range(n):
            t = i / (n - 1)
            p.append((sx + (ex - sx) * Mouse._jerk(t) + random.gauss(0, 0.5),
                      sy + (ey - sy) * Mouse._jerk(t) + random.gauss(0, 0.5)))
        return p

    @staticmethod
    async def click(page, tx, ty):
        sx, sy = tx + random.uniform(-20, 20), ty + random.uniform(-20, 20)
        for x, y in Mouse._path(sx, sy, tx, ty):
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.003, 0.008))
        await asyncio.sleep(random.uniform(0.02, 0.05))
        await page.mouse.click(tx, ty)

    @staticmethod
    async def drag(page, sx, sy, ex, ey):
        for x, y in Mouse._path(sx + random.uniform(-20, 20), sy + random.uniform(-20, 20), sx, sy):
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.003, 0.006))
        await asyncio.sleep(0.05)
        await page.mouse.down()
        await asyncio.sleep(0.03)
        path = Mouse._path(sx, sy, ex, ey)
        for x, y in path:
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.004, 0.008))
        await asyncio.sleep(0.05)
        await page.mouse.up()
        await asyncio.sleep(0.1)


# ── Verifier ──────────────────────────────────────────────

class Verifier:
    def __init__(self, page: Page):
        self.page = page

    async def solved(self) -> bool:
        tok = await self.page.evaluate(
            "()=>{const t=document.querySelector('textarea[name=\"h-captcha-response\"]');return t&&t.value&&t.value.length>20?t.value:''}"
        )
        if tok:
            return True
        try:
            u = self.page.url
            if any(k in u for k in ['channels', 'app', 'verify', 'welcome']):
                return True
        except:
            pass
        return False


# ── Visual Muscle Memory ────────────────────────────────
# Stores cropped target images from successful drag solves.
# On repeat encounters, OpenCV template matching finds the target
# instantly — no AI needed.

TEMPLATE_DIR = "_templates"
TEMPLATE_MEMORY: Dict[str, dict] = {}
# Lazy load templates from disk
_TEMPLATES_LOADED = False


def _save_template(key: str, img: Image.Image):
    """Save a template image to memory and disk."""
    import time as _time
    arr = np.array(img.convert("L"))  # Grayscale for matching
    TEMPLATE_MEMORY[key] = {"img": arr, "size": img.size, "time": _time.time()}
    # Save to disk for persistence
    try:
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        fpath = os.path.join(TEMPLATE_DIR, f"{key}.png")
        img.save(fpath)
    except:
        pass


def _load_templates():
    """Load saved templates from disk."""
    global _TEMPLATES_LOADED
    if _TEMPLATES_LOADED:
        return
    _TEMPLATES_LOADED = True
    try:
        if os.path.isdir(TEMPLATE_DIR):
            for fname in os.listdir(TEMPLATE_DIR):
                if fname.endswith(".png"):
                    key = fname[:-4]
                    img = Image.open(os.path.join(TEMPLATE_DIR, fname)).convert("L")
                    TEMPLATE_MEMORY[key] = {"img": np.array(img), "size": img.size, "time": 0}
    except:
        pass


# ── Iframe helpers ────────────────────────────────────────

IFRAME_SELS = [
    "iframe[src*='newassets.hcaptcha.com/captcha']",
    "iframe[src*='hcaptcha.com/captcha']",
    "iframe[title*='hCaptcha challenge']",
]


async def _find_iframe(page: Page, min_size: int = 80):
    best = None
    best_area = 0
    for sel in IFRAME_SELS:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            for i in range(n):
                box = await loc.nth(i).bounding_box()
                if box and box['width'] > min_size and box['height'] > min_size:
                    area = box['width'] * box['height']
                    if area > best_area:
                        best_area = area
                        best = (loc.nth(i), box)
        except:
            continue
    return best


async def _challenge_text(page: Page) -> str:
    for sel in IFRAME_SELS:
        try:
            f = page.frame_locator(sel)
            for s in [".challenge-header .prompt-text", ".prompt-text", ".task-text", "h2"]:
                try:
                    el = f.locator(s)
                    if await el.count() > 0:
                        t = (await el.first.text_content() or "").strip()
                        if t:
                            return t
                except:
                    continue
        except:
            continue
    return ""


async def _click_verify(page: Page):
    await asyncio.sleep(0.5)
    btn_sels = [
        "button.verifybtn", "button.verify-btn", ".button-submit", "button.submit",
        "button[type='submit']", "#verify", "[data-hcaptcha-submit]",
    ]
    for sel in IFRAME_SELS:
        try:
            f = page.frame_locator(sel)
            for bs in btn_sels:
                try:
                    b = f.locator(bs)
                    if await b.count() > 0 and await b.first.is_visible():
                        bx = await b.first.bounding_box()
                        if bx:
                            await Mouse.click(page, bx['x'] + bx['width'] / 2, bx['y'] + bx['height'] / 2)
                            return
                except:
                    continue
        except:
            continue


# ═══════════════════════════════════════════════════════════
# 1. GRID SOLVER — click matching image tiles
# ═══════════════════════════════════════════════════════════

class GridSolver:
    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda *a: None)
        self.ollama: Optional[OllamaClient] = None

    async def solve(self, page: Page) -> bool:
        self._log("[Grid] Starting...")
        iframe_info = await _find_iframe(page)
        if not iframe_info:
            self._log("[Grid] No iframe", level="error")
            return False

        iframe, box = iframe_info
        text = await _challenge_text(page)
        self._log(f"[Grid] Challenge: '{text[:60]}' | area {box['width']:.0f}x{box['height']:.0f}")

        # Get smaller tile images (DOM if possible, otherwise split screenshot)
        tiles_b64 = await self._get_tile_b64s(page, box)
        if not tiles_b64:
            self._log("[Grid] Splitting screenshot into tiles")
            raw = await page.screenshot(clip={"x": box['x'], "y": box['y'], "width": box['width'], "height": box['height']})
            tiles_pil = self._split_grid(raw, 3)
            tiles_b64 = [_b64(_resize(t, 140)) for t in tiles_pil]

        self._log(f"[Grid] {len(tiles_b64)} tiles prepared")

        if not self.ollama:
            self.ollama = OllamaClient(self.config, log=self._log)

        # Extract target object from challenge text
        subject = self._extract_subject(text)
        self._log(f"[Grid] Looking for: '{subject}'")

        # Query ONE tile at a time — tiny images, simple YES/NO
        nums = []
        for i, tile_b64 in enumerate(tiles_b64):
            ans = await self.ollama.ask_retry(
                f"Does this tile contain a {subject}? Answer YES or NO only.",
                [tile_b64], 50)
            ans_lower = ans.strip().lower()[:10]
            if 'yes' in ans_lower:
                nums.append(i)
                self._log(f"[Grid] Tile {i}: YES")
            else:
                self._log(f"[Grid] Tile {i}: no")
            await asyncio.sleep(0.05)

        # Fallback: ask about whole image
        if not nums:
            self._log("[Grid] No tiles matched, trying single-image fallback...")
            ss = await _iframe_b64(iframe, max_dim=400) or tiles_b64[0]
            ans = await self.ollama.ask_retry(
                f"hCaptcha: \"{text}\". Which numbered tiles have {subject}? Reply numbers with spaces: 0 3 5",
                [ss], 50)
            if ans:
                found = re.findall(r'\\d+', ans)
                nums = [int(n) for n in found if 0 <= int(n) <= 50][:9]
                if nums:
                    self._log(f"[Grid] Fallback tiles: {nums}")

        if not nums:
            self._log("[Grid] No tiles identified", level="error")
            return False

        self._log(f"[Grid] Tiles to click: {nums}")

        # Click each tile
        tboxes = await self._get_tile_boxes(page, box)
        for idx in nums:
            if idx < len(tboxes):
                x, y, w, h = tboxes[idx]
                await Mouse.click(page, x + w / 2 + random.uniform(-3, 3),
                                  y + h / 2 + random.uniform(-3, 3))
                await asyncio.sleep(0.1)

        await _click_verify(page)
        await asyncio.sleep(1.5)
        ok = await Verifier(page).solved()
        self._log(f"[Grid] {'✓' if ok else '✗'}")
        return ok

    @staticmethod
    def _extract_subject(text: str) -> str:
        if not text:
            return "the object"
        t = text.lower()
        for p in ['select all images containing', 'click all images with',
                  'select all squares with', 'click all squares containing',
                  'choose all images with', 'select all matching',
                  'click all', 'select all', 'choose all']:
            if p in t:
                idx = t.index(p) + len(p)
                subj = t[idx:].strip().strip('.!?,:;').strip()
                for art in ['a ', 'an ', 'the ']:
                    if subj.startswith(art):
                        subj = subj[len(art):]
                if subj:
                    return subj.split()[0]
        for kw in ['containing', 'with', 'matching', 'showing']:
            if kw in t:
                idx = t.index(kw) + len(kw)
                subj = t[idx:].strip().strip('.!?,:;').strip()
                for art in ['a ', 'an ', 'the ']:
                    if subj.startswith(art):
                        subj = subj[len(art):]
                if subj:
                    return subj.split()[0]
        return "the object"

    async def _get_tile_b64s(self, page: Page, box: dict) -> Optional[List[str]]:
        for sel in IFRAME_SELS:
            try:
                f = page.frame_locator(sel)
                for ts in [".task-image .image", ".task-image img", ".challenge-item img"]:
                    tiles = f.locator(ts)
                    n = await tiles.count()
                    if n > 0:
                        result = []
                        for i in range(min(n, 12)):
                            try:
                                b = await tiles.nth(i).bounding_box()
                                if b and b['width'] > 20:
                                    raw = await tiles.nth(i).screenshot()
                                    result.append(_b64(_resize(Image.open(io.BytesIO(raw)), 140)))
                            except:
                                continue
                        if result:
                            return result
            except:
                continue
        return None

    def _split_grid(self, raw: bytes, cols: int = 3) -> List[Image.Image]:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        gt = int(h * 0.12)
        gh = h - gt
        tw, th = w // cols, gh // cols
        tiles = []
        for r in range(cols):
            for c in range(cols):
                tiles.append(img.crop((c * tw, gt + r * th, (c + 1) * tw, gt + (r + 1) * th)))
        return tiles

    async def _get_tile_boxes(self, page: Page, box: dict) -> List[Tuple]:
        for sel in IFRAME_SELS:
            try:
                f = page.frame_locator(sel)
                for ts in [".task-image .image", ".task-image img", ".challenge-item img"]:
                    tiles = f.locator(ts)
                    n = await tiles.count()
                    if n > 0:
                        result = []
                        for i in range(n):
                            b = await tiles.nth(i).bounding_box()
                            if b:
                                result.append((b['x'], b['y'], b['width'], b['height']))
                        if result:
                            return result
            except:
                continue
        cols = 3
        bx, by, bw, bh = box['x'], box['y'], box['width'], box['height']
        gt = int(by + bh * 0.12)
        tw, th = bw // cols, (bh - int(bh * 0.12)) // cols
        return [(bx + c * tw, gt + r * th, tw, th) for r in range(cols) for c in range(cols)]

    async def close(self):
        if self.ollama:
            await self.ollama.close()


# ═══════════════════════════════════════════════════════════
# 2. DRAG SOLVER — "Drag rocketship to star" type
# ═══════════════════════════════════════════════════════════

class DragSolver:
    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda *a: None)
        self.ollama: Optional[OllamaClient] = None

    async def solve(self, page: Page) -> bool:
        self._log("[Drag] Starting...")
        iframe_info = await _find_iframe(page)
        if not iframe_info:
            self._log("[Drag] No iframe", level="error")
            return False

        iframe, box = iframe_info
        text = await _challenge_text(page)
        self._log(f"[Drag] Challenge: '{text[:80]}'")

        # Extract target keyword for template memory lookup
        _, template_key = self._extract_drag_subjects(text)
        _load_templates()  # Load any saved templates from disk

        # Method 1: VISUAL MUSCLE MEMORY — ALWAYS first!
        # Uses OpenCV template matching against saved templates (instant, no AI)
        target_pos = await self._find_template_target(page, iframe, box, template_key)
        if target_pos:
            self._log(f"[Drag] Muscle memory found target for '{template_key}'!")
            obj_pos = await self._find_draggable_by_edge(page, iframe, box, target_pos)
            if obj_pos:
                if await self._try_drag(page, obj_pos[0], obj_pos[1], target_pos[0], target_pos[1],
                                        f"Memory '{template_key}'"):
                    return True
            # Fallback: drag from opposite direction of target
            cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
            dx, dy = target_pos[0] - cx, target_pos[1] - cy
            if await self._try_drag(page, cx - dx*1.5, cy - dy*1.5, target_pos[0], target_pos[1],
                                    f"Memory '{template_key}' (heuristic)"):
                return True

        # Method 2: DOM extraction (works for non-canvas hCaptcha)
        coords = await self._extract_drag_coords(page, box)
        if coords:
            if await self._try_drag(page, coords[0], coords[1], coords[2], coords[3], "DOM"):
                return True

        # Method 3: Moondream vision — ask which direction to drag
        self._log("[Drag] Trying Moondream vision...")
        direction = await self._moondream_direction(page, iframe, text)
        moondream_solved = False
        if direction:
            cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
            bw, bh = box['width'], box['height']
            dist = min(bw, bh) * 0.35

            if direction == "left_to_right":
                if await self._try_drag(page, cx - dist, cy, cx + dist, cy, f"Moondream L→R {dist:.0f}px"):
                    moondream_solved = True
            elif direction == "right_to_left":
                if await self._try_drag(page, cx + dist, cy, cx - dist, cy, f"Moondream R→L {dist:.0f}px"):
                    moondream_solved = True
            elif direction == "top_to_bottom":
                if await self._try_drag(page, cx, cy - dist, cx, cy + dist, f"Moondream T→B {dist:.0f}px"):
                    moondream_solved = True
            elif direction == "bottom_to_top":
                if await self._try_drag(page, cx, cy + dist, cx, cy - dist, f"Moondream B→T {dist:.0f}px"):
                    moondream_solved = True

            # If Moondream solved it, SAVE the target side as a template for next time!
            if moondream_solved and template_key:
                self._log(f"[Drag] Saving '{template_key}' template for future muscle memory!")
                await self._save_target_template(page, iframe, box, template_key, direction)
                return True

        if moondream_solved:
            return True

        # Method 4: Element pair scanning (DOM-based big elements)
        elems = await self._scan_iframe_elements(page)
        if elems:
            self._log(f"[Drag] Scanning {len(elems)} elements...")
            for i in range(len(elems)):
                for j in range(len(elems)):
                    if i == j:
                        continue
                    e1, e2 = elems[i], elems[j]
                    a1, a2 = e1['w'] * e1['h'], e2['w'] * e2['h']
                    if 0.5 < a1 / a2 < 2.0:
                        sx = e1['x'] + e1['w']/2 + box['x']
                        sy = e1['y'] + e1['h']/2 + box['y']
                        ex = e2['x'] + e2['w']/2 + box['x']
                        ey = e2['y'] + e2['h']/2 + box['y']
                        dist = math.hypot(ex - sx, ey - sy)
                        if 30 < dist < 400:
                            if await self._try_drag(page, sx, sy, ex, ey, f"elem {i}→{j}"):
                                # Save template too
                                if template_key:
                                    await self._save_target_template(page, iframe, box, template_key)
                                return True

        # Method 5: Multiple heuristic patterns
        self._log("[Drag] Trying heuristic patterns...")
        cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
        bw, bh = box['width'], box['height']
        for frac in [0.25, 0.35, 0.45, 0.55, 0.65]:
            d = min(bw, bh) * frac
            for dx, dy, label in [(-d, 0, 'L'), (d, 0, 'R'), (0, -d, 'U'), (0, d, 'D'),
                                  (-d*0.7, -d*0.3, 'UL'), (d*0.7, d*0.3, 'DR'),
                                  (d*0.3, -d*0.5, 'UR'), (-d*0.3, d*0.5, 'DL')]:
                if await self._try_drag(page, cx + dx, cy + dy, cx - dx, cy - dy,
                                        f"heuristic {frac:.2f} {label}"):
                    # Save template on ANY successful solve for muscle memory!
                    if template_key:
                        # Infer direction from drag coords
                        inf_dir = self._infer_direction(dx, dy, -dx, -dy)
                        await self._save_target_template(page, iframe, box, template_key, inf_dir)
                    return True

        self._log("[Drag] ✗ Failed", level="error")
        return False

    @staticmethod
    def _infer_direction(sx, sy, ex, ey) -> str:
        """Infer drag direction from start→end coordinates."""
        dx = ex - sx
        dy = ey - sy
        if abs(dx) > abs(dy):
            return "left_to_right" if dx > 0 else "right_to_left"
        else:
            return "top_to_bottom" if dy > 0 else "bottom_to_top"

    async def _try_drag(self, page, sx, sy, ex, ey, label: str) -> bool:
        self._log(f"[Drag] Trying {label}: ({sx:.0f},{sy:.0f})→({ex:.0f},{ey:.0f})")
        await Mouse.drag(page, sx, sy, ex, ey)
        await asyncio.sleep(1)
        if await Verifier(page).solved():
            await _click_verify(page)
            await asyncio.sleep(0.5)
            if await Verifier(page).solved():
                self._log(f"[Drag] ✓ ({label})")
                return True
        return False

    async def _moondream_direction(self, page: Page, iframe, text: str) -> Optional[str]:
        """Ask Moondream which direction to drag.
        Takes a small screenshot, asks LEFT→RIGHT, RIGHT→LEFT, TOP→BOTTOM, BOTTOM→TOP."""
        ss = await _iframe_b64(iframe, max_dim=320)
        if not ss:
            self._log("[Drag] Moondream: screenshot failed")
            return None

        if not self.ollama:
            self.ollama = OllamaClient(self.config, log=self._log)

        obj, target = self._extract_drag_subjects(text)
        self._log(f"[Drag] Moondream: looking for '{obj}'→'{target}'")

        ans = await self.ollama.ask_retry(
            f"hCaptcha: '{text}'. Which direction to drag the {obj} to the {target}? "
            "Answer ONLY: left to right | right to left | top to bottom | bottom to top",
            [ss], 30)

        if not ans:
            return None

        dir_result = self._parse_direction(ans)

        return dir_result

    async def _find_template_target(self, page: Page, iframe, box: dict, key: str) -> Optional[Tuple]:
        """Use OpenCV template matching to find a saved target template in the captcha screenshot.
        Multi-scale matching to handle different sizes.
        Returns (x, y) page coordinates of the best match."""
        template_data = TEMPLATE_MEMORY.get(key)
        if not template_data:
            return None

        template_img = template_data["img"]  # Grayscale numpy array
        th, tw = template_img.shape
        if th < 10 or tw < 10:
            return None

        # Screenshot the captcha iframe
        raw = await iframe.screenshot()
        if not raw or len(raw) < 100:
            return None
        frame_rgb = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        if frame_rgb is None or frame_rgb.shape[0] < th or frame_rgb.shape[1] < tw:
            return None

        best_val = 0.5  # Minimum confidence threshold
        best_pt = None

        # Try multiple scales of the template
        scales = [0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5]
        for scale in scales:
            scaled_w, scaled_h = int(tw * scale), int(th * scale)
            if scaled_w > frame_rgb.shape[1] or scaled_h > frame_rgb.shape[0]:
                continue
            scaled = cv2.resize(template_img, (scaled_w, scaled_h))
            try:
                result = cv2.matchTemplate(frame_rgb, scaled, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val > best_val:
                    best_val = max_val
                    best_pt = (max_loc[0] + scaled_w // 2, max_loc[1] + scaled_h // 2)
            except:
                continue

        if best_pt:
            self._log(f"[Drag] Template '{key}' matched! confidence={best_val:.2f} at {best_pt}")
            # Convert iframe-relative to page coordinates
            px = box['x'] + best_pt[0]
            py = box['y'] + best_pt[1]
            return (px, py)

        self._log(f"[Drag] Template '{key}' not found (best={best_val:.2f})")
        return None

    async def _find_draggable_by_edge(self, page: Page, iframe, box: dict, target_pos: Tuple) -> Optional[Tuple]:
        """Find the draggable object by looking for unique edge elements.
        The draggable is usually on the opposite side of the target.
        Returns (x, y) page coordinates."""
        cx = box['x'] + box['width'] / 2
        # Target is on the right? Draggable is on the left, and vice versa
        if target_pos[0] > cx:
            # Target right, look for elements on left side
            test_x = box['x'] + box['width'] * 0.25
        else:
            # Target left, look for elements on right side
            test_x = box['x'] + box['width'] * 0.75

        cy = target_pos[1]  # Same vertical position is likely
        return (test_x, cy)

    async def _save_target_template(self, page: Page, iframe, box: dict, key: str, direction: Optional[str] = None):
        """After a successful solve, save just the TARGET side of the iframe as a template.
        Crops to the half where the target should be based on drag direction.
        E.g., for 'left_to_right', saves the RIGHT half (where the star is).
        This makes template matching possible since the target appearance is consistent."""
        if not key or key == "target":
            return
        try:
            raw = await iframe.screenshot()
            if not raw or len(raw) < 100:
                return
            img = Image.open(io.BytesIO(raw))
            w, h = img.size

            # Crop to the TARGET side based on direction
            if direction == "left_to_right":
                # Target is on the RIGHT side → save right 40%
                crop = img.crop((int(w * 0.6), 0, w, h))
            elif direction == "right_to_left":
                # Target is on the LEFT side → save left 40%
                crop = img.crop((0, 0, int(w * 0.4), h))
            elif direction == "top_to_bottom":
                # Target is at the BOTTOM → save bottom 40%
                crop = img.crop((0, int(h * 0.6), w, h))
            elif direction == "bottom_to_top":
                # Target is at the TOP → save top 40%
                crop = img.crop((0, 0, w, int(h * 0.4)))
            else:
                # Fallback: use top-right quadrant
                crop = img.crop((int(w * 0.5), 0, w, int(h * 0.5)))

            # Resize to a reasonable size for fast matching
            crop = _resize(crop, 200)
            _save_template(key, crop)
            self._log(f"[Drag] ✓ Saved '{key}' template ({crop.size[0]}x{crop.size[1]}) side={direction}")
        except Exception as e:
            self._log(f"[Drag] Error saving template: {e}", level="warn")

    @staticmethod
    def _parse_direction(ans: str) -> Optional[str]:
        if not ans:
            return None
        ans_lower = ans.strip().lower()
        if 'left to right' in ans_lower or 'left→right' in ans_lower:
            return "left_to_right"
        if 'right to left' in ans_lower or 'right→left' in ans_lower:
            return "right_to_left"
        if 'top to bottom' in ans_lower or 'top→bottom' in ans_lower:
            return "top_to_bottom"
        if 'bottom to top' in ans_lower or 'bottom→top' in ans_lower:
            return "bottom_to_top"
        if 'left' in ans_lower and 'right' in ans_lower:
            return "left_to_right"
        if 'right' in ans_lower and 'left' in ans_lower:
            return "right_to_left"
        if 'up' in ans_lower or 'top' in ans_lower:
            return "bottom_to_top"
        if 'down' in ans_lower or 'bottom' in ans_lower:
            return "top_to_bottom"
        return None

    @staticmethod
    def _extract_drag_subjects(text: str) -> Tuple[str, str]:
        """Extract draggable object and target from challenge text.
        E.g. 'Please drag the spaceship to the star' → ('spaceship', 'star')
             'Move the fish to the water' → ('fish', 'water')
        """
        obj, target = "object", "target"
        if not text:
            return obj, target
        t = text.lower().strip()

        # Remove leading polite words like "please", "now", "kindly"
        for polite in ['please ', 'kindly ', 'now ']:
            if t.startswith(polite):
                t = t[len(polite):].strip()

        # Helper: get next real word, skipping articles
        def _next_word(words_list, idx):
            if idx < len(words_list):
                w = words_list[idx]
                if w in ('the', 'a', 'an', 'your', 'this'):
                    if idx + 1 < len(words_list):
                        return words_list[idx + 1]
                return w
            return ""

        # Pattern: "Drag/Move/Place [the] X [to/into/onto/in] [the] Y"
        for prefix in ['drag', 'move', 'slide', 'place']:
            if t.startswith(prefix):
                rest = t[len(prefix):].strip()
                # Strip leading articles
                for art in ['the ', 'a ', 'an ', 'your ']:
                    if rest.startswith(art):
                        rest = rest[len(art):]
                # Find separator
                for sep in [' to ', ' into ', ' onto ', ' in ']:
                    if sep in rest:
                        parts = rest.split(sep, 1)
                        obj_part = parts[0].strip()
                        target_part = parts[1].strip()
                        # Get first real word of each
                        for art in ['the ', 'a ', 'an ', 'your ']:
                            if target_part.startswith(art):
                                target_part = target_part[len(art):]
                        obj = obj_part.split()[0] if obj_part else obj
                        target = target_part.split()[0] if target_part else target
                        return obj, target

        # Fallback: search for "drag/move" + "to" anywhere in text
        words = t.split()
        for i, w in enumerate(words):
            if w in ('drag', 'move', 'slide', 'place') and i + 1 < len(words):
                obj = _next_word(words, i + 1)
            if w == 'to' and i + 1 < len(words):
                target = _next_word(words, i + 1)

        return obj, target

    async def _extract_drag_coords(self, page: Page, iframe_box: dict) -> Optional[Tuple]:
        for sel in IFRAME_SELS:
            try:
                f = page.frame_locator(sel)
                data = await f.first.evaluate("""() => {
    const all = document.querySelectorAll('*');
    const positioned = [];
    for (const el of all) {
        const cls = el.className || '';
        const id = el.id || '';
        const tag = el.tagName || '';
        if (tag === 'SCRIPT' || tag === 'STYLE') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 20 || rect.height < 20) continue;
        if (rect.width > 500 || rect.height > 500) continue;
        const isDraggable = el.draggable ||
            cls.toLowerCase().includes('drag') ||
            cls.toLowerCase().includes('handle') ||
            cls.toLowerCase().includes('target') ||
            id.toLowerCase().includes('drag') ||
            id.toLowerCase().includes('target');
        positioned.push({
            tag: tag,
            x: rect.x, y: rect.y, w: rect.width, h: rect.height,
            draggable: isDraggable,
        });
    }
    positioned.sort((a, b) => a.w * a.h - b.w * b.h);
    const candidates = positioned.filter(e => e.w * e.h < 15000);
    if (candidates.length >= 2) {
        return { source: candidates[0], target: candidates[1] };
    }
    return null;
}""")
                if data and 'source' in data and 'target' in data:
                    src, tgt = data['source'], data['target']
                    sx = iframe_box['x'] + src['x'] + src['w'] / 2
                    sy = iframe_box['y'] + src['y'] + src['h'] / 2
                    ex = iframe_box['x'] + tgt['x'] + tgt['w'] / 2
                    ey = iframe_box['y'] + tgt['y'] + tgt['h'] / 2
                    return (sx, sy, ex, ey)
            except:
                continue
        return None

    async def _scan_iframe_elements(self, page: Page) -> List[dict]:
        for sel in IFRAME_SELS:
            try:
                f = page.frame_locator(sel)
                data = await f.first.evaluate("""() => {
    const results = [];
    for (const el of document.querySelectorAll('*')) {
        if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 25 || rect.height < 25) continue;
        if (rect.width > 400 || rect.height > 400) continue;
        results.push({ x: rect.x, y: rect.y, w: rect.width, h: rect.height });
    }
    return results;
}""")
                if data:
                    return data
            except:
                continue
        return []

    async def close(self):
        if self.ollama:
            await self.ollama.close()


# ═══════════════════════════════════════════════════════════
# 3. SLIDER SOLVER — puzzle slider (OpenCV)
# ═══════════════════════════════════════════════════════════

class SliderSolver:
    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda *a: None)

    async def solve(self, page: Page) -> bool:
        self._log("[Slider] Starting...")
        handle = await self._find_slider(page)
        if not handle:
            self._log("[Slider] No slider handle", level="warn")
            return False

        hb = await handle.bounding_box()
        if not hb:
            return False

        self._log(f"[Slider] Handle at ({hb['x']:.0f},{hb['y']:.0f}) {hb['width']:.0f}x{hb['height']:.0f}")

        track_box = await self._find_track(page, hb)
        max_travel = (track_box['width'] - hb['width']) if track_box else 250

        sx = hb['x'] + hb['width'] / 2
        sy = hb['y'] + hb['height'] / 2

        offsets = await self._get_offsets_opencv(page, hb)
        if not offsets:
            offsets = [int(max_travel * f) for f in [0.3, 0.45, 0.55, 0.65, 0.75, 0.85, 0.4, 0.6, 0.5, 0.7, 0.35, 0.8]]

        for i, offset in enumerate(offsets):
            offset = max(10, min(offset, int(max_travel)))
            tx = sx + offset + random.randint(-3, 3)
            self._log(f"[Slider] Attempt {i + 1}/{len(offsets)}: {offset}px")
            await Mouse.drag(page, sx, sy, tx, sy)
            await asyncio.sleep(0.6)
            if await Verifier(page).solved():
                self._log("[Slider] ✓")
                return True
            await Mouse.drag(page, tx, sy, sx, sy)
            await asyncio.sleep(0.4)

        self._log("[Slider] ✗", level="error")
        return False

    async def _find_slider(self, page: Page):
        sels = ['.slider-handle', '.slide-btn', '.handler', '[role="slider"]',
                'button:has(.slider)', '[class*="handle"]', '[class*="slider"] button']
        for sel in sels:
            try:
                if await page.locator(sel).count() > 0:
                    return page.locator(sel).first
            except:
                continue
        for isel in IFRAME_SELS:
            for sel in sels:
                try:
                    f = page.frame_locator(isel)
                    if await f.locator(sel).count() > 0:
                        return f.locator(sel).first
                except:
                    continue
        return None

    async def _find_track(self, page: Page, hb: dict) -> Optional[dict]:
        sels = ['.slider-track', '.slider-container', '.track', '[class*="track"]', '[class*="slider"]']
        for sel in sels:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    return await el.first.bounding_box()
            except:
                continue
        for isel in IFRAME_SELS:
            for sel in sels:
                try:
                    f = page.frame_locator(isel)
                    if await f.locator(sel).count() > 0:
                        return await f.locator(sel).first.bounding_box()
                except:
                    continue
        return hb

    async def _get_offsets_opencv(self, page: Page, hb: dict) -> Optional[List[int]]:
        try:
            area = {
                'x': hb['x'] - 50, 'y': hb['y'] - 20,
                'width': 350, 'height': hb['height'] + 40
            }
            raw = await page.screenshot(clip=area)
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
            edges = cv2.Canny(img, 50, 150)
            lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 50, minLineLength=20, maxLineGap=10)
            if lines is not None:
                vert_lines = []
                for line in lines:
                    x1, y1, x2, y2 = line[0]
                    if abs(x2 - x1) < 5:
                        vert_lines.append((x1 + x2) // 2 + area['x'])
                if vert_lines:
                    vert_lines.sort()
                    gaps = []
                    for i in range(len(vert_lines) - 1):
                        gap = vert_lines[i + 1] - vert_lines[i]
                        if 30 < gap < 80:
                            gaps.append((vert_lines[i] + vert_lines[i + 1]) // 2)
                    if gaps:
                        return [g - hb['x'] for g in gaps]

            handle_img = await page.screenshot(clip={
                'x': hb['x'], 'y': hb['y'], 'width': hb['width'], 'height': hb['height']
            })
            handle_gray = cv2.imdecode(np.frombuffer(handle_img, np.uint8), cv2.IMREAD_GRAYSCALE)
            if handle_gray.shape[0] > 0 and handle_gray.shape[1] > 0:
                res = cv2.matchTemplate(img, handle_gray, cv2.TM_CCOEFF_NORMED)
                all_locs = np.where(res > 0.6)
                if len(all_locs[0]) > 1:
                    for i in range(1, min(5, len(all_locs[0]))):
                        mx, my = all_locs[1][i], all_locs[0][i]
                        off = int(mx + area['x'] - hb['x'] - hb['width'] / 2)
                        if 20 < abs(off) < 400:
                            return [abs(off)]
        except Exception as e:
            self._log(f"[Slider] OpenCV error: {e}")
        return None

    async def close(self):
        pass


# ═══════════════════════════════════════════════════════════
# MASTER SOLVER — detects challenge type and dispatches
# ═══════════════════════════════════════════════════════════

class MasterSolver:
    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda *a: None)
        self.grid = GridSolver(config, log=self._log)
        self.drag = DragSolver(config, log=self._log)
        self.slider = SliderSolver(config, log=self._log)

    async def solve(self, page: Page) -> bool:
        self._log("[Master] Detecting challenge type...")
        ctype = await self._detect(page)
        self._log(f"[Master] Type = '{ctype}'")

        if ctype == "slider":
            ok = await self.slider.solve(page)
        elif ctype == "drag":
            ok = await self.drag.solve(page)
        else:
            ok = await self.grid.solve(page)

        self._log(f"[Master] {'SUCCESS' if ok else 'FAILED'}")
        return ok

    async def _detect(self, page: Page) -> str:
        slider_sigs = ['[role="slider"]', '.slider-handle', '.slide-btn', '.handler']
        for sel in slider_sigs:
            try:
                if await page.locator(sel).count() > 0:
                    return "slider"
            except:
                continue
        for isel in IFRAME_SELS:
            for sel in slider_sigs:
                try:
                    f = page.frame_locator(isel)
                    if await f.locator(sel).count() > 0:
                        return "slider"
                except:
                    continue

        text = await _challenge_text(page)
        text_lower = text.lower()
        if any(w in text_lower for w in ['drag', 'move', 'slide to', 'place', 'put']):
            for isel in IFRAME_SELS:
                try:
                    f = page.frame_locator(isel)
                    for ds in ['[class*="drag"]', '[class*="handle"]', '[class*="object"]',
                               '[draggable]', '[class*="piece"]']:
                        if await f.locator(ds).count() > 0:
                            return "drag"
                except:
                    continue
            return "drag"

        return "grid"

    async def close(self):
        await self.grid.close()
        await self.drag.close()
        await self.slider.close()


async def main():
    config = SolverConfig()
    solver = MasterSolver(config)
    print("=" * 50)
    print("  MasterSolver — all challenge types")
    print("  Grid (Moondream tiles) | Drag (DOM+Moondream) | Slider (OpenCV)")
    print("=" * 50)
    print(f"  Model: {config.ollama_model}")


if __name__ == "__main__":
    asyncio.run(main())
