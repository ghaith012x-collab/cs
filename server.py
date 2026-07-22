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
        """Select DOB field using JavaScript to directly manipulate Discord's React state.
        This bypasses all the CSS selector issues by using React's internal fiber/props.
        """
        try:
            print(f"[Activity] Selecting {label}: {value}")
            idx = {"Month": 0, "Day": 1, "Year": 2}.get(label, -1)
            if idx < 0:
                return False

            # === METHOD 1: Direct JavaScript manipulation of the select elements ===
            # Discord's DOB uses custom select components but they render as
            # clickable divs. We'll use a robust JS approach.
            success = await self._page.evaluate(f"""
                async (labelText, targetValue, fieldIdx) => {{
                    // Find all DOB dropdown containers
                    // Discord wraps each in a div with role or specific structure
                    // The dropdowns show "Month", "Day", "Year" as placeholder text
                    
                    // Strategy: find elements containing the placeholder text
                    const allElements = document.querySelectorAll('*');
                    let dropdownTrigger = null;
                    
                    // Look for the specific placeholder div
                    for (const el of allElements) {{
                        if (el.textContent === labelText && 
                            el.children.length === 0 &&
                            el.offsetParent !== null) {{
                            // Found the placeholder text element, get the clickable parent
                            let parent = el.parentElement;
                            for (let i = 0; i < 5; i++) {{
                                if (parent && (parent.getAttribute('role') === 'button' ||
                                    parent.getAttribute('role') === 'listbox' ||
                                    parent.classList.toString().includes('control') ||
                                    parent.classList.toString().includes('select') ||
                                    parent.getAttribute('tabindex'))) {{
                                    dropdownTrigger = parent;
                                    break;
                                }}
                                parent = parent ? parent.parentElement : null;
                            }}
                            if (!dropdownTrigger) dropdownTrigger = el.parentElement;
                            break;
                        }}
                    }}
                    
                    if (!dropdownTrigger) {{
                        // Fallback: get all elements that look like select triggers by index
                        const triggers = document.querySelectorAll('[class*="css-"][tabindex], [class*="control"]');
                        // Filter to only those in the DOB area
                        const dobArea = document.querySelector('[class*="birthday"], [class*="dob"], [class*="dateOfBirth"]');
                        if (dobArea) {{
                            const dobTriggers = dobArea.querySelectorAll('[tabindex]');
                            if (dobTriggers.length > fieldIdx) {{
                                dropdownTrigger = dobTriggers[fieldIdx];
                            }}
                        }}
                    }}
                    
                    if (!dropdownTrigger) return false;
                    
                    // Click to open
                    dropdownTrigger.click();
                    dropdownTrigger.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                    
                    // Wait for menu to appear
                    await new Promise(r => setTimeout(r, 500));
                    
                    // Find and click the option
                    const options = document.querySelectorAll('[id*="option"], [role="option"], [class*="option"]');
                    for (const opt of options) {{
                        if (opt.textContent.trim() === targetValue) {{
                            opt.click();
                            return true;
                        }}
                    }}
                    
                    // If not found, try scrolling the menu
                    const menu = document.querySelector('[class*="MenuList"], [class*="menu-list"], [id*="listbox"]');
                    if (menu) {{
                        for (let i = 0; i < 50; i++) {{
                            menu.scrollTop += 150;
                            await new Promise(r => setTimeout(r, 100));
                            const opts = document.querySelectorAll('[id*="option"], [role="option"], [class*="option"]');
                            for (const opt of opts) {{
                                if (opt.textContent.trim() === targetValue) {{
                                    opt.click();
                                    return true;
                                }}
                            }}
                        }}
                    }}
                    
                    return false;
                }}
            """, [label, value, idx])

            if success:
                print(f"[Activity] Selected {label}: {value} via JS method")
                await asyncio.sleep(0.3)
                return True

            # === METHOD 2: Click the visible dropdown text and use keyboard ===
            print(f"[Activity] JS method failed for {label}, trying click+keyboard...")
            
            # Find the dropdown by its visible text
            dropdown = self._page.get_by_text(label, exact=True).first
            if await dropdown.count() > 0:
                await dropdown.click()
                await asyncio.sleep(0.5)
                
                # Type to filter (Discord dropdowns are searchable)
                await self._page.keyboard.type(value, delay=40)
                await asyncio.sleep(0.3)
                await self._page.keyboard.press('Enter')
                await asyncio.sleep(0.3)
                print(f"[Activity] Selected {label}: {value} via click+type+enter")
                return True

            # === METHOD 3: Tab navigation ===
            print(f"[Activity] Click failed for {label}, trying tab navigation...")
            # After password field, tab to Month, Day, Year
            # Password is the last input before DOB
            password_field = self._page.locator('input[name="password"]')
            if await password_field.count() > 0:
                await password_field.click()
                await asyncio.sleep(0.2)
                # Tab forward: after password -> Month(1) -> Day(2) -> Year(3)
                tabs_needed = idx + 1
                for _ in range(tabs_needed):
                    await self._page.keyboard.press('Tab')
                    await asyncio.sleep(0.15)
                # Open dropdown
                await self._page.keyboard.press('Space')
                await asyncio.sleep(0.4)
                # Type value
                await self._page.keyboard.type(value, delay=40)
                await asyncio.sleep(0.3)
                await self._page.keyboard.press('Enter')
                await asyncio.sleep(0.3)
                print(f"[Activity] Selected {label}: {value} via tab+type+enter")
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
