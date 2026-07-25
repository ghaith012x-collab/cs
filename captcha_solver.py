"""
Discord hCaptcha Solver - Qwen2.5-VL powered via Ollama.
KEY OPTIMIZATION: Splits captcha grid into individual tiles and sends them
ALL in ONE query as separate small images. Each tile is ~9x smaller than
the full grid, so inference is MUCH faster on CPU.
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


def _resize_image(img: Image.Image, max_dim: int = 256) -> Image.Image:
    """Resize image so longest side <= max_dim, maintaining aspect ratio."""
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    scale = max_dim / max(w, h)
    new_w, new_h = int(w * scale), int(h * scale)
    return img.resize((new_w, new_h), Image.LANCZOS)


def _img_to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


class OllamaVisionClient:
    """Ollama client for Qwen2.5-VL vision inference.
    Supports batch queries with multiple images."""

    _session: Optional[aiohttp.ClientSession] = None

    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.base_url = config.ollama_base_url
        self.model = config.ollama_model
        self.timeout = config.ollama_timeout
        self.num_ctx = config.ollama_num_ctx
        self.temperature = config.ollama_temperature
        self._log = log or (lambda msg, level="info": None)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def query(self, prompt: str, images_b64: List[str], max_tokens: int = 300) -> str:
        """Query model with one or more images. Multiple images = tile-by-tile analysis."""
        session = await self._get_session()
        total_kb = sum(len(img) for img in images_b64) // 1024
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": images_b64,
            "stream": False,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature,
                "num_predict": max_tokens,
            }
        }
        self._log(f"[Ollama] {self.model} ({len(images_b64)} images, ~{total_kb}KB total)")
        try:
            async with session.post(f"{self.base_url}/api/generate", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response = data.get("response", "").strip()
                    if response:
                        self._log(f"[Ollama] → {response[:250]}")
                    else:
                        self._log("[Ollama] Empty response", level="warn")
                    return response
                err = await resp.text()
                self._log(f"[Ollama] HTTP {resp.status}: {err[:200]}", level="error")
                return ""
        except asyncio.TimeoutError:
            self._log(f"[Ollama] TIMEOUT after {self.timeout}s", level="error")
            return ""
        except Exception as e:
            self._log(f"[Ollama] Error: {e}", level="error")
            return ""

    async def query_with_retry(self, prompt: str, images_b64: List[str],
                               max_tokens: int = 300, retries: int = 1) -> str:
        for attempt in range(retries + 1):
            result = await self.query(prompt, images_b64, max_tokens)
            if result:
                return result
            if attempt < retries:
                await asyncio.sleep(1)
        return ""

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


STEALTH_SCRIPT = """
(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
    Object.defineProperty(navigator, 'languages', { get: () => Object.freeze(['en-US', 'en']) });
    Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
    Object.defineProperty(screen, 'width', { get: () => 1920 });
    Object.defineProperty(screen, 'height', { get: () => 1080 });
    Object.defineProperty(window, 'outerWidth', { get: () => 1920 });
    Object.defineProperty(window, 'outerHeight', { get: () => 1080 });
    const origRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (origRTC) {
        window.RTCPeerConnection = function(...args) {
            const c = args[0] || {}; c.iceTransportPolicy = 'relay';
            return new origRTC(c, ...args.slice(1));
        };
        window.RTCPeerConnection.prototype = origRTC.prototype;
    }
})();
"""


class HumanMouse:
    """Human-like mouse movement with minimum-jerk trajectory."""

    @staticmethod
    def _minimum_jerk(t: float) -> float:
        return 10 * t**3 - 15 * t**4 + 6 * t**5

    @staticmethod
    def _generate_path(sx: float, sy: float, ex: float, ey: float) -> List[Tuple[float, float]]:
        dist = math.hypot(ex - sx, ey - sy)
        n = max(18, min(80, int(dist / 4)))
        path = []
        for i in range(n):
            t = i / (n - 1)
            x = sx + (ex - sx) * HumanMouse._minimum_jerk(t)
            y = sy + (ey - sy) * HumanMouse._minimum_jerk(t)
            path.append((x + random.gauss(0, 0.5), y + random.gauss(0, 0.5)))
        return path

    @staticmethod
    async def move_and_click(page: Page, tx: float, ty: float, click: bool = True):
        sx = tx + random.uniform(-30, 30)
        sy = ty + random.uniform(-30, 30)
        path = HumanMouse._generate_path(sx, sy, tx, ty)
        for x, y in path:
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.003, 0.008))
        if click:
            await asyncio.sleep(random.uniform(0.02, 0.05))
            await page.mouse.click(tx, ty)

    @staticmethod
    async def human_drag(page: Page, sx: float, sy: float, ex: float, ey: float):
        await HumanMouse.move_and_click(page, sx, sy, click=False)
        await asyncio.sleep(0.08)
        await page.mouse.down()
        path = HumanMouse._generate_path(sx, sy, ex, ey)
        for x, y in path:
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.004, 0.01))
        await asyncio.sleep(0.06)
        await page.mouse.up()


class Verifier:
    """Strict captcha-solved check - only trusts hard proof."""

    def __init__(self, page: Page):
        self.page = page

    async def is_solved(self) -> bool:
        token = await self.page.evaluate("""
            () => {
                const ta = document.querySelector('textarea[name="h-captcha-response"]');
                return ta && ta.value && ta.value.length > 20 ? ta.value : '';
            }
        """)
        if token:
            return True
        try:
            url = self.page.url
            if any(kw in url for kw in ['channels', 'app', 'verify', 'welcome']):
                return True
        except:
            pass
        return False


class GridSolver:
    """Solves grid captchas by splitting into individual tiles and sending
    all tiles as separate images in ONE Qwen2.5-VL query.
    Each tile is small (~170x190px), so inference is fast even on CPU."""

    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda msg, level="info": None)
        self.ollama: Optional[OllamaVisionClient] = None

    async def _get_ollama(self):
        if self.ollama is None:
            self.ollama = OllamaVisionClient(self.config, log=self._log)
        return self.ollama

    async def solve(self, page: Page) -> bool:
        self._log("[GridSolver] Starting...")

        # 1. Find the challenge iframe
        iframe = await self._find_challenge_iframe(page)
        if not iframe:
            self._log("[GridSolver] No challenge iframe found", level="error")
            return False

        # 2. Get challenge text
        challenge_text = await self._get_challenge_text(page)

        # 3. Screenshot the iframe area
        box = await iframe.bounding_box()
        if not box or box['width'] < 80:
            self._log("[GridSolver] Iframe bounding box too small", level="error")
            return False

        self._log(f"[GridSolver] Captured {box['width']:.0f}x{box['height']:.0f} challenge area")
        try:
            screenshot_bytes = await page.screenshot(clip={
                "x": box['x'], "y": box['y'],
                "width": box['width'], "height": box['height']
            })
        except Exception as e:
            self._log(f"[GridSolver] Screenshot failed: {e}", level="error")
            return False

        # 4. Split into individual tile images
        tiles_img = await self._extract_tiles(page, box)
        if not tiles_img:
            # Fallback: split the screenshot manually into 3x3 grid
            tiles_img = self._split_grid(screenshot_bytes, cols=3)

        self._log(f"[GridSolver] Split into {len(tiles_img)} tiles")

        # 5. Resize each tile to small (max 140px) for fast inference
        tile_b64s = []
        for tile in tiles_img:
            small = _resize_image(tile, max_dim=140)
            tile_b64s.append(_img_to_b64(small))

        prompt = (f"hCaptcha challenge: \"{challenge_text or 'Select matching images'}\"\\n"
                  f"Tile 0 to {len(tile_b64s)-1}. For each tile, reply YES if it matches the challenge text, "
                  f"NO if not.\\n"
                  f"Reply with a JSON array of matching tile numbers ONLY. Example: [0,3,5] or []")

        # 6. Send ALL tile images in ONE query
        ollama = await self._get_ollama()
        answer = await ollama.query_with_retry(prompt, tile_b64s, max_tokens=100)
        tile_nums = self._parse_tiles(answer)

        if not tile_nums:
            # Retry with simpler prompt
            self._log("[GridSolver] Retrying with simpler prompt...")
            answer2 = await ollama.query_with_retry(
                "Matching tile numbers as JSON array?", tile_b64s, max_tokens=100)
            tile_nums = self._parse_tiles(answer2)

        if not tile_nums:
            self._log("[GridSolver] No matching tiles identified", level="error")
            return False

        self._log(f"[GridSolver] Clicking tiles: {tile_nums}")

        # 7. Click the identified tiles
        tile_boxes = await self._get_tile_boxes(page, box)
        for idx in tile_nums:
            if idx < len(tile_boxes):
                x, y, w, h = tile_boxes[idx]
                cx = x + w/2 + random.uniform(-3, 3)
                cy = y + h/2 + random.uniform(-3, 3)
                await HumanMouse.move_and_click(page, cx, cy)
                await asyncio.sleep(0.12)

        # 8. Click verify/submit button
        await self._click_verify(page)
        await asyncio.sleep(1.5)

        solved = await Verifier(page).is_solved()
        self._log(f"[GridSolver] {'✓ SOLVED' if solved else '✗ Not solved'}")
        return solved

    async def _find_challenge_iframe(self, page: Page):
        selectors = [
            "iframe[src*='newassets.hcaptcha.com/captcha']",
            "iframe[src*='hcaptcha.com/captcha']",
            "iframe[title*='hCaptcha challenge']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                for i in range(count):
                    box = await loc.nth(i).bounding_box()
                    if box and box['width'] > 80 and box['height'] > 80:
                        self._log(f"[GridSolver] Iframe via: {sel} ({box['width']:.0f}x{box['height']:.0f})")
                        return loc.nth(i)
            except:
                continue
        return None

    async def _get_challenge_text(self, page: Page) -> str:
        for sel in ["iframe[src*='newassets.hcaptcha.com/captcha']", "iframe[src*='hcaptcha.com/captcha']"]:
            try:
                frame = page.frame_locator(sel)
                for text_sel in [".challenge-header .prompt-text", ".prompt-text", ".task-text", "h2"]:
                    try:
                        el = frame.locator(text_sel)
                        if await el.count() > 0:
                            text = (await el.first.text_content() or "").strip()
                            if text:
                                return text
                    except:
                        continue
            except:
                continue
        return ""

    async def _extract_tiles(self, page: Page, iframe_box: dict) -> Optional[List[Image.Image]]:
        """Try to screenshot individual tile elements from the iframe."""
        for sel in ["iframe[src*='newassets.hcaptcha.com/captcha']", "iframe[src*='hcaptcha.com/captcha']"]:
            try:
                frame = page.frame_locator(sel)
                for tile_sel in [".task-image .image", ".task-image img", ".challenge-item img"]:
                    tiles = frame.locator(tile_sel)
                    count = await tiles.count()
                    if count > 0:
                        imgs = []
                        for i in range(count):
                            try:
                                b = await tiles.nth(i).bounding_box()
                                if b and b['width'] > 20:
                                    ss = await tiles.nth(i).screenshot()
                                    imgs.append(Image.open(io.BytesIO(ss)))
                            except:
                                continue
                        if imgs:
                            return imgs
            except:
                continue
        return None

    def _split_grid(self, screenshot_bytes: bytes, cols: int = 3) -> List[Image.Image]:
        """Split a captcha screenshot into a grid of individual tile images."""
        img = Image.open(io.BytesIO(screenshot_bytes))
        w, h = img.size
        # The grid is usually in the bottom ~60% of the captcha iframe
        # Header is ~15%, tiles start at ~15% from top
        grid_top = int(h * 0.12)
        grid_height = h - grid_top
        rows = cols
        tw = w // cols
        th = grid_height // rows
        tiles = []
        for r in range(rows):
            for c in range(cols):
                tile = img.crop((c * tw, grid_top + r * th,
                                 (c + 1) * tw, grid_top + (r + 1) * th))
                tiles.append(tile)
        return tiles

    def _parse_tiles(self, answer: str) -> List[int]:
        if not answer:
            return []
        answer = re.sub(r'```(?:json)?\s*|\s*```', '', answer).strip()
        start = answer.find('[')
        end = answer.rfind(']')
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            parsed = json.loads(answer[start:end+1])
            if not isinstance(parsed, list):
                return []
            if all(isinstance(x, (int, float)) for x in parsed):
                return [int(x) for x in parsed if 0 <= int(x) <= 50]
            nums = []
            for item in parsed:
                if isinstance(item, dict):
                    for key in ['number', 'tile', 'index', 'id']:
                        val = item.get(key)
                        if isinstance(val, (int, float)) and 0 <= int(val) <= 50:
                            nums.append(int(val))
                            break
            if nums:
                return nums
        except (json.JSONDecodeError, ValueError):
            pass
        return []

    async def _get_tile_boxes(self, page: Page, iframe_box: dict) -> List[Tuple[float, float, float, float]]:
        """Get bounding boxes for each tile, either from iframe elements or estimated."""
        for sel in ["iframe[src*='newassets.hcaptcha.com/captcha']", "iframe[src*='hcaptcha.com/captcha']"]:
            try:
                frame = page.frame_locator(sel)
                for tile_sel in [".task-image .image", ".task-image img", ".challenge-item img"]:
                    tiles = frame.locator(tile_sel)
                    count = await tiles.count()
                    if count > 0:
                        result = []
                        for i in range(count):
                            box = await tiles.nth(i).bounding_box()
                            if box:
                                result.append((box['x'], box['y'], box['width'], box['height']))
                        if result:
                            return result
            except:
                continue
        # Fallback: estimate grid
        cols = 3
        bx, by, bw, bh = iframe_box['x'], iframe_box['y'], iframe_box['width'], iframe_box['height']
        grid_top = int(by + bh * 0.12)
        grid_bottom = by + bh
        gh = grid_bottom - grid_top
        tw = bw // cols
        th = gh // cols
        result = []
        for r in range(cols):
            for c in range(cols):
                result.append((bx + c * tw, grid_top + r * th, tw, th))
        return result

    async def _click_verify(self, page: Page):
        for sel in ["iframe[src*='newassets.hcaptcha.com/captcha']", "iframe[src*='hcaptcha.com/captcha']"]:
            try:
                frame = page.frame_locator(sel)
                for btn_sel in ["button.verifybtn", ".button-submit", "button[type='submit']"]:
                    btn = frame.locator(btn_sel)
                    if await btn.count() > 0:
                        box = await btn.first.bounding_box()
                        if box:
                            await HumanMouse.move_and_click(page, box['x'] + box['width']/2, box['y'] + box['height']/2)
                            return
            except:
                continue

    async def close(self):
        if self.ollama:
            await self.ollama.close()


class SliderSolver:
    """Solves slider/puzzle captchas with OpenCV template matching."""

    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda msg, level="info": None)

    async def solve(self, page: Page) -> bool:
        self._log("[SliderSolver] Solving with OpenCV...")

        handle_sel = track_sel = piece_sel = bg_sel = None
        groups = [
            ('.slider-handle', '.slider-track', '.puzzle-image', '.background-image'),
            ('.slide-btn', '.slide-track', '.puzzle-piece', '.bg-image'),
            ('[role="slider"]', '.slider-container', '.jigsaw-piece', '.jigsaw-bg'),
        ]
        for h, t, p, b in groups:
            try:
                if await page.locator(h).count() > 0:
                    handle_sel, track_sel, piece_sel, bg_sel = h, t, p, b
                    break
            except:
                continue

        if not handle_sel:
            for ifs in ["iframe[src*='newassets.hcaptcha.com/captcha']", "iframe[src*='hcaptcha.com/captcha']"]:
                frame = page.frame_locator(ifs)
                for h, t, p, b in groups:
                    try:
                        if await frame.locator(h).count() > 0:
                            handle_sel, track_sel, piece_sel, bg_sel = h, t, p, b
                            break
                    except:
                        continue
                if handle_sel:
                    break

        if not handle_sel:
            self._log("[SliderSolver] No slider found", level="warn")
            return False

        for attempt in range(2):
            self._log(f"[SliderSolver] Attempt {attempt + 1}/2")
            try:
                await page.wait_for_selector(handle_sel, timeout=5000)
            except:
                continue

            hb = await self._bounds(page, handle_sel)
            tb = await self._bounds(page, track_sel) if track_sel else hb
            if not hb or not tb:
                continue

            sx = hb['x'] + hb['width'] / 2
            sy = hb['y'] + hb['height'] / 2

            bg = await self._ss(page, bg_sel) if bg_sel else None
            piece = await self._ss(page, piece_sel) if piece_sel else None
            if bg is None:
                bg_bytes = await page.screenshot()
                bg = Image.open(io.BytesIO(bg_bytes))

            offset = self._calc_offset(bg, piece)
            if offset is None:
                offset = int((tb['width'] - hb['width']) * 0.6)

            offset += random.randint(-3, 3)
            tr = tb['x'] + tb['width']
            tx = max(tb['x'] + 5, min(sx + offset, tr - hb['width'] / 2))
            self._log(f"[SliderSolver] Dragging to {tx:.0f} (offset={offset})")

            await HumanMouse.human_drag(page, sx, sy, tx, sy)
            await asyncio.sleep(0.8)

            if await Verifier(page).is_solved():
                self._log("[SliderSolver] SOLVED!")
                return True

            await asyncio.sleep(0.5)
        self._log("[SliderSolver] Failed after 2 attempts", level="error")
        return False

    async def _bounds(self, page: Page, sel: str) -> Optional[dict]:
        try:
            el = page.locator(sel)
            if await el.count() > 0 and await el.is_visible():
                return await el.bounding_box()
        except:
            pass
        return None

    async def _ss(self, page: Page, sel: str) -> Optional[Image.Image]:
        try:
            el = page.locator(sel)
            if await el.count() > 0 and await el.is_visible():
                return Image.open(io.BytesIO(await el.first.screenshot()))
        except:
            pass
        return None

    def _calc_offset(self, bg: Image.Image, piece: Optional[Image.Image]) -> Optional[int]:
        if bg is None:
            return None
        bg_gray = np.array(bg.convert('L'))
        offsets = []
        if piece is not None:
            pg = np.array(piece.convert('L'))
            try:
                res = cv2.matchTemplate(bg_gray, pg, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(res)
                if mv > 0.4:
                    offsets.append(ml[0])
            except:
                pass
            try:
                eb = cv2.Canny(bg_gray, 80, 200)
                ep = cv2.Canny(pg, 80, 200)
                res = cv2.matchTemplate(eb, ep, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(res)
                if mv > 0.3:
                    offsets.append(ml[0])
            except:
                pass
        return int(np.median(offsets)) if offsets else None

    async def close(self):
        pass


class MasterSolver:
    """Routes to GridSolver (Qwen) or SliderSolver (OpenCV)."""

    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda msg, level="info": None)
        self.grid = GridSolver(config, log=self._log)
        self.slider = SliderSolver(config, log=self._log)

    async def solve(self, page: Page) -> bool:
        self._log("[MasterSolver] Detecting challenge type...")
        ctype = await self._detect(page)
        self._log(f"[MasterSolver] Detected = '{ctype}'")
        solver = self.slider if ctype == "slider" else self.grid
        success = await solver.solve(page)
        self._log(f"[MasterSolver] {'SUCCESS' if success else 'FAILED'}")
        return success

    async def _detect(self, page: Page) -> str:
        slider_signals = ['[role="slider"]', '.slider-handle', '.slide-btn', '.handler']
        for sig in slider_signals:
            try:
                if await page.locator(sig).count() > 0:
                    return "slider"
            except:
                continue
        for ifs in ["iframe[src*='newassets.hcaptcha.com/captcha']", "iframe[src*='hcaptcha.com/captcha']"]:
            try:
                frame = page.frame_locator(ifs)
                for sig in slider_signals:
                    if await frame.locator(sig).count() > 0:
                        return "slider"
            except:
                continue
        return "grid"

    async def close(self):
        await self.grid.close()
        await self.slider.close()


async def main():
    config = SolverConfig()
    solver = MasterSolver(config)
    print("=" * 60)
    print("  MasterSolver - Tile-split Qwen2.5-VL")
    print("=" * 60)
    print(f"  Model: {config.ollama_model}")
    print("  Strategy: split grid→9 tiles→1 batch query")
    print("= " * 30)


if __name__ == "__main__":
    asyncio.run(main())
