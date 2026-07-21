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
            '--disable-blink-features=AutomationControlled',
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
        
        self._email = config.get('email', '')
        self._username = config.get('username', self._generate_username())
        self._password = config.get('password', self._generate_password())
    
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
        
        await self._page.goto('https://discord.com/register', wait_until='networkidle')
        await asyncio.sleep(2)
        
        await self.capture_screenshot()
        
        success = await self._fill_registration_form()
        
        await self.capture_screenshot()
        
        return success

    async def _solve_hcaptcha_if_present(self) -> bool:
        try:
            hcaptcha_iframe = await self._page.query_selector('iframe[src*="hcaptcha.com"], iframe[src*="captcha.hcaptcha.com"]')
            if not hcaptcha_iframe:
                return True
            
            config = captcha_solver.SolverConfig(
                headless=False,
                confidence_threshold=0.65,
                max_retries=3,
                timeout=30
            )
            
            solver = captcha_solver.GodSolver(config)
            success = await solver.solve(self._page.url)
            await solver.close()
            
            return success
        except Exception as e:
            print(f"hCaptcha solve error: {e}")
            return True

    async def _fill_registration_form(self) -> bool:
        try:
            email_input = await self._page.wait_for_selector(
                'input[type="email"], input[name="email"], #email',
                timeout=10000
            )
            
            email = self._email or await self.read_email_from_file('test/site.html')
            await email_input.fill(email)
            await self._human_pause()
            
            username_input = await self._page.wait_for_selector(
                'input[autocomplete="username"], input[name="username"], #username',
                timeout=10000
            )
            await username_input.fill(self._username)
            await self._human_pause()
            
            password_input = await self._page.wait_for_selector(
                'input[type="password"], input[name="password"], #password',
                timeout=10000
            )
            await password_input.fill(self._password)
            await self._human_pause()
            
            confirm_input = await self._page.query_selector(
                'input[autocomplete="new-password"], input[name="password-confirm"], #password-confirm'
            )
            if confirm_input:
                await confirm_input.fill(self._password)
                await self._human_pause()
            
            dob_selectors = [
                'select[name="day"], select#day',
                'select[name="month"], select#month',
                'select[name="year"], select#year'
            ]
            
            day_select = await self._page.query_selector(dob_selectors[0])
            month_select = await self._page.query_selector(dob_selectors[1])
            year_select = await self._page.query_selector(dob_selectors[2])
            
            if day_select:
                await day_select.select_option(str(random.randint(1, 28)))
                await self._human_pause()
            
            if month_select:
                await month_select.select_option(str(random.randint(1, 12)))
                await self._human_pause()
            
            if year_select:
                years = [str(y) for y in range(1990, 2005)]
                await year_select.select_option(random.choice(years))
                await self._human_pause()
            
            submit_selectors = [
                'button[type="submit"]',
                'button[name="submit"]',
                '#provided-choices-recaptcha-response button',
                'div[role="button"]:has-text("Next")',
                'div[role="button"]:has-text("Continue")'
            ]
            
            submit_btn = None
            for selector in submit_selectors:
                try:
                    submit_btn = await self._page.query_selector(selector)
                    if submit_btn:
                        break
                except:
                    continue
            
            if submit_btn:
                await self._human_click(submit_btn)
                await asyncio.sleep(3)
                
                if await self._solve_hcaptcha_if_present():
                    return True
                else:
                    return False
            
            return False
            
        except Exception as e:
            print(f"Form filling error: {e}")
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