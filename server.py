import asyncio
import base64
import json
import os
import random
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext

import captcha_solver


class DiscordAutomation:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._screenshots: list = []
        self._email = ""
        self._username = ""
        self._password = ""

    async def initialize(self) -> None:
        self._playwright = await async_playwright().start()
        
        args = [
            '--disable-blink-features=AutomationDetected',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-webgl',
            '--disable-features=IsolateOrigins,site-per-process',
        ]
        
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=args
        )
        
        self._context = await self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        await self._context.add_init_script("""
            // Stealth: webdriver=false (not undefined - absence is now a signal)
            Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
            Object.defineProperty(navigator, 'languages', { get: () => Object.freeze(['en-US', 'en']) });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
            Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
            // WebRTC leak prevention
            const origRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
            if (origRTC) {
                window.RTCPeerConnection = function(...args) {
                    const c = args[0] || {}; c.iceTransportPolicy = 'relay';
                    return new origRTC(c, ...args.slice(1));
                };
                window.RTCPeerConnection.prototype = origRTC.prototype;
            }
        """)
        
        self._page = await self._context.new_page()

    def load_config(self, config_path: str = "config.json") -> None:
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        self._email = config.get('email', '') or ''
        self._username = self._generate_username()  # Always generate fresh random username
        self._password = config.get('password', '') or self._generate_password()
        print(f"[Config] Email: {self._email}, Username: {self._username}, Password set: {bool(self._password)}")
    
    def _generate_username(self) -> str:
        """Generate a natural-looking username like real Discord users.
        No underscores, no real words, just random lowercase letters 8-12 chars.
        Examples: xarjsnoxhhao, kqmvtpwle, bznhcxfwoj
        """
        # Use consonant-vowel mixing for pronounceable but meaningless strings
        consonants = 'bcdfghjklmnpqrstvwxyz'
        vowels = 'aeiou'
        length = random.randint(8, 12)
        username = ''
        for i in range(length):
            if random.random() < 0.35:  # 35% vowels = looks natural but not a word
                username += random.choice(vowels)
            else:
                username += random.choice(consonants)
        return username

    def _generate_password(self) -> str:
        chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*'
        return ''.join(random.choice(chars) for _ in range(16))

    async def read_email_from_file(self, file_path: str) -> str:
        with open(file_path, 'r') as f:
            content = f.read()
        
        import re
        match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', content)
        if match:
            return match.group(0)
        return "test@example.com"

    async def capture_screenshot(self) -> str:
        if not self._page:
            return ""
        
        screenshot = await self._page.screenshot(full_page=True)
        b64 = base64.b64encode(screenshot).decode('utf-8')
        self._screenshots.append(b64)
        
        if len(self._screenshots) > 100:
            self._screenshots = self._screenshots[-50:]
        
        return b64

    async def start_discord_signup(self) -> bool:
        if not self._page:
            await self.initialize()
        
        try:
            success = await asyncio.wait_for(self._fill_registration_form(), timeout=60)
        except asyncio.TimeoutError:
            print("[Activity] Form filling timed out after 60 seconds")
            success = False
        
        await self.capture_screenshot()
        
        return success

    async def _solve_hcaptcha_if_present(self) -> bool:
        """Detect and solve hCaptcha with robust multi-method detection."""
        try:
            print("[Activity] Checking for hCaptcha...")
            
            # Wait up to 10 seconds for captcha to appear (it can take a moment)
            captcha_found = False
            for attempt in range(20):  # 20 * 0.5s = 10s max wait
                # Method 1: iframe with hcaptcha src
                hcaptcha_iframe = await self._page.query_selector(
                    'iframe[src*="hcaptcha.com"], '
                    'iframe[src*="captcha.hcaptcha.com"], '
                    'iframe[title*="hCaptcha"], '
                    'iframe[title*="Widget containing checkbox"]'
                )
                if hcaptcha_iframe:
                    captcha_found = True
                    print(f"[Activity] hCaptcha iframe detected (attempt {attempt+1})")
                    break
                
                # Method 2: Check for hcaptcha div container
                hcaptcha_div = await self._page.query_selector(
                    '#hcaptcha-script, '
                    'div.h-captcha, '
                    'div[data-hcaptcha-widget-id], '
                    'div[class*="hcaptcha"]'
                )
                if hcaptcha_div:
                    captcha_found = True
                    print(f"[Activity] hCaptcha div detected (attempt {attempt+1})")
                    break
                
                # Method 3: Check for hcaptcha response textarea (means it loaded)
                textarea = await self._page.query_selector(
                    'textarea[name="h-captcha-response"], '
                    'textarea[name="g-recaptcha-response"]'
                )
                if textarea:
                    captcha_found = True
                    print(f"[Activity] hCaptcha textarea detected (attempt {attempt+1})")
                    break
                
                # Method 4: Check via JavaScript for any hcaptcha-related elements
                js_detected = await self._page.evaluate("""
                    () => {
                        // Check for hcaptcha script
                        const scripts = document.querySelectorAll('script[src*="hcaptcha"]');
                        if (scripts.length > 0) return 'script';
                        // Check for hcaptcha frames
                        const frames = document.querySelectorAll('iframe');
                        for (const f of frames) {
                            if (f.src && f.src.includes('hcaptcha')) return 'iframe';
                            if (f.title && f.title.toLowerCase().includes('captcha')) return 'iframe';
                        }
                        // Check for challenge overlay
                        const overlay = document.querySelector('[class*="challenge"], [id*="challenge"]');
                        if (overlay) return 'overlay';
                        return null;
                    }
                """)
                if js_detected:
                    captcha_found = True
                    print(f"[Activity] hCaptcha detected via JS ({js_detected}, attempt {attempt+1})")
                    break
                
                await asyncio.sleep(0.5)
            
            if not captcha_found:
                print("[Activity] No hCaptcha detected after 10s - might have passed without captcha")
                # Check if we're on a success page or error page
                current_url = self._page.url
                if 'channels' in current_url or 'app' in current_url:
                    print("[Activity] Redirected to app - registration succeeded without captcha!")
                    return True
                return True  # No captcha = success
            
            # Wait a moment for the captcha to fully render
            await asyncio.sleep(2)
            
            # First, check if there's a checkbox to click before the challenge appears
            try:
                checkbox_frame = self._page.frame_locator('iframe[src*="hcaptcha.com/hcaptcha"], iframe[title*="Widget containing checkbox"]')
                checkbox = checkbox_frame.locator('#checkbox, [role="checkbox"]')
                if await checkbox.count() > 0:
                    print("[Activity] Clicking hCaptcha checkbox...")
                    await checkbox.first.click()
                    await asyncio.sleep(3)
                    
                    # Check if clicking checkbox was enough (sometimes it passes)
                    checked = await self._page.evaluate("""
                        () => {
                            const textarea = document.querySelector('textarea[name="h-captcha-response"]');
                            return textarea && textarea.value && textarea.value.length > 0;
                        }
                    """)
                    if checked:
                        print("[Activity] hCaptcha passed with just checkbox click!")
                        return True
            except Exception as e:
                print(f"[Activity] Checkbox click attempt: {e}")
            
            # Now solve the full challenge
            config = captcha_solver.SolverConfig(
                headless=False,
                clip_confidence_threshold=0.50,
                max_challenge_rounds=5,  # More attempts
                timeout=45
            )
            
            solver = captcha_solver.GodSolver(config)
            success = await solver.solve(self._page)
            await solver.close()
            
            if success:
                print("[Activity] hCaptcha SOLVED!")
                # Wait for form submission to complete after captcha
                await asyncio.sleep(3)
            else:
                print("[Activity] hCaptcha solve FAILED")
            
            return success
        except Exception as e:
            print(f"hCaptcha solve error: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _select_dob(self, label: str, option_text: str) -> bool:
        """Select DOB using multiple strategies: click+type+Enter, keyboard nav, JS with scroll."""
        try:
            combobox = self._page.locator(f'[role="combobox"][aria-label="{label}"]')
            if await combobox.count() == 0:
                print(f"[Activity] Combobox {label} not found")
                return False
            print(f"[Activity] Selecting {label}: {option_text}")
            # Strategy 1: Click combobox, find input, type, press Enter
            await combobox.first.click()
            await asyncio.sleep(0.5)
            # Find input inside combobox
            input_loc = self._page.locator(f'[role="combobox"][aria-label="{label}"] input')
            if await input_loc.count() == 0:
                input_loc = self._page.locator(f'[id^="react-select-"][id$="-input"]')
            if await input_loc.count() == 0:
                input_loc = combobox.first
            await input_loc.first.fill(option_text)
            await asyncio.sleep(0.3)
            await input_loc.first.press("Enter")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"[Activity] Strategy 1 failed for {label}: {e}")
            # Strategy 2: Keyboard navigation - click, ArrowDown, Enter
            try:
                combobox = self._page.locator(f'[role="combobox"][aria-label="{label}"]')
                await combobox.first.click()
                await asyncio.sleep(0.5)
                await self._page.keyboard.press("ArrowDown")
                await asyncio.sleep(0.2)
                await self._page.keyboard.press("Enter")
                await asyncio.sleep(0.5)
                return True
            except Exception as e2:
                print(f"[Activity] Strategy 2 failed for {label}: {e2}")
                # Strategy 3: JavaScript with scroll to find option
                try:
                    await self._page.evaluate(f"""
                        () => {{
                            const cb = document.querySelector('[role="combobox"][aria-label="{label}"]');
                            if (!cb) return false;
                            cb.click();
                        }}
                    """)
                    await asyncio.sleep(0.5)
                    result = await self._page.evaluate(f"""
                        () => {{
                            const options = document.querySelectorAll('[role="option"]');
                            for (const opt of options) {{
                                if (opt.textContent && opt.textContent.trim() === "{option_text}") {{
                                    opt.scrollIntoView({{block: "center", inline: "center"}});
                                    opt.click();
                                    return true;
                                }}
                            }}
                            for (const opt of options) {{
                                if (opt.textContent && opt.textContent.includes("{option_text}")) {{
                                    opt.scrollIntoView({{block: "center", inline: "center"}});
                                    opt.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}
                    """)
                    await asyncio.sleep(0.5)
                    if result:
                        return True
                    print(f"[Activity] JS strategy did not find option {option_text} for {label}")
                    return False
                except Exception as e3:
                    print(f"[Activity] All strategies failed for {label}: {e3}")
                    return False

    async def _fill_registration_form(self) -> bool:
        try:
            print("[Activity] Navigating to Discord registration page...")
            await self._page.goto('https://discord.com/register', wait_until='networkidle')
            await asyncio.sleep(3)
            
            print(f"[Activity] Filling email: {self._email}")
            await self._page.wait_for_selector('input[name="email"]', timeout=15000)
            await self._page.locator('input[name="email"]').fill(self._email)
            await self._human_pause()
            
            display_name = self._username[:15] if len(self._username) > 15 else self._username
            print(f"[Activity] Filling display name: {display_name}")
            await self._page.wait_for_selector('input[name="global_name"]', timeout=10000)
            await self._page.locator('input[name="global_name"]').fill(display_name)
            await self._human_pause()
            
            print(f"[Activity] Filling username: {self._username}")
            await self._page.locator('input[name="username"]').fill(self._username)
            await self._human_pause()
            
            print("[Activity] Filling password")
            await self._page.locator('input[name="password"]').fill(self._password)
            await self._human_pause()
            
            # DOB - year always 1990-1999 (under 2000, guarantees 18+)
            month_val = random.randint(1, 12)
            day_val = str(random.randint(1, 28))
            year_val = str(random.randint(1990, 1999))
            months = ['January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December']
            month_name = months[month_val - 1]
            print(f"[Activity] Selecting DOB: {month_name} {day_val}, {year_val}")
            
            await self._select_dob("Month", month_name)
            await self._human_pause()
            
            await self._select_dob("Day", day_val)
            await self._human_pause()
            
            await self._select_dob("Year", year_val)
            await self._human_pause()
            
            # Click Create Account with multiple fallback methods
            print("[Activity] Clicking Create Account button")
            create_clicked = False
            
            # Method 1: button with exact text
            try:
                btn = self._page.locator('button:has-text("Create Account")')
                if await btn.count() > 0:
                    await btn.first.click()
                    create_clicked = True
            except:
                pass
            
            if not create_clicked:
                # Method 2: get_by_role
                try:
                    btn = self._page.get_by_role("button", name="Create Account")
                    if await btn.count() > 0:
                        await btn.first.click()
                        create_clicked = True
                except:
                    pass
            
            if not create_clicked:
                # Method 3: type=submit button
                try:
                    btn = self._page.locator('button[type="submit"]')
                    if await btn.count() > 0:
                        await btn.first.click()
                        create_clicked = True
                except:
                    pass
            
            if not create_clicked:
                # Method 4: JavaScript click
                try:
                    await self._page.evaluate("""
                        () => {
                            const buttons = document.querySelectorAll('button');
                            for (const btn of buttons) {
                                if (btn.textContent.includes('Create Account') || btn.textContent.includes('Create account')) {
                                    btn.click();
                                    return true;
                                }
                            }
                            // Try submit buttons
                            const submit = document.querySelector('button[type="submit"]');
                            if (submit) { submit.click(); return true; }
                            return false;
                        }
                    """)
                    create_clicked = True
                except:
                    pass
            
            if not create_clicked:
                print("[Activity] ERROR: Could not click Create Account button!")
                return False
            
            print("[Activity] Create Account clicked, waiting for captcha...")
            await asyncio.sleep(3)
            
            # Take screenshot to see what happened
            await self.capture_screenshot()
            
            # Wait for and solve hCaptcha (it always appears after Create Account)
            if await self._solve_hcaptcha_if_present():
                print("[Activity] Registration completed")
                return True
            print("[Activity] Registration failed - hCaptcha error")
            return False
            
        except Exception as e:
            print(f"[Activity] Form filling error: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _human_click(self, element) -> None:
        try:
            box = await element.bounding_box()
            if box:
                x = box['x'] + random.uniform(box['width'] * 0.3, box['width'] * 0.7)
                y = box['y'] + random.uniform(box['height'] * 0.3, box['height'] * 0.7)
                
                steps = random.randint(8, 20)
                await self._page.mouse.move(x, y, steps=steps)
                await asyncio.sleep(random.uniform(0.05, 0.15))
                await self._page.mouse.down()
                await asyncio.sleep(random.uniform(0.03, 0.1))
                await self._page.mouse.up()
            else:
                await element.click()
        except Exception as e:
            print(f"Click error: {e}")
            try:
                await element.click()
            except:
                pass

    async def _human_pause(self) -> None:
        await asyncio.sleep(random.uniform(0.1, 0.5))

    async def live_camera_loop(self, interval: int = 3) -> None:
        while True:
            await self.capture_screenshot()
            await asyncio.sleep(interval)

    async def close(self) -> None:
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def get_screenshots(self) -> list:
        return self._screenshots

    def get_latest_screenshot(self) -> str:
        if self._screenshots:
            return self._screenshots[-1]
        return ""


async def run_discord_automation(config_path: str = "config.json"):
    config = {}
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except:
        pass
    
    headless = config.get('headless', True)
    bot = DiscordAutomation(headless=headless)
    
    try:
        await bot.initialize()
        bot.load_config(config_path)
        
        success = await bot.start_discord_signup()
        
        if success:
            print("Registration form filled successfully")
        else:
            print("Registration form filling failed")
        
        await asyncio.sleep(5)
        
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(run_discord_automation())
