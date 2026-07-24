
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
    # Drag solver
    drag_max_attempts_per_round: int = 3
    drag_precision_threshold: float = 0.7
    # Slider solver
    slider_offset_tolerance: int = 5
    slider_max_retries: int = 3


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

    async def multi_query(self, prompts: List[str], image_b64: str, max_tokens: int = 300) -> List[str]:
        """Send multiple queries concurrently for speed."""
        tasks = [self.vision_query(p, image_b64, max_tokens) for p in prompts]
        return await asyncio.gather(*tasks)

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
# HUMAN-LIKE MOUSE MOVEMENT
# =============================================================================

class HumanMouse:
    """Realistic mouse movement using minimum-jerk trajectory model with overshoot and micro-corrections."""

    @staticmethod
    def _minimum_jerk(t: float) -> float:
        return 10 * t**3 - 15 * t**4 + 6 * t**5

    @staticmethod
    def _generate_path(start_x: float, start_y: float, end_x: float, end_y: float) -> List[Tuple[float, float]]:
        distance = math.sqrt((end_x - start_x)**2 + (end_y - start_y)**2)
        num_points = max(18, min(80, int(distance / 4)))

        overshoot_amount = random.uniform(0, min(18, distance * 0.09))
        overshoot_angle = math.atan2(end_y - start_y, end_x - start_x)
        overshoot_x = end_x + overshoot_amount * math.cos(overshoot_angle)
        overshoot_y = end_y + overshoot_amount * math.sin(overshoot_angle)

        wind_strength = random.uniform(0.08, 0.35) * distance
        wind_angle = overshoot_angle + random.choice([-1, 1]) * math.pi / 2 + random.uniform(-0.25, 0.25)

        cp1_x = start_x + (end_x - start_x) * random.uniform(0.2, 0.4) + wind_strength * math.cos(wind_angle) * random.uniform(0.3, 0.6)
        cp1_y = start_y + (end_y - start_y) * random.uniform(0.2, 0.4) + wind_strength * math.sin(wind_angle) * random.uniform(0.3, 0.6)
        cp2_x = start_x + (overshoot_x - start_x) * random.uniform(0.6, 0.8) + wind_strength * math.cos(wind_angle) * random.uniform(0.1, 0.3)
        cp2_y = start_y + (overshoot_y - start_y) * random.uniform(0.6, 0.8) + wind_strength * math.sin(wind_angle) * random.uniform(0.1, 0.3)

        path = []
        overshoot_point_idx = int(num_points * random.uniform(0.78, 0.92))

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

            tremor_scale = max(0, 1.0 - t) * random.uniform(0.4, 1.8)
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
            start_x = target_x + random.uniform(-45, 45)
        if start_y is None:
            start_y = target_y + random.uniform(-45, 45)

        path = HumanMouse._generate_path(start_x, start_y, target_x, target_y)

        for i, (x, y) in enumerate(path):
            progress = i / len(path)
            speed_factor = math.sin(progress * math.pi)
            base_delay = random.uniform(0.003, 0.012)
            delay = base_delay / max(speed_factor, 0.25)
            await page.mouse.move(x, y)
            await asyncio.sleep(delay)

        if click:
            await asyncio.sleep(random.uniform(0.02, 0.08))
            await page.mouse.click(target_x, target_y)

    @staticmethod
    async def human_drag(page: Page, start_x: float, start_y: float, end_x: float, end_y: float,
                         hold_delay: float = None, release_delay: float = None):
        """Perform a human-like drag from start to end coordinates."""
        # Move to start
        await HumanMouse.move_and_click(page, start_x, start_y, click=False)
        await asyncio.sleep(hold_delay or random.uniform(0.06, 0.18))

        # Press mouse down
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.08, 0.2))

        # Generate drag path with slight wobble
        drag_path = HumanMouse._generate_path(start_x, start_y, end_x, end_y)

        # Add slight vertical wobble for realism (humans don't drag perfectly straight)
        for i, (x, y) in enumerate(drag_path):
            progress = i / len(drag_path)
            speed_factor = math.sin(progress * math.pi)
            base_delay = random.uniform(0.004, 0.014)
            delay = base_delay / max(speed_factor, 0.25)

            # Add slight y-wobble during drag
            y_wobble = random.gauss(0, 1.5) if 0.1 < progress < 0.9 else 0
            await page.mouse.move(x, y + y_wobble)
            await asyncio.sleep(delay)

        # Small pause before release (humans hesitate slightly)
        await asyncio.sleep(release_delay or random.uniform(0.05, 0.15))
        await page.mouse.up()
        await asyncio.sleep(random.uniform(0.1, 0.25))


# =============================================================================
# CHALLENGE DETECTOR (Improved with multiple fallbacks)
# =============================================================================

class ChallengeDetector:
    def __init__(self, page: Page):
        self.page = page

    async def is_solved(self) -> bool:
        """Strict verification that captcha is ACTUALLY solved.
        ONLY trusts hard proof:
        - Token present (h-captcha-response has value) = 100% solved
        - Checkbox aria-checked=true = 100% solved
        - Iframe disappeared is NOT trusted alone (could be new round loading)
        """
        
        signals = await self._collect_signals()
        
        # Token present = DEFINITELY solved (hCaptcha backend accepted the answer)
        if signals['token_present']:
            print("[SOLVE PROOF] h-captcha-response token is SET (hCaptcha backend confirmed solve)")
            return True
        
        # Checkbox checked in hCaptcha = DEFINITELY solved
        if signals['checkbox_checked']:
            print("[SOLVE PROOF] hCaptcha checkbox aria-checked=true (widget confirmed solve)")
            return True
        
        # Challenge iframe gone — BUT only trust this if token is also set after waiting
        # (iframe can disappear temporarily when loading new round)
        if signals['challenge_disappeared']:
            # Wait a bit and re-check for token (it might take a moment to populate)
            await asyncio.sleep(1.0)
            token_now = await self._check_token_present()
            if token_now:
                print("[SOLVE PROOF] Token confirmed after iframe disappeared")
                return True
            checkbox_now = await self._check_checkbox_checked()
            if checkbox_now:
                print("[SOLVE PROOF] Checkbox confirmed after iframe disappeared")
                return True
            # Iframe gone but no token/checkbox = probably just loading new round
            print("[SOLVE WARNING] Iframe disappeared but NO token/checkbox - likely new round loading, NOT solved")
            return False
        
        return False

    async def _collect_signals(self) -> dict:
        """Collect all solve signals at once."""
        checks = [
            self._check_success_class(),
            self._check_checkbox_checked(),
            self._check_challenge_disappeared(),
            self._check_token_present(),
            self._check_error_present(),
        ]
        results = await asyncio.gather(*checks, return_exceptions=True)
        
        # If error is showing, definitely NOT solved
        error_present = results[4] is True if not isinstance(results[4], Exception) else False
        if error_present:
            return {'success_class': False, 'checkbox_checked': False, 
                    'challenge_disappeared': False, 'token_present': False}
        
        return {
            'success_class': results[0] is True if not isinstance(results[0], Exception) else False,
            'checkbox_checked': results[1] is True if not isinstance(results[1], Exception) else False,
            'challenge_disappeared': results[2] is True if not isinstance(results[2], Exception) else False,
            'token_present': results[3] is True if not isinstance(results[3], Exception) else False,
        }

    async def _check_success_class(self) -> bool:
        try:
            return await self.page.locator('.success-text, .challenge-solved, [data-state="solved"]').count() > 0
        except:
            return False

    async def _check_checkbox_checked(self) -> bool:
        try:
            # Check the hCaptcha checkbox iframe specifically
            checkbox_iframe = self.page.frame_locator("iframe[src*='hcaptcha.com/checkbox']")
            checked = await checkbox_iframe.locator('[aria-checked="true"]').count() > 0
            if checked:
                return True
            # Also check page-level
            return await self.page.locator('[aria-checked="true"]').count() > 0
        except:
            return False

    async def _check_challenge_disappeared(self) -> bool:
        try:
            # Check ALL possible challenge iframe selectors
            challenge_selectors = [
                "iframe[src*='newassets.hcaptcha.com/captcha']",
                "iframe[src*='hcaptcha.com/captcha']",
                "iframe[src*='imgs.hcaptcha.com']",
                "iframe[title*='hCaptcha challenge']",
                "iframe[title*='hcaptcha challenge']",
            ]
            for sel in challenge_selectors:
                try:
                    iframe_count = await self.page.locator(sel).count()
                    if iframe_count > 0:
                        # Check if any are actually visible
                        for i in range(iframe_count):
                            try:
                                frame = self.page.locator(sel).nth(i)
                                is_vis = await frame.is_visible()
                                if is_vis:
                                    box = await frame.bounding_box()
                                    if box and box['height'] > 50 and box['width'] > 50:
                                        return False  # Still visible = NOT solved
                            except:
                                continue
                except:
                    continue
            return True  # No visible challenge iframe found = disappeared
        except:
            return False

    async def _check_token_present(self) -> bool:
        try:
            token = await self.page.evaluate("document.querySelector('[name=\"h-captcha-response\"]')?.value || document.querySelector('textarea[name=\"h-captcha-response\"]')?.value || ''")
            return bool(token and len(token) > 20)  # Real tokens are long (>100 chars usually)
        except:
            return False

    async def _check_error_present(self) -> bool:
        """Check if an error/retry message is showing (means NOT solved)."""
        try:
            iframe_locator = self.page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
            error_selectors = [
                '.error-text', '.display-error', '.task-error',
                'text=Try again', 'text=Incorrect', 'text=Please try again',
                '.challenge-error', '[class*="error"]'
            ]
            for sel in error_selectors:
                try:
                    if await iframe_locator.locator(sel).count() > 0:
                        return True
                except:
                    continue
            return False
        except:
            return False

    async def get_challenge_info(self):
        """Returns (challenge_text, tile_images, verify_button_selector, tile_selectors)"""
        try:
            iframe_locator = self.page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")

            # Get challenge text
            challenge_text = ""
            for selector in [".challenge-header .prompt-text", ".prompt-text", ".challenge-header", ".task-text", "h2.prompt-text"]:
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
            for selector in [".task-image .image", ".task-image img", ".challenge-item img", "img.challenge-image", ".image-wrapper img"]:
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

            verify_button_selector = "button.verifybtn, .button-submit, button[type='submit']"
            return (challenge_text, tile_images, verify_button_selector, tile_selectors)
        except Exception as e:
            print(f"Error getting challenge info: {e}")
            return ("", [], "", [])

    async def detect_challenge_type(self) -> str:
        """Detect the type of challenge presented. Tries multiple iframe selectors."""
        challenge_type = "unknown"
        
        # Try multiple iframe selectors to find the challenge
        iframe_selectors = [
            "iframe[src*='newassets.hcaptcha.com/captcha']",
            "iframe[src*='hcaptcha.com/captcha']",
            "iframe[src*='imgs.hcaptcha.com']",
            "iframe[title*='hCaptcha challenge']",
            "iframe[title*='hcaptcha challenge']",
        ]
        
        iframe_locator = None
        for iframe_sel in iframe_selectors:
            try:
                loc = self.page.frame_locator(iframe_sel)
                # Test if this frame has content
                test_el = loc.locator("body")
                if await test_el.count() > 0:
                    iframe_locator = loc
                    break
            except:
                continue
        
        if not iframe_locator:
            # Fallback: use the first one anyway
            iframe_locator = self.page.frame_locator("iframe[src*='hcaptcha.com']")
        
        try:
            prompt_text = ""
            for selector in [".challenge-header .prompt-text", ".prompt-text", ".challenge-header", ".task-text", "h2", ".prompt-padding"]:
                try:
                    el = iframe_locator.locator(selector)
                    if await el.count() > 0:
                        prompt_text = (await el.first.text_content() or "").lower()
                        if prompt_text:
                            print(f"ChallengeDetector: prompt_text = '{prompt_text}'")
                            break
                except:
                    continue

            if any(kw in prompt_text for kw in ["break the pattern", "odd one out", "doesn't belong", "doesn't fit", "not like the others"]):
                challenge_type = "pattern"
            elif any(kw in prompt_text for kw in ["drag", "place", "move", "drop", "fit", "put", "to the"]):
                challenge_type = "drag"
            elif any(kw in prompt_text for kw in ["select all", "click all", "containing", "images of", "with a", "please click", "click each"]):
                challenge_type = "grid"
            else:
                # Check for DOM-based detection
                try:
                    if await iframe_locator.locator('[role="slider"], .slider-track, .slider-handle').count() > 0:
                        challenge_type = "slider"
                    elif await iframe_locator.locator('.draggable, [draggable="true"], .drag-item, .challenge-item[draggable], img[style*="cursor: grab"], img[style*="cursor:grab"]').count() > 0:
                        challenge_type = "drag"
                    elif await iframe_locator.locator('.task-image .image, .challenge-item img').count() > 0:
                        challenge_type = "grid"
                except:
                    pass
        except:
            pass
        
        print(f"ChallengeDetector: detected type = '{challenge_type}'")
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
# PLAYWRIGHT SOLVER (Base for browser-interacting solvers)
# =============================================================================

class PlaywrightSolver(CaptchaSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)

    async def get_screenshot(self, page: Page, locator_selector: str = None) -> Image.Image:
        if locator_selector:
            locator = page.locator(locator_selector)
            try:
                if await locator.is_visible():
                    screenshot_bytes = await locator.screenshot()
                    return Image.open(io.BytesIO(screenshot_bytes))
            except:
                pass
        screenshot_bytes = await page.screenshot()
        return Image.open(io.BytesIO(screenshot_bytes))

    async def get_element_bounds(self, page: Page, selector: str) -> Optional[Dict[str, float]]:
        try:
            element = page.locator(selector)
            if await element.is_visible():
                return await element.bounding_box()
        except:
            pass
        return None

    async def click_element(self, page: Page, selector: str):
        box = await self.get_element_bounds(page, selector)
        if not box:
            raise ValueError(f"Element {selector} not found or not visible.")
        target_x = box['x'] + box['width'] / 2 + random.uniform(-2, 2)
        target_y = box['y'] + box['height'] / 2 + random.uniform(-2, 2)
        await HumanMouse.move_and_click(page, target_x, target_y, element_width=box['width'], element_height=box['height'])


# =============================================================================
# SLIDER SOLVER (Multi-method with consensus + adaptive retry)
# =============================================================================

class SliderSolver(PlaywrightSolver):
    """
    Solves slider/puzzle captchas using multiple CV methods:
    1. Template matching (grayscale + color)
    2. Canny edge detection + template matching
    3. Phase correlation
    4. Edge histogram correlation
    5. SIFT/ORB feature matching
    6. Moondream6 vision fallback
    
    Uses consensus voting and adaptive retry with offset adjustment.
    """

    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.ollama: Optional[OllamaVisionClient] = None

    async def _get_ollama(self):
        if self.ollama is None:
            self.ollama = await OllamaVisionClient.get_instance(self.config)
        return self.ollama

    async def solve(self, page: Page) -> bool:
        print("SliderSolver: Attempting to solve slider/puzzle captcha...")
        detector = ChallengeDetector(page)

        # Try multiple selector patterns for slider elements
        slider_selectors = [
            ('.slider-handle', '.slider-track', '.puzzle-image', '.background-image'),
            ('.slide-btn', '.slide-track', '.puzzle-piece', '.bg-image'),
            ('[role="slider"]', '.slider-container', '.jigsaw-piece', '.jigsaw-bg'),
            ('.handler', '.track', '.piece', '.background'),
        ]

        handle_sel = track_sel = piece_sel = bg_sel = None
        for h, t, p, b in slider_selectors:
            try:
                if await page.locator(h).count() > 0:
                    handle_sel, track_sel, piece_sel, bg_sel = h, t, p, b
                    break
            except:
                continue

        if not handle_sel:
            # Try iframe-based detection
            iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
            for h, t, p, b in slider_selectors:
                try:
                    if await iframe_locator.locator(h).count() > 0:
                        handle_sel, track_sel, piece_sel, bg_sel = h, t, p, b
                        break
                except:
                    continue

        if not handle_sel:
            print("SliderSolver: No slider elements found.")
            return False

        for attempt in range(self.config.slider_max_retries):
            print(f"SliderSolver: Attempt {attempt + 1}/{self.config.slider_max_retries}")

            try:
                await page.wait_for_selector(handle_sel, timeout=5000)
            except:
                print("SliderSolver: Handle not visible")
                return False

            handle_box = await self.get_element_bounds(page, handle_sel)
            track_box = await self.get_element_bounds(page, track_sel)
            if not handle_box or not track_box:
                print("SliderSolver: Cannot get bounds")
                return False

            start_x = handle_box['x'] + handle_box['width'] / 2
            start_y = handle_box['y'] + handle_box['height'] / 2

            # Get images for analysis
            bg_image = await self.get_screenshot(page, bg_sel)
            piece_image = await self.get_screenshot(page, piece_sel) if piece_sel else None

            if bg_image is None:
                bg_image = await self.get_screenshot(page)

            # Calculate offset using multiple methods
            offset = await self._calculate_offset(bg_image, piece_image, page)

            if offset is None:
                print("SliderSolver: Could not determine offset")
                await asyncio.sleep(1)
                continue

            # Apply small random adjustment for anti-detection (±2px)
            offset += random.randint(-2, 2)
            print(f"SliderSolver: Calculated offset: {offset}px")

            # Calculate target position
            target_x = start_x + offset
            track_right = track_box['x'] + track_box['width']
            target_x = max(track_box['x'] + 5, min(target_x, track_right - handle_box['width'] / 2))

            # Human-like drag with variable speed profile
            await HumanMouse.human_drag(page, start_x, start_y, target_x, start_y)

            # Wait and check
            await asyncio.sleep(self.config.min_solve_time_per_round)
            if await detector.is_solved():
                print("SliderSolver: SOLVED!")
                return True

            # If not solved, wait a bit and try again with slight offset adjustment
            print(f"SliderSolver: Attempt {attempt + 1} failed, retrying with adjusted offset...")
            await asyncio.sleep(random.uniform(1, 2))

        print("SliderSolver: Failed after all attempts.")
        return False

    async def _calculate_offset(self, bg_image: Image.Image, piece_image: Optional[Image.Image], page: Page) -> Optional[int]:
        """Calculate slider offset using multiple methods and consensus voting."""
        offsets = []
        confidences = []

        bg_gray = np.array(bg_image.convert('L'))

        if piece_image:
            piece_gray = np.array(piece_image.convert('L'))

            # Method 1: Template Matching (TM_CCOEFF_NORMED)
            try:
                res = cv2.matchTemplate(bg_gray, piece_gray, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val > 0.5:
                    offsets.append(max_loc[0])
                    confidences.append(max_val)
                    print(f"  Template Match: offset={max_loc[0]}, conf={max_val:.3f}")
            except Exception as e:
                print(f"  Template Match failed: {e}")

            # Method 2: Canny Edge + Template Matching
            try:
                edges_bg = cv2.Canny(bg_gray, 80, 200)
                edges_piece = cv2.Canny(piece_gray, 80, 200)
                res_canny = cv2.matchTemplate(edges_bg, edges_piece, cv2.TM_CCOEFF_NORMED)
                _, max_val_c, _, max_loc_c = cv2.minMaxLoc(res_canny)
                if max_val_c > 0.4:
                    offsets.append(max_loc_c[0])
                    confidences.append(max_val_c)
                    print(f"  Canny Match: offset={max_loc_c[0]}, conf={max_val_c:.3f}")
            except Exception as e:
                print(f"  Canny Match failed: {e}")

            # Method 3: Phase Correlation
            try:
                h_bg, w_bg = bg_gray.shape
                h_p, w_p = piece_gray.shape
                padded = np.zeros_like(bg_gray, dtype=np.float32)
                padded[0:h_p, 0:w_p] = piece_gray.astype(np.float32)
                shift, response = cv2.phaseCorrelate(bg_gray.astype(np.float32), padded)
                phase_offset = int(round(abs(shift[0])))
                if 5 < phase_offset < w_bg * 0.9:
                    offsets.append(phase_offset)
                    confidences.append(min(response, 1.0))
                    print(f"  Phase Correlation: offset={phase_offset}, conf={response:.3f}")
            except Exception as e:
                print(f"  Phase Correlation failed: {e}")

            # Method 4: Edge Histogram Correlation
            try:
                edges_bg_hist = np.sum(cv2.Canny(bg_gray, 100, 200), axis=0).astype(np.float64)
                edges_piece_hist = np.sum(cv2.Canny(piece_gray, 100, 200), axis=0).astype(np.float64)

                best_corr = -1
                best_offset = 0
                for i in range(len(edges_bg_hist) - len(edges_piece_hist) + 1):
                    bg_slice = edges_bg_hist[i:i + len(edges_piece_hist)]
                    if np.std(bg_slice) > 0 and np.std(edges_piece_hist) > 0:
                        corr = np.corrcoef(bg_slice, edges_piece_hist)[0, 1]
                        if not np.isnan(corr) and corr > best_corr:
                            best_corr = corr
                            best_offset = i
                if best_corr > 0.4:
                    offsets.append(best_offset)
                    confidences.append(best_corr)
                    print(f"  Edge Histogram: offset={best_offset}, conf={best_corr:.3f}")
            except Exception as e:
                print(f"  Edge Histogram failed: {e}")

            # Method 5: SIFT Feature Matching
            try:
                sift = cv2.SIFT_create()
                kp1, des1 = sift.detectAndCompute(bg_gray, None)
                kp2, des2 = sift.detectAndCompute(piece_gray, None)
                if des1 is not None and des2 is not None and len(des1) > 5 and len(des2) > 5:
                    bf = cv2.BFMatcher()
                    matches = bf.knnMatch(des2, des1, k=2)
                    good_matches = [m for m, n in matches if m.distance < 0.7 * n.distance]
                    if len(good_matches) > 3:
                        x_offsets = [kp1[m.trainIdx].pt[0] - kp2[m.queryIdx].pt[0] for m in good_matches]
                        sift_offset = int(np.median(x_offsets))
                        if 5 < sift_offset < bg_gray.shape[1] * 0.9:
                            offsets.append(sift_offset)
                            confidences.append(0.7)
                            print(f"  SIFT Match: offset={sift_offset}, matches={len(good_matches)}")
            except Exception as e:
                print(f"  SIFT Match failed: {e}")

        # Method 6: Moondream6 Vision (fallback or additional vote)
        if len(offsets) < 2:
            try:
                screenshot_bytes = await page.screenshot()
                b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                ollama = await self._get_ollama()
                prompt = "This is a slider puzzle captcha. How many pixels from the left edge should the puzzle piece be moved to fit into the gap? Reply with ONLY a number (the pixel offset). Example: 156"
                answer = await ollama.vision_query_with_retry(prompt, b64, max_tokens=20)
                if answer:
                    nums = re.findall(r'\d+', answer)
                    if nums:
                        llm_offset = int(nums[0])
                        if 5 < llm_offset < 500:
                            offsets.append(llm_offset)
                            confidences.append(0.5)
                            print(f"  Moondream6: offset={llm_offset}")
            except Exception as e:
                print(f"  Moondream6 slider failed: {e}")

        if not offsets:
            return None

        # Consensus: weighted median based on confidence
        if len(offsets) >= 3:
            # Remove outliers (beyond 2 std from median)
            median_val = np.median(offsets)
            std_val = np.std(offsets) if len(offsets) > 1 else 50
            filtered = [(o, c) for o, c in zip(offsets, confidences) if abs(o - median_val) <= 2 * max(std_val, 10)]
            if filtered:
                # Weighted average by confidence
                total_conf = sum(c for _, c in filtered)
                if total_conf > 0:
                    final_offset = int(sum(o * c for o, c in filtered) / total_conf)
                else:
                    final_offset = int(np.median([o for o, _ in filtered]))
            else:
                final_offset = int(median_val)
        else:
            # With few methods, use the highest confidence one
            best_idx = np.argmax(confidences)
            final_offset = offsets[best_idx]

        print(f"  Consensus offset: {final_offset} (from {len(offsets)} methods)")
        return final_offset


# =============================================================================
# GOD SOLVER (CLIP + Ollama Moondream6 Fallback)
# =============================================================================

class GodSolver(PlaywrightSolver):
    """
    Primary grid-based captcha solver using:
    1. OpenCLIP ViT-L-14 for zero-shot image classification
    2. Multi-scale scoring (full + center crop)
    3. Adaptive thresholding with spatial context boosting
    4. Ollama Moondream6 vision fallback for low-confidence cases
    """

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
            "find the {target}",
            "identify the {target}",
            "where is the {target}",
            "show me the {target}",
        ]
        self.property_prompts = [
            "an object that is {target}",
            "something primarily {target}",
            "a {target} object",
            "an image with {target} color",
            "the {target} colored item",
            "items that are {target}",
        ]
        self.context_aware_prompts = [
            "a {target} seen from above",
            "a close-up of a {target}",
            "a {target} in the foreground",
            "a {target} in the background",
            "a {target} in motion",
            "a stationary {target}",
        ]

    def _initialize_aliases(self):
        # Vehicles
        self.target_aliases["car"] = ["automobile", "vehicle", "sedan", "coupe", "hatchback", "SUV", "truck", "pickup", "van", "minivan", "taxi", "police car", "sports car", "convertible"]
        self.target_aliases["truck"] = ["lorry", "pickup truck", "delivery truck", "articulated lorry", "semi-trailer truck", "dump truck", "fire truck", "tow truck"]
        self.target_aliases["bus"] = ["coach", "double-decker bus", "school bus", "public transport bus"]
        self.target_aliases["motorcycle"] = ["motorbike", "scooter", "moped", "dirt bike"]
        self.target_aliases["bicycle"] = ["bike", "mountain bike", "road bike", "tricycle"]
        self.target_aliases["boat"] = ["ship", "yacht", "sailboat", "ferry", "canoe", "kayak", "rowboat", "speedboat", "cruise ship"]
        self.target_aliases["airplane"] = ["aircraft", "jet", "plane", "helicopter", "seaplane", "biplane"]
        self.target_aliases["train"] = ["locomotive", "railway car", "subway", "metro", "tram", "streetcar"]
        # Road elements
        self.target_aliases["traffic light"] = ["stoplight", "traffic signal"]
        self.target_aliases["fire hydrant"] = ["hydrant", "red fire hydrant"]
        self.target_aliases["parking meter"] = ["meter"]
        self.target_aliases["crosswalk"] = ["zebra crossing", "pedestrian crossing"]
        self.target_aliases["road sign"] = ["street sign", "signpost", "billboard"]
        self.target_aliases["bridge"] = ["overpass", "viaduct", "suspension bridge"]
        self.target_aliases["building"] = ["house", "apartment building", "skyscraper", "office building", "cottage", "shed", "garage", "factory", "warehouse", "store", "shop"]
        self.target_aliases["chimney"] = ["smokestack", "flue"]
        self.target_aliases["palm tree"] = ["date palm", "coconut tree"]
        self.target_aliases["tree"] = ["oak tree", "pine tree", "birch tree", "forest", "woods"]
        self.target_aliases["mountain"] = ["hill", "peak", "summit", "range"]
        self.target_aliases["river"] = ["stream", "creek", "brook", "waterway"]
        self.target_aliases["lake"] = ["pond", "loch"]
        self.target_aliases["ocean"] = ["sea", "beach", "coast"]
        self.target_aliases["cloud"] = ["sky", "cumulus", "stratus", "cirrus"]
        self.target_aliases["sun"] = ["sunrise", "sunset", "sunlight"]
        self.target_aliases["moon"] = ["full moon", "crescent moon", "moonlight"]
        self.target_aliases["star"] = ["stars", "constellation"]
        # Animals
        self.target_aliases["cat"] = ["kitten", "feline"]
        self.target_aliases["dog"] = ["puppy", "canine"]
        self.target_aliases["bird"] = ["sparrow", "robin", "eagle", "owl", "pigeon", "duck", "goose", "chicken"]
        self.target_aliases["horse"] = ["pony", "mare", "stallion", "foal"]
        self.target_aliases["cow"] = ["cattle", "calf", "bull"]
        self.target_aliases["sheep"] = ["lamb", "ewe"]
        self.target_aliases["elephant"] = ["jumbo"]
        self.target_aliases["lion"] = ["king of the jungle", "big cat"]
        self.target_aliases["tiger"] = ["big cat", "striped cat"]
        self.target_aliases["bear"] = ["grizzly", "polar bear"]
        self.target_aliases["snake"] = ["serpent"]
        self.target_aliases["fish"] = ["salmon", "tuna", "shark"]
        self.target_aliases["spider"] = ["arachnid"]
        self.target_aliases["insect"] = ["bug", "ant", "bee", "butterfly", "moth", "fly"]
        # Food
        self.target_aliases["apple"] = ["fruit", "red apple", "green apple"]
        self.target_aliases["banana"] = ["fruit", "yellow banana"]
        self.target_aliases["orange"] = ["fruit", "citrus"]
        self.target_aliases["pizza"] = ["slice of pizza", "pepperoni pizza"]
        self.target_aliases["burger"] = ["hamburger", "cheeseburger"]
        self.target_aliases["sandwich"] = ["sub", "hoagie"]
        self.target_aliases["coffee"] = ["cup of coffee", "latte", "espresso"]
        self.target_aliases["tea"] = ["cup of tea"]
        # Objects
        self.target_aliases["chair"] = ["seat", "stool", "armchair"]
        self.target_aliases["table"] = ["desk", "counter"]
        self.target_aliases["lamp"] = ["light", "lantern"]
        self.target_aliases["book"] = ["novel", "textbook", "magazine"]
        self.target_aliases["computer"] = ["laptop", "desktop", "PC"]
        self.target_aliases["phone"] = ["smartphone", "mobile phone", "cellphone"]
        self.target_aliases["watch"] = ["wristwatch", "clock"]
        self.target_aliases["shoe"] = ["sneaker", "boot", "sandal"]
        self.target_aliases["bag"] = ["backpack", "handbag", "purse", "luggage"]
        self.target_aliases["umbrella"] = ["parasol"]
        self.target_aliases["cup"] = ["mug", "glass"]
        self.target_aliases["bottle"] = ["flask", "container"]
        self.target_aliases["camera"] = ["photographic device", "digital camera"]
        self.target_aliases["television"] = ["TV", "screen", "monitor"]
        self.target_aliases["keyboard"] = ["keypad", "computer keyboard"]
        self.target_aliases["scissors"] = ["shears"]
        self.target_aliases["knife"] = ["blade"]
        self.target_aliases["hammer"] = ["mallet"]
        self.target_aliases["guitar"] = ["acoustic guitar", "electric guitar"]
        self.target_aliases["piano"] = ["keyboard instrument"]
        self.target_aliases["drum"] = ["percussion instrument"]
        self.target_aliases["helmet"] = ["headgear", "safety helmet"]
        self.target_aliases["glasses"] = ["spectacles", "eyewear", "sunglasses"]
        self.target_aliases["backpack"] = ["rucksack", "knapsack", "school bag"]
        self.target_aliases["suitcase"] = ["luggage", "travel bag"]
        # Places/Structures
        self.target_aliases["church"] = ["cathedral", "chapel"]
        self.target_aliases["castle"] = ["fortress", "palace"]
        self.target_aliases["lighthouse"] = ["beacon"]
        self.target_aliases["windmill"] = ["wind turbine"]
        self.target_aliases["barn"] = ["shed", "farm building"]
        self.target_aliases["statue"] = ["sculpture", "monument"]
        self.target_aliases["fountain"] = ["water feature"]
        self.target_aliases["tower"] = ["spire", "clock tower"]
        self.target_aliases["stadium"] = ["arena", "sports ground"]
        # Colors
        self.target_aliases["red"] = ["crimson", "scarlet", "ruby"]
        self.target_aliases["blue"] = ["azure", "navy", "sky blue"]
        self.target_aliases["green"] = ["emerald", "lime", "forest green"]
        self.target_aliases["yellow"] = ["gold", "lemon", "amber"]
        self.target_aliases["black"] = ["ebony", "jet black"]
        self.target_aliases["white"] = ["snow white", "ivory"]
        self.target_aliases["brown"] = ["tan", "sepia", "chocolate"]
        self.target_aliases["orange"] = ["tangerine", "apricot"]
        self.target_aliases["purple"] = ["violet", "lavender"]
        self.target_aliases["pink"] = ["rose", "fuchsia"]
        self.target_aliases["gray"] = ["grey", "silver", "charcoal"]

        # Ensure base terms are in their own alias list
        for key in list(self.target_aliases.keys()):
            if key not in self.target_aliases[key]:
                self.target_aliases[key].insert(0, key)

    def _generate_prompts(self, target: str) -> List[str]:
        prompts = []
        aliases = self.target_aliases.get(target.lower(), [target])
        for alias in aliases[:6]:  # Limit for speed
            for p in self.object_prompts:
                prompts.append(p.format(target=alias))
            if alias.lower() in ["red", "blue", "green", "yellow", "black", "white", "brown", "orange", "purple", "pink", "gray"]:
                for p in self.property_prompts:
                    prompts.append(p.format(target=alias))
        # Add a few context-aware prompts
        if random.random() < 0.5:
            prompts.extend([p.format(target=target) for p in random.sample(self.context_aware_prompts, min(2, len(self.context_aware_prompts)))])
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
            r"Select all (.+)",
            r"Click each (.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, challenge_text, re.IGNORECASE)
            if match:
                target = match.group(1).strip().rstrip('.')
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

        # Multi-scale: full image + center 70% crop for each tile
        all_images = []
        for tile_img in tile_images:
            all_images.append(tile_img)
            w, h = tile_img.size
            cropped = tile_img.crop((int(w * 0.15), int(h * 0.15), int(w * 0.85), int(h * 0.85)))
            all_images.append(cropped)

        # Batch process
        all_features = await clip.get_image_features_batch(all_images)

        scores = []
        for i in range(len(tile_images)):
            full_feat = all_features[i * 2]
            crop_feat = all_features[i * 2 + 1]
            full_sim = (full_feat.unsqueeze(0) @ text_features.T).mean().item() * 0.55
            crop_sim = (crop_feat.unsqueeze(0) @ text_features.T).mean().item() * 0.45
            scores.append(full_sim + crop_sim)

        return scores

    def _adaptive_thresholding(self, scores: List[float]) -> List[int]:
        if not scores:
            return []

        scores_np = np.array(scores)
        sorted_scores = np.sort(scores_np)

        if np.max(scores_np) < 0.20:
            return []  # Signal for LLM fallback

        # Largest gap method for bimodal separation
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

        # Clamp 2-6 tiles
        if len(selected_indices) < 2:
            top_indices = np.argsort(scores_np)[::-1]
            selected_indices = list(top_indices[:min(len(scores), 3)])
        elif len(selected_indices) > 6:
            top_scores = [(scores[i], i) for i in selected_indices]
            top_scores.sort(reverse=True)
            selected_indices = [idx for _, idx in top_scores[:6]]

        # Spatial context boost (15% for neighbors of selected tiles)
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

            # Re-threshold with boosted
            boosted_np = np.array(boosted_scores)
            new_selected = [i for i, score in enumerate(boosted_scores) if score >= threshold * 0.92]
            if 2 <= len(new_selected) <= 6:
                selected_indices = new_selected

        return selected_indices

    async def _llm_vision_fallback(self, page: Page, target_text: str, num_tiles: int) -> List[int]:
        """Use Ollama Moondream6 for vision fallback."""
        print("GodSolver: Falling back to Moondream6 vision...")
        try:
            screenshot_bytes = await page.screenshot()
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            ollama = await self._get_ollama()
            prompt = f"This is a captcha grid with {num_tiles} tiles numbered 1-{num_tiles} left-to-right, top-to-bottom. Which tiles contain a {target_text}? Reply with ONLY the numbers separated by commas. Example: 1,5,9"

            answer = await ollama.vision_query_with_retry(prompt, b64, max_tokens=50)
            print(f"Moondream6 response: {answer}")

            if answer and answer.lower() != 'none':
                numbers_str = re.findall(r'\d+', answer)
                numbers = [int(n) - 1 for n in numbers_str if 1 <= int(n) <= num_tiles]
                return numbers
        except Exception as e:
            print(f"Moondream6 fallback failed: {e}")
        return []

    async def solve(self, page: Page) -> bool:
        print("GodSolver: Starting grid challenge...")
        detector = ChallengeDetector(page)

        for round_num in range(1, self.config.max_challenge_rounds + 1):
            print(f"GodSolver: Round {round_num}/{self.config.max_challenge_rounds}")
            try:
                challenge_info = await detector.get_challenge_info()
                challenge_text = challenge_info[0]
                tile_images = challenge_info[1]
                verify_button_selector = challenge_info[2]
                tile_selectors = challenge_info[3]

                if not challenge_text or not tile_images:
                    print("GodSolver: No challenge info, retrying...")
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    continue

                target = self._parse_challenge_text(challenge_text)
                if not target:
                    print(f"GodSolver: Could not parse target from: {challenge_text}")
                    # Try using the full text as target
                    target = challenge_text.strip()

                print(f"GodSolver: Target='{target}' | Tiles={len(tile_images)}")

                # CLIP scoring
                scores = await self._get_tile_scores(tile_images, target)
                print(f"GodSolver: CLIP scores: {[f'{s:.3f}' for s in scores]}")
                selected_indices = self._adaptive_thresholding(scores)

                # Moondream6 fallback if CLIP fails
                if not selected_indices and self.config.llm_fallback:
                    print("GodSolver: CLIP confidence too low, using Moondream6...")
                    selected_indices = await self._llm_vision_fallback(page, target, len(tile_images))

                if not selected_indices:
                    print("GodSolver: No tiles selected, retrying...")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                print(f"GodSolver: Clicking tiles: {selected_indices}")

                # Click selected tiles
                iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
                for idx in selected_indices:
                    try:
                        for tile_sel in [".task-image .image", ".task-image img", ".challenge-item img"]:
                            tile = iframe_locator.locator(tile_sel).nth(idx)
                            if await tile.count() > 0:
                                box = await tile.bounding_box()
                                if box:
                                    tx = box['x'] + box['width'] / 2 + random.uniform(-3, 3)
                                    ty = box['y'] + box['height'] / 2 + random.uniform(-3, 3)
                                    await HumanMouse.move_and_click(page, tx, ty)
                                    await self._rate_limit_delay()
                                    break
                    except Exception as e:
                        print(f"GodSolver: Error clicking tile {idx}: {e}")

                # Click verify button
                await asyncio.sleep(random.uniform(0.3, 0.7))
                try:
                    for verify_sel in ["button.verifybtn", ".button-submit", "button[type='submit']"]:
                        verify_btn = iframe_locator.locator(verify_sel)
                        if await verify_btn.count() > 0:
                            box = await verify_btn.bounding_box()
                            if box:
                                await HumanMouse.move_and_click(page, box['x'] + box['width']/2, box['y'] + box['height']/2)
                                break
                except:
                    pass

                # Wait for hCaptcha to process our answer
                await asyncio.sleep(2.0)

                if await detector.is_solved():
                    print("GodSolver: SOLVED!")
                    return True

                # Check if error/retry is showing
                error_showing = await detector._check_error_present()
                if error_showing:
                    print(f"GodSolver: WRONG ANSWER in round {round_num} - hCaptcha showing error/retry")
                else:
                    print(f"GodSolver: Round {round_num} - new challenge loaded (previous answer may have been wrong)")
                await asyncio.sleep(random.uniform(0.5, 1.0))

            except Exception as e:
                print(f"GodSolver: Error in round {round_num}: {e}")
                await asyncio.sleep(random.uniform(1, 2))

        print("GodSolver: Failed after all rounds.")
        return False

    async def close(self):
        if self.ollama:
            await self.ollama.close()


# =============================================================================
# DRAG SOLVER (CV + Moondream6 for icon placement challenges)
# =============================================================================

class DragSolver(PlaywrightSolver):
    """
    Solves hCaptcha drag challenges where you need to:
    - Drag an icon to the correct position on a background image
    - Drag puzzle pieces to their matching outlines
    - Place objects in the correct location
    
    Uses:
    1. SIFT/ORB feature matching to find matching regions
    2. Template matching with rotation tolerance
    3. Contour analysis for outline/shape matching
    4. Moondream6 vision for semantic understanding of where to place
    5. Multi-attempt with progressive refinement
    """

    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.ollama: Optional[OllamaVisionClient] = None

    async def _get_ollama(self):
        if self.ollama is None:
            self.ollama = await OllamaVisionClient.get_instance(self.config)
        return self.ollama

    async def solve(self, page: Page) -> bool:
        print("DragSolver: Starting drag challenge...")
        detector = ChallengeDetector(page)

        for round_num in range(1, self.config.max_challenge_rounds + 1):
            print(f"DragSolver: Round {round_num}/{self.config.max_challenge_rounds}")
            try:
                # Try multiple iframe selectors
                iframe_locator = None
                iframe_selectors = [
                    "iframe[src*='newassets.hcaptcha.com/captcha']",
                    "iframe[src*='hcaptcha.com/captcha']",
                    "iframe[src*='imgs.hcaptcha.com']",
                    "iframe[title*='hCaptcha challenge']",
                    "iframe[title*='hcaptcha challenge']",
                ]
                for iframe_sel in iframe_selectors:
                    try:
                        loc = page.frame_locator(iframe_sel)
                        body = loc.locator("body")
                        if await body.count() > 0:
                            iframe_locator = loc
                            break
                    except:
                        continue
                
                if not iframe_locator:
                    iframe_locator = page.frame_locator("iframe[src*='hcaptcha.com']")

                # Read the prompt text to understand what to drag where
                prompt_text = ""
                for selector in [".challenge-header .prompt-text", ".prompt-text", ".challenge-header", ".task-text", "h2", ".prompt-padding"]:
                    try:
                        el = iframe_locator.locator(selector)
                        if await el.count() > 0:
                            prompt_text = (await el.first.text_content() or "").strip()
                            if prompt_text:
                                break
                    except:
                        continue
                print(f"DragSolver: Prompt = '{prompt_text}'")

                # Find the draggable element
                draggable_box = None
                draggable_el = None
                drag_selectors = [".draggable", "[draggable='true']", ".drag-item", ".puzzle-piece",
                                  ".drag-icon", ".moveable", ".source-item", "img.draggable",
                                  ".challenge-item[draggable]", "img[style*='cursor: grab']",
                                  "img[style*='cursor:grab']", ".icon", ".target-icon"]
                for sel in drag_selectors:
                    try:
                        el = iframe_locator.locator(sel)
                        if await el.count() > 0:
                            draggable_box = await el.first.bounding_box()
                            if draggable_box:
                                draggable_el = el.first
                                print(f"DragSolver: Found draggable via iframe selector '{sel}'")
                                break
                    except:
                        continue

                if not draggable_box:
                    # Try page-level selectors
                    for sel in drag_selectors:
                        try:
                            el = page.locator(sel)
                            if await el.count() > 0:
                                draggable_box = await el.first.bounding_box()
                                if draggable_box:
                                    draggable_el = el.first
                                    print(f"DragSolver: Found draggable via page selector '{sel}'")
                                    break
                        except:
                            continue

                if not draggable_box:
                    # Last resort: use Moondream6 to find the draggable element visually
                    print("DragSolver: No draggable element found via DOM, using Moondream6...")
                    screenshot_bytes = await page.screenshot()
                    b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                    ollama = await self._get_ollama()
                    find_prompt = f"This is a captcha that says '{prompt_text}'. I need to find the draggable object/icon. What are its x,y pixel coordinates (center)? Reply with ONLY two numbers: x,y"
                    answer = await ollama.vision_query_with_retry(find_prompt, b64, max_tokens=30)
                    if answer:
                        nums = re.findall(r'\d+', answer)
                        if len(nums) >= 2:
                            draggable_box = {'x': int(nums[0]) - 15, 'y': int(nums[1]) - 15, 'width': 30, 'height': 30}
                            print(f"DragSolver: Moondream6 found draggable at ({nums[0]}, {nums[1]})")
                    
                    if not draggable_box:
                        print("DragSolver: No draggable element found at all")
                        await asyncio.sleep(0.5)
                        continue

                source_x = draggable_box['x'] + draggable_box['width'] / 2
                source_y = draggable_box['y'] + draggable_box['height'] / 2
                print(f"DragSolver: Found draggable at ({source_x:.0f}, {source_y:.0f})")

                # Find target location using multiple methods
                target_x, target_y, confidence = await self._find_target(page, draggable_box, iframe_locator)

                if target_x is None or target_y is None:
                    print("DragSolver: Could not determine target location")
                    await asyncio.sleep(1)
                    continue

                print(f"DragSolver: Target at ({target_x:.0f}, {target_y:.0f}), confidence={confidence:.2f}")

                # Perform the drag with human-like movement
                await HumanMouse.human_drag(page, source_x, source_y, target_x, target_y)

                # Wait and check
                await asyncio.sleep(self.config.min_solve_time_per_round)
                if await detector.is_solved():
                    print("DragSolver: SOLVED!")
                    return True

                # If not solved, try with slight offset variations
                for dx, dy in [(5, 0), (-5, 0), (0, 5), (0, -5), (8, 8), (-8, -8)]:
                    adjusted_x = target_x + dx
                    adjusted_y = target_y + dy
                    await HumanMouse.human_drag(page, source_x, source_y, adjusted_x, adjusted_y)
                    await asyncio.sleep(1.0)
                    if await detector.is_solved():
                        print(f"DragSolver: SOLVED with offset ({dx}, {dy})!")
                        return True

                print("DragSolver: Round failed, retrying...")
                await asyncio.sleep(random.uniform(1, 2))

            except Exception as e:
                print(f"DragSolver: Error in round {round_num}: {e}")
                await asyncio.sleep(1)

        print("DragSolver: Failed after all rounds.")
        return False

    async def _find_target(self, page: Page, draggable_box: dict, iframe_locator) -> Tuple[Optional[float], Optional[float], float]:
        """Find target location using multiple methods. Returns (x, y, confidence)."""
        candidates = []  # List of (x, y, confidence, method)

        # Method 1: Look for explicit drop target elements
        drop_selectors = [".drop-target", ".target-area", ".drop-zone", ".placeholder",
                          ".outline", ".shadow", ".destination", "[data-drop]"]
        for sel in drop_selectors:
            try:
                el = iframe_locator.locator(sel)
                if await el.count() > 0:
                    box = await el.first.bounding_box()
                    if box:
                        cx = box['x'] + box['width'] / 2
                        cy = box['y'] + box['height'] / 2
                        candidates.append((cx, cy, 0.9, "DOM drop target"))
                        break
            except:
                continue

        # Method 2: CV - Template matching (find where the piece fits)
        try:
            screenshot_bytes = await page.screenshot()
            full_img = np.array(Image.open(io.BytesIO(screenshot_bytes)))
            full_gray = cv2.cvtColor(full_img, cv2.COLOR_RGB2GRAY)

            # Crop the draggable piece
            x, y = int(draggable_box['x']), int(draggable_box['y'])
            w, h = int(draggable_box['width']), int(draggable_box['height'])
            piece = full_gray[y:y+h, x:x+w]

            # Template match on the full image
            res = cv2.matchTemplate(full_gray, piece, cv2.TM_CCOEFF_NORMED)
            # Zero out the source area
            res[max(0, y-15):min(res.shape[0], y+h+15), max(0, x-15):min(res.shape[1], x+w+15)] = 0

            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > 0.4:
                cx = max_loc[0] + w / 2
                cy = max_loc[1] + h / 2
                candidates.append((cx, cy, max_val, "template match"))
                print(f"  CV Template: ({cx:.0f}, {cy:.0f}), conf={max_val:.3f}")
        except Exception as e:
            print(f"  CV Template failed: {e}")

        # Method 3: Contour matching (find similar shaped outline)
        try:
            # Get edges of the piece
            piece_edges = cv2.Canny(piece, 50, 150)
            piece_contours, _ = cv2.findContours(piece_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if piece_contours:
                piece_contour = max(piece_contours, key=cv2.contourArea)

                # Find contours in the full image (excluding piece area)
                masked_gray = full_gray.copy()
                masked_gray[y:y+h, x:x+w] = 0  # Mask out the piece
                full_edges = cv2.Canny(masked_gray, 50, 150)
                full_contours, _ = cv2.findContours(full_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

                # Find best matching contour
                best_match = float('inf')
                best_contour = None
                for cnt in full_contours:
                    if cv2.contourArea(cnt) > 100:  # Filter tiny contours
                        match_val = cv2.matchShapes(piece_contour, cnt, cv2.CONTOURS_MATCH_I2, 0)
                        if match_val < best_match:
                            best_match = match_val
                            best_contour = cnt

                if best_contour is not None and best_match < 0.5:
                    M = cv2.moments(best_contour)
                    if M["m00"] > 0:
                        cx = int(M["m10"] / M["m00"])
                        cy = int(M["m01"] / M["m00"])
                        conf = max(0, 1.0 - best_match)
                        candidates.append((cx, cy, conf, "contour match"))
                        print(f"  Contour Match: ({cx}, {cy}), conf={conf:.3f}")
        except Exception as e:
            print(f"  Contour match failed: {e}")

        # Method 4: Moondream6 Vision (semantic understanding - uses actual prompt text)
        try:
            screenshot_bytes = await page.screenshot()
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            ollama = await self._get_ollama()

            # Use the actual captcha prompt text for better accuracy
            # Get prompt_text from the challenge
            challenge_prompt = ""
            try:
                iframe_locator_inner = page.frame_locator("iframe[src*='hcaptcha.com']")
                for sel in [".prompt-text", ".challenge-header", ".task-text", "h2"]:
                    try:
                        el = iframe_locator_inner.locator(sel)
                        if await el.count() > 0:
                            challenge_prompt = (await el.first.text_content() or "").strip()
                            if challenge_prompt:
                                break
                    except:
                        continue
            except:
                pass

            if challenge_prompt:
                prompt = f"This is a captcha challenge. The instruction says: '{challenge_prompt}'. Where should the object be dragged TO (the target/destination)? Reply with ONLY the x,y pixel coordinates. Example: 340,250"
            else:
                prompt = "This is a drag-and-drop captcha. There is a moveable piece/icon that needs to be placed at a specific location on the image. Where should it be placed? Reply with ONLY the x,y pixel coordinates as two numbers separated by a comma. Example: 340,250"

            answer = await ollama.vision_query_with_retry(prompt, b64, max_tokens=30)
            if answer:
                nums = re.findall(r'\d+', answer)
                if len(nums) >= 2:
                    lx, ly = int(nums[0]), int(nums[1])
                    viewport = page.viewport_size
                    if viewport and lx < viewport['width'] and ly < viewport['height']:
                        candidates.append((lx, ly, 0.6, "moondream6"))
                        print(f"  Moondream6: ({lx}, {ly})")
        except Exception as e:
            print(f"  Moondream6 drag failed: {e}")

        # Select best candidate
        if not candidates:
            return None, None, 0.0

        # Sort by confidence, pick the best
        candidates.sort(key=lambda c: c[2], reverse=True)
        best = candidates[0]
        print(f"DragSolver: Best target: ({best[0]:.0f}, {best[1]:.0f}) via {best[3]} (conf={best[2]:.3f})")

        # If multiple high-confidence candidates agree, use their average
        if len(candidates) >= 2:
            high_conf = [c for c in candidates if c[2] > 0.5]
            if len(high_conf) >= 2:
                # Check if they agree (within 30px)
                xs = [c[0] for c in high_conf]
                ys = [c[1] for c in high_conf]
                if max(xs) - min(xs) < 40 and max(ys) - min(ys) < 40:
                    avg_x = sum(xs) / len(xs)
                    avg_y = sum(ys) / len(ys)
                    return avg_x, avg_y, max(c[2] for c in high_conf)

        return best[0], best[1], best[2]


# =============================================================================
# PATTERN BREAKER SOLVER (Moondream6 Vision + CV analysis)
# =============================================================================

class PatternBreakerSolver(CaptchaSolver):
    """
    Solves pattern-breaking hCaptcha challenges where:
    - A grid shows symbols/shapes that mostly follow a pattern
    - You must click the ones that DON'T fit the pattern
    
    Uses:
    1. Moondream6 vision for semantic pattern analysis
    2. OpenCV for structural similarity comparison between tiles
    3. CLIP for visual outlier detection
    4. Multi-method consensus for accuracy
    """

    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.ollama: Optional[OllamaVisionClient] = None
        self.clip_model: Optional[ClipModel] = None

    async def _get_ollama(self):
        if self.ollama is None:
            self.ollama = await OllamaVisionClient.get_instance(self.config)
        return self.ollama

    async def _get_clip_model(self):
        if self.clip_model is None:
            self.clip_model = await ClipModel.get_instance()
        return self.clip_model

    async def solve(self, page: Page) -> bool:
        print("PatternBreakerSolver: Starting pattern challenge...")

        for round_num in range(self.config.max_challenge_rounds):
            try:
                print(f"PatternBreaker: Round {round_num + 1}/{self.config.max_challenge_rounds}")

                # Get the challenge iframe and screenshot
                iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")

                # Try to get iframe bounding box
                iframe_box = None
                try:
                    frame_el = await iframe_locator.locator(":root").element_handle()
                    if frame_el:
                        iframe_box = await frame_el.bounding_box()
                except:
                    pass

                if not iframe_box:
                    # Fallback: use full page
                    viewport = page.viewport_size
                    if viewport:
                        iframe_box = {'x': 0, 'y': 0, 'width': viewport['width'], 'height': viewport['height']}
                    else:
                        print("PatternBreaker: Cannot determine challenge area")
                        return False

                # Take screenshot
                full_screenshot_bytes = await page.screenshot()
                full_image = Image.open(io.BytesIO(full_screenshot_bytes))

                # Crop to challenge area
                cropped = full_image.crop((
                    int(iframe_box['x']), int(iframe_box['y']),
                    int(iframe_box['x'] + iframe_box['width']),
                    int(iframe_box['y'] + iframe_box['height'])
                ))

                img_bytes = io.BytesIO()
                cropped.save(img_bytes, format="PNG")
                screenshot_b64 = base64.b64encode(img_bytes.getvalue()).decode('utf-8')
                img_bytes.seek(0)

                # Detect grid dimensions
                grid_rows, grid_cols = self._detect_grid_dimensions(img_bytes.getvalue())
                print(f"PatternBreaker: Grid={grid_rows}x{grid_cols}")

                # Method 1: Moondream6 vision analysis
                llm_positions = await self._analyze_with_moondream(screenshot_b64, grid_rows, grid_cols)

                # Method 2: CV structural similarity analysis
                cv_positions = self._analyze_with_cv(img_bytes.getvalue(), grid_rows, grid_cols)

                # Method 3: CLIP outlier detection
                clip_positions = await self._analyze_with_clip(cropped, grid_rows, grid_cols)

                # Consensus: combine results
                all_positions = []
                position_votes = defaultdict(int)

                for pos in llm_positions:
                    position_votes[pos] += 3  # LLM gets highest weight for pattern tasks
                for pos in cv_positions:
                    position_votes[pos] += 2
                for pos in clip_positions:
                    position_votes[pos] += 1

                # Select positions with >= 2 votes, or all LLM positions if no consensus
                final_positions = [pos for pos, votes in position_votes.items() if votes >= 2]
                if not final_positions:
                    final_positions = llm_positions  # Trust LLM as primary

                if not final_positions:
                    print("PatternBreaker: No pattern-breaking positions found. Retrying...")
                    await self._rate_limit_delay()
                    continue

                # Validate: shouldn't select more than ~40% of tiles (they're supposed to be outliers)
                max_selections = max(2, int(grid_rows * grid_cols * 0.4))
                if len(final_positions) > max_selections:
                    # Keep only the ones with highest votes
                    sorted_pos = sorted(final_positions, key=lambda p: position_votes[p], reverse=True)
                    final_positions = sorted_pos[:max_selections]

                print(f"PatternBreaker: Clicking positions: {final_positions}")

                # Click the identified tiles
                tile_width = iframe_box['width'] / grid_cols
                tile_height = iframe_box['height'] / grid_rows

                for r, c in final_positions:
                    center_x = iframe_box['x'] + (c * tile_width) + (tile_width / 2) + random.uniform(-3, 3)
                    center_y = iframe_box['y'] + (r * tile_height) + (tile_height / 2) + random.uniform(-3, 3)
                    await HumanMouse.move_and_click(page, center_x, center_y)
                    await self._rate_limit_delay()

                # Click verify
                try:
                    for verify_sel in ["button.verifybtn", ".button-submit", "button[type='submit']"]:
                        verify_btn = iframe_locator.locator(verify_sel)
                        if await verify_btn.count() > 0:
                            box = await verify_btn.bounding_box()
                            if box:
                                await HumanMouse.move_and_click(page, box['x'] + box['width']/2, box['y'] + box['height']/2)
                                break
                except:
                    pass

                await asyncio.sleep(2.0)

                detector = ChallengeDetector(page)
                if await detector.is_solved():
                    print("PatternBreaker: SOLVED!")
                    return True

                print("PatternBreaker: Not solved, retrying...")
                await asyncio.sleep(random.uniform(1, 2))

            except Exception as e:
                print(f"PatternBreaker: Error in round {round_num + 1}: {e}")
                await self._rate_limit_delay()

        print("PatternBreaker: Failed after all rounds.")
        return False

    async def _analyze_with_moondream(self, screenshot_b64: str, grid_rows: int, grid_cols: int) -> List[Tuple[int, int]]:
        """Use Moondream6 to identify pattern-breaking elements."""
        ollama = await self._get_ollama()

        prompt = f"""This image shows a {grid_rows}x{grid_cols} grid of symbols/shapes for a captcha challenge.
Most symbols follow the SAME pattern (same shape, color, orientation, style).
Some symbols are DIFFERENT - they break the pattern.

Which grid cells contain the ODD/DIFFERENT symbols that don't match the majority?
Use 0-indexed positions: row 0 is top, col 0 is left.

Reply with ONLY a JSON array of [row,col] pairs. Example: [[0,2],[1,4],[3,1]]"""

        answer = await ollama.vision_query_with_retry(prompt, screenshot_b64, max_tokens=200)
        print(f"PatternBreaker Moondream6: {answer}")

        positions = self._parse_positions(answer, grid_rows, grid_cols)

        # If first attempt fails, try a simpler prompt
        if not positions:
            simple_prompt = f"In this {grid_rows}x{grid_cols} grid, which cells look DIFFERENT from the rest? Reply as [[row,col],...] with 0-indexed positions."
            answer2 = await ollama.vision_query_with_retry(simple_prompt, screenshot_b64, max_tokens=150)
            positions = self._parse_positions(answer2, grid_rows, grid_cols)

        return positions

    def _analyze_with_cv(self, screenshot_bytes: bytes, grid_rows: int, grid_cols: int) -> List[Tuple[int, int]]:
        """Use OpenCV structural similarity to find outlier tiles."""
        try:
            nparr = np.frombuffer(screenshot_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return []

            h, w = img.shape[:2]
            tile_h = h // grid_rows
            tile_w = w // grid_cols

            # Extract each tile
            tiles = []
            for r in range(grid_rows):
                for c in range(grid_cols):
                    tile = img[r*tile_h:(r+1)*tile_h, c*tile_w:(c+1)*tile_w]
                    tiles.append(tile)

            # Calculate histogram for each tile
            histograms = []
            for tile in tiles:
                hist = cv2.calcHist([tile], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
                hist = cv2.normalize(hist, hist).flatten()
                histograms.append(hist)

            # Calculate pairwise similarity
            n = len(tiles)
            similarity_matrix = np.zeros((n, n))
            for i in range(n):
                for j in range(n):
                    similarity_matrix[i][j] = cv2.compareHist(histograms[i], histograms[j], cv2.HISTCMP_CORREL)

            # Average similarity of each tile to all others
            avg_similarities = []
            for i in range(n):
                others = [similarity_matrix[i][j] for j in range(n) if j != i]
                avg_similarities.append(np.mean(others))

            # Find outliers (tiles with low average similarity)
            avg_sim_np = np.array(avg_similarities)
            mean_sim = np.mean(avg_sim_np)
            std_sim = np.std(avg_sim_np)

            outlier_indices = []
            threshold = mean_sim - 1.2 * std_sim  # Tiles below 1.2 std are outliers
            for i, sim in enumerate(avg_similarities):
                if sim < threshold:
                    r, c = divmod(i, grid_cols)
                    outlier_indices.append((r, c))

            if outlier_indices:
                print(f"  CV outliers: {outlier_indices}")
            return outlier_indices

        except Exception as e:
            print(f"  CV pattern analysis failed: {e}")
            return []

    async def _analyze_with_clip(self, cropped_image: Image.Image, grid_rows: int, grid_cols: int) -> List[Tuple[int, int]]:
        """Use CLIP to find visually different tiles."""
        try:
            clip = await self._get_clip_model()
            w, h = cropped_image.size
            tile_w = w // grid_cols
            tile_h = h // grid_rows

            # Extract tiles
            tile_images = []
            for r in range(grid_rows):
                for c in range(grid_cols):
                    tile = cropped_image.crop((c*tile_w, r*tile_h, (c+1)*tile_w, (r+1)*tile_h))
                    tile_images.append(tile)

            if not tile_images:
                return []

            # Get CLIP features for all tiles
            features = await clip.get_image_features_batch(tile_images)

            # Calculate centroid (average feature)
            centroid = features.mean(dim=0, keepdim=True)
            centroid = centroid / centroid.norm(dim=-1, keepdim=True)

            # Calculate distance from centroid for each tile
            distances = 1.0 - (features @ centroid.T).squeeze().cpu().numpy()

            # Find outliers
            mean_dist = np.mean(distances)
            std_dist = np.std(distances)
            threshold = mean_dist + 1.0 * std_dist

            outliers = []
            for i, dist in enumerate(distances):
                if dist > threshold:
                    r, c = divmod(i, grid_cols)
                    outliers.append((r, c))

            if outliers:
                print(f"  CLIP outliers: {outliers}")
            return outliers

        except Exception as e:
            print(f"  CLIP pattern analysis failed: {e}")
            return []

    def _parse_positions(self, answer: str, max_rows: int, max_cols: int) -> List[Tuple[int, int]]:
        """Parse grid positions from LLM response."""
        if not answer:
            return []
        # Try JSON parse
        try:
            # Find JSON array in response
            json_match = re.search(r'\[[\s\S]*\]', answer)
            if json_match:
                parsed = json.loads(json_match.group())
                if isinstance(parsed, list):
                    positions = []
                    for item in parsed:
                        if isinstance(item, list) and len(item) == 2:
                            r, c = int(item[0]), int(item[1])
                            if 0 <= r < max_rows and 0 <= c < max_cols:
                                positions.append((r, c))
                    if positions:
                        return positions
        except:
            pass
        # Regex fallback
        matches = re.findall(r'\[(\d+)\s*,\s*(\d+)\]|\((\d+)\s*,\s*(\d+)\)', answer)
        positions = []
        for match in matches:
            coords = [int(c) for c in match if c]
            if len(coords) == 2:
                r, c = coords[0], coords[1]
                if 0 <= r < max_rows and 0 <= c < max_cols:
                    positions.append((r, c))
        return positions

    def _detect_grid_dimensions(self, screenshot_bytes: bytes) -> Tuple[int, int]:
        """Detect grid dimensions using line detection and contour analysis."""
        try:
            nparr = np.frombuffer(screenshot_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return 3, 3

            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edged = cv2.Canny(blurred, 50, 150)

            # Find contours that could be grid cells
            contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # Filter for square-ish contours of reasonable size
            min_area = (h * w) / 100
            max_area = (h * w) / 4
            cell_candidates = []
            for cnt in contours:
                area = cv2.contourArea(cnt)
                if min_area < area < max_area:
                    x, y, cw, ch = cv2.boundingRect(cnt)
                    aspect = float(cw) / max(ch, 1)
                    if 0.6 < aspect < 1.6:
                        cell_candidates.append((x, y, cw, ch))

            if len(cell_candidates) >= 4:
                # Cluster by y-coordinate to find rows
                y_coords = sorted(set(c[1] for c in cell_candidates))
                rows = 1
                for i in range(1, len(y_coords)):
                    if y_coords[i] - y_coords[i-1] > 15:
                        rows += 1

                # Cluster by x-coordinate to find cols
                x_coords = sorted(set(c[0] for c in cell_candidates))
                cols = 1
                for i in range(1, len(x_coords)):
                    if x_coords[i] - x_coords[i-1] > 15:
                        cols += 1

                if 2 <= rows <= 6 and 2 <= cols <= 6:
                    return rows, cols

            # Fallback: try line detection
            lines_h = cv2.HoughLinesP(edged, 1, np.pi/180, 50, minLineLength=w*0.4, maxLineGap=10)
            lines_v = cv2.HoughLinesP(edged, 1, np.pi/2, 50, minLineLength=h*0.4, maxLineGap=10)

            h_lines = len(lines_h) if lines_h is not None else 0
            v_lines = len(lines_v) if lines_v is not None else 0

            if h_lines >= 2 and v_lines >= 2:
                rows = min(6, max(2, h_lines - 1))
                cols = min(6, max(2, v_lines - 1))
                return rows, cols

            return 3, 3  # Default fallback

        except:
            return 3, 3


# =============================================================================
# CHALLENGE ROUTER
# =============================================================================

class ChallengeRouter:
    """Routes challenges to the appropriate solver based on detection."""

    def __init__(self, config: SolverConfig):
        self.config = config

    async def detect_and_route(self, page: Page) -> Optional[CaptchaSolver]:
        print("Router: Detecting challenge type...")
        detector = ChallengeDetector(page)
        challenge_type = await detector.detect_challenge_type()
        print(f"Router: Detected type = '{challenge_type}'")

        if challenge_type == "pattern":
            print("Router -> PatternBreakerSolver")
            return PatternBreakerSolver(self.config)
        elif challenge_type == "drag":
            print("Router -> DragSolver")
            return DragSolver(self.config)
        elif challenge_type == "slider":
            print("Router -> SliderSolver")
            return SliderSolver(self.config)
        elif challenge_type == "grid":
            print("Router -> GodSolver")
            return GodSolver(self.config)
        else:
            print("Router -> GodSolver (default)")
            return GodSolver(self.config)


# =============================================================================
# MASTER SOLVER (Top-level with routing + fallback chain)
# =============================================================================

class MasterSolver(CaptchaSolver):
    """
    Top-level solver with:
    - Automatic challenge type detection and routing
    - Fallback chain: specialized solver -> GodSolver -> Moondream6 raw
    - Multi-round retry with different strategies
    """

    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.router = ChallengeRouter(config)
        self.god_solver = GodSolver(config)
        self.drag_solver = DragSolver(config)
        self.pattern_solver = PatternBreakerSolver(config)
        self.slider_solver = SliderSolver(config)

    async def solve(self, page: Page) -> bool:
        print("MasterSolver: Starting challenge resolution.")

        # 1. Detect and route to appropriate solver
        solver_instance = await self.router.detect_and_route(page)
        if solver_instance is None:
            solver_instance = self.god_solver

        print(f"MasterSolver: Primary solver = {solver_instance.__class__.__name__}")

        # 2. Try primary solver
        success = await solver_instance.solve(page)
        if success:
            print("MasterSolver: SUCCESS (primary solver)")
            return True

        # 3. Fallback chain
        fallback_solvers = [self.god_solver, self.drag_solver, self.pattern_solver, self.slider_solver]
        for fallback in fallback_solvers:
            if fallback.__class__ != solver_instance.__class__:
                print(f"MasterSolver: Trying fallback: {fallback.__class__.__name__}")
                try:
                    success = await fallback.solve(page)
                    if success:
                        print(f"MasterSolver: SUCCESS via {fallback.__class__.__name__}")
                        return True
                except Exception as e:
                    print(f"MasterSolver: Fallback {fallback.__class__.__name__} error: {e}")
                    continue

        print("MasterSolver: FAILED (all solvers exhausted)")
        return False

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
    print("=" * 60)
    print("MasterSolver initialized with Ollama Moondream6 backend")
    print("=" * 60)
    print(f"  Ollama URL: {config.ollama_base_url}")
    print(f"  Model: {config.ollama_model}")
    print(f"  Solvers:")
    print(f"    - GodSolver: CLIP ViT-L-14 + Moondream6 fallback (grid challenges)")
    print(f"    - DragSolver: CV + SIFT + Contour + Moondream6 (drag-to-place)")
    print(f"    - PatternBreakerSolver: Moondream6 + CV + CLIP outlier (pattern challenges)")
    print(f"    - SliderSolver: Template + Canny + Phase + SIFT + Moondream6 (slider puzzles)")
    print(f"  Router: Auto-detects challenge type from DOM + prompt text")
    print(f"  Fallback: Full chain (primary -> GodSolver -> others)")
    print("=" * 60)
    print("Pass a Playwright Page object to solver.solve(page)")


if __name__ == "__main__":
    asyncio.run(main())
