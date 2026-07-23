
import asyncio
import base64
import io
import json
import math
import os
import re
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import torch
import open_clip
from PIL import Image, ImageFilter, ImageEnhance
from playwright.async_api import async_playwright, Page, BrowserContext
import aiohttp


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class SolverConfig:
    clip_confidence_threshold: float = 0.55
    max_challenge_rounds: int = 5
    timeout: int = 45  # seconds
    headless: bool = True
    browser_type: str = "chromium"
    rate_limit_min_delay: float = 0.08
    rate_limit_max_delay: float = 0.25
    min_solve_time_per_round: float = 1.8
    # Ollama Moondream6 config (replaces OpenAI)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "moondream"
    ollama_timeout: int = 30
    ollama_num_ctx: int = 2048
    ollama_temperature: float = 0.1
    # Legacy names kept for compatibility
    llm_model: str = "moondream"
    llm_fallback: bool = True
    pattern_solver_model: str = "moondream"


# =============================================================================
# OLLAMA VISION CLIENT (Replaces OpenAI)
# =============================================================================

class OllamaVisionClient:
    """Ultra-fast Ollama client optimized for Moondream6 vision inference."""

    _instance = None
    _session: Optional[aiohttp.ClientSession] = None

    @classmethod
    async def get_instance(cls, config: SolverConfig):
        if cls._instance is None:
            cls._instance = cls(config)
        return cls._instance

    def __init__(self, config: SolverConfig):
        self.base_url = config.ollama_base_url
        self.model = config.ollama_model
        self.timeout = config.ollama_timeout
        self.num_ctx = config.ollama_num_ctx
        self.temperature = config.ollama_temperature

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def vision_query(self, prompt: str, image_b64: str, max_tokens: int = 300) -> str:
        """Send a vision query to Ollama Moondream6. Returns the response text."""
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
        try:
            async with session.post(f"{self.base_url}/api/generate", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("response", "").strip()
                else:
                    error_text = await resp.text()
                    print(f"Ollama error ({resp.status}): {error_text}")
                    return ""
        except asyncio.TimeoutError:
            print("Ollama request timed out")
            return ""
        except Exception as e:
            print(f"Ollama request failed: {e}")
            return ""

    async def vision_query_with_retry(self, prompt: str, image_b64: str, max_tokens: int = 300, retries: int = 2) -> str:
        """Vision query with automatic retry on failure."""
        for attempt in range(retries + 1):
            result = await self.vision_query(prompt, image_b64, max_tokens)
            if result:
                return result
            if attempt < retries:
                await asyncio.sleep(0.3 * (attempt + 1))
        return ""

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# =============================================================================
# CLIP MODEL (Singleton) - Optimized with caching
# =============================================================================

class ClipModel:
    _instance = None

    @classmethod
    async def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._load_model()
        return cls._instance

    def __init__(self):
        self.model = None
        self.preprocess = None
        self.tokenizer = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._text_cache: Dict[str, torch.Tensor] = {}

    async def _load_model(self):
        print(f"Loading OpenCLIP ViT-L-14 on {self.device}...")
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-L-14", pretrained="laion2b_s32b_b82k", device=self.device
        )
        self.tokenizer = open_clip.get_tokenizer("ViT-L-14")
        self.model.eval()
        # Enable torch inference mode for speed
        if self.device == "cuda":
            self.model = self.model.half()  # FP16 for 2x speed on GPU
        print("OpenCLIP ViT-L-14 model loaded.")

    async def get_image_features(self, images: List[Image.Image]):
        image_tensors = torch.stack([self.preprocess(img) for img in images]).to(self.device)
        if self.device == "cuda":
            image_tensors = image_tensors.half()
        with torch.no_grad(), torch.amp.autocast(self.device, enabled=(self.device == "cuda")):
            features = self.model.encode_image(image_tensors)
        return features / features.norm(dim=-1, keepdim=True)

    async def get_text_features(self, texts: List[str]):
        # Cache text features for repeated queries
        cache_key = "|".join(sorted(texts[:5]))
        if cache_key in self._text_cache:
            return self._text_cache[cache_key]
        tokens = self.tokenizer(texts).to(self.device)
        with torch.no_grad(), torch.amp.autocast(self.device, enabled=(self.device == "cuda")):
            features = self.model.encode_text(tokens)
        features = features / features.norm(dim=-1, keepdim=True)
        self._text_cache[cache_key] = features
        return features

    async def get_image_features_batch(self, images: List[Image.Image], batch_size: int = 8):
        """Process images in batches for memory efficiency."""
        all_features = []
        for i in range(0, len(images), batch_size):
            batch = images[i:i + batch_size]
            features = await self.get_image_features(batch)
            all_features.append(features)
        return torch.cat(all_features, dim=0)


# =============================================================================
# STEALTH ENGINE
# =============================================================================

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
    Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
    Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
    Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
    Object.defineProperty(window, 'outerWidth', { get: () => 1920 });
    Object.defineProperty(window, 'outerHeight', { get: () => 1080 });
    Object.defineProperty(window, 'innerWidth', { get: () => 1920 });
    Object.defineProperty(window, 'innerHeight', { get: () => 1080 });
    Object.defineProperty(window, 'screenX', { get: () => 0 });
    Object.defineProperty(window, 'screenY', { get: () => 0 });

    const originalQuery = navigator.permissions?.query;
    if (originalQuery) {
        navigator.permissions.query = (parameters) => {
            if (parameters.name === 'notifications') return Promise.resolve({ state: 'prompt', onchange: null });
            return originalQuery.call(navigator.permissions, parameters);
        };
    }

    const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (Intel)';
        if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
        if (param === 3379) return 16384;
        if (param === 34024) return 16384;
        if (param === 3386) return new Int32Array([32767, 32767]);
        return getParameterOrig.call(this, param);
    };

    if (typeof WebGL2RenderingContext !== 'undefined') {
        const getParam2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {
            if (param === 37445) return 'Google Inc. (Intel)';
            if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
            if (param === 3379) return 16384;
            if (param === 34024) return 16384;
            return getParam2.call(this, param);
        };
    }

    if (navigator.getBattery) navigator.getBattery = undefined;

    const origRTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (origRTCPeerConnection) {
        window.RTCPeerConnection = function(...args) {
            const config = args[0] || {};
            config.iceTransportPolicy = 'relay';
            return new origRTCPeerConnection(config, ...args.slice(1));
        };
        window.RTCPeerConnection.prototype = origRTCPeerConnection.prototype;
    }

    Object.defineProperty(navigator, 'userAgentData', {
        get: () => ({
            brands: [
                { brand: 'Not_A Brand', version: '8' },
                { brand: 'Chromium', version: '120' },
                { brand: 'Google Chrome', version: '120' }
            ],
            mobile: false,
            platform: 'Windows',
            getHighEntropyValues: () => Promise.resolve({
                architecture: 'x86', bitness: '64',
                fullVersionList: [
                    { brand: 'Not_A Brand', version: '8.0.0.0' },
                    { brand: 'Chromium', version: '120.0.6099.109' },
                    { brand: 'Google Chrome', version: '120.0.6099.109' }
                ],
                mobile: false, model: '', platform: 'Windows',
                platformVersion: '15.0.0', uaFullVersion: '120.0.6099.109'
            })
        })
    });

    Object.defineProperty(navigator, 'connection', {
        get: () => ({ effectiveType: '4g', rtt: 50, downlink: 10, saveData: false })
    });

    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
        navigator.mediaDevices.enumerateDevices = async () => [
            { deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' },
            { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
            { deviceId: 'default', kind: 'videoinput', label: '', groupId: 'default' }
        ];
    }
})();
"""


# =============================================================================
# HUMAN-LIKE MOUSE MOVEMENT (Optimized)
# =============================================================================

class HumanMouse:
    """Realistic mouse movement using minimum-jerk trajectory model."""

    @staticmethod
    def _minimum_jerk(t: float) -> float:
        return 10 * t**3 - 15 * t**4 + 6 * t**5

    @staticmethod
    def _generate_path(start_x: float, start_y: float, end_x: float, end_y: float) -> List[Tuple[float, float]]:
        distance = math.sqrt((end_x - start_x)**2 + (end_y - start_y)**2)
        num_points = max(15, min(60, int(distance / 5)))  # Fewer points = faster

        overshoot_amount = random.uniform(0, min(15, distance * 0.08))
        overshoot_angle = math.atan2(end_y - start_y, end_x - start_x)
        overshoot_x = end_x + overshoot_amount * math.cos(overshoot_angle)
        overshoot_y = end_y + overshoot_amount * math.sin(overshoot_angle)

        wind_strength = random.uniform(0.05, 0.3) * distance
        wind_angle = overshoot_angle + random.choice([-1, 1]) * math.pi / 2 + random.uniform(-0.2, 0.2)

        cp1_x = start_x + (end_x - start_x) * random.uniform(0.2, 0.4) + wind_strength * math.cos(wind_angle) * 0.5
        cp1_y = start_y + (end_y - start_y) * random.uniform(0.2, 0.4) + wind_strength * math.sin(wind_angle) * 0.5
        cp2_x = start_x + (overshoot_x - start_x) * random.uniform(0.6, 0.8) + wind_strength * math.cos(wind_angle) * 0.2
        cp2_y = start_y + (overshoot_y - start_y) * random.uniform(0.6, 0.8) + wind_strength * math.sin(wind_angle) * 0.2

        path = []
        overshoot_point_idx = int(num_points * random.uniform(0.8, 0.92))

        for i in range(num_points):
            t = i / (num_points - 1)
            if i < overshoot_point_idx:
                prog = i / overshoot_point_idx
                jerk_prog = HumanMouse._minimum_jerk(prog)
                bx = (1 - jerk_prog)**3 * start_x + 3 * (1 - jerk_prog)**2 * jerk_prog * cp1_x + \
                     3 * (1 - jerk_prog) * jerk_prog**2 * cp2_x + jerk_prog**3 * overshoot_x
                by = (1 - jerk_prog)**3 * start_y + 3 * (1 - jerk_prog)**2 * jerk_prog * cp1_y + \
                     3 * (1 - jerk_prog) * jerk_prog**2 * cp2_y + jerk_prog**3 * overshoot_y
            else:
                correction_prog = (i - overshoot_point_idx) / max(1, (num_points - overshoot_point_idx - 1))
                correction_prog = min(1.0, correction_prog)
                bx = overshoot_x + (end_x - overshoot_x) * HumanMouse._minimum_jerk(correction_prog)
                by = overshoot_y + (end_y - overshoot_y) * HumanMouse._minimum_jerk(correction_prog)

            tremor_scale = max(0, 1.0 - t) * random.uniform(0.3, 1.5)
            bx += random.gauss(0, tremor_scale)
            by += random.gauss(0, tremor_scale)
            path.append((bx, by))

        return path

    @staticmethod
    async def move_and_click(page: Page, target_x: float, target_y: float,
                             start_x: Optional[float] = None, start_y: Optional[float] = None,
                             element_width: Optional[float] = None, element_height: Optional[float] = None,
                             click: bool = True):
        if start_x is None:
            start_x = target_x + random.uniform(-40, 40)
        if start_y is None:
            start_y = target_y + random.uniform(-40, 40)

        path = HumanMouse._generate_path(start_x, start_y, target_x, target_y)

        for i, (x, y) in enumerate(path):
            progress = i / len(path)
            speed_factor = math.sin(progress * math.pi)
            base_delay = random.uniform(0.003, 0.012)
            delay = base_delay / max(speed_factor, 0.3)
            await page.mouse.move(x, y)
            await asyncio.sleep(delay)

        if click:
            await asyncio.sleep(random.uniform(0.02, 0.08))
            await page.mouse.click(target_x, target_y)


# =============================================================================
# CHALLENGE DETECTOR (Improved)
# =============================================================================

class ChallengeDetector:
    def __init__(self, page: Page):
        self.page = page

    async def is_solved(self) -> bool:
        """Check multiple indicators that the captcha has been solved."""
        checks = [
            self._check_success_class(),
            self._check_checkbox_checked(),
            self._check_challenge_disappeared(),
        ]
        results = await asyncio.gather(*checks, return_exceptions=True)
        return any(r is True for r in results)

    async def _check_success_class(self) -> bool:
        try:
            return await self.page.locator('.success-text, .challenge-solved, [data-state="solved"]').count() > 0
        except:
            return False

    async def _check_checkbox_checked(self) -> bool:
        try:
            return await self.page.locator('[aria-checked="true"]').count() > 0
        except:
            return False

    async def _check_challenge_disappeared(self) -> bool:
        try:
            iframe_count = await self.page.locator("iframe[src*='newassets.hcaptcha.com/captcha']").count()
            return iframe_count == 0
        except:
            return False

    async def get_challenge_info(self):
        """Returns (challenge_text, tile_images, verify_button_selector, tile_selectors)"""
        try:
            iframe_locator = self.page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
            
            # Get challenge text
            challenge_text = ""
            for selector in [".challenge-header .prompt-text", ".challenge-header", ".task-text"]:
                try:
                    el = iframe_locator.locator(selector)
                    if await el.count() > 0:
                        challenge_text = await el.first.text_content()
                        if challenge_text:
                            break
                except:
                    continue

            # Get tile images
            tile_images = []
            tile_selectors = []
            for selector in [".task-image .image", ".challenge-item img", "img.challenge-image"]:
                try:
                    tiles = iframe_locator.locator(selector)
                    count = await tiles.count()
                    if count > 0:
                        for i in range(count):
                            tile = tiles.nth(i)
                            screenshot_bytes = await tile.screenshot()
                            tile_images.append(Image.open(io.BytesIO(screenshot_bytes)))
                            tile_selectors.append(f"{selector}:nth-child({i+1})")
                        break
                except:
                    continue

            verify_button_selector = "button.verifybtn"
            return (challenge_text, tile_images, verify_button_selector, tile_selectors)
        except Exception as e:
            print(f"Error getting challenge info: {e}")
            return ("", [], "", [])

    async def detect_challenge_type(self) -> str:
        """Detect the type of challenge presented."""
        challenge_type = "unknown"
        try:
            prompt_text = ""
            iframe_locator = self.page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
            for selector in [".challenge-header .prompt-text", ".challenge-header", ".task-text"]:
                try:
                    el = iframe_locator.locator(selector)
                    if await el.count() > 0:
                        prompt_text = (await el.first.text_content() or "").lower()
                        if prompt_text:
                            break
                except:
                    continue

            if any(kw in prompt_text for kw in ["break the pattern", "odd one out", "doesn't belong", "doesn't fit"]):
                challenge_type = "pattern"
            elif any(kw in prompt_text for kw in ["drag", "place", "move", "drop", "fit"]):
                challenge_type = "drag"
            elif any(kw in prompt_text for kw in ["select all", "click all", "containing", "images of", "with a"]):
                challenge_type = "grid"
            elif await self.page.locator('[role="slider"]').count() > 0:
                challenge_type = "slider"
        except:
            pass
        return challenge_type


# =============================================================================
# BASE SOLVER CLASS
# =============================================================================

class CaptchaSolver:
    def __init__(self, config: SolverConfig):
        self.config = config

    async def solve(self, page: Page) -> bool:
        raise NotImplementedError

    async def _rate_limit_delay(self):
        await asyncio.sleep(random.uniform(self.config.rate_limit_min_delay, self.config.rate_limit_max_delay))


# =============================================================================
# PLAYWRIGHT SOLVER
# =============================================================================

class PlaywrightSolver(CaptchaSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)

    async def get_screenshot(self, page: Page, locator_selector: str = None) -> Image.Image:
        if locator_selector:
            locator = page.locator(locator_selector)
            if await locator.is_visible():
                screenshot_bytes = await locator.screenshot()
            else:
                screenshot_bytes = await page.screenshot()
        else:
            screenshot_bytes = await page.screenshot()
        return Image.open(io.BytesIO(screenshot_bytes))

    async def get_element_bounds(self, page: Page, selector: str) -> Optional[Dict[str, float]]:
        element = page.locator(selector)
        if await element.is_visible():
            return await element.bounding_box()
        return None

    async def click_element(self, page: Page, selector: str):
        box = await self.get_element_bounds(page, selector)
        if not box:
            raise ValueError(f"Element {selector} not found or not visible.")
        target_x = box['x'] + box['width'] / 2
        target_y = box['y'] + box['height'] / 2
        await HumanMouse.move_and_click(page, target_x, target_y, element_width=box['width'], element_height=box['height'])


# =============================================================================
# SLIDER SOLVER
# =============================================================================

class SliderSolver(PlaywrightSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)

    async def solve(self, page: Page) -> bool:
        print("Attempting to solve slider captcha...")
        detector = ChallengeDetector(page)

        slider_handle_selector = '.slider-handle'
        slider_track_selector = '.slider-track'
        puzzle_image_selector = '.puzzle-image'
        background_image_selector = '.background-image'

        try:
            await page.wait_for_selector(slider_handle_selector, timeout=5000)
            await page.wait_for_selector(background_image_selector, timeout=5000)
        except:
            print("Slider elements not found")
            return False

        handle_box = await self.get_element_bounds(page, slider_handle_selector)
        track_box = await self.get_element_bounds(page, slider_track_selector)
        if not handle_box or not track_box:
            return False

        start_x = handle_box['x'] + handle_box['width'] / 2
        start_y = handle_box['y'] + handle_box['height'] / 2

        puzzle_image_full = await self.get_screenshot(page, background_image_selector)
        puzzle_piece_image = await self.get_screenshot(page, puzzle_image_selector)

        puzzle_image_np = np.array(puzzle_image_full.convert('L'))
        puzzle_piece_np = np.array(puzzle_piece_image.convert('L'))

        offsets = []

        # Template Matching
        res = cv2.matchTemplate(puzzle_image_np, puzzle_piece_np, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > 0.6:
            offsets.append(max_loc[0])

        # Canny Edge + Template Matching
        edges_puzzle = cv2.Canny(puzzle_image_np, 100, 200)
        edges_piece = cv2.Canny(puzzle_piece_np, 100, 200)
        res_canny = cv2.matchTemplate(edges_puzzle, edges_piece, cv2.TM_CCOEFF_NORMED)
        _, max_val_canny, _, max_loc_canny = cv2.minMaxLoc(res_canny)
        if max_val_canny > 0.5:
            offsets.append(max_loc_canny[0])

        # Phase Correlation
        try:
            h_puzzle, w_puzzle = puzzle_image_np.shape
            h_piece, w_piece = puzzle_piece_np.shape
            padded_piece = np.zeros_like(puzzle_image_np)
            padded_piece[0:h_piece, 0:w_piece] = puzzle_piece_np
            shift, _ = cv2.phaseCorrelate(np.float32(puzzle_image_np), np.float32(padded_piece))
            offsets.append(int(round(shift[0])))
        except:
            pass

        if not offsets:
            return False

        # Consensus
        final_offset = int(np.median(offsets))
        target_drag_x = start_x + final_offset
        track_right = track_box['x'] + track_box['width']
        target_drag_x = max(track_box['x'], min(target_drag_x, track_right - handle_box['width'] / 2))

        # Human-like drag
        await HumanMouse.move_and_click(page, start_x, start_y, click=False)
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.15))

        drag_path = HumanMouse._generate_path(start_x, start_y, target_drag_x, start_y)
        for i, (x, y) in enumerate(drag_path):
            progress = i / len(drag_path)
            speed_factor = math.sin(progress * math.pi)
            delay = random.uniform(0.004, 0.012) / max(speed_factor, 0.3)
            await page.mouse.move(x, y)
            await asyncio.sleep(delay)

        await page.mouse.up()
        await asyncio.sleep(self.config.min_solve_time_per_round)
        return await detector.is_solved()


# =============================================================================
# GOD SOLVER (CLIP + Ollama Moondream6 Fallback)
# =============================================================================

class GodSolver(PlaywrightSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.clip_model: Optional[ClipModel] = None
        self.ollama: Optional[OllamaVisionClient] = None

        self.target_aliases = defaultdict(list)
        self._initialize_aliases()

        self.object_prompts = [
            "a photo of a {target}",
            "a {target} in this image",
            "a clear photo of a {target}",
            "an image containing a {target}",
            "a {target}",
            "a picture of a {target}",
        ]
        self.property_prompts = [
            "an object that is {target}",
            "something primarily {target}",
            "a {target} object",
        ]
        self.context_aware_prompts = [
            "a {target} seen from above",
            "a close-up of a {target}",
            "a {target} in the foreground",
        ]

    def _initialize_aliases(self):
        # Vehicles
        self.target_aliases["car"] = ["automobile", "vehicle", "sedan", "coupe", "SUV", "truck", "pickup", "van", "taxi", "sports car"]
        self.target_aliases["truck"] = ["lorry", "pickup truck", "delivery truck", "semi-trailer truck", "dump truck", "fire truck"]
        self.target_aliases["bus"] = ["coach", "double-decker bus", "school bus", "public transport bus"]
        self.target_aliases["motorcycle"] = ["motorbike", "scooter", "moped", "dirt bike"]
        self.target_aliases["bicycle"] = ["bike", "mountain bike", "road bike"]
        self.target_aliases["boat"] = ["ship", "yacht", "sailboat", "ferry", "canoe", "speedboat"]
        self.target_aliases["airplane"] = ["aircraft", "jet", "plane", "helicopter", "seaplane"]
        self.target_aliases["train"] = ["locomotive", "railway car", "subway", "metro", "tram"]
        # Road elements
        self.target_aliases["traffic light"] = ["stoplight", "traffic signal"]
        self.target_aliases["fire hydrant"] = ["hydrant", "red fire hydrant"]
        self.target_aliases["crosswalk"] = ["zebra crossing", "pedestrian crossing"]
        self.target_aliases["bridge"] = ["overpass", "viaduct", "suspension bridge"]
        self.target_aliases["building"] = ["house", "apartment", "skyscraper", "office building"]
        self.target_aliases["chimney"] = ["smokestack", "flue"]
        self.target_aliases["palm tree"] = ["date palm", "coconut tree"]
        self.target_aliases["tree"] = ["oak tree", "pine tree", "birch tree"]
        self.target_aliases["mountain"] = ["hill", "peak", "summit"]
        # Animals
        self.target_aliases["cat"] = ["kitten", "feline"]
        self.target_aliases["dog"] = ["puppy", "canine"]
        self.target_aliases["bird"] = ["sparrow", "robin", "eagle", "owl", "pigeon"]
        self.target_aliases["horse"] = ["pony", "mare", "stallion"]
        self.target_aliases["cow"] = ["cattle", "calf", "bull"]
        self.target_aliases["elephant"] = ["jumbo"]
        self.target_aliases["lion"] = ["big cat"]
        # Food
        self.target_aliases["pizza"] = ["slice of pizza"]
        self.target_aliases["burger"] = ["hamburger", "cheeseburger"]
        # Objects
        self.target_aliases["chair"] = ["seat", "stool", "armchair"]
        self.target_aliases["table"] = ["desk", "counter"]
        self.target_aliases["lamp"] = ["light", "lantern"]
        self.target_aliases["phone"] = ["smartphone", "mobile phone"]
        self.target_aliases["umbrella"] = ["parasol"]
        # Colors
        self.target_aliases["red"] = ["crimson", "scarlet"]
        self.target_aliases["blue"] = ["azure", "navy"]
        self.target_aliases["green"] = ["emerald", "lime"]
        self.target_aliases["yellow"] = ["gold", "amber"]

        for key in list(self.target_aliases.keys()):
            if key not in self.target_aliases[key]:
                self.target_aliases[key].insert(0, key)

    def _generate_prompts(self, target: str) -> List[str]:
        prompts = []
        aliases = self.target_aliases.get(target.lower(), [target])
        for alias in aliases[:5]:  # Limit aliases for speed
            for p in self.object_prompts:
                prompts.append(p.format(target=alias))
        return list(set(prompts))

    def _parse_challenge_text(self, challenge_text: str) -> Optional[str]:
        patterns = [
            r"Please click each image containing a (.+)",
            r"Select all images with (.+)",
            r"Click on all squares that contain (.+)",
            r"Select all items that are primarily (.+)",
            r"Which images contain a (.+)",
            r"Click all images of a (.+)",
            r"Find all (.+)",
            r"Identify all (.+)",
            r"Select all tiles with a (.+)",
            r"Choose all images showing a (.+)",
            r"Pick the images that have a (.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, challenge_text, re.IGNORECASE)
            if match:
                target = match.group(1).strip()
                if target.endswith('s') and not target.endswith('ss'):
                    return target[:-1]
                return target
        return None

    async def _get_clip_model(self):
        if self.clip_model is None:
            self.clip_model = await ClipModel.get_instance()
        return self.clip_model

    async def _get_ollama(self):
        if self.ollama is None:
            self.ollama = await OllamaVisionClient.get_instance(self.config)
        return self.ollama

    async def _get_tile_scores(self, tile_images: List[Image.Image], target_text: str) -> List[float]:
        clip = await self._get_clip_model()
        all_prompts = self._generate_prompts(target_text)
        text_features = await clip.get_text_features(all_prompts)

        scores = []
        # Process all tiles at once for speed
        all_images = []
        weights = []
        for tile_img in tile_images:
            # Multi-scale: full + center crop
            all_images.append(tile_img)
            weights.append(0.6)
            # Center 70% crop
            w, h = tile_img.size
            cropped = tile_img.crop((int(w*0.15), int(h*0.15), int(w*0.85), int(h*0.85)))
            all_images.append(cropped)
            weights.append(0.4)

        # Batch process all images
        all_features = await clip.get_image_features_batch(all_images)

        for i in range(len(tile_images)):
            full_feat = all_features[i * 2]
            crop_feat = all_features[i * 2 + 1]
            # Weighted similarity
            full_sim = (full_feat.unsqueeze(0) @ text_features.T).mean().item() * 0.6
            crop_sim = (crop_feat.unsqueeze(0) @ text_features.T).mean().item() * 0.4
            scores.append(full_sim + crop_sim)

        return scores

    def _adaptive_thresholding(self, scores: List[float]) -> List[int]:
        if not scores:
            return []

        scores_np = np.array(scores)
        sorted_scores = np.sort(scores_np)

        if np.max(scores_np) < 0.20:
            return []  # Signal for LLM fallback

        # Largest gap method
        if (sorted_scores[-1] - sorted_scores[0]) > 0.10:
            gaps = np.diff(sorted_scores)
            if len(gaps) > 0:
                largest_gap_idx = np.argmax(gaps)
                threshold = sorted_scores[largest_gap_idx] + gaps[largest_gap_idx] * 0.4
            else:
                threshold = np.mean(scores_np)
        else:
            threshold = np.mean(scores_np) + 0.03

        selected_indices = [i for i, score in enumerate(scores) if score >= threshold]

        # Clamp 2-6
        if len(selected_indices) < 2:
            top_indices = np.argsort(scores_np)[::-1]
            selected_indices = list(top_indices[:min(len(scores), 3)])
        elif len(selected_indices) > 6:
            top_scores = [(scores[i], i) for i in selected_indices]
            top_scores.sort(reverse=True)
            selected_indices = [idx for _, idx in top_scores[:6]]

        # Spatial context boost (neighbors of selected tiles get 15% boost)
        grid_size = int(math.sqrt(len(scores)))
        if grid_size * grid_size == len(scores) and grid_size > 1:
            boosted_scores = list(scores_np)
            for idx in list(selected_indices):
                row, col = divmod(idx, grid_size)
                for dr in [-1, 0, 1]:
                    for dc in [-1, 0, 1]:
                        if dr == 0 and dc == 0:
                            continue
                        nr, nc = row + dr, col + dc
                        if 0 <= nr < grid_size and 0 <= nc < grid_size:
                            neighbor_idx = nr * grid_size + nc
                            if neighbor_idx not in selected_indices:
                                boosted_scores[neighbor_idx] *= 1.15

            # Re-threshold with boosted scores
            boosted_np = np.array(boosted_scores)
            new_threshold = threshold * 0.95  # Slightly lower threshold for boosted
            new_selected = [i for i, score in enumerate(boosted_scores) if score >= new_threshold]
            if 2 <= len(new_selected) <= 6:
                selected_indices = new_selected

        return selected_indices

    async def _llm_vision_fallback(self, page: Page, target_text: str, num_tiles: int) -> List[int]:
        """Use Ollama Moondream6 for vision fallback when CLIP confidence is low."""
        print("Falling back to Ollama Moondream6 for captcha solving...")
        try:
            # Take screenshot of challenge area
            screenshot_bytes = await page.screenshot()
            b64_string = base64.b64encode(screenshot_bytes).decode("utf-8")

            ollama = await self._get_ollama()
            prompt = f"This is a captcha grid with {num_tiles} tiles numbered 1-{num_tiles} left-to-right, top-to-bottom. Which tiles contain a {target_text}? Reply with ONLY the numbers separated by commas. Example: 1,5,9"

            answer = await ollama.vision_query_with_retry(prompt, b64_string, max_tokens=50)
            print(f"Moondream6 response: {answer}")

            if answer and answer.lower() != 'none':
                numbers_str = re.findall(r'\d+', answer)
                numbers = [int(n) - 1 for n in numbers_str if 1 <= int(n) <= num_tiles]
                return numbers
        except Exception as e:
            print(f"Ollama vision fallback failed: {e}")
        return []

    async def solve(self, page: Page) -> bool:
        print("Attempting to solve hCaptcha grid challenge with GodSolver...")
        detector = ChallengeDetector(page)

        for round_num in range(1, self.config.max_challenge_rounds + 1):
            print(f"GodSolver: Challenge Round {round_num}")
            try:
                challenge_info = await detector.get_challenge_info()
                challenge_text = challenge_info[0]
                tile_images = challenge_info[1]
                verify_button_selector = challenge_info[2]
                tile_selectors = challenge_info[3]

                if not challenge_text or not tile_images:
                    print("Could not get challenge info. Retrying...")
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    continue

                target = self._parse_challenge_text(challenge_text)
                if not target:
                    print(f"Could not parse target from: {challenge_text}")
                    await asyncio.sleep(random.uniform(0.5, 1))
                    continue

                print(f"Target: {target} | Tiles: {len(tile_images)}")

                # CLIP scoring
                scores = await self._get_tile_scores(tile_images, target)
                selected_indices = self._adaptive_thresholding(scores)

                # Ollama Moondream6 fallback if CLIP fails
                if not selected_indices and self.config.llm_fallback:
                    print("CLIP confidence too low, using Moondream6 fallback...")
                    selected_indices = await self._llm_vision_fallback(page, target, len(tile_images))

                if not selected_indices:
                    print("No tiles selected, retrying...")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                print(f"Selected tiles: {selected_indices}")

                # Click selected tiles with human-like movement
                iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
                for idx in selected_indices:
                    try:
                        tile = iframe_locator.locator(".task-image .image").nth(idx)
                        box = await tile.bounding_box()
                        if box:
                            target_x = box['x'] + box['width'] / 2 + random.uniform(-3, 3)
                            target_y = box['y'] + box['height'] / 2 + random.uniform(-3, 3)
                            await HumanMouse.move_and_click(page, target_x, target_y)
                            await self._rate_limit_delay()
                    except Exception as e:
                        print(f"Error clicking tile {idx}: {e}")

                # Click verify
                await asyncio.sleep(random.uniform(0.3, 0.8))
                try:
                    verify_btn = iframe_locator.locator(verify_button_selector)
                    if await verify_btn.count() > 0:
                        box = await verify_btn.bounding_box()
                        if box:
                            await HumanMouse.move_and_click(page, box['x'] + box['width']/2, box['y'] + box['height']/2)
                except:
                    pass

                await asyncio.sleep(self.config.min_solve_time_per_round)

                if await detector.is_solved():
                    print("hCaptcha SOLVED!")
                    return True

            except Exception as e:
                print(f"Error in GodSolver round {round_num}: {e}")
                await asyncio.sleep(random.uniform(1, 2))

        print("GodSolver failed after all rounds.")
        return False

    async def close(self):
        if self.ollama:
            await self.ollama.close()


# =============================================================================
# DRAG SOLVER (Ollama Moondream6 for target detection)
# =============================================================================

class DragSolver(PlaywrightSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.ollama: Optional[OllamaVisionClient] = None

    async def _get_ollama(self):
        if self.ollama is None:
            self.ollama = await OllamaVisionClient.get_instance(self.config)
        return self.ollama

    async def solve(self, page: Page) -> bool:
        print("Attempting to solve drag captcha with DragSolver...")
        detector = ChallengeDetector(page)

        for round_num in range(1, self.config.max_challenge_rounds + 1):
            print(f"DragSolver: Round {round_num}")
            try:
                # Find draggable and target elements
                iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")

                # Try to find draggable element
                draggable_selectors = [".draggable", "[draggable='true']", ".drag-item", ".puzzle-piece"]
                draggable_box = None
                for sel in draggable_selectors:
                    try:
                        el = iframe_locator.locator(sel)
                        if await el.count() > 0:
                            draggable_box = await el.first.bounding_box()
                            if draggable_box:
                                break
                    except:
                        continue

                if not draggable_box:
                    print("No draggable element found")
                    await asyncio.sleep(0.5)
                    continue

                source_x = draggable_box['x'] + draggable_box['width'] / 2
                source_y = draggable_box['y'] + draggable_box['height'] / 2

                # Use Moondream6 to find target location
                screenshot_bytes = await page.screenshot()
                b64_string = base64.b64encode(screenshot_bytes).decode("utf-8")

                ollama = await self._get_ollama()
                prompt = "In this captcha image, there is a draggable piece that needs to be placed somewhere. Where should it go? Reply with ONLY the x,y coordinates as percentage of image dimensions, like: 65,40"

                answer = await ollama.vision_query_with_retry(prompt, b64_string, max_tokens=30)
                print(f"Moondream6 drag target response: {answer}")

                target_x, target_y = None, None
                if answer:
                    match = re.search(r'(\d{1,3})[,%\s]+(\d{1,3})', answer)
                    if match:
                        viewport = page.viewport_size
                        if viewport:
                            x_pct = float(match.group(1)) / 100.0
                            y_pct = float(match.group(2)) / 100.0
                            target_x = viewport['width'] * x_pct
                            target_y = viewport['height'] * y_pct

                if target_x is None or target_y is None:
                    # Fallback: try CV template matching
                    target_x, target_y = await self._cv_find_target(page, draggable_box)

                if target_x is None or target_y is None:
                    print("Could not determine target. Retrying...")
                    await asyncio.sleep(1)
                    continue

                # Human-like drag
                await HumanMouse.move_and_click(page, source_x, source_y, click=False)
                await page.mouse.down()
                await asyncio.sleep(random.uniform(0.08, 0.2))

                drag_path = HumanMouse._generate_path(source_x, source_y, target_x, target_y)
                for i, (x, y) in enumerate(drag_path):
                    progress = i / len(drag_path)
                    speed_factor = math.sin(progress * math.pi)
                    delay = random.uniform(0.004, 0.012) / max(speed_factor, 0.3)
                    await page.mouse.move(x, y)
                    await asyncio.sleep(delay)

                await page.mouse.up()
                await asyncio.sleep(self.config.min_solve_time_per_round)

                if await detector.is_solved():
                    print("Drag captcha SOLVED!")
                    return True

            except Exception as e:
                print(f"DragSolver error in round {round_num}: {e}")
                await asyncio.sleep(1)

        print("DragSolver failed after all rounds.")
        return False

    async def _cv_find_target(self, page: Page, draggable_box: dict) -> Tuple[Optional[float], Optional[float]]:
        """Fallback: use CV to find where the draggable piece fits."""
        try:
            screenshot_bytes = await page.screenshot()
            full_img = np.array(Image.open(io.BytesIO(screenshot_bytes)).convert('L'))

            # Crop the draggable piece from the screenshot
            x, y, w, h = int(draggable_box['x']), int(draggable_box['y']), int(draggable_box['width']), int(draggable_box['height'])
            piece = full_img[y:y+h, x:x+w]

            # Template match
            res = cv2.matchTemplate(full_img, piece, cv2.TM_CCOEFF_NORMED)
            # Zero out the area around the source to avoid matching itself
            res[max(0,y-10):min(res.shape[0],y+h+10), max(0,x-10):min(res.shape[1],x+w+10)] = 0

            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > 0.5:
                return max_loc[0] + w/2, max_loc[1] + h/2
        except Exception as e:
            print(f"CV target finding failed: {e}")
        return None, None


# =============================================================================
# PATTERN BREAKER SOLVER (Ollama Moondream6)
# =============================================================================

class PatternBreakerSolver(CaptchaSolver):
    """Solves pattern-breaking challenges using Ollama Moondream6 vision."""

    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.ollama: Optional[OllamaVisionClient] = None

    async def _get_ollama(self):
        if self.ollama is None:
            self.ollama = await OllamaVisionClient.get_instance(self.config)
        return self.ollama

    async def solve(self, page: Page) -> bool:
        print("Attempting to solve Pattern Breaker challenge with Moondream6...")
        for round_num in range(self.config.max_challenge_rounds):
            try:
                print(f"PatternBreaker: Round {round_num + 1}")

                # Find and screenshot the challenge iframe
                iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
                challenge_frame = await iframe_locator.locator(":root").element_handle()
                if not challenge_frame:
                    print("Challenge frame not found")
                    return False

                iframe_box = await challenge_frame.bounding_box()
                if not iframe_box:
                    return False

                # Screenshot and crop to iframe
                full_screenshot = await page.screenshot()
                full_image = Image.open(io.BytesIO(full_screenshot))
                cropped = full_image.crop((
                    int(iframe_box['x']), int(iframe_box['y']),
                    int(iframe_box['x'] + iframe_box['width']),
                    int(iframe_box['y'] + iframe_box['height'])
                ))

                img_bytes = io.BytesIO()
                cropped.save(img_bytes, format="PNG")
                screenshot_b64 = base64.b64encode(img_bytes.getvalue()).decode('utf-8')

                # Detect grid dimensions
                grid_rows, grid_cols = self._detect_grid_dimensions(img_bytes.getvalue())
                print(f"Grid: {grid_rows}x{grid_cols}")

                # Ask Moondream6 to identify pattern breakers
                ollama = await self._get_ollama()
                prompt = f"""This is a captcha grid of {grid_rows}x{grid_cols} symbols. Most symbols follow the same pattern. Some are DIFFERENT from the majority. Which grid positions (row,col starting from 0,0 at top-left) contain the ODD ones that break the pattern? Reply with ONLY a JSON array like: [[0,2],[1,4],[3,1]]"""

                answer = await ollama.vision_query_with_retry(prompt, screenshot_b64, max_tokens=200)
                print(f"Moondream6 pattern response: {answer}")

                # Parse positions
                positions = self._parse_positions(answer, grid_rows, grid_cols)
                if not positions:
                    print("No pattern-breaking positions found. Retrying...")
                    await self._rate_limit_delay()
                    continue

                print(f"Pattern breakers at: {positions}")

                # Click the identified tiles
                tile_width = iframe_box['width'] / grid_cols
                tile_height = iframe_box['height'] / grid_rows

                for r, c in positions:
                    center_x = iframe_box['x'] + (c * tile_width) + (tile_width / 2)
                    center_y = iframe_box['y'] + (r * tile_height) + (tile_height / 2)
                    await HumanMouse.move_and_click(page, center_x, center_y)
                    await self._rate_limit_delay()

                # Click verify
                try:
                    verify_btn = iframe_locator.locator("button.verifybtn")
                    if await verify_btn.count() > 0:
                        box = await verify_btn.bounding_box()
                        if box:
                            await HumanMouse.move_and_click(page, box['x'] + box['width']/2, box['y'] + box['height']/2)
                except:
                    pass

                await asyncio.sleep(1.5)

                detector = ChallengeDetector(page)
                if await detector.is_solved():
                    print("Pattern challenge SOLVED!")
                    return True

            except Exception as e:
                print(f"PatternBreaker error in round {round_num + 1}: {e}")
                await self._rate_limit_delay()

        print("PatternBreaker failed after all rounds.")
        return False

    def _parse_positions(self, answer: str, max_rows: int, max_cols: int) -> List[Tuple[int, int]]:
        """Parse grid positions from LLM response."""
        if not answer:
            return []
        try:
            # Try JSON parse first
            parsed = json.loads(answer)
            if isinstance(parsed, list):
                return [(r, c) for r, c in parsed if 0 <= r < max_rows and 0 <= c < max_cols]
        except:
            pass
        # Regex fallback
        matches = re.findall(r'\[(\d+),\s*(\d+)\]|\((\d+),\s*(\d+)\)', answer)
        positions = []
        for match in matches:
            coords = [int(c) for c in match if c]
            if len(coords) == 2 and 0 <= coords[0] < max_rows and 0 <= coords[1] < max_cols:
                positions.append((coords[0], coords[1]))
        return positions

    def _detect_grid_dimensions(self, screenshot_bytes: bytes) -> Tuple[int, int]:
        """Detect grid dimensions using edge detection."""
        try:
            nparr = np.frombuffer(screenshot_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return 3, 3

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edged = cv2.Canny(blurred, 50, 150)

            # Detect lines
            lines_h = cv2.HoughLinesP(edged, 1, np.pi/180, 50, minLineLength=img.shape[1]*0.5, maxLineGap=10)
            lines_v = cv2.HoughLinesP(edged, 1, np.pi/2, 50, minLineLength=img.shape[0]*0.5, maxLineGap=10)

            h_count = len(lines_h) if lines_h is not None else 0
            v_count = len(lines_v) if lines_v is not None else 0

            # Estimate grid from line count
            rows = max(2, min(6, h_count - 1)) if h_count > 2 else 3
            cols = max(2, min(6, v_count - 1)) if v_count > 2 else 3

            return rows, cols
        except:
            return 3, 3


# =============================================================================
# CHALLENGE ROUTER
# =============================================================================

class ChallengeRouter:
    """Routes challenges to the appropriate solver."""

    def __init__(self, config: SolverConfig):
        self.config = config

    async def detect_and_route(self, page: Page) -> Optional[CaptchaSolver]:
        print("Router: Detecting challenge type...")
        iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")

        try:
            prompt_text = ""
            for selector in [".challenge-header .prompt-text", ".challenge-header", ".task-text"]:
                try:
                    el = iframe_locator.locator(selector)
                    if await el.count() > 0:
                        prompt_text = (await el.first.text_content() or "").lower()
                        if prompt_text:
                            break
                except:
                    continue

            if prompt_text:
                if any(kw in prompt_text for kw in ["break the pattern", "odd one out", "doesn't belong", "doesn't fit"]):
                    print("Router -> PatternBreakerSolver")
                    return PatternBreakerSolver(self.config)
                elif any(kw in prompt_text for kw in ["drag", "place", "move", "drop", "fit"]):
                    print("Router -> DragSolver")
                    return DragSolver(self.config)
                elif any(kw in prompt_text for kw in ["select all", "click all", "containing", "images of", "with a"]):
                    print("Router -> GodSolver")
                    return GodSolver(self.config)

            print("Router -> GodSolver (default)")
            return GodSolver(self.config)
        except Exception as e:
            print(f"Router error: {e}. Defaulting to GodSolver.")
            return GodSolver(self.config)


# =============================================================================
# MASTER SOLVER
# =============================================================================

class MasterSolver(CaptchaSolver):
    """Top-level solver with automatic routing and fallback chain."""

    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.router = ChallengeRouter(config)
        self.god_solver = GodSolver(config)
        self.drag_solver = DragSolver(config)
        self.pattern_solver = PatternBreakerSolver(config)

    async def solve(self, page: Page) -> bool:
        print("MasterSolver: Starting challenge resolution.")
        solver_instance = await self.router.detect_and_route(page)

        if solver_instance is None:
            solver_instance = self.god_solver

        print(f"MasterSolver: Using {solver_instance.__class__.__name__}")

        success = await solver_instance.solve(page)

        if not success and not isinstance(solver_instance, GodSolver):
            print(f"MasterSolver: {solver_instance.__class__.__name__} failed. Falling back to GodSolver.")
            success = await self.god_solver.solve(page)

        print(f"MasterSolver: {'SUCCESS' if success else 'FAILED'}")
        return success

    async def close(self):
        print("MasterSolver: Closing.")
        if hasattr(self.god_solver, 'close'):
            await self.god_solver.close()


# =============================================================================
# CLI
# =============================================================================

async def main():
    config = SolverConfig(
        headless=False,
        clip_confidence_threshold=0.55,
        max_challenge_rounds=5,
        timeout=45,
    )
    solver = MasterSolver(config)
    print("MasterSolver initialized with Ollama Moondream6 backend.")
    print(f"  - Ollama URL: {config.ollama_base_url}")
    print(f"  - Model: {config.ollama_model}")
    print(f"  - PatternBreakerSolver: ready (Moondream6)")
    print(f"  - GodSolver: ready (CLIP + Moondream6 fallback)")
    print(f"  - DragSolver: ready (CV + Moondream6)")
    print(f"  - SliderSolver: ready (CV multi-method)")
    print("Pass a Playwright Page object to solver.solve(page)")


if __name__ == "__main__":
    asyncio.run(main())
