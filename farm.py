"""
Farm Module — automated hCaptcha demo farming for recognition training.

Navigates to https://accounts.hcaptcha.com/demo, fills the field, clicks
the checkbox, then solves every captcha that appears — saving every tile,
symbol, and detection to the Knowledge Database.

The farm page shows:
  - Live screenshot feed (cam)
  - Real-time recognition results
  - What objects it's learning
"""

import asyncio
import base64
import io
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from PIL import Image
from playwright.async_api import async_playwright, Page, BrowserContext

from captcha_solver import (
    SolverConfig, MasterSolver, _challenge_text, _find_iframe,
    IFRAME_SELS
)
from database import KnowledgeDB


@dataclass
class Recognition:
    """A single recognition result from the farm."""
    timestamp: float = 0.0
    challenge_type: str = ""
    challenge_text: str = ""
    objects_found: list = field(default_factory=list)
    objects_count: int = 0
    success: bool = False
    screenshot_b64: str = ""
    time_taken: float = 0.0

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "challenge_type": self.challenge_type,
            "challenge_text": self.challenge_text[:80],
            "objects_found": self.objects_found,
            "objects_count": self.objects_count,
            "success": self.success,
            "screenshot_b64": self.screenshot_b64[:50] + "...",
            "time_taken": round(self.time_taken, 2),
        }


class FarmSession:
    """Runs an automated hCaptcha demo farming session.
    
    Usage:
        farm = FarmSession(db)
        await farm.start()
        # ... check farm.recognitions for results ...
        await farm.stop()
    """

    DEMO_URL = "https://accounts.hcaptcha.com/demo"

    def __init__(self, db: Optional[KnowledgeDB] = None,
                 log: Optional[Callable] = None):
        self.db = db or KnowledgeDB()
        self._log = log or (lambda msg, level="info": None)

        self.running = False
        self.paused = False
        self.captchas_solved = 0
        self.captchas_failed = 0
        self.recognitions: list[Recognition] = []
        self._max_recognitions = 200

        # Browser
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        # Solver
        self._solver: Optional[MasterSolver] = None
        self._solver_config = SolverConfig()
        self._screenshot_task: Optional[asyncio.Task] = None
        self._solve_task: Optional[asyncio.Task] = None
        self._latest_screenshot_b64 = ""

    # ── Start / Stop ──────────────────────────────────────

    async def start(self) -> bool:
        """Start the farming session. Returns True if successful."""
        if self.running:
            self._log("[Farm] Already running")
            return False

        self._log("[Farm] Starting farming session...")
        self.running = True
        self.captchas_solved = 0
        self.captchas_failed = 0
        self.recognitions = []

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationDetected',
                ]
            )
            self._context = await self._browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36'),
            )
            self._page = await self._context.new_page()

            # Stealth
            await self._context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                Object.defineProperty(navigator, 'languages', { get: () => Object.freeze(['en-US', 'en']) });
                Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            """)

            # Initialize solver
            self._solver = MasterSolver(self._solver_config, log=self._log, db=self.db)

            # Start screenshot loop
            self._screenshot_task = asyncio.create_task(self._screenshot_loop())

            # Start the main farming loop
            self._solve_task = asyncio.create_task(self._farming_loop())

            self._log("[Farm] ✓ Farming session started")
            return True

        except Exception as e:
            self._log(f"[Farm] Start error: {e}", level="error")
            self.running = False
            await self._cleanup()
            return False

    async def stop(self):
        """Stop the farming session."""
        self._log("[Farm] Stopping farming session...")
        self.running = False

        if self._solve_task:
            self._solve_task.cancel()
            try:
                await self._solve_task
            except asyncio.CancelledError:
                pass
            self._solve_task = None

        if self._screenshot_task:
            self._screenshot_task.cancel()
            try:
                await self._screenshot_task
            except asyncio.CancelledError:
                pass
            self._screenshot_task = None

        await self._cleanup()
        self._log(f"[Farm] ✓ Stopped. Solved: {self.captchas_solved}, "
                  f"Failed: {self.captchas_failed}")

    async def _cleanup(self):
        if self._page:
            try:
                await self._page.close()
            except:
                pass
            self._page = None
        if self._context:
            try:
                await self._context.close()
            except:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except:
                pass
            self._playwright = None

    # ── Screenshot Loop ───────────────────────────────────

    async def _screenshot_loop(self):
        """Continuously capture screenshots of the page."""
        while self.running:
            try:
                if self._page and not self._page.is_closed():
                    raw = await self._page.screenshot(full_page=False)
                    self._latest_screenshot_b64 = base64.b64encode(raw).decode()
                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"[Farm] Screenshot error: {e}", level="warn")
                await asyncio.sleep(3)

    def get_latest_screenshot(self) -> str:
        return self._latest_screenshot_b64

    def get_latest_png(self) -> Optional[bytes]:
        if self._latest_screenshot_b64:
            try:
                return base64.b64decode(self._latest_screenshot_b64)
            except:
                pass
        return None

    # ── Farming Loop ──────────────────────────────────────

    async def _farming_loop(self):
        """Main farming loop: navigate to demo, fill, click, solve repeatedly."""
        try:
            # Navigate to hCaptcha demo
            self._log(f"[Farm] Navigating to {self.DEMO_URL}")
            await self._page.goto(self.DEMO_URL, wait_until='domcontentloaded',
                                  timeout=30000)
            await asyncio.sleep(3)

            # Fill the email field with 1 character
            await self._fill_demo_form()

            # Click the checkbox
            await self._click_checkbox()

            # Main solve loop
            farm_count = 0
            while self.running and farm_count < 1000:
                farm_count += 1
                self._log(f"[Farm] Farm round {farm_count}...")

                success = await self._wait_and_solve()

                if success:
                    self.captchas_solved += 1
                else:
                    self.captchas_failed += 1

                # Brief pause before next round
                await asyncio.sleep(random.uniform(1.5, 3.5))

                # Re-click checkbox for a new challenge
                if self.running:
                    await self._click_checkbox()
                    await asyncio.sleep(random.uniform(1, 2))

            self._log(f"[Farm] Farming complete: {farm_count} rounds")

        except asyncio.CancelledError:
            self._log("[Farm] Farming loop cancelled")
        except Exception as e:
            self._log(f"[Farm] Farming loop error: {e}", level="error")
        finally:
            self.running = False

    async def _fill_demo_form(self):
        """Fill the email field in the hCaptcha demo with 1 character."""
        try:
            self._log("[Farm] Filling email field with 1 character...")
            # hCaptcha demo uses an input with name="email" or id="email"
            email_input = await self._page.query_selector(
                'input[name="email"], input[type="email"], #email, '
                'input[placeholder*="email"], input[placeholder*="Email"]'
            )
            if email_input:
                char = random.choice('abcdefghijklmnopqrstuvwxyz')
                await email_input.fill(char)
                self._log(f"[Farm] Filled email with '{char}'")
                await asyncio.sleep(0.5)
            else:
                self._log("[Farm] Email field not found, trying generic input...")
                inputs = await self._page.query_selector_all('input')
                if inputs:
                    char = random.choice('abcdefghijklmnopqrstuvwxyz')
                    await inputs[0].fill(char)
                    await asyncio.sleep(0.5)
        except Exception as e:
            self._log(f"[Farm] Fill form error: {e}", level="warn")

    async def _click_checkbox(self):
        """Click the hCaptcha checkbox to trigger a challenge."""
        try:
            self._log("[Farm] Clicking hCaptcha checkbox...")

            # Look for the hCaptcha iframe and its checkbox
            checkbox_selectors = [
                'iframe[src*="hcaptcha.com"][title*="checkbox"]',
                'iframe[src*="hcaptcha.com"][aria-label*="checkbox"]',
                'iframe[title*="checkbox"]',
                'iframe[src*="hcaptcha.com"]',
            ]

            clicked = False
            for sel in checkbox_selectors:
                try:
                    frame_el = await self._page.query_selector(sel)
                    if frame_el:
                        frame = await frame_el.content_frame()
                        if frame:
                            cb = await frame.query_selector('#checkbox')
                            if cb:
                                await cb.click()
                                clicked = True
                                self._log("[Farm] Checkbox clicked via iframe")
                                break
                except:
                    continue

            # Fallback: try to click the center of any hCaptcha iframe
            if not clicked:
                for sel in ['iframe[src*="hcaptcha.com"]']:
                    try:
                        boxes = await self._page.locator(sel).bounding_box()
                        if boxes:
                            x = boxes['x'] + boxes['width'] / 2
                            y = boxes['y'] + boxes['height'] / 2
                            await self._page.mouse.click(x, y)
                            clicked = True
                            self._log("[Farm] Checkbox clicked via coordinates")
                            break
                    except:
                        continue

            if not clicked:
                self._log("[Farm] Could not find hCaptcha checkbox", level="warn")

            await asyncio.sleep(2)

        except Exception as e:
            self._log(f"[Farm] Click checkbox error: {e}", level="warn")

    async def _wait_and_solve(self) -> bool:
        """Wait for a captcha challenge to appear and solve it.
        
        Returns True if solved successfully.
        Records recognition data for the dashboard.
        """
        start_time = time.time()
        rec = Recognition(timestamp=start_time)

        try:
            # Wait for captcha to appear (up to 15s)
            self._log("[Farm] Waiting for captcha challenge...")
            captcha_appeared = False
            for _ in range(30):  # 30 * 0.5s = 15s
                if not self.running:
                    return False
                text = await _challenge_text(self._page)
                if text:
                    captcha_appeared = True
                    rec.challenge_text = text
                    self._log(f"[Farm] Captcha detected: '{text[:60]}'")
                    break
                await asyncio.sleep(0.5)

            if not captcha_appeared:
                # Check if there's an hCaptcha iframe at all
                frame_info = await _find_iframe(self._page)
                if not frame_info:
                    self._log("[Farm] No captcha iframe found", level="warn")
                return False

            # Determine challenge type
            ctype = await self._detect_farm_challenge(self._page)
            rec.challenge_type = ctype
            self._log(f"[Farm] Challenge type: {ctype}")

            # Extract objects from the challenge text
            objects = self._extract_objects(rec.challenge_text, ctype)
            rec.objects_found = objects
            rec.objects_count = len(objects)
            self._log(f"[Farm] Objects: {objects}")

            # Use the MasterSolver to solve it
            if self._solver:
                solution_start = time.time()
                ok = await self._solver.solve(self._page)
                solve_time = time.time() - solution_start
                rec.time_taken = solve_time
                rec.success = ok

                if ok:
                    self._log(f"[Farm] ✓ Solved! ({solve_time:.1f}s)")
                    # Save a screenshot of the solved captcha
                    if self._page and not self._page.is_closed():
                        try:
                            raw = await self._page.screenshot(full_page=False)
                            rec.screenshot_b64 = base64.b64encode(raw).decode()
                        except:
                            pass
                else:
                    self._log(f"[Farm] ✗ Failed ({solve_time:.1f}s)")

            # Record recognition
            rec.timestamp = time.time()
            self.recognitions.append(rec)
            if len(self.recognitions) > self._max_recognitions:
                self.recognitions = self.recognitions[-self._max_recognitions:]

            return rec.success

        except Exception as e:
            self._log(f"[Farm] Solve error: {e}", level="error")
            rec.success = False
            rec.timestamp = time.time()
            self.recognitions.append(rec)
            return False

    async def _detect_farm_challenge(self, page: Page) -> str:
        """Detect the challenge type (same as MasterSolver._detect)."""
        slider_sigs = ['[role="slider"]', '.slider-handle', '.slide-btn']
        for sel in slider_sigs:
            try:
                if await page.locator(sel).count() > 0:
                    return "slider"
            except:
                continue

        text = await _challenge_text(page)
        text_lower = text.lower()
        if any(w in text_lower for w in ['drag', 'move', 'slide to', 'place', 'put']):
            return "drag"
        if any(w in text_lower for w in ['odd', 'different', 'does not belong',
                                          'dissapearing', 'disappearing']):
            return "odd_one_out"

        return "grid"

    @staticmethod
    def _extract_objects(text: str, ctype: str = "grid") -> list:
        """Extract object names from challenge text.
        
        'click all images containing a star' → ['star']
        'select the odd one out' → ['?']
        'drag the rocketship to the star' → ['rocketship', 'star']
        """
        if not text:
            return ["unknown"]

        t = text.lower().strip()
        objects = []

        # Odd one out / which is different
        if any(w in t for w in ['odd', 'different', 'does not belong',
                                 'disappearing', 'dissapearing']):
            # Try to extract all mentioned animals/objects
            animals = ['lion', 'tiger', 'bear', 'elephant', 'gorilla', 'monkey',
                       'zebra', 'giraffe', 'hippo', 'rhino', 'camel', 'deer',
                       'fox', 'wolf', 'rabbit', 'squirrel', 'mouse', 'rat',
                       'bat', 'whale', 'dolphin', 'seal', 'penguin', 'owl',
                       'eagle', 'hawk', 'parrot', 'crow', 'raven', 'swan',
                       'duck', 'goose', 'chicken', 'rooster', 'hen', 'turkey',
                       'peacock', 'ostrich', 'flamingo', 'pigeon', 'sparrow',
                       'robin', 'blue jay', 'cardinal', 'woodpecker', 'hummingbird',
                       'butterfly', 'dragonfly', 'bee', 'ant', 'spider', 'scorpion',
                       'crab', 'lobster', 'shrimp', 'snail', 'worm', 'snake',
                       'lizard', 'turtle', 'frog', 'toad', 'salamander', 'newt',
                       'crocodile', 'alligator', 'dinosaur', 't-rex', 'triceratops',
                       'stegosaurus', 'pterodactyl', 'brontosaurus', 'velociraptor',
                       'star', 'moon', 'sun', 'cloud', 'rainbow', 'lightning',
                       'snowflake', 'heart', 'diamond', 'circle', 'square',
                       'triangle', 'rectangle', 'pentagon', 'hexagon', 'octagon',
                       'rocket', 'spaceship', 'rocketship', 'ufo', 'alien',
                       'robot', 'car', 'bus', 'truck', 'train', 'bicycle',
                       'motorcycle', 'airplane', 'helicopter', 'boat', 'ship',
                       'submarine', 'hot air balloon', 'parachute']
            for animal in animals:
                if animal in t:
                    objects.append(animal)
            if not objects:
                objects.append("odd_one_out")
            return objects

        # Extract from phrases
        phrases = ['select all images containing ', 'click all images with ',
                   'select all squares with ', 'click all squares containing ',
                   'choose all images with ', 'select all matching ',
                   'click all ', 'select all ', 'choose all ',
                   'containing ', 'with ', 'matching ', 'showing ']

        for phrase in phrases:
            if phrase in t:
                subj = t.split(phrase, 1)[1].strip().strip('.!?,:;')
                for art in ['a ', 'an ', 'the ']:
                    if subj.startswith(art):
                        subj = subj[len(art):]
                words = subj.split()
                if words:
                    objects.append(words[0])
                break

        # Check for drag patterns
        if any(w in t for w in ['drag', 'move', 'slide', 'place']):
            for sep in [' to ', ' into ', ' onto ', ' in ']:
                if sep in t:
                    parts = t.split(sep, 1)
                    for part in parts:
                        cleaned = part.strip().strip('.!?,:;')
                        for prefix in ['drag ', 'move ', 'slide ', 'place ',
                                       'the ', 'a ', 'an ', 'your ']:
                            if cleaned.startswith(prefix):
                                cleaned = cleaned[len(prefix):]
                        words = cleaned.split()
                        if words:
                            obj = words[0].strip('.,!?:;')
                            if obj and obj not in objects:
                                objects.append(obj)
                    break

        if not objects:
            objects.append("unknown")

        return objects

    # ── Status ────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "captchas_solved": self.captchas_solved,
            "captchas_failed": self.captchas_failed,
            "total_captchas": self.captchas_solved + self.captchas_failed,
            "recognitions_count": len(self.recognitions),
            "latest_recognition": self.recognitions[-1].to_dict() if self.recognitions else None,
        }

    def get_recent_recognitions(self, count: int = 20) -> list:
        recent = self.recognitions[-count:] if self.recognitions else []
        return [r.to_dict() for r in reversed(recent)]

    async def close(self):
        await self.stop()


# ── Standalone Test ───────────────────────────────────────

async def main():
    db = await KnowledgeDB.create()
    farm = FarmSession(db=db)

    print("Starting farm for 30 seconds...")
    await farm.start()
    await asyncio.sleep(30)
    await farm.stop()

    print(f"\nResults: {farm.captchas_solved} solved, {farm.captchas_failed} failed")
    print(f"Recognitions stored: {len(farm.recognitions)}")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
