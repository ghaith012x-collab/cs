"""
Stealth browser automation for CAPTCHA solving.
Uses Playwright with advanced anti-detection techniques.
"""

import asyncio
import random
import time
from typing import Optional, List, Dict, Tuple, Union
from pathlib import Path
import json
import base64

from playwright.async_api import async_playwright, Browser, Page, BrowserContext


class StealthBrowser:
    """
    Stealth browser automation with advanced anti-detection.
    Uses Playwright with extensive fingerprint spoofing.
    """
    
    def __init__(
        self,
        headless: bool = True,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        viewport: Tuple[int, int] = (1920, 1080),
    ):
        self.headless = headless
        self.proxy = proxy
        self.user_agent = user_agent or self._get_random_ua()
        self.viewport = viewport
        
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
    
    def _get_random_ua(self) -> str:
        """Get a realistic random user agent."""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7_12) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
        ]
        return random.choice(user_agents)
    
    async def start(self) -> None:
        """Start the stealth browser."""
        playwright = await async_playwright().start()
        
        browser_kwargs = {"headless": self.headless}
        if self.proxy:
            browser_kwargs["proxy"] = {"server": self.proxy}
        
        self._browser = await playwright.chromium.launch(**browser_kwargs)
        self._context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
            locale="en-US",
            timezone_id="America/New_York",
        )
        
        await self._apply_stealth(self._context)
        self._page = await self._context.new_page()
    
    async def _apply_stealth(self, context: BrowserContext) -> None:
        """Apply stealth techniques to browser context."""
        await context.add_init_script("""
            // Override webdriver property
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            
            // Override plugins array
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            
            // Override languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            
            // Mock Chrome properties
            Object.defineProperty(navigator, 'chrome', {
                get: () => ({
                    runtime: {},
                    autocomplete: {},
                }),
            });
            
            // Override WebGL vendor/renderer
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                const vendorMap = {
                    37445: 'Intel Inc.',
                    37446: 'Intel Iris OpenGL Engine',
                };
                return vendorMap[parameter] || getParameter.call(this, parameter);
            };
            
            // Override canvas fingerprint
            const toDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function() {
                return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
            };
            
            // Override WebGL debug renderer info
            const getExtension = WebGLRenderingContext.prototype.getExtension;
            WebGLRenderingContext.prototype.getExtension = function(name) {
                if (name === 'WEBGL_debug_renderer_info') {
                    return {
                        UNMASKED_VENDOR_WEBGL: 37445,
                        UNMASKED_RENDERER_WEBGL: 37446,
                    };
                }
                return getExtension.call(this, name);
            };
        """)
        
        await context.add_init_script("""
            // Spoof touch capabilities
            window.navigator.__defineGetter__('maxTouchPoints', () => 5);
            
            // Override audio context fingerprint
            const originalDecode = AudioContext.prototype.decodeAudioData;
            AudioContext.prototype.decodeAudioData = function() {
                return Promise.resolve();
            };
            
            // Spoof media devices
            navigator.mediaDevices = {
                enumerateDevices: () => Promise.resolve([]),
                getDisplayMedia: () => Promise.reject(new Error('Not allowed')),
                getUserMedia: () => Promise.reject(new Error('Not allowed')),
            };
        """)
    
    async def goto(self, url: str) -> None:
        """Navigate to URL."""
        await self._page.goto(url, wait_until="domcontentloaded")
    
    async def wait_for_selector(self, selector: str, timeout: int = 30000) -> None:
        """Wait for selector to appear."""
        await self._page.wait_for_selector(selector, timeout=timeout)
    
    async def click(self, selector: str) -> None:
        """Click element with human-like movement."""
        await self._page.hover(selector)
        await self._page.mouse.move(
            random.randint(100, 500),
            random.randint(100, 500),
            steps=random.randint(10, 30)
        )
        await self._page.click(selector)
    
    async def evaluate(self, script: str):
        """Execute JavaScript in page context."""
        return await self._page.evaluate(script)
    
    async def screenshot_element(self, selector: str) -> bytes:
        """Screenshot an element."""
        element = await self._page.query_selector(selector)
        return await element.screenshot()
    
    async def get_element_src(self, selector: str) -> Optional[str]:
        """Get src attribute of element."""
        return await self._page.get_attribute(selector, "src")
    
    async def get_images_from_page(self) -> List[Dict]:
        """Get all images from page with their URLs."""
        images = await self._page.evaluate('''
            () => {
                const imgs = document.querySelectorAll('img');
                return Array.from(imgs).map(img => ({
                    src: img.src,
                    alt: img.alt,
                    naturalWidth: img.naturalWidth,
                    naturalHeight: img.naturalHeight,
                }));
            }
        ''')
        return images
    
    async def extract_image_urls(self, container_selector: str) -> List[str]:
        """Extract direct image URLs from container."""
        images = await self._page.evaluate(f'''
            (selector) => {{
                const container = document.querySelector(selector);
                if (!container) return [];
                const imgs = container.querySelectorAll('img');
                return Array.from(imgs).map(img => img.src);
            }}
        ''', container_selector)
        return images
    
    async def fetch_image(self, url: str) -> bytes:
        """Fetch image directly via HTTP."""
        response = await self._page.request.get(url)
        return response.body()
    
    async def close(self) -> None:
        """Close browser."""
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()


class HCaptchaPage:
    """
    Specialized handler for hCaptcha pages.
    Provides methods for CAPTCHA-specific operations.
    """
    
    def __init__(self, browser: StealthBrowser):
        self.browser = browser
        self._page = browser._page
    
    async def solve_checkbox(self) -> bool:
        """Solve hCaptcha checkbox challenge."""
        try:
            await self.browser.wait_for_selector("#captcha-checkbox")
            await self.browser.click("#captcha-checkbox")
            
            await asyncio.sleep(random.uniform(1, 3))
            
            challenge_iframe = await self._page.query_selector("iframe[name='challenge']")
            if challenge_iframe:
                await challenge_iframe.click()
                return True
            
            return True
        except Exception as e:
            return False
    
    async def solve_image_challenge(
        self,
        target_label: str,
        solver_func,
    ) -> bool:
        """
        Solve hCaptcha image challenge.
        
        Args:
            target_label: Target object to find
            solver_func: Function that takes image and returns bool
            
        Returns:
            True if solved successfully
        """
        try:
            await self.browser.wait_for_selector(".hc-image-carousel")
            
            tiles = await self.browser.extract_image_urls(".hc-image-tile")
            
            correct_indices = []
            for i, url in enumerate(tiles):
                img_data = await self.browser.fetch_image(url)
                
                is_match = solver_func(img_data, target_label)
                
                if is_match:
                    correct_indices.append(i)
            
            for idx in correct_indices:
                await self._page.evaluate(f'''
                    document.querySelectorAll('.hc-image-tile')[{idx}].click();
                ''')
                await asyncio.sleep(random.uniform(0.1, 0.3))
            
            await self._page.evaluate("""
                document.querySelector('.hc-verify-button')?.click();
            """)
            
            return True
        except Exception as e:
            return False
    
    async def solve_slider_challenge(
        self,
        puzzle_image_url: str,
        background_image_url: str,
    ) -> int:
        """
        Solve slider CAPTCHA (e.g., Geetest).
        
        Returns offset distance.
        """
        puzzle_img = await self.browser.fetch_image(puzzle_image_url)
        bg_img = await self.browser.fetch_image(background_image_url)
        
        offset = await self._calculate_slider_offset(puzzle_img, bg_img)
        
        slider = await self._page.query_selector(".slider-track")
        await slider.hover()
        
        await self._page.mouse.move(
            offset + random.randint(-5, 5),
            random.randint(0, 10),
            steps=offset // 2 + random.randint(10, 20)
        )
        
        return offset
    
    async def _calculate_slider_offset(self, puzzle: bytes, bg: bytes) -> int:
        """Calculate slide offset using image analysis."""
        import cv2
        import numpy as np
        
        puzzle_arr = np.frombuffer(puzzle, np.uint8)
        puzzle_cv = cv2.imdecode(puzzle_arr, cv2.IMREAD_COLOR)
        
        bg_arr = np.frombuffer(bg, np.uint8)
        bg_cv = cv2.imdecode(bg_arr, cv2.IMREAD_COLOR)
        
        puzzle_gray = cv2.cvtColor(puzzle_cv, cv2.COLOR_BGR2GRAY)
        bg_gray = cv2.cvtColor(bg_cv, cv2.COLOR_BGR2GRAY)
        
        puzzle_blur = cv2.GaussianBlur(puzzle_gray, (5, 5), 0)
        bg_blur = cv2.GaussianBlur(bg_gray, (5, 5), 0)
        
        result = cv2.matchTemplate(bg_blur, puzzle_blur, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(result)
        
        return max_loc[0]


class MouseTrajectory:
    """Generate human-like mouse trajectories."""
    
    @staticmethod
    def bezier_curve(start: Tuple[int, int], end: Tuple[int, int], points: int = 30) -> List[Tuple[int, int]]:
        """Generate points along a bezier curve."""
        import random
        
        cx = random.randint(start[0], end[0])
        cy = random.randint(start[1], end[1])
        
        trajectory = []
        for i in range(points):
            t = i / (points - 1)
            x = (1 - t) ** 3 * start[0] + 3 * (1 - t) ** 2 * t * cx + 3 * (1 - t) * t ** 2 * end[0] + t ** 3 * end[0]
            y = (1 - t) ** 3 * start[1] + 3 * (1 - t) ** 2 * t * cy + 3 * (1 - t) * t ** 2 * end[1] + t ** 3 * end[1]
            trajectory.append((int(x), int(y)))
        
        return trajectory
    
    @staticmethod
    def add_jitter(trajectory: List[Tuple[int, int]], max_jitter: int = 3) -> List[Tuple[int, int]]:
        """Add random jitter to trajectory."""
        import random
        
        jittered = []
        for x, y in trajectory:
            jittered.append((
                x + random.randint(-max_jitter, max_jitter),
                y + random.randint(-max_jitter, max_jitter),
            ))
        
        return jittered