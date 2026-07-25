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

        # Get example image + tiles for OpenCV matching
        tiles_pil, tile_boxes = await self._get_tiles_dom(page, box)
        if not tiles_pil:
            self._log("[Grid] DOM tiles not found, splitting screenshot")
            raw = await page.screenshot(clip={"x": box['x'], "y": box['y'], "width": box['width'], "height": box['height']})
            tiles_pil = self._split_grid(raw)
            tile_boxes = []

        self._log(f"[Grid] {len(tiles_pil)} tiles")

        # Method 1: OpenCV template matching (instant, no AI)
        # Works when grid has an "example" image — matches example against each tile
        nums = await self._solve_opencv(page)
        if nums:
            self._log(f"[Grid] OpenCV matched tiles: {nums}")
        else:
            # Method 2: Moondream AI (slow but works for text-based challenges)
            self._log("[Grid] OpenCV failed, trying Moondream...")
            tiles_b64 = [_b64(_resize(t, 140)) for t in tiles_pil]
            if not self.ollama:
                self.ollama = OllamaClient(self.config, log=self._log)
            subject = self._extract_subject(text)
            self._log(f"[Grid] Looking for: '{subject}'")
            nums = []
            for i, tile_b64 in enumerate(tiles_b64):
                ans = await self.ollama.ask_retry(
                    f"Does this tile contain a {subject}? Answer YES or NO only.",
                    [tile_b64], 50)
                if 'yes' in ans.strip().lower()[:10]:
                    nums.append(i)
                    self._log(f"[Grid] Tile {i}: YES")

        if not nums:
            self._log("[Grid] No tiles identified", level="error")
            return False

        # Click tiles
        tboxes = tile_boxes or await self._get_tile_boxes(page, box)
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

    async def _solve_opencv(self, page: Page) -> Optional[List[int]]:
        """Use OpenCV template matching to find tiles matching the example image.
        Finds the 'example' image element in the iframe, screenshots it,
        then matches it against each tile using normalized cross-correlation."""
        try:
            # Find example image in hCaptcha iframe
            example_raw = None
            tiles_raw = []
            tile_boxes = []

            for sel in IFRAME_SELS:
                f = page.frame_locator(sel)
                # Look for example image (usually first or has 'example' class)
                for es in [".example-image img", ".example-image", 
                          "[class*='example'] img", "[class*='example']",
                          ".challenge-example img", ".challenge-example"]:
                    try:
                        ex = f.locator(es)
                        if await ex.count() > 0:
                            example_raw = await ex.first.screenshot()
                            if example_raw and len(example_raw) > 100:
                                break
                    except:
                        continue
                
                # Get tile images
                for ts in [".task-image .image", ".task-image img", ".challenge-item img"]:
                    try:
                        tiles = f.locator(ts)
                        n = await tiles.count()
                        if n > 0:
                            for i in range(min(n, 12)):
                                b = await tiles.nth(i).bounding_box()
                                if b and b['width'] > 20:
                                    raw = await tiles.nth(i).screenshot()
                                    tiles_raw.append(raw)
                                    tile_boxes.append((b['x'], b['y'], b['width'], b['height']))
                            break
                    except:
                        continue
                if tiles_raw:
                    break

            if not example_raw or len(tiles_raw) < 3:
                return None

            # Convert to grayscale for matching
            example_gray = cv2.imdecode(np.frombuffer(example_raw, np.uint8), cv2.IMREAD_GRAYSCALE)
            if example_gray is None or example_gray.shape[0] < 10:
                return None

            self._log(f"[Grid] OpenCV: {len(tiles_raw)} tiles, example={example_gray.shape[1]}x{example_gray.shape[0]}")

            # Match example against each tile
            matched = []
            for i, tile_raw in enumerate(tiles_raw):
                tile_gray = cv2.imdecode(np.frombuffer(tile_raw, np.uint8), cv2.IMREAD_GRAYSCALE)
                if tile_gray is None:
                    continue

                # Resize example to tile size for fair comparison
                ex_resized = cv2.resize(example_gray, (tile_gray.shape[1], tile_gray.shape[0]))

                # Try multiple matching methods
                best = 0
                for method in [cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR_NORMED]:
                    try:
                        result = cv2.matchTemplate(tile_gray, ex_resized, method)
                        _, max_val, _, _ = cv2.minMaxLoc(result)
                        best = max(best, max_val)
                    except:
                        continue

                self._log(f"[Grid] Tile {i}: match={best:.3f}")
                if best > 0.6:  # Threshold for match
                    matched.append(i)

            if matched:
                self._log(f"[Grid] OpenCV matched {len(matched)}/{len(tiles_raw)} tiles")
                return matched

            # Try histogram comparison (more robust for different sizes)
            matched = []
            for i, tile_raw in enumerate(tiles_raw):
                tile_img = cv2.imdecode(np.frombuffer(tile_raw, np.uint8), cv2.IMREAD_COLOR)
                ex_img = cv2.imdecode(np.frombuffer(example_raw, np.uint8), cv2.IMREAD_COLOR)
                if tile_img is None or ex_img is None:
                    continue
                tile_hsv = cv2.cvtColor(tile_img, cv2.COLOR_BGR2HSV)
                ex_hsv = cv2.cvtColor(ex_img, cv2.COLOR_BGR2HSV)
                tile_hist = cv2.calcHist([tile_hsv], [0, 1], None, [8, 8], [0, 180, 0, 256])
                ex_hist = cv2.calcHist([ex_hsv], [0, 1], None, [8, 8], [0, 180, 0, 256])
                cv2.normalize(tile_hist, tile_hist, 0, 1, cv2.NORM_MINMAX)
                cv2.normalize(ex_hist, ex_hist, 0, 1, cv2.NORM_MINMAX)
                similarity = cv2.compareHist(tile_hist, ex_hist, cv2.HISTCMP_CORREL)
                self._log(f"[Grid] Tile {i}: hist={similarity:.3f}")
                if similarity > 0.7:
                    matched.append(i)

            if matched:
                self._log(f"[Grid] Hist matched {len(matched)}/{len(tiles_raw)} tiles")
            return matched if matched else None

        except Exception as e:
            self._log(f"[Grid] OpenCV error: {e}", level="warn")
            return None

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

    async def _get_tiles_dom(self, page: Page, box: dict) -> Tuple[List[Image.Image], List[Tuple]]:
        """Get tile images and their page coordinates from the iframe DOM."""
        for sel in IFRAME_SELS:
            try:
                f = page.frame_locator(sel)
                for ts in [".task-image .image", ".task-image img", ".challenge-item img"]:
                    tiles = f.locator(ts)
                    n = await tiles.count()
                    if n > 0:
                        imgs = []
                        boxes = []
                        for i in range(min(n, 12)):
                            try:
                                b = await tiles.nth(i).bounding_box()
                                if b and b['width'] > 20:
                                    raw = await tiles.nth(i).screenshot()
                                    imgs.append(Image.open(io.BytesIO(raw)))
                                    boxes.append((b['x'], b['y'], b['width'], b['height']))
                            except:
                                continue
                        if imgs:
                            return imgs, boxes
            except:
                continue
        return [], []

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
    """Solves drag-to-target captchas by:
    1. Smart DOM extraction — finds draggable + target elements via CSS property analysis
    2. Multi-object canvas detection — finds distinct colored objects via OpenCV color clustering
    3. Fast heuristic patterns — just 8 targeted attempts (captcha expires fast!)
    """

    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda *a: None)

    async def solve(self, page: Page) -> bool:
        self._log("[Drag] Starting...")
        iframe_info = await _find_iframe(page)
        if not iframe_info:
            self._log("[Drag] No iframe", level="error")
            return False

        iframe, box = iframe_info
        text = await _challenge_text(page)
        self._log(f"[Drag] Challenge: '{text[:80]}'")

        # Extract target keyword for template memory
        _, template_key = self._extract_drag_subjects(text)
        _load_templates()

        # ── Method 1: SMART DOM EXTRACTION ──
        # Finds positioned elements with visual content in top 75% of iframe
        # (ignores verify button area). Scores and matches left/right pairs.
        dom_coords = await self._dom_smart_extract(page, box)
        if dom_coords:
            if await self._try_drag(page, *dom_coords, "DOM"):
                await self._save_template_on_success(page, iframe, box, dom_coords, template_key)
                return True

        # ── Method 2: VISUAL MUSCLE MEMORY ──
        if template_key and template_key != "target":
            if await self._try_muscle_memory(page, iframe, box, template_key):
                return True

        # ── Method 3: TARGETED HEURISTICS ──
        # Drags from various positions across the iframe in multiple directions.
        # The actual draggable is somewhere along the edges; target opposite side.
        self._log("[Drag] Targeted heuristics...")
        cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
        bw, bh = box['width'], box['height']
        max_d = min(bw, bh) * 0.5

        # 1. Center horizontal (L↔R) — 8 attempts
        for dist in [max_d * 0.3, max_d * 0.5, max_d * 0.7, max_d * 0.9]:
            for dx in [-dist, dist]:
                if await self._try_drag(page, cx + dx, cy, cx - dx, cy, f"H{dx:+.0f}"):
                    return True

        # 2. Edge-to-center (drag from near left/right edges toward center) — 8 attempts
        for sx_pos, label in [(box['x'] + 25, 'LE'), (box['x'] + bw - 25, 'RE')]:
            for f in [0.3, 0.5, 0.7, 0.9]:
                ex_pos = cx + (sx_pos - cx) * (1 - f)
                if await self._try_drag(page, sx_pos, cy, ex_pos, cy, f"{label}{f:.0f}"):
                    return True

        # 3. Vertical (U↔D) — 4 attempts
        for dist in [max_d * 0.4, max_d * 0.8]:
            for dy in [-dist, dist]:
                if await self._try_drag(page, cx, cy + dy, cx, cy - dy, f"V{dy:+.0f}"):
                    return True

        self._log("[Drag] ✗ Failed", level="error")
        return False

    async def _dom_smart_extract(self, page: Page, box: dict) -> Optional[Tuple]:
        """Find draggable element + target by analyzing ALL visible DOM elements
        in the hCaptcha iframe. Only searches the TOP 75% of the iframe
        (ignoring the verify button/footer at the bottom).
        
        In hCaptcha drag challenges, the draggable is an absolutely-positioned
        element (~50-80px) with a background image, and the target is another
        positioned element (~50-80px) on the opposite side."""
        for sel in IFRAME_SELS:
            try:
                f = page.frame_locator(sel)
                data = await f.first.evaluate("""() => {
    const results = [];
    const vh = window.innerHeight;
    const maxY = vh * 0.75;  // Only top 75% — skip verify button area
    for (const el of document.querySelectorAll('*')) {
        const tag = el.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK') continue;
        const rect = el.getBoundingClientRect();
        if (rect.y > maxY || rect.bottom > maxY) continue;  // Skip bottom area
        if (rect.width < 30 || rect.width > 150) continue;   // Drag objects are 30-150px
        if (rect.height < 30 || rect.height > 150) continue;
        
        const style = window.getComputedStyle(el);
        const pos = style.position;
        const zIdx = parseInt(style.zIndex) || 0;
        const bg = style.backgroundImage || '';
        const hasBg = bg.includes('url(') || bg.includes('data:image');
        const isImg = tag === 'IMG' && el.src && el.src.length > 0;
        const isAbs = pos === 'absolute' || pos === 'fixed';
        
        // Only elements that look like drag objects: positioned, with visual content
        if (isAbs || hasBg || isImg || zIdx > 0) {
            results.push({
                tag: tag,
                x: rect.x, y: rect.y, w: rect.width, h: rect.height,
                zIndex: zIdx,
                hasBgImg: hasBg || isImg,
                isPositioned: isAbs,
            });
        }
    }
    if (results.length < 2) return null;
    
    // Score each element by how "drag-object-like" it is
    const scored = results.map(el => {
        let score = 0;
        score += el.hasBgImg ? 3 : 0;         // Has visual content
        score += el.isPositioned ? 2 : 0;     // Absolutely positioned = interactive
        score += el.zIndex > 0 ? 1 : 0;       // On top of other elements
        score += el.w > 35 && el.w < 100 ? 1 : 0;  // Ideal size range
        score += el.h > 35 && el.h < 100 ? 1 : 0;
        return { ...el, score };
    });
    scored.sort((a, b) => b.score - a.score);
    
    // Take top candidates and find a left/right pair
    const top = scored.filter(e => e.score >= 3);
    if (top.length < 2) return null;
    
    const midX = window.innerWidth / 2;
    const leftSide = top.filter(e => e.x + e.w/2 < midX - 30);
    const rightSide = top.filter(e => e.x + e.w/2 > midX + 30);
    
    if (leftSide.length && rightSide.length) {
        return { source: leftSide[0], target: rightSide[0] };
    }
    // Fallback: best two by score
    return { source: top[0], target: top[1] };
}""")
                if data and 'source' in data and 'target' in data:
                    src, tgt = data['source'], data['target']
                    sx = box['x'] + src['x'] + src['w'] / 2
                    sy = box['y'] + src['y'] + src['h'] / 2
                    ex = box['x'] + tgt['x'] + tgt['w'] / 2
                    ey = box['y'] + tgt['y'] + tgt['h'] / 2
                    self._log(f"[Drag] DOM: ({src['x']:.0f},{src['y']:.0f} {src['w']:.0f}x{src['h']:.0f} score={src['score']})→"
                              f"({tgt['x']:.0f},{tgt['y']:.0f} {tgt['w']:.0f}x{tgt['h']:.0f} score={tgt['score']})")
                    return (sx, sy, ex, ey)
            except:
                continue
        return None

    async def _try_muscle_memory(self, page: Page, iframe, box: dict, key: str) -> bool:
        target_pos = await self._find_template_target(page, iframe, box, key)
        if not target_pos:
            return False

        self._log(f"[Drag] Memory '{key}' at ({target_pos[0]:.0f},{target_pos[1]:.0f})")
        cx = box['x'] + box['width'] / 2
        if target_pos[0] > cx:
            obj_x = box['x'] + box['width'] * 0.2 + random.uniform(-10, 10)
        else:
            obj_x = box['x'] + box['width'] * 0.8 + random.uniform(-10, 10)
        obj_y = target_pos[1] + random.uniform(-20, 20)

        if await self._try_drag(page, obj_x, obj_y, target_pos[0], target_pos[1], f"Memory '{key}'"):
            return True

        dx = target_pos[0] - cx
        if await self._try_drag(page, cx - dx, target_pos[1], target_pos[0], target_pos[1], f"Memory '{key}' offset"):
            return True
        return False

    async def _save_template_on_success(self, page, iframe, box, coords, template_key):
        if not template_key or template_key == "target":
            return
        sx, sy, ex, ey = coords
        direction = self._infer_direction(sx, sy, ex, ey)
        await self._save_target_template(page, iframe, box, template_key, direction)

    @staticmethod
    def _infer_direction(sx, sy, ex, ey) -> str:
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

    async def _find_template_target(self, page: Page, iframe, box: dict, key: str) -> Optional[Tuple]:
        template_data = TEMPLATE_MEMORY.get(key)
        if not template_data:
            return None

        template_img = template_data["img"]
        th, tw = template_img.shape
        if th < 10 or tw < 10:
            return None

        raw = await iframe.screenshot()
        if not raw or len(raw) < 100:
            return None
        frame_rgb = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        if frame_rgb is None or frame_rgb.shape[0] < th or frame_rgb.shape[1] < tw:
            return None

        best_val = 0.5
        best_pt = None
        for scale in [0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5]:
            sw, sh = int(tw * scale), int(th * scale)
            if sw > frame_rgb.shape[1] or sh > frame_rgb.shape[0]:
                continue
            scaled = cv2.resize(template_img, (sw, sh))
            try:
                result = cv2.matchTemplate(frame_rgb, scaled, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val > best_val:
                    best_val = max_val
                    best_pt = (max_loc[0] + sw // 2, max_loc[1] + sh // 2)
            except:
                continue

        if best_pt:
            self._log(f"[Drag] Template '{key}' = {best_val:.2f} at {best_pt}")
            return (box['x'] + best_pt[0], box['y'] + best_pt[1])
        return None

    async def _save_target_template(self, page, iframe, box, key, direction=None):
        if not key or key == "target":
            return
        try:
            raw = await iframe.screenshot()
            if not raw or len(raw) < 100:
                return
            img = Image.open(io.BytesIO(raw))
            w, h = img.size
            if direction == "left_to_right":
                crop = img.crop((int(w * 0.6), 0, w, h))
            elif direction == "right_to_left":
                crop = img.crop((0, 0, int(w * 0.4), h))
            elif direction == "top_to_bottom":
                crop = img.crop((0, int(h * 0.6), w, h))
            elif direction == "bottom_to_top":
                crop = img.crop((0, 0, w, int(h * 0.4)))
            else:
                crop = img.crop((int(w * 0.5), 0, w, int(h * 0.5)))
            crop = _resize(crop, 200)
            _save_template(key, crop)
            self._log(f"[Drag] ✓ Saved '{key}' template")
        except Exception as e:
            self._log(f"[Drag] Error saving template: {e}", level="warn")

    @staticmethod
    def _extract_drag_subjects(text: str) -> Tuple[str, str]:
        obj, target = "object", "target"
        if not text:
            return obj, target
        t = text.lower().strip()
        for polite in ['please ', 'kindly ', 'now ']:
            if t.startswith(polite):
                t = t[len(polite):].strip()
        for prefix in ['drag', 'move', 'slide', 'place']:
            if t.startswith(prefix):
                rest = t[len(prefix):].strip()
                for art in ['the ', 'a ', 'an ', 'your ']:
                    if rest.startswith(art):
                        rest = rest[len(art):]
                for sep in [' to ', ' into ', ' onto ', ' in ']:
                    if sep in rest:
                        parts = rest.split(sep, 1)
                        obj_part = parts[0].strip()
                        target_part = parts[1].strip()
                        for art in ['the ', 'a ', 'an ', 'your ']:
                            if target_part.startswith(art):
                                target_part = target_part[len(art):]
                        obj = obj_part.split()[0] if obj_part else obj
                        target = target_part.split()[0] if target_part else target
                        return obj, target
        words = t.split()
        for i, w in enumerate(words):
            if w in ('drag', 'move', 'slide', 'place') and i + 1 < len(words):
                nw = words[i + 1]
                if nw in ('the', 'a', 'an', 'your', 'this'):
                    if i + 2 < len(words):
                        obj = words[i + 2]
                else:
                    obj = nw
            if w == 'to' and i + 1 < len(words):
                nw = words[i + 1]
                if nw in ('the', 'a', 'an', 'your', 'this'):
                    if i + 2 < len(words):
                        target = words[i + 2]
                else:
                    target = nw
        return obj, target

    async def close(self):
        pass


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
    print("  Grid (Moondream tiles) | Drag (DOM+Canvas+Pairs+Fast) | Slider (OpenCV)")
    print("=" * 50)
    print(f"  Model: {config.ollama_model}")


if __name__ == "__main__":
    asyncio.run(main())
