
import asyncio
import json
import random
import time
from datetime import datetime
from typing import Optional

from playwright.async_api import Page, expect

from captcha_solver import GodSolver, SolverConfig

class DiscordAutomation:
    def __init__(self, page: Page):
        self.page = page
        self.base_url = "https://discord.com/register"
        self.captcha_solver: Optional[GodSolver] = None

    async def navigate_to_register(self):
        await self.page.goto(self.base_url)
        await expect(self.page).to_have_url(self.base_url)

    async def _solve_hcaptcha_if_present(self):
        # Check for hCaptcha iframe
        hcaptcha_iframe_locator = self.page.frame_locator('iframe[src*="hcaptcha.com/captcha"]').or_(
            self.page.frame_locator('iframe[title="hCaptcha security check"]')
        )

        if await hcaptcha_iframe_locator.count() > 0:
            print("hCaptcha detected, attempting to solve...")
            if not self.captcha_solver:
                config = SolverConfig(
                    headless=False,
                    clip_confidence_threshold=0.55,
                    max_challenge_rounds=3,
                    timeout=30
                )
                self.captcha_solver = GodSolver(config)
            
            # Pass the current page to the solver
            solved = await self.captcha_solver.solve(self.page)
            if solved:
                print("hCaptcha solved successfully.")
                # Wait for the iframe to potentially disappear or for the page to react
                await self.page.wait_for_timeout(2000) # Small delay
                return True
            else:
                print("Failed to solve hCaptcha.")
                return False
        return True # No hCaptcha present

    async def _select_dob(self, month: str, day: str, year: str):
        print(f"Attempting to select DOB: {month}/{day}/{year}")

        # 1. Try native <select> elements first
        try:
            await self.page.locator('select[aria-label="Month"]').select_option(month)
            await self.page.locator('select[aria-label="Day"]').select_option(day)
            await self.page.locator('select[aria-label="Year"]').select_option(year)
            print("Successfully set DOB using native select elements.")
            return
        except Exception as e:
            print(f"Native select failed: {e}. Trying custom dropdowns.")

        # 2. Try clicking custom dropdown divs
        # Month
        month_div_locator = self.page.locator('div[id^="react-select-"][id$="-placeholder"]:has-text("Month")')
        if await month_div_locator.count() > 0:
            await month_div_locator.click()
            await self.page.locator(f'[id^="react-select-"][id$="-option-"]:has-text("{month}")').click()
            print(f"Selected month: {month}")
        else:
            print("Could not find custom month dropdown.")

        # Day
        day_div_locator = self.page.locator('div[id^="react-select-"][id$="-placeholder"]:has-text("Day")')
        if await day_div_locator.count() > 0:
            await day_div_locator.click()
            await self.page.locator(f'[id^="react-select-"][id$="-option-"]:has-text("{day}")').click()
            print(f"Selected day: {day}")
        else:
            print("Could not find custom day dropdown.")

        # Year (needs scrolling)
        year_div_locator = self.page.locator('div[id^="react-select-"][id$="-placeholder"]:has-text("Year")')
        if await year_div_locator.count() > 0:
            await year_div_locator.click()
            # Discord's year dropdown is a virtualized list, need to scroll
            year_option_locator = self.page.locator(f'[id^="react-select-"][id$="-option-"]:has-text("{year}")]')
            listbox_locator = self.page.locator('[id^="react-select-"][role="listbox"]').or_(self.page.locator('[class*="menu"]'))

            if await listbox_locator.count() > 0:
                listbox = listbox_locator.first
                max_scrolls = 50 # Prevent infinite loop
                for _ in range(max_scrolls):
                    if await year_option_locator.is_visible():
                        await year_option_locator.click()
                        print(f"Selected year: {year}")
                        break
                    await listbox.evaluate("node => node.scrollTop += 200") # Scroll down
                    await asyncio.sleep(0.1) # Small delay for rendering
                else:
                    print(f"Could not find year {year} after scrolling.")
            else:
                print("Could not find year listbox.")
        else:
            print("Could not find custom year dropdown.")

        # 3. JavaScript fallback (if all else fails)
        try:
            await self.page.evaluate("""
                (month, day, year) => {
                    const monthSelect = document.querySelector('select[aria-label="Month"]');
                    const daySelect = document.querySelector('select[aria-label="Day"]');
                    const yearSelect = document.querySelector('select[aria-label="Year"]');

                    if (monthSelect) { monthSelect.value = month; monthSelect.dispatchEvent(new Event('change')); }
                    if (daySelect) { daySelect.value = day; daySelect.dispatchEvent(new Event('change')); }
                    if (yearSelect) { yearSelect.value = year; yearSelect.dispatchEvent(new Event('change')); }
                }
            """, month, day, year)
            print("Successfully set DOB using JavaScript fallback.")
        except Exception as e:
            print(f"JavaScript fallback for DOB failed: {e}")

    async def fill_registration_form(self, email, username, password):
        print("Filling registration form...")
        await self.page.locator('input[name="email"]').fill(email)
        await self.page.locator('input[name="username"]').fill(username)
        await self.page.locator('input[name="password"]').fill(password)

        # Date of Birth
        # Year is always 1990-1999
        year = str(random.randint(1990, 1999))
        # Month uses number string ("1"-"12") for value-based select, and full name for text-based click
        month_num = str(random.randint(1, 12))
        month_name = datetime.strptime(month_num, "%m").strftime("%B")
        # Day uses number string ("1"-"28")
        day = str(random.randint(1, 28))

        await self._select_dob(month_name, day, year) # Pass month_name for custom dropdowns, day and year as numbers

        await self.page.locator('input[type="checkbox"]').check()
        await self.page.wait_for_timeout(1000) # Small delay

        # Attempt to solve hCaptcha before clicking continue
        if not await self._solve_hcaptcha_if_present():
            print("Failed to solve hCaptcha during registration form fill. Aborting.")
            return False

        await self.page.locator('button[type="submit"]').click()
        print("Clicked continue button.")
        return True

    async def close(self):
        if self.captcha_solver:
            await self.captcha_solver.close()

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False) # For testing, set headless=False
        page = await browser.new_page()
        discord_automation = DiscordAutomation(page)
        try:
            await discord_automation.navigate_to_register()
            # Replace with actual data for testing
            email = f"test_{int(time.time())}@example.com"
            username = f"testuser_{int(time.time())}"
            password = "TestPassword123!"
            
            success = await discord_automation.fill_registration_form(email, username, password)
            if success:
                print("Registration form filled, awaiting next steps (e.g., email verification).")
                # You would add logic here to handle email verification, etc.
                await page.wait_for_timeout(10000) # Keep page open for a bit to observe
            else:
                print("Registration failed.")

        except Exception as e:
            print(f"An error occurred: {e}")
        finally:
            await discord_automation.close()
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
