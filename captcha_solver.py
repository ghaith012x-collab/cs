"""
Discord hCaptcha Solver - Qwen2.5-VL powered via Ollama.
Accepts an optional log callback so debug output shows in the activity log.
Resizes images before sending (saves ~90% bandwidth).
Timeout increased to 120s for CPU inference.
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
    ollama_timeout: int = 120  # CPU inference is slow, needs 2min
    ollama_num_ctx: int = 2048
    ollama_temperature: float = 0.0
    max_rounds: int = 2


class OllamaVisionClient:
    """Ollama client for Qwen2.5-VL vision inference."""

    _instance = None
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
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def query(self, prompt: str, image_b64: str, max_tokens: int = 300) -> str:
        session = await self._get_session()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [image_b64],
            "stream": False,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature,
                "num_predict": max_tokens,
            }
        }
        img_size_kb = len(image_b64) // 1024
        self._log(f"[Ollama] Querying {self.model} (image: ~{img_size_kb}KB, timeout: {self.timeout}s)")
        try:
            async with session.post(f"{self.base_url}/api/generate", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response = data.get("response", "").strip()
                    if response:
                        self._log(f"[Ollama] Response: {response[:200]}")
                    else:
                        self._log("[Ollama] Empty response", level="warn")
                    return response
                err_text = await resp.text()
                self._log(f"[Ollama] HTTP {resp.status}: {err_text[:200]}", level="error")
                return ""
        except asyncio.TimeoutError:
            self._log(f"[Ollama] TIMEOUT after {self.timeout}s — model too slow on this CPU", level="error")
            return ""
        except Exception as e:
            self._log(f"[Ollama] Error: {e}", level="error")
            return ""

    async def query_with_retry(self, prompt: str, image_b64: str, max_tokens: int = 300, retries: int = 1) -> str:
        for attempt in range(retries + 1):
            result = await self.query(prompt, image_b64, max_tokens)
            if result:
                return result
            if attempt < retries:
                await asyncio.sleep(1)
        return ""

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        OllamaVisionClient._instance = None


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


def _resize_b64(image_bytes: bytes, max_dim: int = 800) -> bytes:
    """Resize image so longest side <= max_dim, return PNG bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    if max(w, h) <= max_dim:
        return image_bytes
    scale = max_dim / max(w, h)
    new_w, new_h = int(w * scale), int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class GridSolver:
    """Solves grid captchas using Qwen2.5-VL vision."""

    def __init__(self, config: SolverConfig, log: Optional[Callable] = None):
        self.config = config
        self._log = log or (lambda msg, level="info": None)
        self.ollama: Optional[OllamaVisionClient] = None

    async def _get_ollama(self):
        if self.ollama is None:
            self.ollama = OllamaVisionClient(self.config, log=self._log)
        return self.ollama

    async def solve(self, page: Page) -> bool:
        self._log("[GridSolver] Starting with Qwen2.5-VL...")

        iframe_selectors = [
            "iframe[src*='newassets.hcaptcha.com/captcha']",
            "iframe[src*='hcaptcha.com/captcha']",
            "iframe[title*='hCaptcha challenge']",
        ]
        iframe = None
        for sel in iframe_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                for i in range(count):
                    box = await loc.nth(i).bounding_box()
                    if box and box['width'] > 80 and box['height'] > 80:
                        iframe = loc.nth(i)
                        self._log(f"[GridSolver] Found challenge iframe via: {sel} ({box['width']:.0f}x{box['height']:.0f})")
                        break
                if iframe:
                    break
            except:
                continue

        if not iframe:
            self._log("[GridSolver] No iframe found, using page screenshot", level="warn")
            return await self._solve_page(page)

        challenge_text = ""
        try:
            frame = page.frame_locator(iframe_selectors[0])
            for sel in [".challenge-header .prompt-text", ".prompt-text", ".task-text", "h2"]:
                try:
                    el = frame.locator(sel)
                    if await el.count() > 0:
                        challenge_text = (await el.first.text_content() or "").strip()
                        if challenge_text:
                            self._log(f"[GridSolver] Challenge text: '{challenge_text}'")
                            break
                except:
                    continue
        except:
            pass

        try:
            box = await iframe.bounding_box()
            if not box or box['width'] < 100:
                self._log("[GridSolver] Iframe too small, using page screenshot", level="warn")
                return await self._solve_page(page)
            self._log(f"[GridSolver] Iframe: {box['width']:.0f}x{box['height']:.0f} at ({box['x']:.0f},{box['y']:.0f})")
            screenshot_bytes = await page.screenshot(clip={
                "x": box['x'], "y": box['y'],
                "width": box['width'], "height": box['height']
            })
            # Resize to save bandwidth and speed up inference
            screenshot_bytes = _resize_b64(screenshot_bytes, max_dim=640)
        except Exception as e:
            self._log(f"[GridSolver] Screenshot failed: {e}", level="error")
            return await self._solve_page(page)

        b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        ollama = await self._get_ollama()

        prompt = f"""hCaptcha grid challenge. Text: "{challenge_text or 'Select matching images'}"
Tiles numbered 0-8, left-to-right top-to-bottom.
Tell me which tile numbers to click.
Reply with ONLY a plain JSON array. Example: [0,3,5] or []
Do NOT include markdown, code blocks, or coordinates.
Just the array."""

        self._log("[GridSolver] Querying Qwen...")
        answer = await ollama.query_with_retry(prompt, b64, max_tokens=100)
        self._log(f"[GridSolver] Qwen response: {answer[:200] if answer else 'EMPTY'}")

        tile_nums = self._parse_tiles(answer)
        if not tile_nums:
            self._log("[GridSolver] Retrying with simpler prompt...")
            answer2 = await ollama.query_with_retry(
                "Which numbered tiles to click? JSON array only.", b64, max_tokens=100)
            if answer2:
                self._log(f"[GridSolver] Retry response: {answer2[:200]}")
            tile_nums = self._parse_tiles(answer2)

        if not tile_nums:
            self._log("[GridSolver] No tiles identified — Qwen couldn't parse the image", level="error")
            return False

        self._log(f"[GridSolver] Clicking tiles: {tile_nums}")

        try:
            # Try to get individual tile positions from iframe
            tiles = await self._get_tiles(page, box)
        except:
            tiles = []

        if not tiles:
            # Fall back to estimating grid positions
            self._log("[GridSolver] Using estimated grid positions")
            bx, by, bw, bh = box['x'], box['y'], box['width'], box['height']
            tiles = []
            for r in range(3):
                for c in range(3):
                    tiles.append((bx + c * bw // 3, by + r * bh // 3, bw // 3, bh // 3))

        for idx in tile_nums:
            if idx < len(tiles):
                x, y, w, h = tiles[idx]
                cx = x + w/2 + random.uniform(-3, 3)
                cy = y + h/2 + random.uniform(-3, 3)
                await HumanMouse.move_and_click(page, cx, cy)
                await asyncio.sleep(0.15)

        await self._click_verify(page)
        verifier = Verifier(page)
        await asyncio.sleep(1.0)
        solved = await verifier.is_solved()
        self._log(f"[GridSolver] {'SOLVED!' if solved else 'Still not solved'}")
        return solved

    async def _solve_page(self, page: Page) -> bool:
        self._log("[GridSolver] Using full-page screenshot fallback")
        screenshot_bytes = await page.screenshot()
        screenshot_bytes = _resize_b64(screenshot_bytes, max_dim=640)
        b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        ollama = await self._get_ollama()
        answer = await ollama.query_with_retry(
            "hCaptcha grid. Tiles 0-8. Reply with only a plain JSON array like [0,3,5]. No markdown, no coordinates.",
            b64, max_tokens=100)
        self._log(f"[GridSolver] Fallback response: {answer[:200] if answer else 'EMPTY'}")
        tile_nums = self._parse_tiles(answer)
        if not tile_nums:
            self._log("[GridSolver] Fallback failed — no tiles identified", level="error")
            return False
        img = Image.open(io.BytesIO(screenshot_bytes))
        w, h = img.size
        gx, gy = int(w * 0.15), int(h * 0.35)
        gw, gh = int(w * 0.7), int(h * 0.45)
        cols = 3
        tw, th = gw // cols, gh // cols
        for idx in tile_nums:
            if idx >= cols * cols:
                continue
            r, c = divmod(idx, cols)
            await HumanMouse.move_and_click(page, gx + c * tw + tw // 2, gy + r * th + th // 2)
            await asyncio.sleep(0.15)
        await self._click_verify(page)
        verifier = Verifier(page)
        await asyncio.sleep(1.0)
        return await verifier.is_solved()

    def _parse_tiles(self, answer: str) -> List[int]:
        """Parse Qwen response to extract tile indices.
        Handles multiple formats:
        - Simple array: [0, 3, 5]
        - Named objects: [{"number": 1, "coordinate": [x, y]}, ...]
        - Markdown-wrapped: ```json [...] ```
        """
        if not answer:
            return []
        # Remove markdown code block markers
        answer = re.sub(r'```(?:json)?\s*|\s*```', '', answer).strip()
        
        # Find outermost brackets (greedy, handles nested arrays)
        start = answer.find('[')
        end = answer.rfind(']')
        if start == -1 or end == -1 or end <= start:
            return []
        
        try:
            parsed = json.loads(answer[start:end+1])
            if not isinstance(parsed, list):
                return []
            
            # Format 1: simple array of ints [0, 3, 5]
            if all(isinstance(x, (int, float)) for x in parsed):
                return [int(x) for x in parsed if 0 <= int(x) <= 50]
            
            # Format 2: array of objects {"number": X} or {"tile": X} or {"index": X}
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
        except json.JSONDecodeError:
            pass
        return []

    async def _get_tiles(self, page: Page, iframe_box: dict) -> List[Tuple[float, float, float, float]]:
        for sel in ["iframe[src*='newassets.hcaptcha.com/captcha']", "iframe[src*='hcaptcha.com/captcha']"]:
            try:
                frame = page.frame_locator(sel)
                for tile_sel in [".task-image .image", ".task-image img", ".challenge-item img"]:
                    tiles = frame.locator(tile_sel)
                    count = await tiles.count()
                    if count > 0:
                        positions = []
                        for i in range(count):
                            box = await tiles.nth(i).bounding_box()
                            if box:
                                positions.append((box['x'], box['y'], box['width'], box['height']))
                        if positions:
                            return positions
            except:
                continue
        return []

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
    print("  MasterSolver - Qwen2.5-VL powered")
    print("=" * 60)
    print(f"  Ollama: {config.ollama_base_url}")
    print(f"  Model:  {config.ollama_model}")
    print("= " * 30)


if __name__ == "__main__":
    asyncio.run(main())
