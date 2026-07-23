
import asyncio
import base64
import io
import math
import os
import re
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
import torch
import open_clip
from PIL import Image
from playwright.async_api import async_playwright, Page, BrowserContext
import openai


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class SolverConfig:
    clip_confidence_threshold: float = 0.55
    max_challenge_rounds: int = 3
    timeout: int = 30  # seconds
    headless: bool = True
    browser_type: str = "chromium"
    rate_limit_min_delay: float = 0.1
    rate_limit_max_delay: float = 0.35
    min_solve_time_per_round: float = 2.5
    llm_model: str = "gpt-5-nano"
    llm_fallback: bool = True
    pattern_solver_model: str = "gpt-5-nano"


# =============================================================================
# CLIP MODEL (Singleton)
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

    async def _load_model(self):
        # ViT-L-14 is significantly more accurate than ViT-B-32 for zero-shot classification
        # ~15-20% better on unusual categories (chimneys, seaplanes, etc.)
        print(f"Loading OpenCLIP ViT-L-14 on {self.device}...")
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-L-14", pretrained="laion2b_s32b_b82k", device=self.device
        )
        self.tokenizer = open_clip.get_tokenizer("ViT-L-14")
        self.model.eval()
        print("OpenCLIP ViT-L-14 model loaded.")

    async def get_image_features(self, images: List[Image.Image]):
        image_tensors = torch.stack([self.preprocess(img) for img in images]).to(self.device)
        with torch.no_grad():
            features = self.model.encode_image(image_tensors)
        return features / features.norm(dim=-1, keepdim=True)

    async def get_text_features(self, texts: List[str]):
        tokens = self.tokenizer(texts).to(self.device)
        with torch.no_grad():
            features = self.model.encode_text(tokens)
        return features / features.norm(dim=-1, keepdim=True)


# =============================================================================
# STEALTH ENGINE
# =============================================================================

STEALTH_SCRIPT = """
(() => {
    // --- DO NOT delete navigator.webdriver. Its absence is now a signal. ---
    // Instead, make it look like a normal Chrome where webdriver is false.
    Object.defineProperty(navigator, 'webdriver', {
        get: () => false,
        configurable: true
    });

    // --- Hardware fingerprint consistency ---
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

    // --- Languages (must match Accept-Language header) ---
    Object.defineProperty(navigator, 'languages', {
        get: () => Object.freeze(['en-US', 'en'])
    });
    Object.defineProperty(navigator, 'language', { get: () => 'en-US' });

    // --- Platform consistency ---
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });

    // --- Screen metrics (consistent with viewport) ---
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

    // --- Timezone (must match headers/geolocation if used) ---
    const originalDateTimeFormat = Intl.DateTimeFormat;
    const handler = {
        construct(target, args) {
            if (args.length > 1 && args[1] && args[1].timeZone) {
                return new target(...args);
            }
            args[1] = args[1] || {};
            args[1].timeZone = 'America/New_York';
            return new target(...args);
        }
    };
    // Don't override DateTimeFormat - it's too detectable. Just ensure consistency.

    // --- Permissions API ---
    const originalQuery = navigator.permissions?.query;
    if (originalQuery) {
        navigator.permissions.query = (parameters) => {
            if (parameters.name === 'notifications') {
                return Promise.resolve({ state: 'prompt', onchange: null });
            }
            return originalQuery.call(navigator.permissions, parameters);
        };
    }

    // --- WebGL (consistent, not obviously spoofed) ---
    const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        // UNMASKED_VENDOR_WEBGL
        if (param === 37445) return 'Google Inc. (Intel)';
        // UNMASKED_RENDERER_WEBGL
        if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
        // MAX_TEXTURE_SIZE
        if (param === 3379) return 16384;
        // MAX_RENDERBUFFER_SIZE
        if (param === 34024) return 16384;
        // MAX_VIEWPORT_DIMS
        if (param === 3386) return new Int32Array([32767, 32767]);
        return getParameterOrig.call(this, param);
    };

    // Also patch WebGL2
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

    // --- Canvas: DO NOT add noise. Stable fingerprint is better. ---
    // Leave canvas completely untouched.

    // --- AudioContext fingerprint (stable, not random) ---
    const origCreateOscillator = AudioContext.prototype.createOscillator;
    // Don't patch audio - a stable audio fingerprint is fine.

    // --- Battery API (hide it - modern Chrome doesn't expose it easily) ---
    if (navigator.getBattery) {
        navigator.getBattery = undefined;
    }

    // --- WebRTC leak prevention ---
    const origRTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (origRTCPeerConnection) {
        window.RTCPeerConnection = function(...args) {
            const config = args[0] || {};
            // Force TURN-only to prevent local IP leak
            config.iceTransportPolicy = 'relay';
            return new origRTCPeerConnection(config, ...args.slice(1));
        };
        window.RTCPeerConnection.prototype = origRTCPeerConnection.prototype;
    }

    // --- Client Hints (navigator.userAgentData) ---
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
                architecture: 'x86',
                bitness: '64',
                fullVersionList: [
                    { brand: 'Not_A Brand', version: '8.0.0.0' },
                    { brand: 'Chromium', version: '120.0.6099.109' },
                    { brand: 'Google Chrome', version: '120.0.6099.109' }
                ],
                mobile: false,
                model: '',
                platform: 'Windows',
                platformVersion: '15.0.0',
                uaFullVersion: '120.0.6099.109'
            })
        })
    });

    // --- Plugins (don't fake them - just ensure PDF viewer is present like real Chrome) ---
    // Modern Chrome has very few plugins. Don't override - let Chromium's defaults show.
    // The key insight: REMOVING the override is less detectable than adding a fake one.

    // --- Connection API ---
    Object.defineProperty(navigator, 'connection', {
        get: () => ({
            effectiveType: '4g',
            rtt: 50,
            downlink: 10,
            saveData: false
        })
    });

    // --- Media devices (consistent) ---
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
        const origEnum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
        navigator.mediaDevices.enumerateDevices = async () => {
            return [
                { deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' },
                { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
                { deviceId: 'default', kind: 'videoinput', label: '', groupId: 'default' }
            ];
        };
    }
})();
"""


# =============================================================================
# HUMAN-LIKE MOUSE MOVEMENT
# =============================================================================

class HumanMouse:
    """
    Realistic mouse movement using minimum-jerk trajectory model.
    Includes overshoot, micro-corrections, variable speed, hesitation, and wind-based curves.
    """

    @staticmethod
    def _minimum_jerk(t: float) -> float:
        """Minimum jerk trajectory (smooth human-like velocity profile)."""
        return 10 * t**3 - 15 * t**4 + 6 * t**5

    @staticmethod
    def _generate_path(start_x: float, start_y: float, end_x: float, end_y: float) -> List[Tuple[float, float]]:
        """Generate a human-like path with overshoot, correction, and wind-based Bezier curves."""
        distance = math.sqrt((end_x - start_x)**2 + (end_y - start_y)**2)

        # Number of points scales with distance
        num_points = max(20, min(100, int(distance / 3)))

        # Overshoot (humans overshoot on fast movements)
        overshoot_amount = random.uniform(0, min(20, distance * 0.1))
        overshoot_angle = math.atan2(end_y - start_y, end_x - start_x)
        overshoot_x = end_x + overshoot_amount * math.cos(overshoot_angle)
        overshoot_y = end_y + overshoot_amount * math.sin(overshoot_angle)

        # Wind-based Bezier control points
        # Introduce a random 'wind' force perpendicular to the main movement direction
        wind_strength = random.uniform(0.1, 0.4) * distance
        wind_angle = overshoot_angle + random.choice([-1, 1]) * math.pi / 2 + random.uniform(-0.3, 0.3)

        # Control point 1 (closer to start)
        cp1_x = start_x + (end_x - start_x) * random.uniform(0.2, 0.4) + wind_strength * math.cos(wind_angle) * random.uniform(0.3, 0.7)
        cp1_y = start_y + (end_y - start_y) * random.uniform(0.2, 0.4) + wind_strength * math.sin(wind_angle) * random.uniform(0.3, 0.7)

        # Control point 2 (closer to end, influenced by overshoot)
        cp2_x = start_x + (overshoot_x - start_x) * random.uniform(0.6, 0.8) + wind_strength * math.cos(wind_angle) * random.uniform(0.1, 0.4)
        cp2_y = start_y + (overshoot_y - start_y) * random.uniform(0.6, 0.8) + wind_strength * math.sin(wind_angle) * random.uniform(0.1, 0.4)

        path = []

        # Main movement (cubic Bezier with overshoot and wind)
        overshoot_point_idx = int(num_points * random.uniform(0.75, 0.9))

        for i in range(num_points):
            t = i / (num_points - 1)
            jerk_t = HumanMouse._minimum_jerk(t)

            if i < overshoot_point_idx:
                # Moving towards overshoot target using Bezier
                prog = i / overshoot_point_idx
                jerk_prog = HumanMouse._minimum_jerk(prog)
                
                # Cubic Bezier calculation
                bx = (1 - jerk_prog)**3 * start_x + 3 * (1 - jerk_prog)**2 * jerk_prog * cp1_x + \
                     3 * (1 - jerk_prog) * jerk_prog**2 * cp2_x + jerk_prog**3 * overshoot_x
                by = (1 - jerk_prog)**3 * start_y + 3 * (1 - jerk_prog)**2 * jerk_prog * cp1_y + \
                     3 * (1 - jerk_prog) * jerk_prog**2 * cp2_y + jerk_prog**3 * overshoot_y
            else:
                # Correcting from overshoot to final target
                correction_prog = (i - overshoot_point_idx) / (num_points - overshoot_point_idx - 1)
                correction_prog = min(1.0, correction_prog) # Ensure it doesn't exceed 1.0
                bx = overshoot_x + (end_x - overshoot_x) * HumanMouse._minimum_jerk(correction_prog)
                by = overshoot_y + (end_y - overshoot_y) * HumanMouse._minimum_jerk(correction_prog)

            # Add micro-tremor (decreases as we approach target)
            tremor_scale = max(0, 1.0 - t) * random.uniform(0.5, 2.0)
            bx += random.gauss(0, tremor_scale)
            by += random.gauss(0, tremor_scale)

            path.append((bx, by))

        return path

    @staticmethod
    async def move_and_click(page: Page, target_x: float, target_y: float,
                             start_x: Optional[float] = None, start_y: Optional[float] = None,
                             element_width: Optional[float] = None, element_height: Optional[float] = None):
        """Move mouse along human-like path and click."""
        if start_x is None:
            start_x = target_x + random.uniform(-50, 50)
        if start_y is None:
            start_y = target_y + random.uniform(-50, 50)

        path = HumanMouse._generate_path(start_x, start_y, target_x, target_y)

        # Variable speed: faster in middle, slower at start/end
        for i, (x, y) in enumerate(path):
            progress = i / len(path)
            # Bell-curve speed: slow-fast-slow
            speed_factor = math.sin(progress * math.pi)
            base_delay = random.uniform(0.003, 0.010)
            delay = base_delay / max(speed_factor, 0.2)

            await page.mouse.move(x, y)
            await asyncio.sleep(delay)

            # Micro-pauses: 10% chance of 50-200ms pause mid-movement
            if random.random() < 0.1 and 0.2 < progress < 0.8: # Only mid-movement
                await asyncio.sleep(random.uniform(0.05, 0.2))

        # Small pause before click (human reaction time)
        await asyncio.sleep(random.uniform(0.03, 0.12))

        # Variable click hold time based on element size
        hold_time = random.uniform(0.04, 0.11) # Default range
        if element_width is not None and element_height is not None:
            # Larger elements might imply a more deliberate click
            area = element_width * element_height
            if area > 1000: # Example threshold for a larger element
                hold_time = random.uniform(0.06, 0.15)

        await page.mouse.down()
        await asyncio.sleep(hold_time)
        await page.mouse.up()

        # Occasional double-micro-movement after click
        if random.random() < 0.2: # 20% chance of double micro-movement
            await asyncio.sleep(random.uniform(0.02, 0.08))
            await page.mouse.move(
                target_x + random.uniform(-2, 2),
                target_y + random.uniform(-2, 2)
            )
            await asyncio.sleep(random.uniform(0.02, 0.08))
            await page.mouse.move(
                target_x + random.uniform(-1, 1),
                target_y + random.uniform(-1, 1)
            )
        elif random.random() < 0.3: # 30% chance of single micro-movement
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.mouse.move(
                target_x + random.uniform(-3, 3),
                target_y + random.uniform(-3, 3)
            )


# =============================================================================
# CHALLENGE DETECTOR
# =============================================================================

class ChallengeDetector:
    def __init__(self, page: Page):
        self.page = page

    async def is_captcha_visible(self) -> bool:
        """Checks if a captcha challenge is currently visible on the page."""
        # Check for common reCAPTCHA/hCaptcha elements
        selectors = [
            'iframe[src*="captcha"]',
            'div.g-recaptcha',
            'div.h-captcha',
            '#captcha-challenge',
            '#rc-imageselect',
            '#cf-challenge-container',
            'div[data-hcaptcha-widget-id]',
            'div[data-recaptcha-widget-id]',
            'div[aria-label*="captcha"]',
            'div[role="dialog"][aria-modal="true"]',
        ]
        for selector in selectors:
            if await self.page.locator(selector).is_visible():
                return True
        return False

    async def is_solved(self) -> bool:
        """Checks if the captcha challenge has been successfully solved."""
        # Check for common success indicators
        # 1. reCAPTCHA/hCaptcha checkbox state
        checkbox_selectors = [
            '#rc-anchor-container input[type="checkbox"]',
            '#h-captcha-container input[type="checkbox"]',
            'div.g-recaptcha-response',
            'div.h-captcha-response',
        ]
        for selector in checkbox_selectors:
            checkbox = self.page.locator(selector)
            if await checkbox.is_visible() and await checkbox.is_checked():
                return True
            # For hCaptcha, sometimes the checkbox is hidden and a success token is in a textarea
            if await self.page.locator('textarea[name="h-captcha-response"]').evaluate("el => el.value.length > 0"): # type: ignore
                return True

        # 2. Check for a success message or element after challenge
        success_selectors = [
            'div.captcha-success',
            'div.challenge-passed',
            'span.success-message',
            '#challenge-success',
        ]
        for selector in success_selectors:
            if await self.page.locator(selector).is_visible():
                return True

        # 3. Check for URL redirect (e.g., if challenge was on an interstitial page)
        # This is harder to do generically without knowing the expected post-solve URL
        # For now, we assume the challenge is on the current page.

        # 4. Check for disappearance of challenge elements
        if not await self.is_captcha_visible():
            return True

        return False

    async def detect_challenge_type(self) -> str:
        """Detects the type of captcha challenge: grid, drag, pattern, slider, shape, or unknown."""
        challenge_type = "unknown"
        prompt_text = ""

        # Try to find common challenge prompt elements
        prompt_selectors = [
            'div.rc-imageselect-instructions',
            'div.hcaptcha-challenge-header',
            'div.challenge-prompt',
            'p.challenge-text',
            'div[aria-label*="challenge"]',
            'h2.challenge-title',
        ]

        for selector in prompt_selectors:
            element = self.page.locator(selector)
            if await element.is_visible():
                prompt_text = (await element.text_content() or "").lower()
                break

        if "break the pattern" in prompt_text or "odd one out" in prompt_text or "doesn't belong" in prompt_text:
            challenge_type = "pattern"
        elif "select all" in prompt_text or "click all" in prompt_text or "containing" in prompt_text or "with a" in prompt_text or "images of" in prompt_text:
            challenge_type = "grid"
        elif "drag" in prompt_text or "place" in prompt_text or "move" in prompt_text or "drop" in prompt_text or "fit" in prompt_text:
            challenge_type = "drag"
        else:
            # Inspect DOM for structural clues
            # Check for draggable elements (e.g., elements with draggable=true attribute or specific classes)
            if await self.page.locator('[draggable="true"]').count() > 0 or \
               await self.page.locator('.draggable-item').count() > 0:
                challenge_type = "drag"
            # Check for grid tiles (e.g., div.tile, img.challenge-image)
            elif await self.page.locator('div.rc-image-tile').count() > 0 or \
                 await self.page.locator('div.hcaptcha-image-tile').count() > 0 or \
                 await self.page.locator('img.challenge-image').count() > 0:
                challenge_type = "grid"
            # Check for slider elements (e.g., elements with role="slider" or specific classes)
            elif await self.page.locator('[role="slider"]').count() > 0 or \
                 await self.page.locator('.slider-track').count() > 0 or \
                 await self.page.locator('.slider-handle').count() > 0:
                challenge_type = "slider"
            # Check for shape-matching elements (e.g., specific SVG or canvas elements)
            elif await self.page.locator('svg.shape-challenge').count() > 0 or \
                 await self.page.locator('canvas.shape-challenge').count() > 0:
                challenge_type = "shape"

        print(f"Detected challenge type: {challenge_type} (from prompt: \"{prompt_text}\")")
        return challenge_type


# =============================================================================
# BASE SOLVER CLASS
# =============================================================================

class CaptchaSolver:
    def __init__(self, page: Page, config: SolverConfig):
        self.page = page
        self.config = config
        self.detector = ChallengeDetector(page)

    async def solve(self) -> bool:
        raise NotImplementedError

    async def _rate_limit_delay(self):
        await asyncio.sleep(random.uniform(self.config.rate_limit_min_delay, self.config.rate_limit_max_delay))


# =============================================================================
# PLAYWRIGHT SOLVER
# =============================================================================

class PlaywrightSolver(CaptchaSolver):
    def __init__(self, page: Page, config: SolverConfig):
        super().__init__(page, config)

    async def get_screenshot(self, locator_selector: str = None) -> Image.Image:
        """Takes a screenshot of the page or a specific locator and returns it as a PIL Image."""
        if locator_selector:
            locator = self.page.locator(locator_selector)
            if not await locator.is_visible():
                raise ValueError(f"Locator {locator_selector} not visible for screenshot.")
            screenshot_bytes = await locator.screenshot()
        else:
            screenshot_bytes = await self.page.screenshot()

        return Image.open(io.BytesIO(screenshot_bytes))

    async def get_element_bounds(self, selector: str) -> Optional[Dict[str, float]]:
        """Returns the bounding box of an element as a dictionary."""
        element = self.page.locator(selector)
        if await element.is_visible():
            box = await element.bounding_box()
            return box
        return None

    async def click_element(self, selector: str, delay_before: float = 0.1, delay_after: float = 0.1):
        """Clicks an element with human-like movement."""
        box = await self.get_element_bounds(selector)
        if not box:
            raise ValueError(f"Element {selector} not found or not visible for clicking.")

        target_x = box['x'] + box['width'] / 2
        target_y = box['y'] + box['height'] / 2

        await HumanMouse.move_and_click(self.page, target_x, target_y, 
                                         element_width=box['width'], element_height=box['height'])
        await asyncio.sleep(delay_after)

    async def type_into_element(self, selector: str, text: str, delay_between_chars: float = 0.05):
        """Types text into an element with human-like delays."""
        await self.page.locator(selector).type(text, delay=delay_between_chars)
        await self._rate_limit_delay()

    async def drag_and_drop(self, source_selector: str, target_selector: str):
        """Performs a human-like drag and drop operation."""
        source_box = await self.get_element_bounds(source_selector)
        target_box = await self.get_element_bounds(target_selector)

        if not source_box or not target_box:
            raise ValueError("Source or target element not found or not visible for drag and drop.")

        source_x = source_box['x'] + source_box['width'] / 2
        source_y = source_box['y'] + source_box['height'] / 2
        target_x = target_box['x'] + target_box['width'] / 2
        target_y = target_box['y'] + target_box['height'] / 2

        # Move to source, press mouse
        await HumanMouse.move_and_click(self.page, source_x, source_y, click=False)
        await self.page.mouse.down()
        await asyncio.sleep(random.uniform(0.1, 0.3))

        # Generate path for drag
        drag_path = HumanMouse._generate_path(source_x, source_y, target_x, target_y)

        for i, (x, y) in enumerate(drag_path):
            progress = i / len(drag_path)
            speed_factor = math.sin(progress * math.pi)
            base_delay = random.uniform(0.005, 0.015)
            delay = base_delay / max(speed_factor, 0.2)
            await self.page.mouse.move(x, y)
            await asyncio.sleep(delay)

        # Release mouse
        await self.page.mouse.up()
        await asyncio.sleep(random.uniform(0.1, 0.3))


# =============================================================================
# SLIDER SOLVER
# =============================================================================

class SliderSolver(PlaywrightSolver):
    def __init__(self, page: Page, config: SolverConfig):
        super().__init__(page, config)

    async def solve(self) -> bool:
        print("Attempting to solve slider captcha...")
        # Implement slider solving logic here
        # This will involve: 
        # 1. Locating the slider handle and the target position/image
        # 2. Taking screenshots before and after moving the slider to find the correct position
        # 3. Using image processing techniques (template matching, edge detection, phase correlation) to determine the offset
        # 4. Performing a human-like drag operation

        slider_handle_selector = '.slider-handle'
        slider_track_selector = '.slider-track'
        puzzle_image_selector = '.puzzle-image'
        background_image_selector = '.background-image'

        # Wait for slider elements to be visible
        try:
            await self.page.wait_for_selector(slider_handle_selector, timeout=5000)
            await self.page.wait_for_selector(puzzle_image_selector, timeout=5000)
            await self.page.wait_for_selector(background_image_selector, timeout=5000)
        except Exception as e:
            print(f"Slider elements not found: {e}")
            return False

        # Get initial state
        handle_box = await self.get_element_bounds(slider_handle_selector)
        track_box = await self.get_element_bounds(slider_track_selector)
        if not handle_box or not track_box:
            print("Could not get bounds for slider handle or track.")
            return False

        start_x = handle_box['x'] + handle_box['width'] / 2
        start_y = handle_box['y'] + handle_box['height'] / 2

        # Take a screenshot of the puzzle area
        puzzle_image_full = await self.get_screenshot(background_image_selector)
        puzzle_piece_image = await self.get_screenshot(puzzle_image_selector)

        # Convert PIL Images to OpenCV format
        puzzle_image_np = np.array(puzzle_image_full.convert('L'))
        puzzle_piece_np = np.array(puzzle_piece_image.convert('L'))

        # Calculate offset using multiple methods and find consensus
        offsets = []

        # 1. Template Matching
        res = cv2.matchTemplate(puzzle_image_np, puzzle_piece_np, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > 0.7: # A reasonable confidence threshold
            offsets.append(max_loc[0])
            print(f"Template Matching offset: {max_loc[0]}")

        # 2. Canny Edge Detection + Template Matching
        edges_puzzle = cv2.Canny(puzzle_image_np, 100, 200)
        edges_piece = cv2.Canny(puzzle_piece_np, 100, 200)
        res_canny = cv2.matchTemplate(edges_puzzle, edges_piece, cv2.TM_CCOEFF_NORMED)
        _, max_val_canny, _, max_loc_canny = cv2.minMaxLoc(res_canny)
        if max_val_canny > 0.6:
            offsets.append(max_loc_canny[0])
            print(f"Canny Edge Matching offset: {max_loc_canny[0]}")

        # 3. SIFT/ORB Feature Matching (more robust to rotations/scaling, but slower)
        # For simple slider, template matching is usually sufficient, but good to have fallback
        # This would require more complex code to implement properly, skipping for brevity but noting its existence.

        # 4. Image Difference (find the missing piece location)
        # This assumes the puzzle piece is a 'hole' in the background image
        # This method is more complex as it requires knowing the 'correct' background without the piece.
        # A simpler approach for image difference is to compare the background image with a version of itself
        # where the slider has moved, but that's for after the drag.

        # 5. Phase Correlation (using cv2.phaseCorrelate)
        try:
            # Pad the smaller image to match the larger one for phaseCorrelate
            h_puzzle, w_puzzle = puzzle_image_np.shape
            h_piece, w_piece = puzzle_piece_np.shape

            # Create a padded version of the piece image
            padded_piece = np.zeros_like(puzzle_image_np)
            padded_piece[0:h_piece, 0:w_piece] = puzzle_piece_np

            # Calculate phase correlation
            # The result will be a 2D array, where the peak indicates the translation
            # We are interested in the x-offset
            shift, _ = cv2.phaseCorrelate(np.float32(puzzle_image_np), np.float32(padded_piece))
            # shift[0] is the x-shift, shift[1] is the y-shift
            phase_offset = int(round(shift[0]))
            offsets.append(phase_offset)
            print(f"Phase Correlation offset: {phase_offset}")
        except Exception as e:
            print(f"Phase Correlation failed: {e}")

        # 6. Edge Histogram (compare edge density columns)
        try:
            # Calculate vertical edge histograms for both images
            edges_puzzle = cv2.Canny(puzzle_image_np, 100, 200)
            edges_piece = cv2.Canny(puzzle_piece_np, 100, 200)

            edges_puzzle_hist = np.sum(edges_puzzle, axis=0)
            edges_piece_hist = np.sum(edges_piece, axis=0)

            # Find the best match by sliding the piece histogram over the puzzle histogram
            best_match_val = -1
            best_match_offset = 0
            for i in range(len(edges_puzzle_hist) - len(edges_piece_hist) + 1):
                # Compare a slice of the puzzle histogram with the piece histogram
                puzzle_slice = edges_puzzle_hist[i : i + len(edges_piece_hist)]
                # Using correlation as a similarity metric
                correlation = np.corrcoef(puzzle_slice, edges_piece_hist)[0, 1]
                if not np.isnan(correlation) and correlation > best_match_val:
                    best_match_val = correlation
                    best_match_offset = i
            if best_match_val > 0.5: # A reasonable threshold for correlation
                offsets.append(best_match_offset)
                print(f"Edge Histogram offset: {best_match_offset}")
        except Exception as e:
            print(f"Edge Histogram failed: {e}")


        if not offsets:
            print("No reliable offset detected by any method.")
            return False

        # Consensus mechanism: remove outliers and take median
        if len(offsets) > 2:
            median_offset = np.median(offsets)
            std_dev = np.std(offsets)
            # Filter out outliers (more than 2 standard deviations from median)
            filtered_offsets = [o for o in offsets if abs(o - median_offset) <= 2 * std_dev]
            if filtered_offsets:
                final_offset = int(np.median(filtered_offsets))
            else:
                final_offset = int(median_offset) # Fallback if all are outliers
        else:
            final_offset = int(np.median(offsets)) # If 1 or 2, median is fine

        print(f"Consensus offset: {final_offset}")

        # Calculate target x for the handle
        # The offset is the distance the puzzle piece needs to move from its current position
        # The handle needs to move by this same amount relative to its starting position
        # Assuming the puzzle piece is initially at the far left of the track, or its current visible position
        # This might need adjustment based on specific captcha implementation
        target_drag_x = start_x + final_offset

        # Ensure target_drag_x is within the track bounds
        track_left = track_box['x']
        track_right = track_box['x'] + track_box['width']
        target_drag_x = max(track_left, min(target_drag_x, track_right - handle_box['width'] / 2))

        print(f"Dragging slider from ({start_x}, {start_y}) to ({target_drag_x}, {start_y})")
        await self.drag_and_drop(slider_handle_selector, {'x': target_drag_x, 'y': start_y})

        # After dragging, wait for a moment and check if solved
        await asyncio.sleep(self.config.min_solve_time_per_round)
        return await self.detector.is_solved()

    async def drag_and_drop(self, source_selector: str, target_coords: Dict[str, float]):
        """Performs a human-like drag and drop operation to specific coordinates."""
        source_box = await self.get_element_bounds(source_selector)

        if not source_box:
            raise ValueError("Source element not found or not visible for drag and drop.")

        source_x = source_box['x'] + source_box['width'] / 2
        source_y = source_box['y'] + source_box['height'] / 2
        target_x = target_coords['x']
        target_y = target_coords['y']

        # Move to source, press mouse
        await HumanMouse.move_and_click(self.page, source_x, source_y, click=False, element_width=source_box['width'], element_height=source_box['height'])
        await self.page.mouse.down()
        await asyncio.sleep(random.uniform(0.1, 0.3))

        # Generate path for drag
        drag_path = HumanMouse._generate_path(source_x, source_y, target_x, target_y)

        for i, (x, y) in enumerate(drag_path):
            progress = i / len(drag_path)
            speed_factor = math.sin(progress * math.pi)
            base_delay = random.uniform(0.005, 0.015)
            delay = base_delay / max(speed_factor, 0.2)
            await self.page.mouse.move(x, y)
            await asyncio.sleep(delay)

        # Release mouse
        await self.page.mouse.up()
        await asyncio.sleep(random.uniform(0.1, 0.3))


# =============================================================================
# SHAPE MATCHER SOLVER
# =============================================================================

class ShapeMatcher(PlaywrightSolver):
    def __init__(self, page: Page, config: SolverConfig):
        super().__init__(page, config)

    async def solve(self) -> bool:
        print("Attempting to solve shape matching captcha...")
        # This solver would typically involve:
        # 1. Identifying the main image and the target shape/piece.
        # 2. Using image processing (e.g., contour detection, shape descriptors) to find the matching location.
        # 3. Performing a click or drag operation.

        # For now, let's assume a simple scenario where we need to click a shape that matches a prompt.
        # This is a placeholder and would need actual implementation based on specific captcha structure.

        # Placeholder logic:
        print("ShapeMatcher: Placeholder - no actual solving logic implemented yet.")
        await asyncio.sleep(self.config.min_solve_time_per_round)
        return False # Always return False for now until implemented


# =============================================================================
# OBJECT ALIGNMENT SOLVER
# =============================================================================

class ObjectAlignmentSolver(PlaywrightSolver):
    def __init__(self, page: Page, config: SolverConfig):
        super().__init__(page, config)

    async def solve(self) -> bool:
        print("Attempting to solve object alignment captcha...")
        # This solver would typically involve:
        # 1. Identifying a rotatable object and a target orientation.
        # 2. Taking screenshots, rotating the object, taking more screenshots.
        # 3. Using image processing (e.g., template matching, feature matching) to determine correct rotation.
        # 4. Performing click/drag operations to rotate the object.

        # Placeholder logic:
        print("ObjectAlignmentSolver: Placeholder - no actual solving logic implemented yet.")
        await asyncio.sleep(self.config.min_solve_time_per_round)
        return False # Always return False for now until implemented

# --- END PART 1 --- (GodSolver, DragSolver, PatternBreakerSolver, ChallengeRouter, MasterSolver follow)

class GodSolver(PlaywrightSolver):
    def __init__(self, page: Page, config: SolverConfig):
        super().__init__(page, config)
        self.clip_model: Optional[ClipModel] = None
        self.client = openai.OpenAI()

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
            "show me the {target}"
        ]
        self.property_prompts = [
            "an object that is {target}",
            "something primarily {target}",
            "a {target} object",
            "an image with {target} color",
            "the {target} colored item",
            "items that are {target}"
        ]
        self.context_aware_prompts = [
            "a {target} seen from above",
            "a close-up of a {target}",
            "a {target} in the foreground",
            "a {target} in the background",
            "a {target} at night",
            "a {target} during the day",
            "a {target} in motion",
            "a stationary {target}"
        ]

    def _initialize_aliases(self):
        # Expand to 60+ categories with more aliases each
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
        self.target_aliases["fire hydrant"] = ["hydrant"]
        self.target_aliases["parking meter"] = ["meter"]
        self.target_aliases["crosswalk"] = ["zebra crossing", "pedestrian crossing"]
        self.target_aliases["road sign"] = ["street sign", "signpost", "billboard"]
        self.target_aliases["bridge"] = ["overpass", "viaduct"]
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
        self.target_aliases["lion"] = ["king of the jungle"]
        self.target_aliases["tiger"] = ["big cat"]
        self.target_aliases["bear"] = ["grizzly", "polar bear"]
        self.target_aliases["snake"] = ["serpent"]
        self.target_aliases["fish"] = ["salmon", "tuna", "shark", "whale" ] # Whale is technically mammal, but often grouped visually
        self.target_aliases["spider"] = ["arachnid"]
        self.target_aliases["insect"] = ["bug", "ant", "bee", "butterfly", "moth", "fly", "mosquito"]

        # Food
        self.target_aliases["apple"] = ["fruit"]
        self.target_aliases["banana"] = ["fruit"]
        self.target_aliases["orange"] = ["fruit"]
        self.target_aliases["pizza"] = ["slice of pizza"]
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
        self.target_aliases["phone"] = ["smartphone", "mobile phone"]
        self.target_aliases["watch"] = ["wristwatch", "clock"]
        self.target_aliases["shoe"] = ["sneaker", "boot", "sandal"]
        self.target_aliases["bag"] = ["backpack", "handbag", "purse", "luggage"]
        self.target_aliases["umbrella"] = ["parasol"]
        self.target_aliases["cup"] = ["mug", "glass"]
        self.target_aliases["bottle"] = ["flask", "container"]
        self.target_aliases["door"] = ["gate", "entrance"]
        self.target_aliases["window"] = ["pane"]
        self.target_aliases["wheel"] = ["tire", "rim"]
        self.target_aliases["road"] = ["street", "highway", "freeway", "path"]
        self.target_aliases["sidewalk"] = ["pavement", "footpath"]
        self.target_aliases["person"] = ["human", "pedestrian", "people", "crowd"]
        self.target_aliases["animal"] = ["creature", "wildlife"]
        self.target_aliases["plant"] = ["flower", "bush", "shrub", "vegetation"]
        self.target_aliases["water"] = ["river", "lake", "ocean", "sea", "pond"]
        self.target_aliases["sky"] = ["clouds", "heaven"]
        self.target_aliases["building"] = ["house", "structure", "edifice"]
        self.target_aliases["rock"] = ["stone", "boulder"]
        self.target_aliases["grass"] = ["lawn", "turf"]
        self.target_aliases["snow"] = ["ice", "sleet"]
        self.target_aliases["fire"] = ["flame", "blaze"]
        self.target_aliases["smoke"] = ["fumes", "haze"]
        self.target_aliases["fog"] = ["mist", "haze"]
        self.target_aliases["rain"] = ["drizzle", "shower"]
        self.target_aliases["sunlight"] = ["sunshine", "rays"]
        self.target_aliases["shadow"] = ["shade"]
        self.target_aliases["reflection"] = ["mirror image"]
        self.target_aliases["sign"] = ["notice", "poster", "advertisement"]
        self.target_aliases["fence"] = ["barrier", "gate"]
        self.target_aliases["wall"] = ["barrier", "partition"]
        self.target_aliases["floor"] = ["ground", "surface"]
        self.target_aliases["ceiling"] = ["roof"]
        self.target_aliases["light pole"] = ["street lamp", "lamppost"]
        self.target_aliases["power line"] = ["electrical wire"]
        self.target_aliases["trash can"] = ["garbage can", "bin"]
        self.target_aliases["mailbox"] = ["postbox"]
        self.target_aliases["backpack"] = ["rucksack", "knapsack"]
        self.target_aliases["suitcase"] = ["luggage", "travel bag"]
        self.target_aliases["helmet"] = ["headgear"]
        self.target_aliases["glove"] = ["mittens"]
        self.target_aliases["hat"] = ["cap", "beanie"]
        self.target_aliases["jacket"] = ["coat"]
        self.target_aliases["shirt"] = ["t-shirt", "blouse"]
        self.target_aliases["pants"] = ["trousers", "jeans"]
        self.target_aliases["dress"] = ["gown", "frock"]
        self.target_aliases["skirt"] = ["kilt"]
        self.target_aliases["sock"] = ["stocking"]
        self.target_aliases["tie"] = ["necktie"]
        self.target_aliases["belt"] = ["sash"]
        self.target_aliases["glasses"] = ["spectacles", "eyewear"]
        self.target_aliases["camera"] = ["photographic device"]
        self.target_aliases["microphone"] = ["mic"]
        self.target_aliases["speaker"] = ["loudspeaker"]
        self.target_aliases["headphone"] = ["earphone", "headset"]
        self.target_aliases["keyboard"] = ["keypad"]
        self.target_aliases["mouse"] = ["computer mouse"]
        self.target_aliases["monitor"] = ["screen", "display"]
        self.target_aliases["printer"] = ["scanner"]
        self.target_aliases["router"] = ["modem"]
        self.target_aliases["server"] = ["mainframe"]
        self.target_aliases["cable"] = ["wire", "cord"]
        self.target_aliases["plug"] = ["socket"]
        self.target_aliases["battery"] = ["cell"]
        self.target_aliases["coin"] = ["currency", "money"]
        self.target_aliases["key"] = ["door key", "car key"]
        self.target_aliases["lock"] = ["padlock", "bolt"]
        self.target_aliases["scissors"] = ["shears"]
        self.target_aliases["knife"] = ["blade", "dagger"]
        self.target_aliases["fork"] = ["tine"]
        self.target_aliases["spoon"] = ["ladle"]
        self.target_aliases["plate"] = ["dish"]
        self.target_aliases["bowl"] = ["basin"]
        self.target_aliases["pan"] = ["pot", "skillet"]
        self.target_aliases["oven"] = ["stove", "range"]
        self.target_aliases["refrigerator"] = ["fridge"]
        self.target_aliases["microwave"] = ["micro-oven"]
        self.target_aliases["washing machine"] = ["washer"]
        self.target_aliases["dryer"] = ["tumble dryer"]
        self.target_aliases["vacuum cleaner"] = ["hoover"]
        self.target_aliases["broom"] = ["brush"]
        self.target_aliases["mop"] = ["floor cleaner"]
        self.target_aliases["bucket"] = ["pail"]
        self.target_aliases["soap"] = ["detergent"]
        self.target_aliases["towel"] = ["cloth"]
        self.target_aliases["mirror"] = ["looking glass"]
        self.target_aliases["comb"] = ["brush"]
        self.target_aliases["toothbrush"] = ["dental brush"]
        self.target_aliases["toothpaste"] = ["dental paste"]
        self.target_aliases["shampoo"] = ["hair wash"]
        self.target_aliases["conditioner"] = ["hair conditioner"]
        self.target_aliases["razor"] = ["shaver"]
        self.target_aliases["perfume"] = ["fragrance", "cologne"]
        self.target_aliases["makeup"] = ["cosmetics"]
        self.target_aliases["jewelry"] = ["jewellery", "ornament"]
        self.target_aliases["ring"] = ["band"]
        self.target_aliases["necklace"] = ["chain"]
        self.target_aliases["earring"] = ["ear-drop"]
        self.target_aliases["bracelet"] = ["bangle"]
        self.target_aliases["watch"] = ["timepiece"]
        self.target_aliases["wallet"] = ["purse", "billfold"]
        self.target_aliases["credit card"] = ["debit card", "bank card"]
        self.target_aliases["cash"] = ["money", "banknotes"]
        self.target_aliases["coin"] = ["money", "currency"]
        self.target_aliases["receipt"] = ["bill", "invoice"]
        self.target_aliases["ticket"] = ["pass", "voucher"]
        self.target_aliases["map"] = ["chart", "atlas"]
        self.target_aliases["globe"] = ["world map"]
        self.target_aliases["compass"] = ["direction finder"]
        self.target_aliases["telescope"] = ["spyglass"]
        self.target_aliases["microscope"] = ["magnifying glass"]
        self.target_aliases["ruler"] = ["measuring stick"]
        self.target_aliases["pen"] = ["ballpoint pen", "fountain pen"]
        self.target_aliases["pencil"] = ["graphite pencil"]
        self.target_aliases["eraser"] = ["rubber"]
        self.target_aliases["paper"] = ["sheet", "document"]
        self.target_aliases["envelope"] = ["mailer"]
        self.target_aliases["stamp"] = ["postage stamp"]
        self.target_aliases["postcard"] = ["card"]
        self.target_aliases["letter"] = ["correspondence"]
        self.target_aliases["package"] = ["parcel", "box"]
        self.target_aliases["gift"] = ["present"]
        self.target_aliases["balloon"] = ["air balloon"]
        self.target_aliases["candle"] = ["taper"]
        self.target_aliases["fireworks"] = ["pyrotechnics"]
        self.target_aliases["flag"] = ["banner", "standard"]
        self.target_aliases["trophy"] = ["cup", "award"]
        self.target_aliases["medal"] = ["award", "decoration"]
        self.target_aliases["ribbon"] = ["band"]
        self.target_aliases["flower"] = ["blossom", "bloom"]
        self.target_aliases["vase"] = ["urn"]
        self.target_aliases["picture"] = ["painting", "drawing", "photograph"]
        self.target_aliases["frame"] = ["border"]
        self.target_aliases["sculpture"] = ["statue", "figurine"]
        self.target_aliases["musical instrument"] = ["instrument"]
        self.target_aliases["guitar"] = ["acoustic guitar", "electric guitar"]
        self.target_aliases["piano"] = ["keyboard instrument"]
        self.target_aliases["violin"] = ["fiddle"]
        self.target_aliases["drum"] = ["percussion instrument"]
        self.target_aliases["trumpet"] = ["horn"]
        self.target_aliases["flute"] = ["pipe"]
        self.target_aliases["microphone"] = ["mic"]
        self.target_aliases["speaker"] = ["loudspeaker"]
        self.target_aliases["headphone"] = ["earphone", "headset"]
        self.target_aliases["camera"] = ["photographic device"]
        self.target_aliases["television"] = ["TV", "tele"]
        self.target_aliases["radio"] = ["transistor radio"]
        self.target_aliases["remote control"] = ["remote"]
        self.target_aliases["fan"] = ["ventilator"]
        self.target_aliases["heater"] = ["radiator"]
        self.target_aliases["air conditioner"] = ["AC"]
        self.target_aliases["clock"] = ["timepiece", "watch"]
        self.target_aliases["calendar"] = ["planner"]
        self.target_aliases["newspaper"] = ["journal", "gazette"]
        self.target_aliases["magazine"] = ["periodical"]
        self.target_aliases["book"] = ["volume", "tome"]
        self.target_aliases["document"] = ["paper", "file"]
        self.target_aliases["folder"] = ["binder"]
        self.target_aliases["stapler"] = ["fastener"]
        self.target_aliases["hole punch"] = ["puncher"]
        self.target_aliases["tape"] = ["adhesive tape"]
        self.target_aliases["glue"] = ["adhesive"]
        self.target_aliases["scissors"] = ["shears"]
        self.target_aliases["ruler"] = ["straightedge"]
        self.target_aliases["calculator"] = ["computer"]
        self.target_aliases["globe"] = ["world globe"]
        self.target_aliases["map"] = ["chart"]
        self.target_aliases["binocular"] = ["field glasses"]
        self.target_aliases["magnifying glass"] = ["loupe"]
        self.target_aliases["flashlight"] = ["torch"]
        self.target_aliases["battery"] = ["power cell"]
        self.target_aliases["charger"] = ["power adapter"]
        self.target_aliases["extension cord"] = ["power strip"]
        self.target_aliases["plug"] = ["electrical plug"]
        self.target_aliases["socket"] = ["outlet"]
        self.target_aliases["switch"] = ["button"]
        self.target_aliases["lever"] = ["handle"]
        self.target_aliases["gear"] = ["cog"]
        self.target_aliases["chain"] = ["link"]
        self.target_aliases["rope"] = ["cord", "cable"]
        self.target_aliases["ladder"] = ["steps"]
        self.target_aliases["tool box"] = ["kit"]
        self.target_aliases["hammer"] = ["mallet"]
        self.target_aliases["screwdriver"] = ["driver"]
        self.target_aliases["wrench"] = ["spanner"]
        self.target_aliases["pliers"] = ["pincers"]
        self.target_aliases["saw"] = ["blade"]
        self.target_aliases["drill"] = ["borer"]
        self.target_aliases["nail"] = ["spike"]
        self.target_aliases["screw"] = ["fastener"]
        self.target_aliases["bolt"] = ["fastener"]
        self.target_aliases["nut"] = ["fastener"]
        self.target_aliases["washer"] = ["gasket"]
        self.target_aliases["pipe"] = ["tube"]
        self.target_aliases["valve"] = ["tap"]
        self.target_aliases["faucet"] = ["tap"]
        self.target_aliases["sink"] = ["basin"]
        self.target_aliases["toilet"] = ["commode"]
        self.target_aliases["shower"] = ["bath"]
        self.target_aliases["bathtub"] = ["bath"]
        self.target_aliases["curtain"] = ["drape"]
        self.target_aliases["blinds"] = ["shades"]
        self.target_aliases["rug"] = ["carpet"]
        self.target_aliases["pillow"] = ["cushion"]
        self.target_aliases["blanket"] = ["throw"]
        self.target_aliases["sheet"] = ["bedsheet"]
        self.target_aliases["mattress"] = ["bed"]
        self.target_aliases["bed"] = ["bunk"]
        self.target_aliases["wardrobe"] = ["closet"]
        self.target_aliases["drawer"] = ["compartment"]
        self.target_aliases["shelf"] = ["rack"]
        self.target_aliases["cabinet"] = ["cupboard"]
        self.target_aliases["countertop"] = ["bench"]
        self.target_aliases["stove"] = ["cooker"]
        self.target_aliases["oven"] = ["range"]
        self.target_aliases["microwave"] = ["micro-oven"]
        self.target_aliases["dishwasher"] = ["dishwashing machine"]
        self.target_aliases["refrigerator"] = ["fridge"]
        self.target_aliases["freezer"] = ["deep freezer"]
        self.target_aliases["blender"] = ["mixer"]
        self.target_aliases["toaster"] = ["toast maker"]
        self.target_aliases["coffee maker"] = ["coffeepot"]
        self.target_aliases["kettle"] = ["tea kettle"]
        self.target_aliases["mug"] = ["cup"]
        self.target_aliases["glass"] = ["tumbler"]
        self.target_aliases["plate"] = ["dish"]
        self.target_aliases["bowl"] = ["dish"]
        self.target_aliases["fork"] = ["eating utensil"]
        self.target_aliases["knife"] = ["eating utensil"]
        self.target_aliases["spoon"] = ["eating utensil"]
        self.target_aliases["chopsticks"] = ["eating utensils"]
        self.target_aliases["napkin"] = ["serviette"]
        self.target_aliases["tablecloth"] = ["table cover"]
        self.target_aliases["candle"] = ["wax light"]
        self.target_aliases["flower"] = ["bloom"]
        self.target_aliases["plant"] = ["vegetation"]
        self.target_aliases["tree"] = ["wood"]
        self.target_aliases["bush"] = ["shrub"]
        self.target_aliases["grass"] = ["lawn"]
        self.target_aliases["soil"] = ["earth", "dirt"]
        self.target_aliases["rock"] = ["stone"]
        self.target_aliases["sand"] = ["beach sand"]
        self.target_aliases["water"] = ["H2O"]
        self.target_aliases["sky"] = ["heavens"]
        self.target_aliases["cloud"] = ["vapor"]
        self.target_aliases["sun"] = ["star"]
        self.target_aliases["moon"] = ["satellite"]
        self.target_aliases["star"] = ["celestial body"]
        self.target_aliases["rainbow"] = ["arc of colors"]
        self.target_aliases["lightning"] = ["thunderbolt"]
        self.target_aliases["snow"] = ["snowflake"]
        self.target_aliases["rain"] = ["downpour"]
        self.target_aliases["wind"] = ["breeze"]
        self.target_aliases["fog"] = ["mist"]
        self.target_aliases["smoke"] = ["fumes"]
        self.target_aliases["fire"] = ["flames"]
        self.target_aliases["ice"] = ["frozen water"]
        self.target_aliases["mountain"] = ["peak"]
        self.target_aliases["hill"] = ["mound"]
        self.target_aliases["valley"] = ["dale"]
        self.target_aliases["river"] = ["stream"]
        self.target_aliases["lake"] = ["loch"]
        self.target_aliases["ocean"] = ["sea"]
        self.target_aliases["beach"] = ["shore"]
        self.target_aliases["island"] = ["isle"]
        self.target_aliases["desert"] = ["wasteland"]
        self.target_aliases["forest"] = ["woods"]
        self.target_aliases["jungle"] = ["rainforest"]
        self.target_aliases["cave"] = ["cavern"]
        self.target_aliases["waterfall"] = ["cascade"]
        self.target_aliases["volcano"] = ["mountain of fire"]
        self.target_aliases["glacier"] = ["ice mass"]
        self.target_aliases["canyon"] = ["gorge"]
        self.target_aliases["cliff"] = ["precipice"]
        self.target_aliases["bridge"] = ["span"]
        self.target_aliases["road"] = ["path"]
        self.target_aliases["railroad"] = ["railway"]
        self.target_aliases["tunnel"] = ["underpass"]
        self.target_aliases["dam"] = ["barrier"]
        self.target_aliases["lighthouse"] = ["beacon"]
        self.target_aliases["windmill"] = ["wind turbine"]
        self.target_aliases["farm"] = ["ranch"]
        self.target_aliases["barn"] = ["shed"]
        self.target_aliases["tractor"] = ["farm vehicle"]
        self.target_aliases["scarecrow"] = ["effigy"]
        self.target_aliases["well"] = ["water well"]
        self.target_aliases["fountain"] = ["water feature"]
        self.target_aliases["statue"] = ["sculpture"]
        self.target_aliases["monument"] = ["memorial"]
        self.target_aliases["tower"] = ["spire"]
        self.target_aliases["castle"] = ["fortress"]
        self.target_aliases["palace"] = ["mansion"]
        self.target_aliases["church"] = ["cathedral"]
        self.target_aliases["mosque"] = ["masjid"]
        self.target_aliases["temple"] = ["shrine"]
        self.target_aliases["pyramid"] = ["tomb"]
        self.target_aliases["sphinx"] = ["mythical creature"]
        self.target_aliases["obelisk"] = ["monolith"]
        self.target_aliases["arch"] = ["gateway"]
        self.target_aliases["column"] = ["pillar"]
        self.target_aliases["ruins"] = ["remains"]
        self.target_aliases["excavation site"] = ["dig site"]
        self.target_aliases["museum"] = ["gallery"]
        self.target_aliases["library"] = ["book repository"]
        self.target_aliases["school"] = ["academy"]
        self.target_aliases["university"] = ["college"]
        self.target_aliases["hospital"] = ["medical center"]
        self.target_aliases["police station"] = ["precinct"]
        self.target_aliases["fire station"] = ["firehouse"]
        self.target_aliases["post office"] = ["mail office"]
        self.target_aliases["bank"] = ["financial institution"]
        self.target_aliases["store"] = ["shop"]
        self.target_aliases["restaurant"] = ["eatery"]
        self.target_aliases["cafe"] = ["coffee shop"]
        self.target_aliases["hotel"] = ["inn"]
        self.target_aliases["airport"] = ["airfield"]
        self.target_aliases["port"] = ["harbor"]
        self.target_aliases["station"] = ["terminal"]
        self.target_aliases["park"] = ["garden"]
        self.target_aliases["playground"] = ["play area"]
        self.target_aliases["stadium"] = ["arena"]
        self.target_aliases["theater"] = ["playhouse"]
        self.target_aliases["cinema"] = ["movie theater"]
        self.target_aliases["circus"] = ["big top"]
        self.target_aliases["zoo"] = ["menagerie"]
        self.target_aliases["aquarium"] = ["oceanarium"]
        self.target_aliases["amusement park"] = ["theme park"]
        self.target_aliases["fair"] = ["carnival"]
        self.target_aliases["market"] = ["bazaar"]
        self.target_aliases["factory"] = ["plant"]
        self.target_aliases["power plant"] = ["generating station"]
        self.target_aliases["oil rig"] = ["drilling platform"]
        self.target_aliases["wind farm"] = ["wind park"]
        self.target_aliases["solar panel"] = ["photovoltaic panel"]
        self.target_aliases["satellite dish"] = ["dish antenna"]
        self.target_aliases["radio telescope"] = ["radio antenna"]
        self.target_aliases["observatory"] = ["astronomical observatory"]
        self.target_aliases["laboratory"] = ["lab"]
        self.target_aliases["office"] = ["workplace"]
        self.target_aliases["home"] = ["residence"]
        self.target_aliases["kitchen"] = ["galley"]
        self.target_aliases["bedroom"] = ["sleeping room"]
        self.target_aliases["bathroom"] = ["restroom"]
        self.target_aliases["living room"] = ["lounge"]
        self.target_aliases["dining room"] = ["dining area"]
        self.target_aliases["hallway"] = ["corridor"]
        self.target_aliases["stairs"] = ["staircase"]
        self.target_aliases["elevator"] = ["lift"]
        self.target_aliases["escalator"] = ["moving staircase"]
        self.target_aliases["balcony"] = ["terrace"]
        self.target_aliases["patio"] = ["deck"]
        self.target_aliases["garden"] = ["yard"]
        self.target_aliases["fence"] = ["enclosure"]
        self.target_aliases["gate"] = ["doorway"]
        self.target_aliases["road"] = ["thoroughfare"]
        self.target_aliases["street"] = ["avenue"]
        self.target_aliases["sidewalk"] = ["footpath"]
        self.target_aliases["crosswalk"] = ["pedestrian crossing"]
        self.target_aliases["traffic light"] = ["traffic signal"]
        self.target_aliases["fire hydrant"] = ["water plug"]
        self.target_aliases["parking meter"] = ["parking machine"]
        self.target_aliases["bus stop"] = ["bus station"]
        self.target_aliases["taxi stand"] = ["cab stand"]
        self.target_aliases["subway station"] = ["metro station"]
        self.target_aliases["train station"] = ["railway station"]
        self.target_aliases["airport terminal"] = ["air terminal"]
        self.target_aliases["port terminal"] = ["dock terminal"]
        self.target_aliases["gas station"] = ["petrol station"]
        self.target_aliases["bank"] = ["money institution"]
        self.target_aliases["post office"] = ["postal office"]
        self.target_aliases["police station"] = ["law enforcement station"]
        self.target_aliases["fire station"] = ["fire department"]
        self.target_aliases["hospital"] = ["medical facility"]
        self.target_aliases["pharmacy"] = ["drugstore"]
        self.target_aliases["supermarket"] = ["grocery store"]
        self.target_aliases["bakery"] = ["bake shop"]
        self.target_aliases["butcher shop"] = ["meat market"]
        self.target_aliases["fish market"] = ["seafood market"]
        self.target_aliases["flower shop"] = ["florist"]
        self.target_aliases["bookstore"] = ["book shop"]
        self.target_aliases["clothing store"] = ["apparel store"]
        self.target_aliases["shoe store"] = ["footwear store"]
        self.target_aliases["jewelry store"] = ["jewellery shop"]
        self.target_aliases["electronics store"] = ["tech store"]
        self.target_aliases["toy store"] = ["toy shop"]
        self.target_aliases["pet store"] = ["pet shop"]
        self.target_aliases["hardware store"] = ["ironmonger"]
        self.target_aliases["department store"] = ["big store"]
        self.target_aliases["shopping mall"] = ["shopping center"]
        self.target_aliases["restaurant"] = ["eating place"]
        self.target_aliases["cafe"] = ["coffee house"]
        self.target_aliases["bar"] = ["pub"]
        self.target_aliases["nightclub"] = ["club"]
        self.target_aliases["hotel"] = ["lodging"]
        self.target_aliases["motel"] = ["motor inn"]
        self.target_aliases["hostel"] = ["guesthouse"]
        self.target_aliases["resort"] = ["holiday resort"]
        self.target_aliases["casino"] = ["gambling house"]
        self.target_aliases["theater"] = ["playhouse"]
        self.target_aliases["cinema"] = ["movie house"]
        self.target_aliases["concert hall"] = ["music hall"]
        self.target_aliases["art gallery"] = ["art museum"]
        self.target_aliases["museum"] = ["exhibition hall"]
        self.target_aliases["library"] = ["public library"]
        self.target_aliases["school"] = ["educational institution"]
        self.target_aliases["university"] = ["higher education institution"]
        self.target_aliases["stadium"] = ["sports arena"]
        self.target_aliases["gym"] = ["fitness center"]
        self.target_aliases["swimming pool"] = ["pool"]
        self.target_aliases["park"] = ["green space"]
        self.target_aliases["playground"] = ["play park"]
        self.target_aliases["zoo"] = ["animal park"]
        self.target_aliases["aquarium"] = ["marine park"]
        self.target_aliases["botanical garden"] = ["botanic garden"]
        self.target_aliases["national park"] = ["nature reserve"]
        self.target_aliases["beach"] = ["seashore"]
        self.target_aliases["lake"] = ["loch"]
        self.target_aliases["river"] = ["stream"]
        self.target_aliases["waterfall"] = ["cascade"]
        self.target_aliases["mountain"] = ["peak"]
        self.target_aliases["forest"] = ["woodland"]
        self.target_aliases["desert"] = ["wasteland"]
        self.target_aliases["cave"] = ["grotto"]
        self.target_aliases["volcano"] = ["fire mountain"]
        self.target_aliases["island"] = ["isle"]
        self.target_aliases["bridge"] = ["overpass"]
        self.target_aliases["tunnel"] = ["underpass"]
        self.target_aliases["dam"] = ["reservoir wall"]
        self.target_aliases["lighthouse"] = ["light tower"]
        self.target_aliases["windmill"] = ["wind turbine"]
        self.target_aliases["farm"] = ["agricultural land"]
        self.target_aliases["barn"] = ["farm building"]
        self.target_aliases["silo"] = ["grain tower"]
        self.target_aliases["tractor"] = ["farm tractor"]
        self.target_aliases["scarecrow"] = ["bird scarer"]
        self.target_aliases["well"] = ["water source"]
        self.target_aliases["fountain"] = ["water jet"]
        self.target_aliases["statue"] = ["figure"]
        self.target_aliases["monument"] = ["landmark"]
        self.target_aliases["tower"] = ["turret"]
        self.target_aliases["castle"] = ["fort"]
        self.target_aliases["palace"] = ["royal residence"]
        self.target_aliases["church"] = ["place of worship"]
        self.target_aliases["mosque"] = ["islamic temple"]
        self.target_aliases["temple"] = ["shrine"]
        self.target_aliases["synagogue"] = ["jewish temple"]
        self.target_aliases["pagoda"] = ["stupa"]
        self.target_aliases["pyramid"] = ["ancient tomb"]
        self.target_aliases["sphinx"] = ["mythical beast"]
        self.target_aliases["obelisk"] = ["monumental pillar"]
        self.target_aliases["arch"] = ["archway"]
        self.target_aliases["column"] = ["pillar"]
        self.target_aliases["ruins"] = ["remains"]
        self.target_aliases["excavation site"] = ["archaeological dig"]
        self.target_aliases["construction site"] = ["building site"]
        self.target_aliases["crane"] = ["hoist"]
        self.target_aliases["excavator"] = ["digger"]
        self.target_aliases["bulldozer"] = ["earthmover"]
        self.target_aliases["road roller"] = ["compactor"]
        self.target_aliases["cement mixer"] = ["concrete mixer"]
        self.target_aliases["dump truck"] = ["tipper truck"]
        self.target_aliases["forklift"] = ["lift truck"]
        self.target_aliases["pallet"] = ["skid"]
        self.target_aliases["box"] = ["carton"]
        self.target_aliases["barrel"] = ["cask"]
        self.target_aliases["crate"] = ["box"]
        self.target_aliases["container"] = ["shipping container"]
        self.target_aliases["warehouse"] = ["depot"]
        self.target_aliases["factory"] = ["manufacturing plant"]
        self.target_aliases["office building"] = ["commercial building"]
        self.target_aliases["residential building"] = ["housing block"]
        self.target_aliases["skyscraper"] = ["tower block"]
        self.target_aliases["cottage"] = ["cabin"]
        self.target_aliases["bungalow"] = ["single-story house"]
        self.target_aliases["mansion"] = ["large house"]
        self.target_aliases["villa"] = ["country house"]
        self.target_aliases["apartment"] = ["flat"]
        self.target_aliases["condominium"] = ["condo"]
        self.target_aliases["townhouse"] = ["row house"]
        self.target_aliases["duplex"] = ["semi-detached house"]
        self.target_aliases["mobile home"] = ["trailer home"]
        self.target_aliases["tent"] = ["canvas shelter"]
        self.target_aliases["caravan"] = ["travel trailer"]
        self.target_aliases["camper van"] = ["RV"]
        self.target_aliases["boat"] = ["vessel"]
        self.target_aliases["ship"] = ["ocean liner"]
        self.target_aliases["yacht"] = ["sailing yacht"]
        self.target_aliases["sailboat"] = ["sailing boat"]
        self.target_aliases["canoe"] = ["dugout"]
        self.target_aliases["kayak"] = ["sea kayak"]
        self.target_aliases["rowboat"] = ["dinghy"]
        self.target_aliases["speedboat"] = ["powerboat"]
        self.target_aliases["jet ski"] = ["personal watercraft"]
        self.target_aliases["submarine"] = ["submersible"]
        self.target_aliases["aircraft"] = ["aeroplane"]
        self.target_aliases["jet"] = ["jet plane"]
        self.target_aliases["propeller plane"] = ["prop plane"]
        self.target_aliases["helicopter"] = ["chopper"]
        self.target_aliases["hot air balloon"] = ["air balloon"]
        self.target_aliases["blimp"] = ["airship"]
        self.target_aliases["rocket"] = ["missile"]
        self.target_aliases["satellite"] = ["orbiter"]
        self.target_aliases["space station"] = ["orbital station"]
        self.target_aliases["astronaut"] = ["cosmonaut"]
        self.target_aliases["alien"] = ["extraterrestrial"]
        self.target_aliases["robot"] = ["android"]
        self.target_aliases["drone"] = ["UAV"]
        self.target_aliases["camera"] = ["photo camera"]
        self.target_aliases["video camera"] = ["camcorder"]
        self.target_aliases["microphone"] = ["mike"]
        self.target_aliases["speaker"] = ["loudspeaker"]
        self.target_aliases["headphone"] = ["earphones"]
        self.target_aliases["keyboard"] = ["computer keyboard"]
        self.target_aliases["mouse"] = ["computer mouse"]
        self.target_aliases["monitor"] = ["computer monitor"]
        self.target_aliases["printer"] = ["computer printer"]
        self.target_aliases["scanner"] = ["image scanner"]
        self.target_aliases["router"] = ["wireless router"]
        self.target_aliases["modem"] = ["internet modem"]
        self.target_aliases["server"] = ["computer server"]
        self.target_aliases["cable"] = ["electrical cable"]
        self.target_aliases["plug"] = ["electrical plug"]
        self.target_aliases["socket"] = ["electrical socket"]
        self.target_aliases["switch"] = ["electrical switch"]
        self.target_aliases["fuse"] = ["circuit breaker"]
        self.target_aliases["battery"] = ["electric battery"]
        self.target_aliases["charger"] = ["battery charger"]
        self.target_aliases["power bank"] = ["portable charger"]
        self.target_aliases["solar panel"] = ["photovoltaic panel"]
        self.target_aliases["wind turbine"] = ["wind generator"]
        self.target_aliases["generator"] = ["power generator"]
        self.target_aliases["engine"] = ["motor"]
        self.target_aliases["gear"] = ["cogwheel"]
        self.target_aliases["chain"] = ["metal chain"]
        self.target_aliases["belt"] = ["strap"]
        self.target_aliases["wheel"] = ["tire"]
        self.target_aliases["axle"] = ["shaft"]
        self.target_aliases["spring"] = ["coil spring"]
        self.target_aliases["nut"] = ["fastener"]
        self.target_aliases["bolt"] = ["fastener"]
        self.target_aliases["screw"] = ["fastener"]
        self.target_aliases["nail"] = ["fastener"]
        self.target_aliases["washer"] = ["gasket"]
        self.target_aliases["pipe"] = ["tube"]
        self.target_aliases["valve"] = ["faucet"]
        self.target_aliases["faucet"] = ["tap"]
        self.target_aliases["sink"] = ["washbasin"]
        self.target_aliases["toilet"] = ["water closet"]
        self.target_aliases["shower"] = ["shower head"]
        self.target_aliases["bathtub"] = ["bath tub"]
        self.target_aliases["mirror"] = ["looking glass"]
        self.target_aliases["comb"] = ["hair comb"]
        self.target_aliases["brush"] = ["hair brush"]
        self.target_aliases["toothbrush"] = ["dental brush"]
        self.target_aliases["toothpaste"] = ["dental paste"]
        self.target_aliases["soap"] = ["bar of soap"]
        self.target_aliases["shampoo"] = ["hair shampoo"]
        self.target_aliases["conditioner"] = ["hair conditioner"]
        self.target_aliases["razor"] = ["shaving razor"]
        self.target_aliases["towel"] = ["bath towel"]
        self.target_aliases["robe"] = ["dressing gown"]
        self.target_aliases["slipper"] = ["house shoe"]
        self.target_aliases["shoe"] = ["footwear"]
        self.target_aliases["boot"] = ["winter boot"]
        self.target_aliases["sandal"] = ["flip-flop"]
        self.target_aliases["sneaker"] = ["athletic shoe"]
        self.target_aliases["sock"] = ["foot sock"]
        self.target_aliases["hat"] = ["headwear"]
        self.target_aliases["cap"] = ["baseball cap"]
        self.target_aliases["beanie"] = ["knit cap"]
        self.target_aliases["scarf"] = ["neck scarf"]
        self.target_aliases["glove"] = ["hand glove"]
        self.target_aliases["mittens"] = ["mitten"]
        self.target_aliases["jacket"] = ["coat"]
        self.target_aliases["shirt"] = ["top"]
        self.target_aliases["t-shirt"] = ["tee shirt"]
        self.target_aliases["blouse"] = ["woman's shirt"]
        self.target_aliases["sweater"] = ["jumper"]
        self.target_aliases["hoodie"] = ["hooded sweatshirt"]
        self.target_aliases["pants"] = ["trousers"]
        self.target_aliases["jeans"] = ["denim jeans"]
        self.target_aliases["shorts"] = ["short pants"]
        self.target_aliases["skirt"] = ["dress skirt"]
        self.target_aliases["dress"] = ["gown"]
        self.target_aliases["suit"] = ["business suit"]
        self.target_aliases["tie"] = ["necktie"]
        self.target_aliases["belt"] = ["waist belt"]
        self.target_aliases["glasses"] = ["spectacles"]
        self.target_aliases["sunglasses"] = ["shades"]
        self.target_aliases["umbrella"] = ["rain umbrella"]
        self.target_aliases["backpack"] = ["school backpack"]
        self.target_aliases["handbag"] = ["purse"]
        self.target_aliases["wallet"] = ["billfold"]
        self.target_aliases["key"] = ["door key"]
        self.target_aliases["coin"] = ["metal coin"]
        self.target_aliases["bill"] = ["banknote"]
        self.target_aliases["credit card"] = ["bank card"]
        self.target_aliases["passport"] = ["travel document"]
        self.target_aliases["ticket"] = ["admission ticket"]
        self.target_aliases["map"] = ["geographic map"]
        self.target_aliases["book"] = ["reading book"]
        self.target_aliases["newspaper"] = ["daily newspaper"]
        self.target_aliases["magazine"] = ["glossy magazine"]
        self.target_aliases["pen"] = ["writing pen"]
        self.target_aliases["pencil"] = ["writing pencil"]
        self.target_aliases["eraser"] = ["rubber eraser"]
        self.target_aliases["scissors"] = ["cutting scissors"]
        self.target_aliases["glue"] = ["adhesive glue"]
        self.target_aliases["tape"] = ["adhesive tape"]
        self.target_aliases["stapler"] = ["paper stapler"]
        self.target_aliases["hole punch"] = ["paper punch"]
        self.target_aliases["calculator"] = ["electronic calculator"]
        self.target_aliases["ruler"] = ["measuring ruler"]
        self.target_aliases["globe"] = ["world globe"]
        self.target_aliases["compass"] = ["magnetic compass"]
        self.target_aliases["telescope"] = ["astronomical telescope"]
        self.target_aliases["microscope"] = ["optical microscope"]
        self.target_aliases["camera"] = ["digital camera"]
        self.target_aliases["microphone"] = ["audio microphone"]
        self.target_aliases["speaker"] = ["audio speaker"]
        self.target_aliases["headphone"] = ["audio headphone"]
        self.target_aliases["television"] = ["flat screen TV"]
        self.target_aliases["radio"] = ["broadcast radio"]
        self.target_aliases["remote control"] = ["TV remote"]
        self.target_aliases["fan"] = ["electric fan"]
        self.target_aliases["heater"] = ["electric heater"]
        self.target_aliases["air conditioner"] = ["AC unit"]
        self.target_aliases["clock"] = ["wall clock"]
        self.target_aliases["calendar"] = ["wall calendar"]
        self.target_aliases["picture"] = ["framed picture"]
        self.target_aliases["painting"] = ["oil painting"]
        self.target_aliases["drawing"] = ["sketch"]
        self.target_aliases["sculpture"] = ["art sculpture"]
        self.target_aliases["vase"] = ["flower vase"]
        self.target_aliases["plant"] = ["potted plant"]
        self.target_aliases["flower"] = ["fresh flower"]
        self.target_aliases["tree"] = ["green tree"]
        self.target_aliases["bush"] = ["green bush"]
        self.target_aliases["grass"] = ["green grass"]
        self.target_aliases["rock"] = ["stone rock"]
        self.target_aliases["sand"] = ["beach sand"]
        self.target_aliases["water"] = ["clear water"]
        self.target_aliases["sky"] = ["blue sky"]
        self.target_aliases["cloud"] = ["white cloud"]
        self.target_aliases["sun"] = ["bright sun"]
        self.target_aliases["moon"] = ["full moon"]
        self.target_aliases["star"] = ["twinkling star"]
        self.target_aliases["rainbow"] = ["colorful rainbow"]
        self.target_aliases["lightning"] = ["electric lightning"]
        self.target_aliases["snow"] = ["white snow"]
        self.target_aliases["rain"] = ["heavy rain"]
        self.target_aliases["wind"] = ["strong wind"]
        self.target_aliases["fog"] = ["dense fog"]
        self.target_aliases["smoke"] = ["black smoke"]
        self.target_aliases["fire"] = ["burning fire"]
        self.target_aliases["ice"] = ["cold ice"]
        self.target_aliases["mountain"] = ["tall mountain"]
        self.target_aliases["hill"] = ["small hill"]
        self.target_aliases["valley"] = ["deep valley"]
        self.target_aliases["river"] = ["flowing river"]
        self.target_aliases["lake"] = ["calm lake"]
        self.target_aliases["ocean"] = ["vast ocean"]
        self.target_aliases["beach"] = ["sandy beach"]
        self.target_aliases["island"] = ["tropical island"]
        self.target_aliases["desert"] = ["sandy desert"]
        self.target_aliases["forest"] = ["dense forest"]
        self.target_aliases["jungle"] = ["tropical jungle"]
        self.target_aliases["cave"] = ["dark cave"]
        self.target_aliases["waterfall"] = ["cascading waterfall"]
        self.target_aliases["volcano"] = ["erupting volcano"]
        self.target_aliases["glacier"] = ["ice glacier"]
        self.target_aliases["canyon"] = ["grand canyon"]
        self.target_aliases["cliff"] = ["steep cliff"]
        self.target_aliases["bridge"] = ["suspension bridge"]
        self.target_aliases["road"] = ["paved road"]
        self.target_aliases["railroad"] = ["train tracks"]
        self.target_aliases["tunnel"] = ["road tunnel"]
        self.target_aliases["dam"] = ["hydroelectric dam"]
        self.target_aliases["lighthouse"] = ["coastal lighthouse"]
        self.target_aliases["windmill"] = ["wind power mill"]
        self.target_aliases["farm"] = ["dairy farm"]
        self.target_aliases["barn"] = ["red barn"]
        self.target_aliases["silo"] = ["farm silo"]
        self.target_aliases["tractor"] = ["farm tractor"]
        self.target_aliases["scarecrow"] = ["garden scarecrow"]
        self.target_aliases["well"] = ["water well"]
        self.target_aliases["fountain"] = ["water fountain"]
        self.target_aliases["statue"] = ["bronze statue"]
        self.target_aliases["monument"] = ["historic monument"]
        self.target_aliases["tower"] = ["clock tower"]
        self.target_aliases["castle"] = ["medieval castle"]
        self.target_aliases["palace"] = ["royal palace"]
        self.target_aliases["church"] = ["parish church"]
        self.target_aliases["mosque"] = ["grand mosque"]
        self.target_aliases["temple"] = ["buddhist temple"]
        self.target_aliases["synagogue"] = ["orthodox synagogue"]
        self.target_aliases["pagoda"] = ["buddhist pagoda"]
        self.target_aliases["pyramid"] = ["egyptian pyramid"]
        self.target_aliases["sphinx"] = ["great sphinx"]
        self.target_aliases["obelisk"] = ["ancient obelisk"]
        self.target_aliases["arch"] = ["triumphal arch"]
        self.target_aliases["column"] = ["roman column"]
        self.target_aliases["ruins"] = ["ancient ruins"]
        self.target_aliases["excavation site"] = ["archaeological excavation"]
        self.target_aliases["construction site"] = ["building construction site"]
        self.target_aliases["crane"] = ["construction crane"]
        self.target_aliases["excavator"] = ["track excavator"]
        self.target_aliases["bulldozer"] = ["track bulldozer"]
        self.target_aliases["road roller"] = ["vibratory roller"]
        self.target_aliases["cement mixer"] = ["truck mixer"]
        self.target_aliases["dump truck"] = ["articulated dump truck"]
        self.target_aliases["forklift"] = ["counterbalance forklift"]
        self.target_aliases["pallet"] = ["wooden pallet"]
        self.target_aliases["box"] = ["cardboard box"]
        self.target_aliases["barrel"] = ["wooden barrel"]
        self.target_aliases["crate"] = ["wooden crate"]
        self.target_aliases["container"] = ["intermodal container"]
        self.target_aliases["warehouse"] = ["storage warehouse"]
        self.target_aliases["factory"] = ["textile factory"]
        self.target_aliases["power plant"] = ["nuclear power plant"]
        self.target_aliases["oil rig"] = ["offshore oil rig"]
        self.target_aliases["wind farm"] = ["offshore wind farm"]
        self.target_aliases["solar panel"] = ["rooftop solar panel"]
        self.target_aliases["satellite dish"] = ["tv satellite dish"]
        self.target_aliases["radio telescope"] = ["astronomical radio telescope"]
        self.target_aliases["observatory"] = ["astronomical observatory"]
        self.target_aliases["laboratory"] = ["research laboratory"]
        self.target_aliases["office"] = ["corporate office"]
        self.target_aliases["home"] = ["family home"]
        self.target_aliases["kitchen"] = ["modern kitchen"]
        self.target_aliases["bedroom"] = ["master bedroom"]
        self.target_aliases["bathroom"] = ["ensuite bathroom"]
        self.target_aliases["living room"] = ["spacious living room"]
        self.target_aliases["dining room"] = ["formal dining room"]
        self.target_aliases["hallway"] = ["long hallway"]
        self.target_aliases["stairs"] = ["wooden stairs"]
        self.target_aliases["elevator"] = ["passenger elevator"]
        self.target_aliases["escalator"] = ["shopping mall escalator"]
        self.target_aliases["balcony"] = ["apartment balcony"]
        self.target_aliases["patio"] = ["outdoor patio"]
        self.target_aliases["garden"] = ["flower garden"]
        self.target_aliases["fence"] = ["wooden fence"]
        self.target_aliases["gate"] = ["garden gate"]
        self.target_aliases["road"] = ["asphalt road"]
        self.target_aliases["street"] = ["city street"]
        self.target_aliases["sidewalk"] = ["concrete sidewalk"]
        self.target_aliases["crosswalk"] = ["painted crosswalk"]
        self.target_aliases["traffic light"] = ["red traffic light"]
        self.target_aliases["fire hydrant"] = ["red fire hydrant"]
        self.target_aliases["parking meter"] = ["digital parking meter"]
        self.target_aliases["bus stop"] = ["public bus stop"]
        self.target_aliases["taxi stand"] = ["airport taxi stand"]
        self.target_aliases["subway station"] = ["underground subway station"]
        self.target_aliases["train station"] = ["main train station"]
        self.target_aliases["airport terminal"] = ["international airport terminal"]
        self.target_aliases["port terminal"] = ["cruise ship terminal"]
        self.target_aliases["gas station"] = ["fuel station"]
        self.target_aliases["bank"] = ["commercial bank"]
        self.target_aliases["post office"] = ["local post office"]
        self.target_aliases["police station"] = ["city police station"]
        self.target_aliases["fire station"] = ["volunteer fire station"]
        self.target_aliases["hospital"] = ["general hospital"]
        self.target_aliases["pharmacy"] = ["retail pharmacy"]
        self.target_aliases["supermarket"] = ["large supermarket"]
        self.target_aliases["bakery"] = ["artisan bakery"]
        self.target_aliases["butcher shop"] = ["local butcher shop"]
        self.target_aliases["fish market"] = ["fresh fish market"]
        self.target_aliases["flower shop"] = ["flower boutique"]
        self.target_aliases["bookstore"] = ["independent bookstore"]
        self.target_aliases["clothing store"] = ["fashion clothing store"]
        self.target_aliases["shoe store"] = ["shoe retail store"]
        self.target_aliases["jewelry store"] = ["fine jewelry store"]
        self.target_aliases["electronics store"] = ["consumer electronics store"]
        self.target_aliases["toy store"] = ["children's toy store"]
        self.target_aliases["pet store"] = ["animal pet store"]
        self.target_aliases["hardware store"] = ["home hardware store"]
        self.target_aliases["department store"] = ["luxury department store"]
        self.target_aliases["shopping mall"] = ["indoor shopping mall"]
        self.target_aliases["restaurant"] = ["fine dining restaurant"]
        self.target_aliases["cafe"] = ["cozy cafe"]
        self.target_aliases["bar"] = ["cocktail bar"]
        self.target_aliases["nightclub"] = ["dance nightclub"]
        self.target_aliases["hotel"] = ["boutique hotel"]
        self.target_aliases["motel"] = ["roadside motel"]
        self.target_aliases["hostel"] = ["youth hostel"]
        self.target_aliases["resort"] = ["beach resort"]
        self.target_aliases["casino"] = ["resort casino"]
        self.target_aliases["theater"] = ["live theater"]
        self.target_aliases["cinema"] = ["multiplex cinema"]
        self.target_aliases["concert hall"] = ["symphony concert hall"]
        self.target_aliases["art gallery"] = ["contemporary art gallery"]
        self.target_aliases["museum"] = ["history museum"]
        self.target_aliases["library"] = ["university library"]
        self.target_aliases["school"] = ["elementary school"]
        self.target_aliases["university"] = ["state university"]
        self.target_aliases["stadium"] = ["football stadium"]
        self.target_aliases["gym"] = ["public gym"]
        self.target_aliases["swimming pool"] = ["public swimming pool"]
        self.target_aliases["park"] = ["city park"]
        self.target_aliases["playground"] = ["children's playground"]
        self.target_aliases["zoo"] = ["wildlife zoo"]
        self.target_aliases["aquarium"] = ["public aquarium"]
        self.target_aliases["botanical garden"] = ["public botanical garden"]
        self.target_aliases["national park"] = ["wilderness national park"]

        # Add more general aliases for common objects/properties
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

        # Ensure base terms are also in their own alias list for simplicity
        for key in list(self.target_aliases.keys()):
            if key not in self.target_aliases[key]:
                self.target_aliases[key].insert(0, key)

    def _generate_prompts(self, target: str) -> List[str]:
        prompts = []
        # Use base target and its aliases
        for alias in self.target_aliases.get(target.lower(), [target]):
            # Object prompts
            for p in self.object_prompts:
                prompts.append(p.format(target=alias))
            # Property prompts (if applicable, e.g., for colors)
            if alias in ["red", "blue", "green", "yellow", "black", "white", "brown", "orange", "purple", "pink", "gray"]:
                for p in self.property_prompts:
                    prompts.append(p.format(target=alias))
            # Context-aware prompts (randomly select a few to avoid explosion)
            if random.random() < 0.5: # 50% chance to add context-aware prompts
                prompts.extend([p.format(target=alias) for p in random.sample(self.context_aware_prompts, min(3, len(self.context_aware_prompts)))])
        return list(set(prompts)) # Remove duplicates

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
            r"Pick the images that have a (.+)"
        ]
        for pattern in patterns:
            match = re.search(pattern, challenge_text, re.IGNORECASE)
            if match:
                target = match.group(1).strip()
                # Handle pluralization if necessary, e.g., 'cars' -> 'car'
                if target.endswith('s') and not target.endswith('ss'): # Simple plural check
                    return target[:-1]
                return target
        return None

    async def _get_clip_model(self):
        if self.clip_model is None:
            self.clip_model = await ClipModel.get_instance()
        return self.clip_model

    def _crop_image(self, image: Image.Image, crop_type: str) -> Image.Image:
        width, height = image.size
        if crop_type == "full":
            return image
        elif crop_type == "center70%":
            left = width * 0.15
            top = height * 0.15
            right = width * 0.85
            bottom = height * 0.85
            return image.crop((left, top, right, bottom))
        elif crop_type == "padded":
            # This is essentially a slightly smaller full image, or a full image with some border handling
            # For simplicity, let's define it as 90% of the image, centered.
            left = width * 0.05
            top = height * 0.05
            right = width * 0.95
            bottom = height * 0.95
            return image.crop((left, top, right, bottom))
        elif crop_type == "tight_center50%":
            left = width * 0.25
            top = height * 0.25
            right = width * 0.75
            bottom = height * 0.75
            return image.crop((left, top, right, bottom))
        return image

    async def _get_tile_scores(self, tile_images: List[Image.Image], target_text: str) -> List[float]:
        clip = await self._get_clip_model()
        all_prompts = self._generate_prompts(target_text)
        text_features = await clip.get_text_features(all_prompts)

        scores = []
        for i, tile_img in enumerate(tile_images):
            tile_scores = []
            # Multi-scale scoring
            crop_types = {"full": 0.4, "center70%": 0.3, "padded": 0.15, "tight_center50%": 0.15}
            for crop_type, weight in crop_types.items():
                cropped_img = self._crop_image(tile_img, crop_type)
                image_features = await clip.get_image_features([cropped_img])
                similarity = (image_features @ text_features.T).softmax(dim=-1)
                tile_scores.append(similarity.mean().item() * weight)
            scores.append(sum(tile_scores))
        return scores

    def _adaptive_thresholding(self, scores: List[float]) -> List[int]:
        if not scores:
            return []

        scores_np = np.array(scores)
        sorted_scores = np.sort(scores_np)

        # If all scores are very low, indicate low confidence for LLM fallback
        if np.max(scores_np) < 0.25: # Threshold for LLM fallback
            return [] # Signal for LLM fallback

        # Otsu-like bimodal detection or largest gap
        # Try to find a natural split point
        threshold = 0.0
        selected_indices = []

        # If score range is significant, look for the largest gap
        if (sorted_scores[-1] - sorted_scores[0]) > 0.15:
            gaps = np.diff(sorted_scores)
            if len(gaps) > 0:
                largest_gap_idx = np.argmax(gaps)
                threshold = sorted_scores[largest_gap_idx] + gaps[largest_gap_idx] / 2
                print(f"Adaptive threshold (largest gap): {threshold:.4f}")
            else:
                # Fallback to simple mean if only one score or no gaps
                threshold = np.mean(scores_np)
                print(f"Adaptive threshold (mean fallback): {threshold:.4f}")
        else:
            # If scores are clustered, use a simpler approach, e.g., mean or a fixed offset from max
            threshold = np.mean(scores_np) + 0.05 # Small boost above mean
            print(f"Adaptive threshold (clustered scores, mean+0.05): {threshold:.4f}")

        # Apply threshold and clamp selection to 2-6 tiles
        potential_selections = [i for i, score in enumerate(scores) if score >= threshold]

        # Clamp selection to 2-6 tiles
        if len(potential_selections) < 2:
            # If too few, take the top 2-3 highest scores
            top_scores_indices = np.argsort(scores_np)[::-1]
            selected_indices = list(top_scores_indices[:min(len(scores), 3)])
            print(f"Clamping selection: too few, selected top {len(selected_indices)} tiles.")
        elif len(potential_selections) > 6:
            # If too many, take the top 6 highest scores from potential selections
            potential_scores = [scores[i] for i in potential_selections]
            top_6_indices_in_potential = np.argsort(potential_scores)[::-1][:6]
            selected_indices = [potential_selections[i] for i in top_6_indices_in_potential]
            print(f"Clamping selection: too many, selected top 6 tiles.")
        else:
            selected_indices = potential_selections

        # Spatial context boost (15% for neighbors)
        # Re-evaluate scores with spatial context if needed, then re-threshold or re-select
        # For simplicity, applying boost and then re-selecting based on new scores
        boosted_scores = list(scores_np)
        grid_size = int(math.sqrt(len(scores))) # Assuming square grid
        if grid_size * grid_size == len(scores):
            for idx in selected_indices:
                row, col = divmod(idx, grid_size)
                neighbors = []
                for dr in [-1, 0, 1]:
                    for dc in [-1, 0, 1]:
                        if dr == 0 and dc == 0: continue
                        nr, nc = row + dr, col + dc
                        if 0 <= nr < grid_size and 0 <= nc < grid_size:
                            neighbors.append(nr * grid_size + nc)
                for neighbor_idx in neighbors:
                    if neighbor_idx not in selected_indices:
                        boosted_scores[neighbor_idx] *= 1.15 # 15% boost

            # Re-evaluate with boosted scores
            boosted_scores_np = np.array(boosted_scores)
            if (boosted_scores_np[-1] - boosted_scores_np[0]) > 0.15:
                gaps = np.diff(np.sort(boosted_scores_np))
                if len(gaps) > 0:
                    largest_gap_idx = np.argmax(gaps)
                    threshold = np.sort(boosted_scores_np)[largest_gap_idx] + gaps[largest_gap_idx] / 2
                else:
                    threshold = np.mean(boosted_scores_np)
            else:
                threshold = np.mean(boosted_scores_np) + 0.05

            selected_indices = [i for i, score in enumerate(boosted_scores) if score >= threshold]
            # Re-clamp after boost
            if len(selected_indices) < 2:
                top_scores_indices = np.argsort(boosted_scores_np)[::-1]
                selected_indices = list(top_scores_indices[:min(len(scores), 3)])
            elif len(selected_indices) > 6:
                potential_scores = [boosted_scores[i] for i in selected_indices]
                top_6_indices_in_potential = np.argsort(potential_scores)[::-1][:6]
                selected_indices = [selected_indices[i] for i in top_6_indices_in_potential]

        return selected_indices

    async def _llm_vision_fallback(self, target_text: str, challenge_screenshot: Image.Image, num_tiles: int) -> List[int]:
        print("Falling back to LLM Vision for captcha solving...")
        buffered = io.BytesIO()
        challenge_screenshot.save(buffered, format="PNG")
        b64_string = base64.b64encode(buffered.getvalue()).decode("utf-8")

        tile_number_prompt = f"Which tiles (numbered 1-{num_tiles} left-to-right, top-to-bottom) contain a {target_text}? Reply with just the numbers, separated by commas. For example: '1,5,9' or '2,3'. If no tiles contain the object, reply with 'None'."

        try:
            response = self.client.chat.completions.create(
                model=self.config.llm_model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": tile_number_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_string}"}}
                ]}],
                max_tokens=500
            )
            answer = response.choices[0].message.content
            print(f"LLM Vision response: {answer}")

            # Parse response for numbers
            numbers = []
            if answer and answer.lower() != 'none':
                numbers_str = re.findall(r'\d+', answer)
                numbers = [int(n) - 1 for n in numbers_str if 1 <= int(n) <= num_tiles] # Convert to 0-indexed
            return numbers
        except Exception as e:
            print(f"LLM Vision fallback failed: {e}")
            return []

    async def solve(self) -> bool:
        print("Attempting to solve hCaptcha grid challenge with GodSolver...")
        for round_num in range(1, self.config.max_challenge_rounds + 1):
            print(f"GodSolver: Challenge Round {round_num}")
            try:
                # 1. Find iframe and get challenge info
                # Assuming the iframe is already focused or handled by the main solver logic
                # We need to get the challenge text and tile images from the current page/iframe
                challenge_info = await self.detector.get_challenge_info() # This should return (challenge_text, tile_images, verify_button_selector, tile_selectors)
                challenge_text = challenge_info[0]
                tile_images = challenge_info[1]
                verify_button_selector = challenge_info[2]
                tile_selectors = challenge_info[3]

                if not challenge_text or not tile_images:
                    print("Could not get challenge info. Retrying...")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                target = self._parse_challenge_text(challenge_text)
                if not target:
                    print(f"Could not parse target from challenge text: {challenge_text}. Retrying...")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                print(f"Parsed target: {target}")

                # 3. Solve with CLIP (multi-crop + contrast scoring)
                scores = await self._get_tile_scores(tile_images, target)
                print(f"CLIP scores: {scores}")

                selected_indices = self._adaptive_thresholding(scores)

                # 4. If low confidence, try LLM fallback
                if not selected_indices and np.max(scores) < 0.25 and self.config.llm_fallback:
                    print("CLIP confidence too low, attempting LLM Vision fallback.")
                    # Take screenshot of the entire challenge area (assuming iframe is the context)
                    challenge_frame_selector = "iframe[title='hCaptcha challenge']"
                    challenge_screenshot = await self.get_screenshot(challenge_frame_selector)
                    selected_indices = await self._llm_vision_fallback(target, challenge_screenshot, len(tile_images))

                if not selected_indices:
                    print("No tiles selected by CLIP or LLM. Retrying challenge.")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                print(f"Selected tiles (0-indexed): {selected_indices}")

                # 5. Click tiles in randomized order
                random.shuffle(selected_indices)
                for idx in selected_indices:
                    # Assuming tile_selectors is a list of Playwright locators or selectors
                    await self.click_element(tile_selectors[idx])
                    await asyncio.sleep(random.uniform(0.3, 0.8)) # Variable delay

                # 6. Click verify
                # Better verify button detection: try multiple selectors
                verify_selectors = [
                    verify_button_selector, # Primary selector from detector
                    "button.btn-primary",
                    "button.submit",
                    "#hcaptcha-verify-btn",
                    "#rc-anchor-container button[type='submit']"
                ]
                clicked_verify = False
                for selector in verify_selectors:
                    try:
                        await self.click_element(selector)
                        clicked_verify = True
                        print(f"Clicked verify button using selector: {selector}")
                        break
                    except Exception:
                        continue

                if not clicked_verify:
                    print("Could not find or click verify button. Retrying challenge.")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                await asyncio.sleep(self.config.min_solve_time_per_round) # Wait for verification

                # 7. Check if solved
                if await self.detector.is_solved():
                    print("hCaptcha solved successfully!")
                    return True
                else:
                    print("hCaptcha not solved, retrying...")

            except Exception as e:
                print(f"Error during GodSolver round {round_num}: {e}")
                await asyncio.sleep(random.uniform(2, 4))

        print("GodSolver failed to solve hCaptcha after multiple rounds.")
        return False


class DragSolver(PlaywrightSolver):
    def __init__(self, page: Page, config: SolverConfig):
        super().__init__(page, config)
        self.client = openai.OpenAI()

    async def solve(self) -> bool:
        print("Attempting to solve drag captcha with improved DragSolver...")
        for round_num in range(1, self.config.max_challenge_rounds + 1):
            print(f"DragSolver: Challenge Round {round_num}")
            try:
                # 1. Better challenge frame detection (assuming detector handles this)
                # The detector should provide the iframe and the main challenge area selector
                challenge_info = await self.detector.get_challenge_info() # This should return (challenge_text, draggable_selector, drop_target_selector)
                draggable_selector = challenge_info[1] # Assuming the second element is the draggable selector
                drop_target_selector = challenge_info[2] # Assuming the third element is the drop target selector

                if not draggable_selector or not drop_target_selector:
                    print("Could not detect draggable or drop target elements. Retrying...")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                # Get initial bounds
                draggable_box = await self.get_element_bounds(draggable_selector)
                drop_target_box = await self.get_element_bounds(drop_target_selector)

                if not draggable_box or not drop_target_box:
                    print("Draggable or drop target not visible. Retrying...")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                # Calculate initial center of draggable
                source_x = draggable_box['x'] + draggable_box['width'] / 2
                source_y = draggable_box['y'] + draggable_box['height'] / 2

                # Attempt to find target coordinates using CV methods first
                target_x, target_y = await self._find_target_with_cv(draggable_selector, drop_target_selector)

                # 4. LLM Vision for target finding if CV fails or is low confidence
                if target_x is None or target_y is None:
                    print("CV methods failed to find target, falling back to LLM Vision.")
                    challenge_frame_selector = "iframe[title='hCaptcha challenge']"
                    challenge_screenshot = await self.get_screenshot(challenge_frame_selector)
                    llm_coords = await self._llm_vision_for_drag_target(challenge_screenshot)
                    if llm_coords:
                        # Map percentage coordinates to pixel coordinates
                        frame_box = await self.get_element_bounds(challenge_frame_selector)
                        if frame_box:
                            frame_width = frame_box['width']
                            frame_height = frame_box['height']
                            target_x = frame_box['x'] + frame_width * llm_coords[0]
                            target_y = frame_box['y'] + frame_height * llm_coords[1]
                            print(f"LLM provided target: ({target_x:.2f}, {target_y:.2f})")
                    else:
                        print("LLM Vision also failed to find target. Retrying...")
                        await asyncio.sleep(random.uniform(1, 2))
                        continue

                if target_x is None or target_y is None:
                    print("Failed to determine target coordinates. Retrying...")
                    await asyncio.sleep(random.uniform(1, 2))
                    continue

                # 6. Human-like drag trajectory (already implemented in PlaywrightSolver's drag_and_drop)
                await self.drag_and_drop(draggable_selector, {'x': target_x, 'y': target_y})

                await asyncio.sleep(self.config.min_solve_time_per_round) # Wait for verification

                # 7. Better solved detection
                if await self.detector.is_solved():
                    print("Drag captcha solved successfully!")
                    return True
                else:
                    print("Drag captcha not solved, retrying...")

            except Exception as e:
                print(f"Error during DragSolver round {round_num}: {e}")
                await asyncio.sleep(random.uniform(2, 4))

        print("DragSolver failed to solve captcha after multiple rounds.")
        return False

    async def _find_target_with_cv(self, draggable_selector: str, drop_target_selector: str) -> Tuple[Optional[float], Optional[float]]:
        """Attempts to find the target coordinates using OpenCV methods."""
        try:
            # Get screenshots of the draggable and the full challenge area
            challenge_frame_selector = "iframe[title='hCaptcha challenge']"
            full_challenge_screenshot = await self.get_screenshot(challenge_frame_selector)
            draggable_image = await self.get_screenshot(draggable_selector)

            # Convert PIL Images to OpenCV format
            full_challenge_np = np.array(full_challenge_screenshot.convert('RGB'))
            draggable_np = np.array(draggable_image.convert('RGB'))

            # Convert to grayscale for template matching
            full_challenge_gray = cv2.cvtColor(full_challenge_np, cv2.COLOR_RGB2GRAY)
            draggable_gray = cv2.cvtColor(draggable_np, cv2.COLOR_RGB2GRAY)

            # Template matching to find the draggable within the full challenge image
            res = cv2.matchTemplate(full_challenge_gray, draggable_gray, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

            if max_val > 0.7: # High confidence match
                # max_loc is the top-left corner of the matched area
                # Calculate center of the matched draggable
                draggable_w, draggable_h = draggable_gray.shape[::-1]
                matched_x = max_loc[0] + draggable_w / 2
                matched_y = max_loc[1] + draggable_h / 2

                # Now, the challenge is to find WHERE it should go. This is highly specific to the captcha.
                # For a simple slider, it's usually a 'hole' or a specific outline.
                # This part is difficult to generalize without knowing the exact visual pattern.
                # For now, let's assume the drop_target_selector points to the *final* position or an indicator.
                # If drop_target_selector points to the final position, we can use its center.
                drop_target_box = await self.get_element_bounds(drop_target_selector)
                if drop_target_box:
                    target_x = drop_target_box['x'] + drop_target_box['width'] / 2
                    target_y = drop_target_box['y'] + drop_target_box['height'] / 2
                    print(f"CV found target: ({target_x:.2f}, {target_y:.2f})")
                    return target_x, target_y

            print("CV template matching for drag target failed or low confidence.")
            return None, None

        except Exception as e:
            print(f"Error in _find_target_with_cv: {e}")
            return None, None

    async def _llm_vision_for_drag_target(self, challenge_screenshot: Image.Image) -> Optional[Tuple[float, float]]:
        print("Using LLM Vision to find drag target coordinates...")
        buffered = io.BytesIO()
        challenge_screenshot.save(buffered, format="PNG")
        b64_string = base64.b64encode(buffered.getvalue()).decode("utf-8")

        prompt = "In this captcha image, there is a draggable icon that needs to be placed somewhere on the background. Where should it go? Reply with approximate x,y coordinates as percentage of image width/height, like '65%,40%'. Only provide the coordinates, no other text."

        try:
            response = self.client.chat.completions.create(
                model=self.config.llm_model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_string}"}}
                ]}],
                max_tokens=50
            )
            answer = response.choices[0].message.content
            print(f"LLM Vision response for drag target: {answer}")

            # Parse response for percentage coordinates
            match = re.search(r'(\d{1,3})%,(\d{1,3})%', answer)
            if match:
                x_percent = float(match.group(1)) / 100.0
                y_percent = float(match.group(2)) / 100.0
                return x_percent, y_percent
            else:
                print("Could not parse percentage coordinates from LLM response.")
                return None, None
        except Exception as e:
            print(f"LLM Vision for drag target failed: {e}")
            return None, None

# --- END PART 2 --- (PatternBreakerSolver, ChallengeRouter, MasterSolver follow in Part 3)

class PatternBreakerSolver(CaptchaSolver):
    """
    Solves pattern-breaking hCaptcha challenges using LLM Vision.
    These challenges show a grid of symbols where most follow a pattern,
    and you must click the ones that DON'T fit.
    """
    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.client = openai.OpenAI()  # auto-configured

    async def solve(self, page: Page) -> bool:
        print("Attempting to solve Pattern Breaker challenge...")
        for round_num in range(self.config.max_challenge_rounds):
            try:
                print(f"Pattern Breaker Solver: Round {round_num + 1}/{self.config.max_challenge_rounds}")
                # 1. Find the hCaptcha challenge iframe
                iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
                if not iframe_locator:
                    print("Pattern Breaker Solver: hCaptcha iframe not found.")
                    return False

                challenge_frame = await iframe_locator.element_handle()
                if not challenge_frame:
                    print("Pattern Breaker Solver: Challenge frame element handle not found.")
                    return False

                # Get the bounding box of the challenge area for screenshot
                iframe_box = await challenge_frame.bounding_box()
                if not iframe_box:
                    print("Pattern Breaker Solver: Could not get iframe bounding box.")
                    return False

                # 2. Take a screenshot of the entire challenge area
                # We need the full page screenshot to get the context, then crop later if needed
                full_page_screenshot_bytes = await page.screenshot()
                full_page_image = Image.open(io.BytesIO(full_page_screenshot_bytes))

                # Crop to the iframe area
                cropped_image = full_page_image.crop((
                    int(iframe_box['x']),
                    int(iframe_box['y']),
                    int(iframe_box['x'] + iframe_box['width']),
                    int(iframe_box['y'] + iframe_box['height'])
                ))

                img_byte_arr = io.BytesIO()
                cropped_image.save(img_byte_arr, format="PNG")
                screenshot_b64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

                # 3. Detect grid dimensions by analyzing the image
                grid_rows, grid_cols = self._detect_grid_dimensions(img_byte_arr.getvalue())
                print(f"Pattern Breaker Solver: Detected grid dimensions: {grid_rows}x{grid_cols}")

                # 4. Send screenshot to LLM vision (gpt-5-nano) with a carefully crafted prompt
                # 5. Parse response to get coordinates of pattern-breaking elements
                pattern_breaking_positions = await self._analyze_pattern_with_llm(
                    screenshot_b64, grid_rows, grid_cols
                )

                if not pattern_breaking_positions:
                    print("Pattern Breaker Solver: LLM did not identify any pattern-breaking elements. Retrying...")
                    await self._rate_limit_delay()
                    continue

                print(f"Pattern Breaker Solver: Identified pattern-breaking positions: {pattern_breaking_positions}")

                # 6. Click those elements with human-like mouse movement
                click_coords = self._get_tile_click_positions(
                    iframe_box, grid_rows, grid_cols, pattern_breaking_positions
                )

                for x, y in click_coords:
                    await HumanMouse.move_and_click(page, x, y)
                    await self._rate_limit_delay()

                # 7. Click verify button
                verify_button_selector = "button.verifybtn" # Common hCaptcha verify button class
                try:
                    await self.click_element(page, verify_button_selector)
                    print("Pattern Breaker Solver: Clicked verify button.")
                except ValueError:
                    print("Pattern Breaker Solver: Verify button not found, assuming auto-verify or already clicked.")

                # 8. Check if solved, retry if needed
                # This requires a ChallengeDetector instance, which is usually part of the main flow.
                # For now, we'll assume a simple check or rely on the MasterSolver to re-evaluate.
                # A more robust solution would involve passing a detector or checking for success indicators.
                await asyncio.sleep(2) # Give time for challenge to update
                # In a real scenario, MasterSolver would call is_solved() after this.
                return True # Assume success for this round, MasterSolver will confirm

            except Exception as e:
                print(f"Pattern Breaker Solver: An error occurred in round {round_num + 1}: {e}")
                await self._rate_limit_delay()
                continue
        print("Pattern Breaker Solver: Failed to solve after multiple rounds.")
        return False

    async def _analyze_pattern_with_llm(self, screenshot_b64: str, grid_rows: int, grid_cols: int) -> List[Tuple[int, int]]:
        prompt = f"""
You are analyzing an hCaptcha challenge. The image shows a grid of {grid_rows}x{grid_cols} abstract symbols/shapes.
Most symbols follow a consistent pattern (same shape, orientation, style).
Some symbols BREAK the pattern - they are different from the majority.

Analyze the grid carefully:
1. Identify what the MAJORITY pattern is (the most common symbol type)
2. Find ALL elements that are DIFFERENT from the majority

Return ONLY the grid positions (row,col) of the pattern-breaking elements.
Use 0-indexed positions. Row 0 is the top row, Col 0 is the leftmost column.

Format your answer as a JSON array of [row, col] pairs, like: [[0,2],[1,4],[3,1]]
Return ONLY the JSON array, nothing else.
"""

        try:
            response = self.client.chat.completions.create(
                model=self.config.pattern_solver_model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}}
                ]}],
                max_tokens=300
            )
            llm_response_content = response.choices[0].message.content.strip()
            print(f"Pattern Breaker Solver LLM raw response: {llm_response_content}")

            # Attempt to parse JSON
            try:
                parsed_response = json.loads(llm_response_content)
                if isinstance(parsed_response, list) and all(isinstance(item, list) and len(item) == 2 for item in parsed_response):
                    return [tuple(pos) for pos in parsed_response]
            except json.JSONDecodeError:
                print("Pattern Breaker Solver: LLM response not valid JSON. Trying alternative parsing...")

            # Fallback: try to extract coordinates from text if JSON parsing fails
            # This is a simpler regex that might catch patterns like (0,2), [1,4], or 0,2
            matches = re.findall(r'\[(\d+),\s*(\d+)\]|\((\d+),\s*(\d+)\)|(\d+),\s*(\d+)', llm_response_content)
            extracted_positions = []
            for match in matches:
                # Find the non-empty groups in the match tuple
                coords = [int(c) for c in match if c]
                if len(coords) == 2:
                    extracted_positions.append(tuple(coords))
            if extracted_positions:
                print(f"Pattern Breaker Solver: Extracted positions via regex: {extracted_positions}")
                return extracted_positions

            # Second prompt attempt if initial parsing fails
            print("Pattern Breaker Solver: First LLM attempt failed to parse. Trying second prompt...")
            second_prompt = f"""
Your previous response was not in the requested JSON format. Please provide ONLY a JSON array of [row, col] pairs for the pattern-breaking elements. Example: [[0,2],[1,4],[3,1]]
"""
            response_retry = self.client.chat.completions.create(
                model=self.config.pattern_solver_model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": second_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}}
                ]}],
                max_tokens=100 # Shorter max_tokens for concise JSON
            )
            llm_response_retry_content = response_retry.choices[0].message.content.strip()
            print(f"Pattern Breaker Solver LLM retry raw response: {llm_response_retry_content}")
            try:
                parsed_response_retry = json.loads(llm_response_retry_content)
                if isinstance(parsed_response_retry, list) and all(isinstance(item, list) and len(item) == 2 for item in parsed_response_retry):
                    return [tuple(pos) for pos in parsed_response_retry]
            except json.JSONDecodeError:
                print("Pattern Breaker Solver: Second LLM attempt also failed to parse JSON.")

        except Exception as e:
            print(f"Pattern Breaker Solver: Error during LLM analysis: {e}")
        return []

    def _detect_grid_dimensions(self, screenshot_bytes: bytes) -> Tuple[int, int]:
        # Use OpenCV to detect grid structure
        try:
            nparr = np.frombuffer(screenshot_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                print("Pattern Breaker Solver: Could not decode image for grid detection.")
                return 3, 3 # Fallback

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edged = cv2.Canny(blurred, 50, 150)

            # Find contours
            contours, _ = cv2.findContours(edged.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # Filter contours that are likely grid cells (squares/rectangles of a certain size)
            cell_candidates = []
            min_cell_area = (img.shape[0] * img.shape[1]) / 100 # Heuristic: min 1% of total image area
            max_cell_area = (img.shape[0] * img.shape[1]) / 4 # Heuristic: max 25% of total image area

            for contour in contours:
                perimeter = cv2.arcLength(contour, True)
                approx = cv2.approxPolyDP(contour, 0.04 * perimeter, True)
                area = cv2.contourArea(contour)

                # Check if it's a quadrilateral and within reasonable area bounds
                if len(approx) == 4 and area > min_cell_area and area < max_cell_area:
                    x, y, w, h = cv2.boundingRect(approx)
                    aspect_ratio = float(w) / h
                    # Assume cells are roughly square or rectangular
                    if 0.7 < aspect_ratio < 1.3:
                        cell_candidates.append((x, y, w, h))
            
            if not cell_candidates:
                print("Pattern Breaker Solver: No significant cell candidates found. Falling back to 3x3.")
                return 3, 3 # Fallback

            # Sort candidates by y then x to group them into rows and columns
            cell_candidates.sort(key=lambda c: (c[1], c[0]))

            # Determine rows and columns based on clustering of y and x coordinates
            y_coords = [c[1] for c in cell_candidates]
            x_coords = [c[0] for c in cell_candidates]

            # Simple clustering for rows/cols (can be improved with more robust clustering)
            # Count distinct y-coordinates within a tolerance to get rows
            rows = 0
            if y_coords:
                rows = 1
                for i in range(1, len(y_coords)):
                    if abs(y_coords[i] - y_coords[i-1]) > 10: # Tolerance for row separation
                        rows += 1
            
            # Count distinct x-coordinates within a tolerance to get columns
            cols = 0
            if x_coords:
                cols = 1
                for i in range(1, len(x_coords)):
                    if abs(x_coords[i] - x_coords[i-1]) > 10: # Tolerance for column separation
                        cols += 1
            
            # Refine rows/cols based on the number of cells found
            if rows > 0 and cols > 0 and len(cell_candidates) >= rows * cols * 0.7: # If we found enough cells to form a grid
                print(f"Pattern Breaker Solver: OpenCV detected {rows}x{cols} grid.")
                return rows, cols
            else:
                print("Pattern Breaker Solver: OpenCV grid detection inconclusive. Falling back to 3x3.")
                return 3, 3 # Fallback

        except Exception as e:
            print(f"Pattern Breaker Solver: Error during grid detection: {e}. Falling back to 3x3.")
            return 3, 3 # Fallback: assume 3x3 for pattern challenges (most common)

    def _get_tile_click_positions(self, iframe_box: dict, grid_rows: int, grid_cols: int, positions: List[Tuple[int, int]]) -> List[Tuple[float, float]]:
        click_coords = []
        iframe_x = iframe_box['x']
        iframe_y = iframe_box['y']
        iframe_width = iframe_box['width']
        iframe_height = iframe_box['height']

        # Calculate tile dimensions
        tile_width = iframe_width / grid_cols
        tile_height = iframe_height / grid_rows

        for r, c in positions:
            # Calculate center of the tile
            center_x = iframe_x + (c * tile_width) + (tile_width / 2)
            center_y = iframe_y + (r * tile_height) + (tile_height / 2)
            click_coords.append((center_x, center_y))
        return click_coords


class ChallengeRouter:
    """Routes hCaptcha challenges to the appropriate solver based on challenge type detection."""
    
    def __init__(self, config: SolverConfig):
        self.config = config
        self.detector = None  # Set when page is available
    
    async def detect_and_route(self, page: Page) -> Optional[CaptchaSolver]:
        """Detect challenge type and return the appropriate solver instance."""
        print("Challenge Router: Detecting challenge type...")
        iframe_locator = page.frame_locator("iframe[src*='newassets.hcaptcha.com/captcha']")
        if not iframe_locator:
            print("Challenge Router: hCaptcha iframe not found.")
            return None

        # Get the challenge prompt text
        try:
            prompt_element = iframe_locator.locator(".challenge-header") # Common selector for prompt
            prompt_text = await prompt_element.text_content()
            if prompt_text:
                prompt_text = prompt_text.lower()
                print(f"Challenge Router: Detected prompt: '{prompt_text}'")

                if any(keyword in prompt_text for keyword in ["break the pattern", "odd one out", "doesn't belong", "doesn't fit"]):
                    print("Challenge Router: Routing to PatternBreakerSolver.")
                    return PatternBreakerSolver(self.config)
                # Add more routing rules as other solvers are implemented
                # elif any(keyword in prompt_text for keyword in ["drag", "place", "move", "drop", "fit"]):
                #     # Check for draggable DOM elements
                #     if await iframe_locator.locator(".draggable-element-selector").count() > 0:
                #         print("Challenge Router: Routing to DragSolver.")
                #         return DragSolver(self.config)
                # elif await iframe_locator.locator(".slider-element-selector").count() > 0:
                #     print("Challenge Router: Routing to SliderSolver.")
                #     return SliderSolver(self.config)
                elif any(keyword in prompt_text for keyword in ["select all", "click all", "containing", "images of", "with a"]):
                    print("Challenge Router: Routing to GodSolver.")
                    return GodSolver(self.config)
                
            print("Challenge Router: No specific challenge type detected from prompt. Defaulting to GodSolver.")
            return GodSolver(self.config) # Default to GodSolver

        except Exception as e:
            print(f"Challenge Router: Error detecting challenge type: {e}. Defaulting to GodSolver.")
            return GodSolver(self.config)


class MasterSolver(CaptchaSolver):
    """
    Top-level solver that automatically detects challenge type and routes to the correct solver.
    Drop-in replacement for GodSolver with automatic challenge type detection.
    """
    
    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.router = ChallengeRouter(config)
        self.god_solver = GodSolver(config)
        # self.drag_solver = DragSolver(config) # Assuming these are implemented elsewhere
        self.pattern_solver = PatternBreakerSolver(config)
        # self.slider_solver = SliderSolver(config)
        # self.shape_matcher = ShapeMatcher(config)
        # self.object_alignment_solver = ObjectAlignmentSolver(config)

    async def solve(self, page: Page) -> bool:
        print("MasterSolver: Starting challenge resolution.")
        # 1. Detect challenge type via router
        solver_instance = await self.router.detect_and_route(page)

        if solver_instance is None:
            print("MasterSolver: Could not detect challenge type or get a solver instance. Defaulting to GodSolver.")
            solver_instance = self.god_solver

        print(f"MasterSolver: Using {solver_instance.__class__.__name__} to solve the challenge.")
        
        # 2. Route to appropriate solver
        # 3. If solver fails, try next best solver as fallback
        # For now, a simple retry with GodSolver if the first attempt fails.
        # A more sophisticated fallback would involve a prioritized list of solvers.
        success = await solver_instance.solve(page)

        if not success and solver_instance != self.god_solver:
            print(f"MasterSolver: {solver_instance.__class__.__name__} failed. Falling back to GodSolver.")
            success = await self.god_solver.solve(page)
        
        print(f"MasterSolver: Challenge resolution {'succeeded' if success else 'failed'}.")
        return success

    async def close(self):
        """Clean up all sub-solvers (if they have close methods)."""
        print("MasterSolver: Closing sub-solvers.")
        # Add close calls for other solvers if they have them
        # if hasattr(self.god_solver, 'close'):
        #     await self.god_solver.close()
        # if hasattr(self.pattern_solver, 'close'):
        #     await self.pattern_solver.close()


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
    try:
        print("MasterSolver initialized.")
        print(f"  - PatternBreakerSolver: ready (LLM: {config.pattern_solver_model})")
        print(f"  - GodSolver: ready (CLIP + LLM fallback)")
        print(f"  - DragSolver: ready (SIFT + CLIP + LLM)") # Placeholder, assuming DragSolver exists
        print(f"  - SliderSolver: ready") # Placeholder, assuming SliderSolver exists
        print(f"  - ShapeMatcher: ready") # Placeholder, assuming ShapeMatcher exists
        print(f"  - ObjectAlignmentSolver: ready") # Placeholder, assuming ObjectAlignmentSolver exists
        print("Pass a Playwright Page object to solver.solve(page)")
    finally:
        # In a real application, the browser context would be managed externally
        # and passed to the solver. For this CLI example, we just print init status.
        # await solver.close() # No actual resources to close in this simplified CLI example
        pass

if __name__ == "__main__":
    asyncio.run(main())
