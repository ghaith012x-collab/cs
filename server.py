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
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
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
        """Select DOB field. Discord uses react-select custom dropdowns."""
        try:
            print(f"[Activity] Selecting {label}: {value}")
            
            # Method 1: Try native <select> elements first
            selects = self._page.locator('select')
            select_count = await selects.count()
            if select_count >= 3:
                idx = {"Month": 0, "Day": 1, "Year": 2}.get(label, -1)
                if idx >= 0 and idx < select_count:
                    try:
                        await selects.nth(idx).select_option(value=value)
                        await asyncio.sleep(0.3)
                        print(f"[Activity] Selected {label}: {value} via native select (value)")
                        return True
                    except:
                        try:
                            await selects.nth(idx).select_option(label=value)
                            await asyncio.sleep(0.3)
                            print(f"[Activity] Selected {label}: {value} via native select (label)")
                            return True
                        except:
                            pass
            
            # Method 2: Discord's react-select custom dropdowns
            # These have IDs like react-select-*-placeholder with text "Month"/"Day"/"Year"
            placeholder = self._page.locator(f'div[id*="react-select"][id*="placeholder"]:has-text("{label}")')
            if await placeholder.count() == 0:
                # Try the parent container that shows the placeholder text
                placeholder = self._page.locator(f'div[class*="css-"]:has(> div[id*="placeholder"]:has-text("{label}"))')
            if await placeholder.count() == 0:
                # Try any clickable element near the label text
                placeholder = self._page.locator(f'div[class*="indicatorContainer"]').nth(
                    {"Month": 0, "Day": 1, "Year": 2}.get(label, 0)
                )
            
            if await placeholder.count() > 0:
                await placeholder.first.click()
                await asyncio.sleep(0.5)
                
                # Look for the option in the opened menu
                option = self._page.locator(f'div[id*="react-select"][id*="option"]:has-text("{value}")')
                if await option.count() == 0:
                    option = self._page.locator(f'[role="option"]:has-text("{value}")')
                if await option.count() == 0:
                    option = self._page.locator(f'div[class*="option"]:has-text("{value}")')
                
                if await option.count() > 0:
                    await option.first.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
                    await option.first.click()
                    await asyncio.sleep(0.3)
                    print(f"[Activity] Selected {label}: {value} via react-select")
                    return True
                else:
                    # Year needs scrolling - the menu is virtualized
                    menu = self._page.locator('div[class*="MenuList"], div[class*="menu-list"], [id*="react-select"][id*="listbox"]')
                    if await menu.count() > 0:
                        for scroll_attempt in range(30):
                            await menu.first.evaluate("el => el.scrollTop += 200")
                            await asyncio.sleep(0.15)
                            option = self._page.locator(f'div[id*="react-select"][id*="option"]:has-text("{value}")')
                            if await option.count() == 0:
                                option = self._page.locator(f'[role="option"]:has-text("{value}")')
                            if await option.count() > 0:
                                await option.first.scroll_into_view_if_needed()
                                await asyncio.sleep(0.1)
                                await option.first.click()
                                await asyncio.sleep(0.3)
                                print(f"[Activity] Selected {label}: {value} via scroll")
                                return True
                    
                    # Try scrolling up too (year might be above)
                    if await menu.count() > 0:
                        await menu.first.evaluate("el => el.scrollTop = 0")
                        await asyncio.sleep(0.2)
                        for scroll_attempt in range(30):
                            option = self._page.locator(f'div[id*="react-select"][id*="option"]:has-text("{value}")')
                            if await option.count() == 0:
                                option = self._page.locator(f'[role="option"]:has-text("{value}")')
                            if await option.count() > 0:
                                await option.first.scroll_into_view_if_needed()
                                await asyncio.sleep(0.1)
                                await option.first.click()
                                await asyncio.sleep(0.3)
                                print(f"[Activity] Selected {label}: {value} via scroll (up)")
                                return True
                            await menu.first.evaluate("el => el.scrollTop += 100")
                            await asyncio.sleep(0.1)
                    
                    print(f"[Activity] Could not find option '{value}' for {label} after scrolling")
                    # Close the menu by pressing Escape
                    await self._page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    return False
            
            # Method 3: JavaScript fallback - directly set react-select value
            js_success = await self._page.evaluate(f'''() => {{
                // Try native selects
                const selects = document.querySelectorAll('select');
                if (selects.length >= 3) {{
                    const idx = {{"Month": 0, "Day": 1, "Year": 2}}["{label}"];
                    if (idx !== undefined && selects[idx]) {{
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLSelectElement.prototype, 'value'
                        ).set;
                        nativeInputValueSetter.call(selects[idx], "{value}");
                        selects[idx].dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                }}
                return false;
            }}''')
            
            if js_success:
                print(f"[Activity] Selected {label}: {value} via JS fallback")
                return True
            
            print(f"[Activity] All methods failed for {label}: {value}")
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
