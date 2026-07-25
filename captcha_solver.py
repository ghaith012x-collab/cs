"""
hCaptcha Master Solver — supports ALL challenge types:
- GRID: click matching image tiles (Qwen batch tile analysis)
- DRAG: drag object to target (Qwen coordinate detection + human drag)
- SLIDER: puzzle slider (OpenCV template matching + Qwen fallback)

All queries use small images (max 224px) for fast CPU inference.
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
from PIL import Image
from playwright.async_api import Page
import aiohttp


@dataclass
class SolverConfig:
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = field(default_factory=lambda: os.environ.get(
        "OLLAMA_MODEL", "qwen2.5vl:3b"
    ))
    ollama_timeout: int = 120
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
    """Screenshot an iframe element directly, resize, return base64.
    Element-level screenshot is more reliable than page.clip() for iframes."""
    try:
        raw = await iframe.screenshot()
        if not raw or len(raw) < 100:
            return None
        img = Image.open(io.BytesIO(raw))
        return _b64(_resize(img, max_dim))
    except Exception as e:
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

STEALTH = """
(()=>{
Object.defineProperty(navigator,'webdriver',{get:()=>false});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
Object.defineProperty(navigator,'deviceMemory',{get:()=>8});
Object.defineProperty(navigator,'languages',{get:()=>Object.freeze(['en-US','en'])});
})();
"""


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
        """Human-like drag from (sx,sy) to (ex,ey)."""
        # Move to start
        for x, y in Mouse._path(sx + random.uniform(-20, 20), sy + random.uniform(-20, 20), sx, sy):
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.003, 0.006))
        await asyncio.sleep(0.05)
        await page.mouse.down()
        await asyncio.sleep(0.03)
        # Drag to end
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


# ── Drag patterns (offsets from iframe center) ───────────

DRAG_PATTERNS = [
    (100, 0, -120, 0),     # right→left 220px
    (80, 0, -150, 0),      # right→left 230px
    (120, 0, -80, 0),      # right→left 200px
    (-100, 0, 100, 0),     # left→right 200px
    (0, 80, 0, -80),       # bottom→top 160px
    (0, -80, 0, 80),       # top→bottom 160px
    (140, 30, -100, -30),  # diagonal down-right→up-left
    (-140, -30, 100, 30),  # diagonal up-left→down-right
    (0, 0, -180, 0),       # from center left 180px
    (0, 0, 180, 0),        # from center right 180px
    (0, 0, 0, -120),       # from center up 120px
    (0, 0, 0, 120),        # from center down 120px
]


# ── Iframe helpers ────────────────────────────────────────

IFRAME_SELS = [
    "iframe[src*='newassets.hcaptcha.com/captcha']",
    "iframe[src*='hcaptcha.com/captcha']",
    "iframe[title*='hCaptcha challenge']",
]


async def _find_iframe(page: Page, min_size: int = 80):
    """Find the largest captcha iframe (not the checkbox)."""
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
    """Click the verify/submit button inside the captcha iframe only.
    NEVER searches the main page (was clicking Skip buttons on Discord).
    Uses exact hCaptcha button selectors only."""
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

        # Take screenshot of challenge area via element-level screenshot
        ss = await _iframe_b64(iframe, max_dim=400)
        if not ss:
            self._log("[Grid] Iframe screenshot failed, retrying via page clip...", level="warn")
            try:
                raw = await page.screenshot(clip={"x": box['x'], "y": box['y'], "width": box['width'], "height": box['height']})
                if raw and len(raw) > 100:
                    ss = _b64(_resize(Image.open(io.BytesIO(raw)), 400))
            except:
                pass
        if not ss:
            self._log("[Grid] Screenshot failed", level="error")
            return False

        # Try to get individual tile images
        tiles_b64 = await self._get_tile_b64s(page, box)
        if not tiles_b64:
            # Split full screenshot into 3x3 grid
            self._log("[Grid] Splitting screenshot into tiles")
            raw = await page.screenshot(clip={"x": box['x'], "y": box['y'], "width": box['width'], "height": box['height']})
            tiles_pil = self._split_grid(raw, 3)
            tiles_b64 = [_b64(_resize(t, 140)) for t in tiles_pil]

        self._log(f"[Grid] {len(tiles_b64)} tiles prepared")

        if not self.ollama:
            self.ollama = OllamaClient(self.config, log=self._log)

        # Query: send ALL tiles as separate images
        p = (f"hCaptcha grid: \"{text or 'Select matching images'}\"\n"
             f"Tiles 0-{len(tiles_b64)-1}. Which tiles match? Reply JSON array only. Example: [0,3,5] or []")
        ans = await self.ollama.ask_retry(p, tiles_b64, 100)
        nums = self._parse_nums(ans)

        if not nums:
            self._log("[Grid] Qwen gave empty, trying single-image fallback")
            ans2 = await self.ollama.ask_retry(
                "Which numbered tiles to click? JSON array only.", [ss], 100)
            nums = self._parse_nums(ans2)

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

    def _parse_nums(self, ans: str) -> List[int]:
        if not ans:
            return []
        ans = re.sub(r'```(?:json)?\s*|\s*```', '', ans).strip()
        s, e = ans.find('['), ans.rfind(']')
        if s == -1 or e == -1:
            return []
        try:
            p = json.loads(ans[s:e + 1])
            if not isinstance(p, list):
                return []
            if all(isinstance(x, (int, float)) for x in p):
                return [int(x) for x in p if 0 <= int(x) <= 50]
            nums = []
            for item in p:
                if isinstance(item, dict):
                    for k in ['number', 'tile', 'index', 'id']:
                        v = item.get(k)
                        if isinstance(v, (int, float)) and 0 <= int(v) <= 50:
                            nums.append(int(v))
                            break
            return nums
        except:
            return []

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
    """Uses Qwen vision to find source and target coordinates
    in drag-to-target captchas, then performs a human-like drag."""

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

        # Method 1: Try to find draggable element and target from DOM
        src_box, tgt_box = await self._find_drag_targets_dom(page)
        if src_box and tgt_box:
            self._log(f"[Drag] DOM targets: src=({src_box['x']:.0f},{src_box['y']:.0f}) "
                      f"tgt=({tgt_box['x']:.0f},{tgt_box['y']:.0f})")
            await self._do_drag(page, src_box, tgt_box)
            await asyncio.sleep(1)
            if await Verifier(page).solved():
                self._log("[Drag] ✓ Solved via DOM")
                return True

        # Method 2: Try heuristic drag patterns (Qwen CPU inference too slow — skip!)
        self._log(f"[Drag] Trying {len(DRAG_PATTERNS)} heuristic patterns...")
        for pi, (sx, sy, ex, ey) in enumerate(DRAG_PATTERNS):
            # Offset patterns from iframe center for each attempt
            cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
            px, py = cx + sx, cy + sy
            qx, qy = cx + ex, cy + ey
            self._log(f"[Drag] Pattern {pi + 1}/{len(DRAG_PATTERNS)}: ({px:.0f},{py:.0f})→({qx:.0f},{qy:.0f})")
            await Mouse.drag(page, px, py, qx, qy)
            await asyncio.sleep(0.8)
            if await Verifier(page).solved():
                await _click_verify(page)
                await asyncio.sleep(0.5)
                if await Verifier(page).solved():
                    self._log("[Drag] ✓ Solved via heuristic")
                    return True
        
        # Method 3: broader range with varying distances
        self._log("[Drag] Trying broader range...")
        for dist in [60, 90, 120, 150, 180, 210, 250]:
            for dx, dy in [(-dist, 0), (dist, 0), (0, -dist), (0, dist), (-dist, -dist//2)]:
                px = box['x'] + box['width'] / 2 + dx
                py = box['y'] + box['height'] / 2 + dy
                qx = box['x'] + box['width'] / 2 - dx
                qy = box['y'] + box['height'] / 2 - dy
                await Mouse.drag(page, px, py, qx, qy)
                await asyncio.sleep(0.6)
                if await Verifier(page).solved():
                    await _click_verify(page)
                    await asyncio.sleep(0.4)
                    if await Verifier(page).solved():
                        self._log(f"[Drag] ✓ Solved at dist={dist}, dir=({dx},{dy})")
                        return True

        self._log("[Drag] ✗ All methods failed", level="error")
        return False

    async def _find_drag_targets_dom(self, page: Page) -> Tuple[Optional[dict], Optional[dict]]:
        """Try to find draggable and target elements from hCaptcha DOM."""
        for sel in IFRAME_SELS:
            try:
                f = page.frame_locator(sel)
                # Look for draggable elements
                drag_els = f.locator('[class*="drag"], [class*="handle"], [class*="object"], [class*="piece"]')
                n = await drag_els.count()
                if n > 0:
                    src = await drag_els.nth(0).bounding_box()
                    tgt = await drag_els.nth(min(1, n - 1)).bounding_box() if n > 1 else None
                    if src and tgt:
                        return src, tgt
            except:
                continue
        return None, None

    def _parse_coords(self, ans: str, iframe_box: dict) -> Optional[Tuple[float, float, float, float]]:
        """Parse Qwen's JSON coordinate response."""
        if not ans:
            return None
        try:
            ans = re.sub(r'```(?:json)?\s*|\s*```', '', ans).strip()
            data = json.loads(ans)
            src = data.get('source') or data.get('from') or data.get('start')
            tgt = data.get('target') or data.get('to') or data.get('end')
            if src and tgt and len(src) == 2 and len(tgt) == 2:
                # Convert image-local coords to page coords
                sx = iframe_box['x'] + float(src[0])
                sy = iframe_box['y'] + float(src[1])
                ex = iframe_box['x'] + float(tgt[0])
                ey = iframe_box['y'] + float(tgt[1])
                return sx, sy, ex, ey
        except:
            pass
        return None

    async def _do_drag(self, page: Page, src: dict, tgt: dict):
        sx, sy = src['x'] + src['width'] / 2, src['y'] + src['height'] / 2
        ex, ey = tgt['x'] + tgt['width'] / 2, tgt['y'] + tgt['height'] / 2
        await Mouse.drag(page, sx, sy, ex, ey)

    async def close(self):
        if self.ollama:
            await self.ollama.close()


# ═══════════════════════════════════════════════════════════
# 3. SLIDER SOLVER — puzzle slider (improved OpenCV)
# ═══════════════════════════════════════════════════════════

class SliderSolver:
    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda *a: None)

    async def solve(self, page: Page) -> bool:
        self._log("[Slider] Starting...")

        # Find slider handle (try multiple approaches)
        handle = await self._find_slider(page)
        if not handle:
            self._log("[Slider] No slider handle found", level="warn")
            return False

        hb = await handle.bounding_box()
        if not hb:
            return False

        self._log(f"[Slider] Handle at ({hb['x']:.0f},{hb['y']:.0f}) size {hb['width']:.0f}x{hb['height']:.0f}")

        # Determine track bounds and full drag distance
        track_box = await self._find_track(page, hb)
        if track_box:
            max_travel = track_box['width'] - hb['width']
        else:
            max_travel = 250  # default guess

        sx = hb['x'] + hb['width'] / 2
        sy = hb['y'] + hb['height'] / 2

        # Try OpenCV template matching for exact offset
        offsets = await self._get_offsets_opencv(page, hb)

        # If OpenCV failed, try multiple offsets
        if not offsets:
            offsets = [int(max_travel * f) for f in [0.3, 0.45, 0.55, 0.65, 0.75, 0.85, 0.4, 0.6, 0.5, 0.7, 0.35, 0.8]]

        for i, offset in enumerate(offsets):
            offset = max(10, min(offset, int(max_travel)))
            tx = sx + offset + random.randint(-3, 3)
            self._log(f"[Slider] Attempt {i + 1}/{len(offsets)}: drag {offset}px")
            await Mouse.drag(page, sx, sy, tx, sy)
            await asyncio.sleep(0.6)

            if await Verifier(page).solved():
                self._log("[Slider] ✓ Solved")
                return True

            # Reset slider position for next attempt
            await Mouse.drag(page, tx, sy, sx, sy)
            await asyncio.sleep(0.4)

        self._log("[Slider] ✗ Failed", level="error")
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
        # Check inside iframe
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
        """Use OpenCV template matching to find puzzle piece offset."""
        try:
            # Screenshot the slider area
            area = {
                'x': hb['x'] - 50, 'y': hb['y'] - 20,
                'width': 350, 'height': hb['height'] + 40
            }
            raw = await page.screenshot(clip=area)
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
            h, w = img.shape

            # Try to find the puzzle gap using edge detection + contour analysis
            edges = cv2.Canny(img, 50, 150)
            lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 50,
                                     minLineLength=20, maxLineGap=10)
            if lines is not None:
                # Find vertical lines that might indicate puzzle gap edges
                vert_lines = []
                for line in lines:
                    x1, y1, x2, y2 = line[0]
                    if abs(x2 - x1) < 5:  # near-vertical line
                        vert_lines.append((x1 + x2) // 2 + area['x'])
                if vert_lines:
                    # The gap is usually between two vertical lines
                    vert_lines.sort()
                    gaps = []
                    for i in range(len(vert_lines) - 1):
                        gap = vert_lines[i + 1] - vert_lines[i]
                        if 30 < gap < 80:  # puzzle piece width range
                            gaps.append((vert_lines[i] + vert_lines[i + 1]) // 2)
                    if gaps:
                        return [g - hb['x'] for g in gaps]

            # Also try template matching with the handle itself
            handle_img = await page.screenshot(clip={
                'x': hb['x'], 'y': hb['y'], 'width': hb['width'], 'height': hb['height']
            })
            handle_gray = cv2.imdecode(np.frombuffer(handle_img, np.uint8), cv2.IMREAD_GRAYSCALE)
            if handle_gray.shape[0] > 0 and handle_gray.shape[1] > 0:
                res = cv2.matchTemplate(img, handle_gray, cv2.TM_CCOEFF_NORMED)
                _, _, _, max_loc = cv2.minMaxLoc(res)
                # The handle's best match position other than its current location
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
        """Detect challenge type: slider, drag, or grid."""
        # Check for slider signals
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

        # Check challenge text for drag keywords
        text = await _challenge_text(page)
        text_lower = text.lower()
        drag_words = ['drag', 'move', 'slide to', 'place', 'put']
        if any(w in text_lower for w in drag_words):
            # Check for draggable elements
            for isel in IFRAME_SELS:
                try:
                    f = page.frame_locator(isel)
                    drag_sels = ['[class*="drag"]', '[class*="handle"]', '[class*="object"]',
                                 '[draggable]', '[class*="piece"]']
                    for ds in drag_sels:
                        if await f.locator(ds).count() > 0:
                            return "drag"
                except:
                    continue
            # Even without DOM confirmation, assume drag if text says drag
            return "drag"

        # Default: grid
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
    print("  Grid | Drag | Slider")
    print("=" * 50)
    print(f"  Model: {config.ollama_model}")


if __name__ == "__main__":
    asyncio.run(main())
