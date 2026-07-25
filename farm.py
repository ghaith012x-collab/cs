"""
Farm Module — automated hCaptcha demo farming for recognition training.

Navigates to https://accounts.hcaptcha.com/demo, fills the field, clicks
the checkbox, then captures every captcha that appears — saving every tile,
symbol, and detection to the Knowledge Database (no solving).

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
    IFRAME_SELS, GridSolver, _challenge_text as _get_text,
    _b64, _resize, _smart_crop,
)
from database import KnowledgeDB


@dataclass
class Recognition:
    """A single recognition result from the farm."""
    timestamp: float = 0.0
    challenge_type: str = ""
    challenge_text: str = ""
    objects_found: list = field(default_factory=list)
    objects_found_count: int = 0
    source: str = ""  # 'demo' or 'discord'
    screenshot_b64: str = ""
    time_taken: float = 0.0
    tiles_captured: int = 0
    tiles_saved: int = 0

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "challenge_type": self.challenge_type,
            "challenge_text": self.challenge_text[:80],
            "objects_found": self.objects_found,
            "objects_found_count": self.objects_found_count,
            "source": self.source,
            "screenshot_b64": self.screenshot_b64[:50] + "..." if self.screenshot_b64 else "",
            "time_taken": round(self.time_taken, 2),
            "tiles_captured": self.tiles_captured,
            "tiles_saved": self.tiles_saved,
        }


class FarmSession:
    """Farms recognitions from hCaptcha challenges — detection only, no solving.
    
    Two modes:
      - 'demo': uses hCaptcha demo page (accounts.hcaptcha.com/demo)
      - 'discord': uses Discord register page (discord.com/register)
    
    In both modes, when a captcha appears:
      1. Screenshots the tiles
      2. Uses Moondream to identify what objects are in each tile
      3. Saves tiles + labels to the knowledge database
      4. Refreshes the page and repeats
    """

    DEMO_URL = "https://accounts.hcaptcha.com/demo"
    DISCORD_URL = "https://discord.com/register"

    def __init__(self, db: Optional[KnowledgeDB] = None,
                 log: Optional[Callable] = None):
        self.db = db or KnowledgeDB()
        if log:
            self._external_log = log
        else:
            self._external_log = None

        self.running = False
        self.mode = "demo"
        self.captchas_captured = 0
        self.tiles_saved_total = 0
        self.recognitions: list[Recognition] = []
        self._farm_logs: list = []
        self._max_recognitions = 200
        self._max_logs = 100

        # Browser
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        # Solver (only used for Moondream tile analysis, NOT for actual solving)
        self._solver_config = SolverConfig()
        self._grid_solver: Optional[GridSolver] = None
        self._screenshot_task: Optional[asyncio.Task] = None
        self._farm_task: Optional[asyncio.Task] = None
        self._latest_screenshot_b64 = ""

    def _log(self, msg: str, level: str = "info"):
        """Log a farm message (stored + printed)."""
        line = f"[Farm][{level.upper()}] {msg}"
        print(line, flush=True)
        if self._external_log:
            try: self._external_log(msg, level)
            except: pass
        self._farm_logs.append({"time": time.strftime("%H:%M:%S"), "msg": msg, "level": level})
        if len(self._farm_logs) > self._max_logs:
            self._farm_logs = self._farm_logs[-self._max_logs:]

    # ── Start / Stop / Mode ───────────────────────────────

    async def start(self, mode: str = "demo") -> bool:
        """Start the farming session in the given mode."""
        if self.running:
            self._log("Already running — stop first", level="warn")
            return False

        self.mode = mode
        self.running = True
        self.captchas_captured = 0
        self.tiles_saved_total = 0
        self.recognitions = []
        self._farm_logs = []

        self._log(f"Starting farm in '{mode}' mode...")

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox',
                      '--disable-blink-features=AutomationDetected'],
            )
            self._context = await self._browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            )
            self._page = await self._context.new_page()

            await self._context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                Object.defineProperty(navigator, 'languages', { get: () => Object.freeze(['en-US', 'en']) });
                Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            """)

            # GridSolver for tile detection + Moondream
            self._grid_solver = GridSolver(self._solver_config, log=self._log, db=self.db)

            # Start screenshot loop
            self._screenshot_task = asyncio.create_task(self._screenshot_loop())

            # Start farming loop based on mode
            if mode == "discord":
                self._farm_task = asyncio.create_task(self._discord_farming_loop())
            else:
                self._farm_task = asyncio.create_task(self._demo_farming_loop())

            self._log("✓ Farm started")
            return True

        except Exception as e:
            self._log(f"Start error: {e}", level="error")
            self.running = False
            await self._cleanup()
            return False

    async def stop(self):
        self._log("Stopping farming session...")
        self.running = False

        if self._farm_task:
            self._farm_task.cancel()
            try: await self._farm_task
            except asyncio.CancelledError: pass
            self._farm_task = None

        if self._screenshot_task:
            self._screenshot_task.cancel()
            try: await self._screenshot_task
            except asyncio.CancelledError: pass
            self._screenshot_task = None

        await self._cleanup()
        self._log(f"✓ Stopped. Captured: {self.captchas_captured}, Tiles saved: {self.tiles_saved_total}")

    async def _cleanup(self):
        for obj in [self._page, self._context, self._browser]:
            if obj:
                try: await obj.close()
                except: pass
        if self._playwright:
            try: await self._playwright.stop()
            except: pass
        self._page = self._context = self._browser = self._playwright = None

    # ── Screenshot Loop ───────────────────────────────────

    async def _screenshot_loop(self):
        while self.running:
            try:
                if self._page and not self._page.is_closed():
                    raw = await self._page.screenshot(full_page=False)
                    self._latest_screenshot_b64 = base64.b64encode(raw).decode()
                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"Screenshot error: {e}", level="warn")
                await asyncio.sleep(3)

    def get_latest_screenshot(self) -> str:
        return self._latest_screenshot_b64

    def get_latest_png(self) -> Optional[bytes]:
        if self._latest_screenshot_b64:
            try: return base64.b64decode(self._latest_screenshot_b64)
            except: pass
        return None

    # ── Demo Mode Farming ─────────────────────────────────

    async def _demo_farming_loop(self):
        """Farming loop for hCaptcha demo page."""
        try:
            self._log("Navigating to hCaptcha demo...")
            await self._page.goto(self.DEMO_URL, wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(4)

            await self._fill_first_input()
            await asyncio.sleep(0.5)
            await self._click_hcaptcha()

            rounds = 0
            while self.running and rounds < 500:
                rounds += 1
                self._log(f"Round {rounds} — waiting for captcha...")

                captured = await self._capture_captcha("demo")
                if captured:
                    self.captchas_captured += 1

                if self.running:
                    self._log("Refreshing page for next captcha...")
                    try:
                        await self._page.goto(self.DEMO_URL, wait_until='domcontentloaded', timeout=30000)
                        await asyncio.sleep(3)
                        await self._fill_first_input()
                        await asyncio.sleep(0.5)
                        await self._click_hcaptcha()
                    except Exception as e:
                        self._log(f"Refresh error: {e}", level="warn")
                        await asyncio.sleep(3)

            self._log(f"Demo farming complete: {rounds} rounds")

        except asyncio.CancelledError:
            self._log("Demo farming cancelled")
        except Exception as e:
            self._log(f"Demo farming error: {e}", level="error")
        finally:
            self.running = False

    # ── Discord Mode Farming ──────────────────────────────

    async def _discord_farming_loop(self):
        """Farming loop for Discord register page."""
        try:
            rounds = 0
            while self.running and rounds < 500:
                rounds += 1
                self._log(f"Discord round {rounds}...")

                try:
                    await self._page.goto(self.DISCORD_URL, wait_until='domcontentloaded', timeout=60000)
                    await asyncio.sleep(5)

                    await self._fill_discord_form()
                    await asyncio.sleep(1)

                    await self._click_create_account()

                    captured = await self._capture_captcha("discord")
                    if captured:
                        self.captchas_captured += 1

                except Exception as e:
                    self._log(f"Discord round error: {e}", level="warn")
                    await asyncio.sleep(2)

            self._log(f"Discord farming complete: {rounds} rounds")

        except asyncio.CancelledError:
            self._log("Discord farming cancelled")
        except Exception as e:
            self._log(f"Discord farming error: {e}", level="error")
        finally:
            self.running = False

    async def _fill_discord_form(self):
        """Fill Discord register form with random info."""
        try:
            self._log("Filling Discord form with random info...")

            rand_str = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=10))
            email = f"{rand_str}@gmail.com"

            consonants = 'bcdfghjklmnpqrstvwxyz'
            vowels = 'aeiou'
            name_len = random.randint(8, 12)
            name = ''
            for i in range(name_len):
                name += random.choice(consonants) if random.random() < 0.65 else random.choice(vowels)

            username = name

            specials = '!@#$%&*'
            password = random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ') + \
                       ''.join(random.choices(consonants, k=8)) + \
                       random.choice(specials) + str(random.randint(10, 99))

            month = random.randint(1, 12)
            day = random.randint(1, 28)
            year = random.randint(1980, 1999)
            months = ['January', 'February', 'March', 'April', 'May', 'June',
                      'July', 'August', 'September', 'October', 'November', 'December']
            month_name = months[month - 1]

            self._log(f"Random email: {email}")
            self._log(f"Random DOB: {month_name} {day}, {year}")

            email_input = await self._page.query_selector('input[name="email"]')
            if email_input:
                await email_input.fill(email)
                await asyncio.sleep(0.3)

            display_input = await self._page.query_selector('input[name="global_name"]')
            if not display_input:
                display_input = await self._page.query_selector('input[name="username"]')
            if display_input:
                await display_input.fill(name)
                await asyncio.sleep(0.3)

            user_input = await self._page.query_selector('input[name="username"]')
            if user_input and user_input != display_input:
                await user_input.fill(username)
                await asyncio.sleep(0.3)

            pass_input = await self._page.query_selector('input[name="password"]')
            if pass_input:
                await pass_input.fill(password)
                await asyncio.sleep(0.3)

            await self._select_dob("Month", month_name)
            await self._select_dob("Day", str(day))
            await self._select_dob("Year", str(year))

            self._log("Discord form filled ✓")

        except Exception as e:
            self._log(f"Discord form error: {e}", level="warn")

    async def _select_dob(self, label: str, value: str):
        try:
            placeholder = self._page.get_by_text(label, exact=True)
            if await placeholder.count() > 0:
                await placeholder.first.click()
                await asyncio.sleep(0.3)
                await self._page.keyboard.type(value, delay=20)
                await asyncio.sleep(0.2)
                await self._page.keyboard.press('Enter')
                await asyncio.sleep(0.2)
        except:
            pass

    async def _click_create_account(self):
        try:
            self._log("Clicking Create Account...")
            btn = await self._page.query_selector('button[type="submit"]')
            if btn:
                await btn.click()
                self._log("Create Account clicked")
                await asyncio.sleep(3)
            else:
                btn2 = self._page.locator('button:has-text("Create Account")')
                if await btn2.count() > 0:
                    await btn2.first.click()
                    self._log("Create Account clicked (text match)")
                    await asyncio.sleep(3)
        except Exception as e:
            self._log(f"Click Create Account error: {e}", level="warn")

    # ── Shared Helpers ────────────────────────────────────

    async def _fill_first_input(self):
        """Fill the first input on the page (for hCaptcha demo)."""
        try:
            inputs = await self._page.query_selector_all('input')
            if inputs:
                char = random.choice('abcdefghijklmnopqrstuvwxyz')
                await inputs[0].fill(char)
                self._log(f"Filled input with '{char}'")
                await asyncio.sleep(0.5)
        except Exception as e:
            self._log(f"Fill input error: {e}", level="warn")

    async def _click_hcaptcha(self):
        """Click the hCaptcha checkbox — tries multiple strategies."""
        try:
            self._log("Clicking hCaptcha checkbox...")

            # Strategy 1: Find iframe, access content frame, click #checkbox
            for sel in ['iframe[src*="hcaptcha.com"]']:
                try:
                    frame_el = await self._page.query_selector(sel)
                    if frame_el:
                        frame = await frame_el.content_frame()
                        if frame:
                            cb = await frame.query_selector('#checkbox')
                            if cb:
                                await cb.click()
                                self._log("Checkbox clicked via iframe")
                                await asyncio.sleep(2)
                                return
                except:
                    continue

            # Strategy 2: Click iframe center via coordinates
            for sel in ['iframe[src*="hcaptcha.com"]']:
                try:
                    box = await self._page.locator(sel).bounding_box()
                    if box:
                        x = box['x'] + box['width'] / 2
                        y = box['y'] + box['height'] / 2
                        await self._page.mouse.click(x, y)
                        self._log("Checkbox clicked via coords")
                        await asyncio.sleep(2)
                        return
                except:
                    continue

            # Strategy 3: Use page.evaluate to click via DOM
            try:
                clicked = await self._page.evaluate("""() => {
                    const iframes = document.querySelectorAll('iframe');
                    for (const f of iframes) {
                        if (f.src && f.src.includes('hcaptcha')) {
                            try {
                                const doc = f.contentDocument || f.contentWindow.document;
                                const cb = doc.querySelector('#checkbox');
                                if (cb) { cb.click(); return true; }
                            } catch(e) {}
                        }
                    }
                    return false;
                }""")
                if clicked:
                    self._log("Checkbox clicked via evaluate")
                    await asyncio.sleep(2)
                    return
            except:
                pass

            # Strategy 4: Tab to checkbox and press Space
            try:
                for _ in range(15):
                    await self._page.keyboard.press('Tab')
                    await asyncio.sleep(0.1)
                await self._page.keyboard.press('Space')
                self._log("Checkbox clicked via Tab+Space")
                await asyncio.sleep(2)
                return
            except:
                pass

            self._log("Could not click hCaptcha checkbox", level="warn")
            await asyncio.sleep(2)

        except Exception as e:
            self._log(f"Click checkbox error: {e}", level="warn")

    # ── Recognition Capture (recognition-only, NO solving) ─

    async def _capture_captcha(self, source: str = "demo") -> bool:
        """Wait for a captcha, capture tiles + identify objects, save to DB.
        
        This is recognition-ONLY — no solving happens.
        """
        start = time.time()
        rec = Recognition(timestamp=start, source=source)

        try:
            # Wait for captcha (up to 20s)
            captcha_text = ""
            for _ in range(40):
                if not self.running:
                    return False
                text = await _challenge_text(self._page)
                if text:
                    captcha_text = text
                    break
                await asyncio.sleep(0.5)

            if not captcha_text:
                frame_info = await _find_iframe(self._page)
                if not frame_info:
                    self._log("No captcha found", level="warn")
                    return False
                await asyncio.sleep(3)
                captcha_text = await _challenge_text(self._page) or "unknown"

            self._log(f"🎯 Captcha detected: '{captcha_text[:80]}'")
            rec.challenge_text = captcha_text

            ctype = await self._detect_farm_challenge(self._page)
            rec.challenge_type = ctype
            objects = self._extract_objects(captcha_text, ctype)
            rec.objects_found = objects
            rec.objects_found_count = len(objects)
            self._log(f"  Type: {ctype} | Objects: {objects}")

            # Screenshot the captcha iframe
            frame_info = await _find_iframe(self._page)
            if frame_info:
                iframe, box = frame_info
                try:
                    raw = await iframe.screenshot()
                    rec.screenshot_b64 = base64.b64encode(raw).decode()
                except:
                    pass

                # Get individual tile images
                if self._grid_solver:
                    tiles_pil, tile_boxes = await self._grid_solver._get_tiles_dom(self._page, box)
                    if not tiles_pil:
                        self._log("  No tiles found in DOM, splitting screenshot")
                        raw = await self._page.screenshot(clip={
                            "x": box["x"], "y": box["y"],
                            "width": box["width"], "height": box["height"]
                        })
                        tiles_pil = self._grid_solver._split_grid(raw)

                    rec.tiles_captured = len(tiles_pil) if tiles_pil else 0
                    self._log(f"  Tiles captured: {rec.tiles_captured}")

                    # Save tiles to database
                    if tiles_pil and not self.db._noop:
                        class_name = objects[0] if objects else "unknown"
                        tile_records = []
                        for tile in tiles_pil:
                            cropped = _smart_crop(tile, padding=0.15)
                            buf = io.BytesIO()
                            cropped.save(buf, format='PNG', optimize=True)
                            b64 = base64.b64encode(buf.getvalue()).decode()
                            tile_records.append({
                                'class_name': class_name,
                                'image_b64': b64,
                                'challenge': captcha_text[:100],
                                'confidence': 0.9,
                                'success': True,
                            })
                        saved = await self.db.save_tiles_batch(tile_records)
                        rec.tiles_saved = saved
                        self.tiles_saved_total += saved
                        self._log(f"  💾 Saved {saved} tiles to DB [{class_name}]")

            rec.time_taken = time.time() - start
            self.recognitions.append(rec)
            if len(self.recognitions) > self._max_recognitions:
                self.recognitions = self.recognitions[-self._max_recognitions:]

            self._log(f"✅ Captcha captured in {rec.time_taken:.1f}s")
            return True

        except Exception as e:
            self._log(f"Capture error: {e}", level="error")
            rec.timestamp = time.time()
            self.recognitions.append(rec)
            return False

    async def _detect_farm_challenge(self, page: Page) -> str:
        """Detect the challenge type."""
        for sel in ['[role="slider"]', '.slider-handle', '.slide-btn']:
            try:
                if await page.locator(sel).count() > 0:
                    return "slider"
            except:
                continue
        text = await _challenge_text(page)
        tl = text.lower()
        if any(w in tl for w in ['drag', 'move', 'slide to', 'place', 'put']):
            return "drag"
        if any(w in tl for w in ['odd', 'different', 'does not belong', 'dissapearing', 'disappearing']):
            return "odd_one_out"
        return "grid"

    @staticmethod
    def _extract_objects(text: str, ctype: str = "grid") -> list:
        """Extract object names from challenge text."""
        if not text:
            return ["unknown"]
        t = text.lower().strip()
        objects = []

        if any(w in t for w in ['odd', 'different', 'does not belong',
                                 'disappearing', 'dissapearing']):
            known = ['lion', 'tiger', 'bear', 'elephant', 'gorilla', 'monkey',
                     'zebra', 'giraffe', 'hippo', 'rhino', 'camel', 'deer',
                     'fox', 'wolf', 'rabbit', 'squirrel', 'mouse', 'rat',
                     'bat', 'whale', 'dolphin', 'seal', 'penguin', 'owl',
                     'eagle', 'hawk', 'parrot', 'crow', 'raven', 'swan',
                     'duck', 'goose', 'chicken', 'rooster', 'hen', 'turkey',
                     'peacock', 'ostrich', 'flamingo', 'pigeon', 'sparrow',
                     'robin', 'blue jay', 'butterfly', 'dragonfly', 'bee', 'ant',
                     'spider', 'scorpion', 'crab', 'lobster', 'snail', 'snake',
                     'lizard', 'turtle', 'frog', 'toad', 'crocodile', 'alligator',
                     'star', 'moon', 'sun', 'cloud', 'rainbow', 'lightning',
                     'snowflake', 'heart', 'diamond', 'circle', 'square',
                     'triangle', 'rectangle', 'pentagon', 'hexagon', 'octagon',
                     'rocket', 'spaceship', 'rocketship', 'ufo', 'alien',
                     'robot', 'car', 'bus', 'truck', 'train', 'bicycle',
                     'motorcycle', 'airplane', 'helicopter', 'boat', 'ship']
            for a in known:
                if a in t:
                    objects.append(a)
            if not objects:
                objects.append("odd_one_out")
            return objects

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
                            o = words[0].strip('.,!?:;')
                            if o and o not in objects:
                                objects.append(o)
                    break

        if not objects:
            objects.append("unknown")
        return objects

    # ── Status ────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode,
            "captchas_captured": self.captchas_captured,
            "tiles_saved_total": self.tiles_saved_total,
            "recognitions_count": len(self.recognitions),
            "latest_recognition": self.recognitions[-1].to_dict() if self.recognitions else None,
        }

    def get_recent_recognitions(self, count: int = 50) -> list:
        recent = self.recognitions[-count:] if self.recognitions else []
        return [r.to_dict() for r in reversed(recent)]

    def get_logs(self, count: int = 50) -> list:
        return self._farm_logs[-count:]

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

    print(f"\nResults: {farm.captchas_captured} captured")
    print(f"Tiles saved: {farm.tiles_saved_total}")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
