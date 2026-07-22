
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
    Includes overshoot, micro-corrections, variable speed, and hesitation.
    """

    @staticmethod
    def _minimum_jerk(t: float) -> float:
        """Minimum jerk trajectory (smooth human-like velocity profile)."""
        return 10 * t**3 - 15 * t**4 + 6 * t**5

    @staticmethod
    def _generate_path(start_x, start_y, end_x, end_y) -> List[Tuple[float, float]]:
        """Generate a human-like path with overshoot and correction."""
        distance = math.sqrt((end_x - start_x)**2 + (end_y - start_y)**2)

        # Number of points scales with distance
        num_points = max(15, min(60, int(distance / 5)))

        # Overshoot (humans overshoot on fast movements)
        overshoot_amount = random.uniform(0, min(12, distance * 0.08))
        overshoot_angle = math.atan2(end_y - start_y, end_x - start_x)
        overshoot_x = end_x + overshoot_amount * math.cos(overshoot_angle)
        overshoot_y = end_y + overshoot_amount * math.sin(overshoot_angle)

        # Control points for cubic bezier (adds natural curve)
        # Humans don't move in straight lines
        perpendicular = overshoot_angle + math.pi / 2
        curve_amount = random.uniform(-distance * 0.15, distance * 0.15)
        mid_x = (start_x + end_x) / 2 + curve_amount * math.cos(perpendicular)
        mid_y = (start_y + end_y) / 2 + curve_amount * math.sin(perpendicular)

        path = []

        # Main movement (with overshoot)
        overshoot_point = int(num_points * random.uniform(0.75, 0.9))
        for i in range(num_points):
            t = i / (num_points - 1)
            jerk_t = HumanMouse._minimum_jerk(t)

            if i < overshoot_point:
                # Moving toward overshoot target
                prog = i / overshoot_point
                jerk_prog = HumanMouse._minimum_jerk(prog)
                # Quadratic bezier through midpoint to overshoot
                bx = (1-jerk_prog)**2 * start_x + 2*(1-jerk_prog)*jerk_prog * mid_x + jerk_prog**2 * overshoot_x
                by = (1-jerk_prog)**2 * start_y + 2*(1-jerk_prog)*jerk_prog * mid_y + jerk_prog**2 * overshoot_y
            else:
                # Correcting from overshoot to target
                correction_prog = (i - overshoot_point) / (num_points - overshoot_point - 1)
                correction_prog = min(1.0, correction_prog)
                bx = overshoot_x + (end_x - overshoot_x) * HumanMouse._minimum_jerk(correction_prog)
                by = overshoot_y + (end_y - overshoot_y) * HumanMouse._minimum_jerk(correction_prog)

            # Add micro-tremor (decreases as we approach target)
            tremor_scale = max(0, 1.0 - t) * 1.5
            bx += random.gauss(0, tremor_scale)
            by += random.gauss(0, tremor_scale)

            path.append((bx, by))

        return path

    @staticmethod
    async def move_and_click(page: Page, target_x: float, target_y: float,
                             start_x: float = None, start_y: float = None):
        """Move mouse along human-like path and click."""
        if start_x is None:
            start_x = target_x + random.uniform(-200, 200)
        if start_y is None:
            start_y = target_y + random.uniform(-200, 200)

        path = HumanMouse._generate_path(start_x, start_y, target_x, target_y)

        # Variable speed: faster in middle, slower at start/end
        for i, (x, y) in enumerate(path):
            progress = i / len(path)
            # Bell-curve speed: slow-fast-slow
            speed_factor = math.sin(progress * math.pi)
            base_delay = random.uniform(0.004, 0.012)
            delay = base_delay / max(speed_factor, 0.3)

            await page.mouse.move(x, y)
            await asyncio.sleep(delay)

        # Small pause before click (human reaction time)
        await asyncio.sleep(random.uniform(0.03, 0.12))

        # Click with variable hold time
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.04, 0.11))
        await page.mouse.up()

        # Occasional post-click micro-movement (humans don't freeze after clicking)
        if random.random() < 0.3:
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
        try:
            iframe = self.page.frame_locator('iframe[src*="hcaptcha.com/captcha"]')
            if await iframe.locator('.challenge-image').count() > 0:
                return True
        except:
            pass
        return False

    async def is_solved(self) -> bool:
        try:
            # Check if the hcaptcha response textarea has a value
            response = await self.page.evaluate("""
                () => {
                    const textarea = document.querySelector('textarea[name="h-captcha-response"]');
                    return textarea && textarea.value && textarea.value.length > 0;
                }
            """)
            if response:
                return True

            # Check if the checkbox iframe shows solved state
            checkbox_frame = self.page.frame_locator('iframe[src*="hcaptcha.com/hcaptcha"]')
            if await checkbox_frame.locator('[aria-checked="true"]').count() > 0:
                return True
        except:
            pass
        return False


# =============================================================================
# BASE SOLVER
# =============================================================================

class CaptchaSolver:
    def __init__(self, config: SolverConfig):
        self.config = config
        self.clip_model = None
        self.rate_limit_last_action = 0

    async def _apply_rate_limit(self):
        now = time.time()
        elapsed = now - self.rate_limit_last_action
        delay = random.uniform(self.config.rate_limit_min_delay, self.config.rate_limit_max_delay)
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self.rate_limit_last_action = time.time()

    async def _get_clip_model(self):
        if self.clip_model is None:
            self.clip_model = await ClipModel.get_instance()
        return self.clip_model

    async def solve(self, page: Page) -> bool:
        raise NotImplementedError

    async def close(self):
        pass


# =============================================================================
# PLAYWRIGHT SOLVER (Browser Management)
# =============================================================================

class PlaywrightSolver(CaptchaSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.browser = None
        self.context: Optional[BrowserContext] = None
        self._pw = None

    async def _launch_browser(self):
        self._pw = await async_playwright().start()

        # Get the actual browser version to match UA
        browser_type = getattr(self._pw, self.config.browser_type)
        self.browser = await browser_type.launch(
            headless=self.config.headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-background-networking',
                '--disable-default-apps',
                '--disable-extensions',
                '--disable-sync',
                '--disable-translate',
                '--metrics-recording-only',
                '--no-first-run',
            ]
        )

        # Use the ACTUAL browser version in the UA string
        version = self.browser.version
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"

        self.context = await self.browser.new_context(
            user_agent=ua,
            viewport={'width': 1920, 'height': 1080},
            screen={'width': 1920, 'height': 1080},
            locale='en-US',
            timezone_id='America/New_York',
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9',
                'sec-ch-ua': f'"Not_A Brand";v="8", "Chromium";v="{version.split(".")[0]}", "Google Chrome";v="{version.split(".")[0]}"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
            }
        )

        # Apply stealth BEFORE any navigation
        await self.context.add_init_script(STEALTH_SCRIPT)

    async def new_page(self) -> Page:
        if self.browser is None:
            await self._launch_browser()
        return await self.context.new_page()

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()


# =============================================================================
# SLIDER SOLVER
# =============================================================================

class SliderSolver:
    """
    Multi-method slider/puzzle CAPTCHA solver.
    Uses template matching, Canny edge detection, SIFT features, and
    difference-based gap detection with consensus voting.
    """

    def __init__(self):
        self._last_offset = 0

    def solve(self, puzzle_image, background_image) -> int:
        """
        Calculate slider offset using multiple methods and pick best via median.
        """
        puzzle = self._load_image(puzzle_image)
        bg = self._load_image(background_image)

        if puzzle is None or bg is None:
            return 0

        offsets = []

        offset_tm = self._template_match(puzzle, bg)
        if offset_tm > 0:
            offsets.append(offset_tm)

        offset_canny = self._canny_gap_detection(puzzle, bg)
        if offset_canny > 0:
            offsets.append(offset_canny)

        offset_sift = self._sift_match(puzzle, bg)
        if offset_sift > 0:
            offsets.append(offset_sift)

        offset_diff = self._difference_detection(puzzle, bg)
        if offset_diff > 0:
            offsets.append(offset_diff)

        if not offsets:
            return 0

        offsets.sort()
        self._last_offset = offsets[len(offsets) // 2]
        return self._last_offset

    def _load_image(self, image):
        if isinstance(image, bytes):
            nparr = np.frombuffer(image, np.uint8)
            return cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(image, str):
            return cv2.imread(image)
        elif isinstance(image, np.ndarray):
            return image
        elif hasattr(image, 'convert'):  # PIL Image
            return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        return None

    def _template_match(self, puzzle: np.ndarray, bg: np.ndarray) -> int:
        """Edge-based template matching with multiple methods."""
        try:
            puzzle_gray = cv2.cvtColor(puzzle, cv2.COLOR_BGR2GRAY)
            bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)

            puzzle_edges = cv2.Canny(puzzle_gray, 100, 200)
            bg_edges = cv2.Canny(bg_gray, 100, 200)

            kernel = np.ones((3, 3), np.uint8)
            puzzle_edges = cv2.dilate(puzzle_edges, kernel, iterations=1)
            bg_edges = cv2.dilate(bg_edges, kernel, iterations=1)

            best_offset = 0
            best_score = 0

            for method in [cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR_NORMED]:
                result = cv2.matchTemplate(bg_edges, puzzle_edges, method)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val > best_score:
                    best_score = max_val
                    best_offset = max_loc[0]

            return best_offset if best_score > 0.3 else 0
        except Exception:
            return 0

    def _canny_gap_detection(self, puzzle: np.ndarray, bg: np.ndarray) -> int:
        """Detect gap using contour analysis on edges."""
        try:
            bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
            puzzle_gray = cv2.cvtColor(puzzle, cv2.COLOR_BGR2GRAY)

            edges = cv2.Canny(bg_gray, 50, 150)
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            puzzle_h, puzzle_w = puzzle_gray.shape[:2]
            target_area = puzzle_h * puzzle_w

            best_match = None
            best_area_diff = float('inf')

            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                area = w * h
                area_diff = abs(area - target_area)

                if (0.5 < w / max(puzzle_w, 1) < 2.0 and
                    0.5 < h / max(puzzle_h, 1) < 2.0 and
                    area_diff < best_area_diff):
                    best_area_diff = area_diff
                    best_match = x

            return best_match if best_match is not None else 0
        except Exception:
            return 0

    def _sift_match(self, puzzle: np.ndarray, bg: np.ndarray) -> int:
        """SIFT feature matching for precise offset."""
        try:
            sift = cv2.SIFT_create(nfeatures=500)
            puzzle_gray = cv2.cvtColor(puzzle, cv2.COLOR_BGR2GRAY)
            bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)

            kp1, des1 = sift.detectAndCompute(puzzle_gray, None)
            kp2, des2 = sift.detectAndCompute(bg_gray, None)

            if des1 is None or des2 is None or len(des1) < 4 or len(des2) < 4:
                return 0

            bf = cv2.BFMatcher()
            matches = bf.knnMatch(des1, des2, k=2)
            good_matches = [m for m, n in matches if m.distance < 0.65 * n.distance]

            if len(good_matches) < 4:
                return 0

            src_pts = np.float32([kp1[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp2[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

            M, _ = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if M is None:
                return 0

            h, w = puzzle_gray.shape
            corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
            transformed = cv2.perspectiveTransform(corners, M)
            return max(0, int(np.mean(transformed[:, 0, 0])))
        except Exception:
            return 0

    def _difference_detection(self, puzzle: np.ndarray, bg: np.ndarray) -> int:
        """Detect gap by column-wise brightness difference."""
        try:
            bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY).astype(np.float32)
            blur = cv2.GaussianBlur(bg_gray, (15, 15), 0)
            diff = cv2.absdiff(bg_gray, blur)

            col_sums = np.sum(diff, axis=0)
            puzzle_w = puzzle.shape[1]
            kernel_size = max(2, puzzle_w // 2)
            smoothed = np.convolve(col_sums, np.ones(kernel_size) / kernel_size, mode='same')
            return int(np.argmax(smoothed))
        except Exception:
            return 0

    async def solve_on_page(self, page: Page, slider_sel: str = '.slider',
                            track_sel: str = '.slider-track') -> bool:
        """Solve slider captcha on a page with human-like drag."""
        try:
            slider = page.locator(slider_sel)
            track = page.locator(track_sel)

            if await slider.count() == 0 or await track.count() == 0:
                return False

            slider_box = await slider.bounding_box()
            track_box = await track.bounding_box()
            if not slider_box or not track_box:
                return False

            # Try to get puzzle/bg images for offset calculation
            puzzle_el = page.locator('.puzzle-piece, .slider-puzzle, img[class*="puzzle"]')
            bg_el = page.locator('.slider-bg, .captcha-bg, img[class*="bg"]')

            if await puzzle_el.count() > 0 and await bg_el.count() > 0:
                puzzle_bytes = await puzzle_el.screenshot()
                bg_bytes = await bg_el.screenshot()
                offset = self.solve(puzzle_bytes, bg_bytes)
            else:
                offset = int(track_box['width'] * random.uniform(0.4, 0.6))

            # Human-like drag
            start_x = slider_box['x'] + slider_box['width'] / 2
            start_y = slider_box['y'] + slider_box['height'] / 2
            end_x = start_x + offset

            await page.mouse.move(start_x, start_y)
            await asyncio.sleep(random.uniform(0.15, 0.4))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.15))

            # Drag with ease-in-out + overshoot + correction
            steps = random.randint(25, 50)
            overshoot = random.uniform(5, 18)

            for i in range(steps):
                progress = i / steps
                # Ease-in-out
                if progress < 0.5:
                    eased = 2 * progress * progress
                else:
                    eased = 1 - (-2 * progress + 2) ** 2 / 2

                # Overshoot near end
                if progress > 0.8:
                    overshoot_decay = (progress - 0.8) / 0.2
                    current_x = start_x + (offset + overshoot) * eased - overshoot * overshoot_decay
                else:
                    current_x = start_x + (offset + overshoot) * eased

                wobble_y = start_y + random.gauss(0, 1.5)
                await page.mouse.move(current_x, wobble_y)

                # Variable speed
                speed = math.sin(progress * math.pi)
                await asyncio.sleep(random.uniform(0.006, 0.018) / max(speed, 0.3))

            # Final correction
            await page.mouse.move(end_x, start_y + random.uniform(-1, 1))
            await asyncio.sleep(random.uniform(0.08, 0.2))
            await page.mouse.up()
            await asyncio.sleep(random.uniform(0.3, 0.8))

            return True
        except Exception as e:
            print(f"Slider solve error: {e}")
            return False


# =============================================================================
# SHAPE MATCHER
# =============================================================================

class ShapeMatcher:
    """
    Multi-method shape matching using contour analysis, Hu moments,
    Fourier descriptors, aspect ratio, solidity, and circularity.
    """

    def match_shapes(self, target_shape: np.ndarray, candidates: List[np.ndarray],
                     threshold: float = 0.65) -> List[int]:
        """Find matching shapes. Returns indices sorted by confidence."""
        target_gray = cv2.cvtColor(target_shape, cv2.COLOR_BGR2GRAY)
        _, target_thresh = cv2.threshold(target_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        target_contours, _ = cv2.findContours(target_thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not target_contours:
            return []

        target_contour = max(target_contours, key=cv2.contourArea)

        scores = []
        for i, candidate in enumerate(candidates):
            score = self._compute_similarity(target_contour, target_thresh, candidate)
            scores.append((i, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [idx for idx, score in scores if score >= threshold]

    def _compute_similarity(self, target_contour, target_thresh, candidate: np.ndarray) -> float:
        """Multi-method ensemble similarity."""
        try:
            cand_gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
            _, cand_thresh = cv2.threshold(cand_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            cand_contours, _ = cv2.findContours(cand_thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if not cand_contours:
                return 0.0

            cand_contour = max(cand_contours, key=cv2.contourArea)

            # Method 1: Hu moments matching
            hu_dist = cv2.matchShapes(target_contour, cand_contour, cv2.CONTOURS_MATCH_I1, 0)
            hu_score = 1.0 / (1.0 + hu_dist)

            # Method 2: Aspect ratio
            t_rect = cv2.minAreaRect(target_contour)
            c_rect = cv2.minAreaRect(cand_contour)
            t_aspect = min(t_rect[1]) / max(max(t_rect[1]), 1)
            c_aspect = min(c_rect[1]) / max(max(c_rect[1]), 1)
            aspect_score = 1.0 - abs(t_aspect - c_aspect)

            # Method 3: Solidity
            t_area = cv2.contourArea(target_contour)
            t_hull_area = cv2.contourArea(cv2.convexHull(target_contour))
            t_solidity = t_area / max(t_hull_area, 1)

            c_area = cv2.contourArea(cand_contour)
            c_hull_area = cv2.contourArea(cv2.convexHull(cand_contour))
            c_solidity = c_area / max(c_hull_area, 1)
            solidity_score = 1.0 - abs(t_solidity - c_solidity)

            # Method 4: Circularity
            t_perim = cv2.arcLength(target_contour, True)
            t_circ = 4 * math.pi * t_area / max(t_perim ** 2, 1)
            c_perim = cv2.arcLength(cand_contour, True)
            c_circ = 4 * math.pi * c_area / max(c_perim ** 2, 1)
            circ_score = 1.0 - abs(t_circ - c_circ)

            # Method 5: Fourier descriptors
            fourier_score = self._fourier_similarity(target_contour, cand_contour)

            # Weighted ensemble
            return (0.30 * hu_score + 0.15 * aspect_score + 0.15 * solidity_score +
                    0.15 * circ_score + 0.25 * fourier_score)
        except Exception:
            return 0.0

    def _fourier_similarity(self, c1, c2, n: int = 15) -> float:
        """Rotation/scale invariant shape comparison via Fourier descriptors."""
        try:
            fd1 = self._fourier_descriptors(c1, n)
            fd2 = self._fourier_descriptors(c2, n)
            if fd1 is None or fd2 is None:
                return 0.0
            fd1 = fd1 / max(np.abs(fd1[0]), 1e-10)
            fd2 = fd2 / max(np.abs(fd2[0]), 1e-10)
            return 1.0 / (1.0 + np.sum(np.abs(fd1 - fd2)))
        except Exception:
            return 0.0

    def _fourier_descriptors(self, contour, n: int):
        try:
            pts = contour.reshape(-1, 2).astype(np.float64)
            complex_pts = pts[:, 0] + 1j * pts[:, 1]
            fourier = np.fft.fft(complex_pts)
            return np.abs(fourier[1:n + 1])
        except Exception:
            return None

    def match_hu_moments(self, target_shape: np.ndarray, candidates: List[np.ndarray],
                         threshold: float = 0.05) -> List[int]:
        """Match using log-transformed Hu moments."""
        target_gray = cv2.cvtColor(target_shape, cv2.COLOR_BGR2GRAY)
        _, target_thresh = cv2.threshold(target_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        target_contours, _ = cv2.findContours(target_thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not target_contours:
            return []

        target_contour = max(target_contours, key=cv2.contourArea)
        target_hu = cv2.HuMoments(cv2.moments(target_contour)).flatten()
        target_log = -np.sign(target_hu) * np.log10(np.abs(target_hu) + 1e-10)

        matches = []
        for i, candidate in enumerate(candidates):
            try:
                cand_gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
                _, cand_thresh = cv2.threshold(cand_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                cand_contours, _ = cv2.findContours(cand_thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if not cand_contours:
                    continue
                cand_contour = max(cand_contours, key=cv2.contourArea)
                cand_hu = cv2.HuMoments(cv2.moments(cand_contour)).flatten()
                cand_log = -np.sign(cand_hu) * np.log10(np.abs(cand_hu) + 1e-10)
                if np.sum(np.abs(target_log - cand_log)) < threshold:
                    matches.append(i)
            except Exception:
                continue
        return matches

    async def solve_on_page(self, page: Page, target_sel: str, candidate_sel: str) -> List[int]:
        """Solve shape matching on a page."""
        try:
            target_el = page.locator(target_sel)
            if await target_el.count() == 0:
                return []
            target_bytes = await target_el.screenshot()
            target_img = cv2.imdecode(np.frombuffer(target_bytes, np.uint8), cv2.IMREAD_COLOR)

            candidates_els = await page.locator(candidate_sel).all()
            candidate_imgs = []
            for el in candidates_els:
                cand_bytes = await el.screenshot()
                candidate_imgs.append(cv2.imdecode(np.frombuffer(cand_bytes, np.uint8), cv2.IMREAD_COLOR))

            return self.match_shapes(target_img, candidate_imgs)
        except Exception as e:
            print(f"Shape solve error: {e}")
            return []


# =============================================================================
# OBJECT ALIGNMENT SOLVER
# =============================================================================

class ObjectAlignmentSolver:
    """
    Solve alignment/rotation puzzles using SIFT feature matching.
    Finds position and rotation angle.
    """

    def __init__(self):
        self.sift = cv2.SIFT_create(nfeatures=1000)
        self.bf = cv2.BFMatcher()

    def find_alignment(self, object_image: np.ndarray, background_image: np.ndarray) -> Tuple[int, int]:
        """Find (x, y) position to place object in background."""
        try:
            obj_gray = cv2.cvtColor(object_image, cv2.COLOR_BGR2GRAY)
            bg_gray = cv2.cvtColor(background_image, cv2.COLOR_BGR2GRAY)

            kp1, des1 = self.sift.detectAndCompute(obj_gray, None)
            kp2, des2 = self.sift.detectAndCompute(bg_gray, None)

            if des1 is None or des2 is None or len(des1) < 4 or len(des2) < 4:
                return 0, 0

            matches = self.bf.knnMatch(des1, des2, k=2)
            good = [m for m, n in matches if m.distance < 0.65 * n.distance]

            if len(good) < 4:
                return 0, 0

            src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

            M, _ = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if M is None:
                return 0, 0

            h, w = obj_gray.shape
            corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
            transformed = cv2.perspectiveTransform(corners, M)

            return int(np.mean(transformed[:, 0, 0])), int(np.mean(transformed[:, 0, 1]))
        except Exception:
            return 0, 0

    def find_rotation_angle(self, object_image: np.ndarray, background_image: np.ndarray) -> float:
        """Find rotation angle in degrees to align object with background."""
        try:
            obj_gray = cv2.cvtColor(object_image, cv2.COLOR_BGR2GRAY)
            bg_gray = cv2.cvtColor(background_image, cv2.COLOR_BGR2GRAY)

            kp1, des1 = self.sift.detectAndCompute(obj_gray, None)
            kp2, des2 = self.sift.detectAndCompute(bg_gray, None)

            if des1 is None or des2 is None:
                return 0.0

            matches = self.bf.knnMatch(des1, des2, k=2)
            good = [m for m, n in matches if m.distance < 0.65 * n.distance]

            if len(good) < 4:
                return 0.0

            src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

            M, _ = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if M is None:
                return 0.0

            return math.degrees(math.atan2(M[1, 0], M[0, 0]))
        except Exception:
            return 0.0

    async def solve_on_page(self, page: Page, obj_sel: str, bg_sel: str) -> Tuple[int, int]:
        """Solve alignment on a page."""
        try:
            obj_el = page.locator(obj_sel)
            bg_el = page.locator(bg_sel)
            if await obj_el.count() == 0 or await bg_el.count() == 0:
                return 0, 0

            obj_bytes = await obj_el.screenshot()
            bg_bytes = await bg_el.screenshot()
            obj_img = cv2.imdecode(np.frombuffer(obj_bytes, np.uint8), cv2.IMREAD_COLOR)
            bg_img = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)

            return self.find_alignment(obj_img, bg_img)
        except Exception as e:
            print(f"Alignment solve error: {e}")
            return 0, 0


# =============================================================================
# GOD SOLVER (Main Facade)
# =============================================================================

class GodSolver(CaptchaSolver):
    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self.playwright_solver = PlaywrightSolver(config)
        self.slider_solver = SliderSolver()
        self.shape_matcher = ShapeMatcher()
        self.alignment_solver = ObjectAlignmentSolver()
        self.alias_mapping = self._load_alias_mapping()
        self.negative_prompts = [
            "an empty background with nothing in it",
            "a photo of the sky",
            "a photo of the ground",
            "a photo of a plain wall",
            "a photo of nothing interesting",
            "a photo of pavement",
            "a photo of grass",
            "a photo of trees and foliage",
            "a photo of a road surface",
            "a blurry unclear image",
        ]

    def _load_alias_mapping(self) -> Dict[str, List[str]]:
        return defaultdict(list, {
            "car": ["automobile", "vehicle", "sedan", "coupe"],
            "bus": ["coach", "public transport", "school bus", "motorbus", "transit bus"],
            "truck": ["lorry", "pickup truck", "delivery truck", "semi truck"],
            "bicycle": ["bike", "mountain bike", "road bike", "cycle"],
            "motorcycle": ["motorbike", "scooter", "moped"],
            "traffic light": ["traffic signal", "stop light", "signal light"],
            "fire hydrant": ["hydrant", "fire plug"],
            "crosswalk": ["zebra crossing", "pedestrian crossing"],
            "bridge": ["overpass", "viaduct", "footbridge"],
            "boat": ["ship", "yacht", "ferry", "vessel", "sailboat"],
            "airplane": ["plane", "aircraft", "jet", "aeroplane"],
            "seaplane": ["floatplane", "flying boat", "amphibious aircraft"],
            "train": ["locomotive", "railway", "rail car"],
            "chimney": ["smokestack", "flue", "chimney stack"],
            "staircase": ["stairs", "stairway", "steps"],
            "tractor": ["farm vehicle", "agricultural machine"],
            "excavator": ["digger", "backhoe", "construction equipment"],
            "crane": ["tower crane", "construction crane"],
            "helicopter": ["chopper", "rotorcraft"],
            "palm tree": ["coconut tree", "tropical tree"],
            "lighthouse": ["beacon", "light tower"],
            "parking meter": ["meter", "pay station"],
            "stop sign": ["stop", "octagonal sign"],
            "swimming pool": ["pool", "swimming area"],
            "taxi": ["cab", "taxicab", "yellow cab"],
            "mountain": ["hill", "peak", "summit"],
            "river": ["stream", "creek", "waterway"],
            "lion": ["big cat", "feline"],
            "elephant": ["pachyderm"],
            "horse": ["stallion", "mare", "equine"],
            "dog": ["canine", "hound", "puppy"],
            "cat": ["feline", "kitten"],
            "bird": ["avian", "fowl"],
        })

    def _normalize_target(self, target: str) -> str:
        target = target.lower().strip()
        # Careful plural stripping: don't strip 's' from words like 'bus', 'grass'
        if target.endswith('ies'):
            target = target[:-3] + 'y'  # e.g. 'chimneys' -> 'chimney' (wrong), but 'berries' -> 'berry'
        elif target.endswith('es') and not target.endswith('ses'):
            target = target[:-2]  # 'buses' -> 'bus', 'boxes' -> 'box'
        elif target.endswith('s') and not target.endswith(('ss', 'us', 'is')):
            target = target[:-1]  # 'cars' -> 'car', but not 'bus', 'grass'
        return target

    def _get_prompts(self, target: str) -> List[str]:
        normalized = self._normalize_target(target)
        # Multiple prompt templates improve CLIP's zero-shot accuracy significantly
        prompts = [
            f"a photo of a {normalized}",
            f"a {normalized} in this image",
            f"a clear photo of a {normalized}",
            f"an image containing a {normalized}",
            f"a {normalized}",
        ]
        # Add aliases (each with 2 templates to keep batch reasonable)
        for alias in self.alias_mapping.get(normalized, [])[:3]:  # Max 3 aliases
            prompts.append(f"a photo of a {alias}")
            prompts.append(f"a {alias} in this image")
        return prompts

    async def _get_challenge_info(self, iframe) -> Tuple[str, List[Image.Image]]:
        """Extract target text and tile images from hCaptcha challenge."""
        target = ""
        try:
            challenge_text = await iframe.locator(".challenge-header .text").text_content()
            if not challenge_text:
                challenge_text = await iframe.locator(".challenge-header").text_content()

            # Multiple patterns for target extraction
            patterns = [
                r'(?:select|click|choose) all (?:images|squares|tiles) (?:with|containing|of|that contain) (?:a |an )?(.+?)(?:\.|$)',
                r'(?:select|click) (?:the |all )?(?:images|squares) (?:of |with )?(?:a |an )?(.+?)(?:\.|$)',
                r'Please click each image containing (?:a |an )?(.+?)(?:\.|$)',
            ]
            for pattern in patterns:
                match = re.search(pattern, challenge_text, re.IGNORECASE)
                if match:
                    target = match.group(1).strip()
                    break

            if not target and challenge_text:
                # Last resort: take everything after common prefixes
                cleaned = re.sub(r'^(please )?(select|click|choose) (all )?(images?|squares?|tiles?) (with|containing|of|that contain) (a |an )?', '', challenge_text, flags=re.IGNORECASE)
                target = cleaned.strip().rstrip('.')

            print(f"hCaptcha target: '{target}' (from: '{challenge_text}')")
        except Exception as e:
            print(f"Error getting challenge text: {e}")

        # Get tile images
        images = []
        try:
            tiles_data = await iframe.evaluate(r"""
                () => {
                    const tiles = Array.from(document.querySelectorAll('.task-image .image-wrapper .image, .challenge-image .image-wrapper .image'));
                    return tiles.map(tile => {
                        const style = window.getComputedStyle(tile);
                        const bg = style.backgroundImage;
                        if (bg && bg !== 'none') {
                            const m = bg.match(/url\("(.+?)"\)/);
                            if (m) return m[1];
                        }
                        const img = tile.querySelector('img');
                        if (img && img.src) return img.src;
                        return null;
                    }).filter(Boolean);
                }
            """)

            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as session:
                for url in tiles_data:
                    try:
                        if url.startswith('data:image/'):
                            _, encoded = url.split(',', 1)
                            img_bytes = base64.b64decode(encoded)
                            images.append(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
                        elif url.startswith('http'):
                            # Actually fetch the tile image from hCaptcha CDN
                            async with session.get(url, timeout=_aiohttp.ClientTimeout(total=5)) as resp:
                                if resp.status == 200:
                                    img_bytes = await resp.read()
                                    images.append(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
                                else:
                                    images.append(Image.new("RGB", (100, 100), color='gray'))
                        else:
                            images.append(Image.new("RGB", (100, 100), color='gray'))
                    except Exception:
                        images.append(Image.new("RGB", (100, 100), color='gray'))
        except Exception as e:
            print(f"Error getting tile images: {e}")

        return target, images

    async def _solve_grid_challenge(self, iframe, target: str, images: List[Image.Image]) -> List[int]:
        """CLIP-based grid solving with contrast scoring, multi-crop, and spatial context."""
        clip_model = await self._get_clip_model()

        positive_prompts = self._get_prompts(target)
        all_prompts = positive_prompts + self.negative_prompts

        text_features = await clip_model.get_text_features(all_prompts)
        pos_features = text_features[:len(positive_prompts)]
        neg_features = text_features[len(positive_prompts):]

        # === Multi-crop: score each tile with 3 views (full, center crop, padded) ===
        all_crops = []
        for img in images:
            # Full tile
            all_crops.append(img)
            # Center crop (70% of tile - focuses on main object)
            w, h = img.size
            margin_w, margin_h = int(w * 0.15), int(h * 0.15)
            center_crop = img.crop((margin_w, margin_h, w - margin_w, h - margin_h))
            all_crops.append(center_crop)
            # Padded (add context border - helps with partial objects)
            padded = Image.new("RGB", (int(w * 1.2), int(h * 1.2)), (128, 128, 128))
            padded.paste(img, (int(w * 0.1), int(h * 0.1)))
            all_crops.append(padded)

        # Batch all crops in one forward pass
        all_image_features = await clip_model.get_image_features(all_crops)

        # Average features across 3 crops per tile
        num_tiles = len(images)
        image_features = torch.zeros(num_tiles, all_image_features.shape[1], device=all_image_features.device)
        for i in range(num_tiles):
            # Weighted average: full=0.5, center=0.3, padded=0.2
            image_features[i] = (
                0.5 * all_image_features[i * 3] +
                0.3 * all_image_features[i * 3 + 1] +
                0.2 * all_image_features[i * 3 + 2]
            )
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)

        # Contrast scoring: positive similarity minus max negative similarity
        pos_sim = (image_features @ pos_features.T).mean(dim=1)
        neg_sim = (image_features @ neg_features.T).max(dim=1).values
        scores = pos_sim - neg_sim

        # === Spatial context: boost tiles whose neighbors also score high ===
        # hCaptcha grids are typically 3x3 or 4x4
        grid_size = int(math.sqrt(num_tiles))
        if grid_size * grid_size == num_tiles and grid_size >= 3:
            spatial_boost = torch.zeros_like(scores)
            for i in range(num_tiles):
                row, col = i // grid_size, i % grid_size
                neighbor_scores = []
                for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    nr, nc = row + dr, col + dc
                    if 0 <= nr < grid_size and 0 <= nc < grid_size:
                        neighbor_scores.append(scores[nr * grid_size + nc].item())
                if neighbor_scores:
                    # If neighbors score high, boost this tile
                    avg_neighbor = sum(neighbor_scores) / len(neighbor_scores)
                    spatial_boost[i] = avg_neighbor * 0.15  # 15% neighbor influence
            scores = scores + spatial_boost

        # === Adaptive thresholding ===
        if len(scores) == 0:
            return []

        max_s = scores.max().item()
        min_s = scores.min().item()
        score_range = max_s - min_s

        if max_s < 0.25:
            # All very low: pick top 3 (likely wrong target parsing)
            indices = torch.topk(scores, min(3, len(scores))).indices.tolist()
        elif score_range > 0.12:
            # Bimodal: find the largest gap in sorted scores
            sorted_scores, sorted_idx = torch.sort(scores)
            gaps = sorted_scores[1:] - sorted_scores[:-1]
            # Only consider gaps in the middle 60% of the range
            valid_start = max(1, len(gaps) // 5)
            valid_end = min(len(gaps) - 1, len(gaps) * 4 // 5)
            if valid_end > valid_start:
                best_gap_idx = valid_start + torch.argmax(gaps[valid_start:valid_end]).item()
            else:
                best_gap_idx = torch.argmax(gaps).item()
            threshold = (sorted_scores[best_gap_idx].item() + sorted_scores[best_gap_idx + 1].item()) / 2
            indices = [i for i, s in enumerate(scores) if s.item() > threshold]
        else:
            # Narrow range: use percentile-based threshold (top 40%)
            k = max(2, int(num_tiles * 0.4))
            top_k = torch.topk(scores, k)
            threshold = top_k.values[-1].item()
            indices = [i for i, s in enumerate(scores) if s.item() >= threshold]

        # hCaptcha almost always expects 2-6 tiles selected
        if len(indices) < 2:
            # Add next best tile
            indices = torch.topk(scores, 2).indices.tolist()
        elif len(indices) > 6:
            indices = torch.topk(scores, 6).indices.tolist()

        print(f"Scores: {[f'{s:.3f}' for s in scores.tolist()]}")
        print(f"Selected {len(indices)} tiles: {indices}")

        return indices

    async def _click_tiles(self, iframe, tile_indices: List[int], page: Page):
        """Click tiles with human-like mouse movement."""
        tiles = await iframe.locator(".task-image .image-wrapper .image, .challenge-image .image-wrapper .image").all()

        # Random order (humans don't always click left-to-right)
        click_order = list(tile_indices)
        if random.random() < 0.4:
            random.shuffle(click_order)

        last_x, last_y = random.uniform(400, 600), random.uniform(300, 500)

        for idx in click_order:
            if idx < len(tiles):
                box = await tiles[idx].bounding_box()
                if box:
                    # Random point within tile (not always center)
                    tx = box["x"] + random.uniform(box["width"] * 0.2, box["width"] * 0.8)
                    ty = box["y"] + random.uniform(box["height"] * 0.2, box["height"] * 0.8)

                    await HumanMouse.move_and_click(page, tx, ty, last_x, last_y)
                    last_x, last_y = tx, ty

                    # Variable inter-click delay (humans hesitate, sometimes fast)
                    if random.random() < 0.15:
                        # Hesitation
                        await asyncio.sleep(random.uniform(0.4, 1.2))
                    else:
                        await asyncio.sleep(random.uniform(0.08, 0.35))

    async def solve(self, page: Page) -> bool:
        """Main solve loop."""
        start_time = time.time()
        detector = ChallengeDetector(page)

        for round_num in range(self.config.max_challenge_rounds):
            print(f"hCaptcha round {round_num + 1}/{self.config.max_challenge_rounds}")
            round_start = time.time()

            # Check if already solved
            if await detector.is_solved():
                print("Already solved!")
                return True

            # Find hCaptcha iframe
            try:
                await page.wait_for_selector(
                    'iframe[src*="hcaptcha.com/captcha"], iframe[title*="hCaptcha"]',
                    timeout=self.config.timeout * 1000
                )
                # Get the frame
                frames = page.frames
                iframe = None
                for f in frames:
                    if 'hcaptcha.com' in (f.url or ''):
                        iframe = f
                        break

                if not iframe:
                    # Try frame_locator approach
                    fl = page.frame_locator('iframe[src*="hcaptcha.com/captcha"]')
                    # Use the locator directly
                    iframe = fl

                # Wait for challenge to load
                await asyncio.sleep(1.5)

            except Exception as e:
                print(f"Could not find hCaptcha iframe: {e}")
                if await detector.is_solved():
                    return True
                return False

            # Get challenge info
            target, images = await self._get_challenge_info(iframe)
            if not target or not images:
                print("Failed to get challenge info, retrying...")
                await asyncio.sleep(1)
                continue

            # Solve
            selected = await self._solve_grid_challenge(iframe, target, images)
            print(f"Selected tiles: {selected}")

            if selected:
                await self._click_tiles(iframe, selected, page)

                # Click verify
                await asyncio.sleep(random.uniform(0.3, 0.8))
                try:
                    verify_btn = iframe.locator(".verify-button, .button-submit")
                    if await verify_btn.count() > 0:
                        box = await verify_btn.bounding_box()
                        if box:
                            await HumanMouse.move_and_click(
                                page,
                                box['x'] + box['width'] / 2,
                                box['y'] + box['height'] / 2
                            )
                        else:
                            await verify_btn.click()
                        print("Clicked verify.")
                except Exception as e:
                    print(f"Verify click error: {e}")

            # Enforce minimum solve time
            elapsed = time.time() - round_start
            if elapsed < self.config.min_solve_time_per_round:
                await asyncio.sleep(self.config.min_solve_time_per_round - elapsed)

            # Check result
            await asyncio.sleep(1.5)
            if await detector.is_solved():
                print("hCaptcha solved!")
                return True

        print("Failed after max rounds.")
        return False

    async def close(self):
        await self.playwright_solver.close()


# =============================================================================
# SELENIUM SOLVER (Placeholder)
# =============================================================================

class SeleniumSolver(CaptchaSolver):
    async def solve(self, page) -> bool:
        print("SeleniumSolver not implemented.")
        return False


# =============================================================================
# CLI
# =============================================================================

async def main():
    config = SolverConfig(
        headless=False,
        clip_confidence_threshold=0.55,
        max_challenge_rounds=3,
        timeout=30,
    )
    solver = GodSolver(config)
    try:
        print("GodSolver initialized.")
        print(f"  - SliderSolver: ready")
        print(f"  - ShapeMatcher: ready")
        print(f"  - AlignmentSolver: ready")
        print(f"  - CLIP model: will load on first use")
        print("Pass a Playwright Page object to solver.solve(page)")
    finally:
        await solver.close()


if __name__ == "__main__":
    asyncio.run(main())
