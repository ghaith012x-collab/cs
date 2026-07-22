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
        self._username = config.get('username', '') or self._generate_username()
        self._password = config.get('password', '') or self._generate_password()
        print(f"[Config] Email: {self._email}, Username: {self._username}, Password set: {bool(self._password)}")
    
    def _generate_username(self) -> str:
        chars = 'abcdefghijklmnopqrstuvwxyz'
        return ''.join(random.choice(chars) for _ in range(8))

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
        try:
            hcaptcha_iframe = await self._page.query_selector('iframe[src*="hcaptcha.com"], iframe[src*="captcha.hcaptcha.com"]')
            if not hcaptcha_iframe:
                return True
            
            config = captcha_solver.SolverConfig(
                headless=False,
                clip_confidence_threshold=0.55,
                max_challenge_rounds=3,
                timeout=30
            )
            
            solver = captcha_solver.GodSolver(config)
            success = await solver.solve(self._page)
            await solver.close()
            
            return success
        except Exception as e:
            print(f"hCaptcha solve error: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _select_dob(self, label: str, value: str) -> bool:
        """Select DOB field. Discord uses custom dropdown divs (not native selects).
        The dropdowns have a clickable container with placeholder text like 'Month', 'Day', 'Year'.
        Clicking opens a listbox with options. For Year, the list is long and needs keyboard input.
        """
        try:
            print(f"[Activity] Selecting {label}: {value}")
            
            # === APPROACH 1: Type-to-search in the dropdown ===
            # Discord's DOB dropdowns are searchable. Click the dropdown, then type the value.
            # This is the most reliable method because it avoids scrolling entirely.
            
            # Find the dropdown container. Discord renders them in order: Month, Day, Year
            # Each has a div with class containing 'css-' and shows placeholder text
            idx = {"Month": 0, "Day": 1, "Year": 2}.get(label, -1)
            if idx < 0:
                return False
            
            # Try multiple selectors for the dropdown trigger
            dropdown_clicked = False
            
            # Selector 1: The input control containers (most reliable for Discord 2024+)
            containers = self._page.locator('div[class*="inputContainer"] div[class*="css-"][class*="control"], div[class*="select"] div[class*="control"]')
            if await containers.count() >= 3:
                await containers.nth(idx).click()
                dropdown_clicked = True
            
            if not dropdown_clicked:
                # Selector 2: Look for the placeholder/value text divs
                dob_labels = self._page.locator(f'div[class*="css-"]:has-text("{label}")')
                for i in range(await dob_labels.count()):
                    el = dob_labels.nth(i)
                    text = await el.text_content()
                    if text and text.strip() == label:
                        # Click the parent control
                        parent = el.locator('..')
                        await parent.click()
                        dropdown_clicked = True
                        break
            
            if not dropdown_clicked:
                # Selector 3: Generic approach - find all dropdown-like containers
                dropdowns = self._page.locator('[class*="lookFilled"][class*="select"], [class*="Select"] [class*="css-"]')
                if await dropdowns.count() >= 3:
                    await dropdowns.nth(idx).click()
                    dropdown_clicked = True
            
            if not dropdown_clicked:
                # Selector 4: Just find any element showing the placeholder text for this field
                placeholder = self._page.locator(f'text="{label}"').first
                if await placeholder.count() > 0:
                    await placeholder.click()
                    dropdown_clicked = True
            
            if not dropdown_clicked:
                print(f"[Activity] Could not find dropdown for {label}")
                # Last resort: use keyboard to tab to the field
                for _ in range(idx + 1):
                    await self._page.keyboard.press('Tab')
                    await asyncio.sleep(0.1)
                await self._page.keyboard.press('Space')
                dropdown_clicked = True
            
            await asyncio.sleep(0.5)
            
            # === Now type the value to search/filter ===
            # Discord's dropdowns support keyboard input to filter options
            await self._page.keyboard.type(value, delay=50)
            await asyncio.sleep(0.4)
            
            # Try to find and click the matching option
            option_found = False
            
            # Look for visible option matching our value
            option_selectors = [
                f'[id*="option"]:has-text("{value}")',
                f'[role="option"]:has-text("{value}")',
                f'div[class*="option"]:has-text("{value}")',
                f'li:has-text("{value}")',
            ]
            
            for sel in option_selectors:
                option = self._page.locator(sel)
                if await option.count() > 0:
                    # Click the first exact or closest match
                    for i in range(await option.count()):
                        opt_text = await option.nth(i).text_content()
                        if opt_text and value.lower() in opt_text.lower():
                            await option.nth(i).click()
                            option_found = True
                            break
                    if option_found:
                        break
            
            if not option_found:
                # Press Enter to select the first filtered result
                await self._page.keyboard.press('Enter')
                option_found = True
            
            await asyncio.sleep(0.3)
            
            if option_found:
                print(f"[Activity] Selected {label}: {value} via type-to-search")
                return True
            
            # Close menu if still open
            await self._page.keyboard.press('Escape')
            await asyncio.sleep(0.2)
            print(f"[Activity] Failed to select {label}: {value}")
            return False
            
        except Exception as e:
            print(f"[Activity] DOB selection error for {label}: {e}")
            import traceback
            traceback.print_exc()
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
            
            print("[Activity] Clicking Create Account button")
            await self._page.get_by_role("button", {"name": "Create Account"}).click()
            await asyncio.sleep(5)
            
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
