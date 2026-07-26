import asyncio
import base64
import json
import os
import random
import socket
from typing import Optional

from playwright.async_api import async_playwright

from captcha_solver import VisionSolver


# ── TOR Control ───────────────────────────────────────────

def _tor_newnym():
    """Signal TOR to switch to a new identity."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", 9051))
        s.recv(1024)
        s.sendall(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(1024).decode().strip()
        s.close()
        return "250" in resp
    except Exception as e:
        print(f"[TOR] newnym error: {e}", flush=True)
    return False


def _tor_check():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 9050))
        s.close()
        return True
    except:
        return False


USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
]


AUTO_EMAIL = "alistra742@gmail.com"


class DiscordAutomation:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._screenshots: list = []
        self._activity_log: list = []
        self._email = AUTO_EMAIL
        self._username = ""
        self._password = ""
        self._vision = VisionSolver(log=self._log)

    def _log(self, message: str, level: str = "info") -> None:
        import time as _time
        entry = {
            "time": _time.strftime("%H:%M:%S"),
            "timestamp": _time.time(),
            "level": level,
            "message": message
        }
        self._activity_log.append(entry)
        if len(self._activity_log) > 500:
            self._activity_log = self._activity_log[-500:]
        print(f"[{entry['time']}] [{level.upper()}] {message}", flush=True)

    def get_activity_log(self) -> list:
        return self._activity_log

    async def initialize(self) -> None:
        if _tor_check():
            self._log("[TOR] Rotating IP for new session...")
            if _tor_newnym():
                self._log("[TOR] New identity requested")
            else:
                self._log("[TOR] Using current circuit", level="warn")
            await asyncio.sleep(2)
        else:
            self._log("[TOR] Not available", level="warn")

        self._playwright = await async_playwright().start()

        args = [
            '--disable-blink-features=AutomationDetected',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-webgl',
            '--disable-features=IsolateOrigins,site-per-process',
        ]

        ua = random.choice(USER_AGENTS)
        self._browser = await self._playwright.chromium.launch(headless=self.headless, args=args)

        ctx_opts = {
            'viewport': {'width': 1920, 'height': 1080},
            'user_agent': ua,
        }
        if _tor_check():
            ctx_opts['proxy'] = {'server': 'socks5://127.0.0.1:9050'}

        self._context = await self._browser.new_context(**ctx_opts)
        self._log(f"User-Agent: {ua[:60]}...")

        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
            Object.defineProperty(navigator, 'languages', { get: () => Object.freeze(['en-US', 'en']) });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
        """)

        self._page = await self._context.new_page()

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

        self._log("=" * 40)
        self._log(f"Starting Discord signup with email: {self._email}")
        self._log("=" * 40)

        try:
            self._log("Navigating to Discord registration...")
            await self._page.goto('https://discord.com/register', wait_until='domcontentloaded', timeout=20000)
            await asyncio.sleep(5)
            await self.capture_screenshot()

            # Fill the form
            form_ok = await self._fill_registration_form()
            if form_ok:
                self._log("✓ Form filled — now solving captcha with AI Vision...")
                success = await self._solve_hcaptcha_if_present()
                if success:
                    self._log("✓ CAPTCHA SOLVED! Registration submitted.")
                else:
                    self._log("✗ Captcha solving failed", level="error")
            else:
                self._log("✗ Form filling failed", level="error")
                success = False

        except Exception as e:
            self._log(f"Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            success = False

        await self.capture_screenshot()
        return success

    async def _solve_hcaptcha_if_present(self) -> bool:
        """Detect and solve hCaptcha using CLIP vision AI (no APIs)."""
        try:
            self._log("[Vision AI] Checking for hCaptcha...")

            # Find the captcha iframe
            iframe = None
            for attempt in range(25):
                iframe_el = await self._page.query_selector('iframe[src*="hcaptcha.com"]')
                if iframe_el:
                    self._log(f"[Vision AI] hCaptcha iframe found (attempt {attempt+1})")
                    iframe = iframe_el
                    break
                # Check if the page navigated away (solved/redirected)
                cur_url = self._page.url
                if any(k in cur_url for k in ['/channels', '/verify', '/welcome', '/login']):
                    self._log(f"[Vision AI] Page navigated to {cur_url} — no captcha needed")
                    return True
                await asyncio.sleep(0.5)

            if not iframe:
                self._log("[Vision AI] No hCaptcha detected — proceeding")
                return True

            await asyncio.sleep(1)
            await self.capture_screenshot()

            # Solve using CLIP vision AI
            self._log("[Vision AI] Solving with CLIP (no APIs)...")

            # Pre-load the model
            await self._vision.ensure_model_loaded()

            # The solver handles: click checkbox → extract text → classify tiles → click → verify
            token = await self._vision.solve_captcha(self._page, iframe)

            if token:
                self._log(f"[Vision AI] ✓ Token obtained! {token[:25]}...")
                # Inject token into the parent page
                await self._vision.set_token_on_page(self._page, token)
                return True
            else:
                self._log("[Vision AI] ✗ Could not solve captcha", level="error")
                return False

        except Exception as e:
            self._log(f"[Vision AI] Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _select_dob(self, label: str, option_text: str) -> bool:
        """Select DOB dropdown. Discord uses custom React-Select components."""
        try:
            self._log(f"Selecting {label}: {option_text}")

            # Strategy 1: JS click on placeholder, then find and click option
            success = await self._page.evaluate(f"""
                async () => {{
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    let targetEl = null;
                    while (node = walker.nextNode()) {{
                        if (node.textContent.trim() === '{label}') {{
                            const parent = node.parentElement;
                            if (parent && parent.offsetParent !== null &&
                                !parent.querySelector('input[name="email"]')) {{
                                targetEl = parent;
                                break;
                            }}
                        }}
                    }}

                    if (!targetEl) return 'no_element';

                    let clickTarget = targetEl;
                    for (let i = 0; i < 5; i++) {{
                        clickTarget = clickTarget.parentElement;
                        if (!clickTarget) break;
                        const style = window.getComputedStyle(clickTarget);
                        if (style.cursor === 'pointer' ||
                            clickTarget.getAttribute('tabindex') !== null ||
                            clickTarget.className.includes('control') ||
                            clickTarget.className.includes('css-')) {{
                            break;
                        }}
                    }}

                    if (!clickTarget) clickTarget = targetEl;
                    clickTarget.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
                    await new Promise(r => setTimeout(r, 600));

                    const allOptions = document.querySelectorAll(
                        '[id*="option"], [role="option"], [class*="option"]'
                    );
                    for (const opt of allOptions) {{
                        const text = opt.textContent.trim();
                        if (text === '{option_text}') {{
                            opt.scrollIntoView({{block: 'nearest'}});
                            opt.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                            opt.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                            opt.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                            return 'selected';
                        }}
                    }}
                    return 'option_not_found';
                }}
            """)

            if success and 'selected' in str(success):
                self._log(f"Selected {label}: {option_text} ({success})")
                await asyncio.sleep(0.4)
                return True

            self._log(f"JS result for {label}: {success}")

            # Strategy 2: Click the placeholder text, then type to filter
            try:
                placeholder = self._page.get_by_text(label, exact=True)
                if await placeholder.count() > 0:
                    await placeholder.first.click()
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.type(option_text, delay=30)
                    await asyncio.sleep(0.4)
                    await self._page.keyboard.press('Enter')
                    await asyncio.sleep(0.4)
                    self._log(f"Selected {label} via text click")
                    return True
            except:
                pass

            # Strategy 3: Tab navigation
            try:
                idx = {"Month": 0, "Day": 1, "Year": 2}.get(label, 0)
                pw = self._page.locator('input[name="password"]')
                if await pw.count() > 0:
                    await pw.click()
                    await asyncio.sleep(0.2)
                    for _ in range(idx + 1):
                        await self._page.keyboard.press('Tab')
                        await asyncio.sleep(0.15)
                    await self._page.keyboard.press('Space')
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.type(option_text, delay=30)
                    await asyncio.sleep(0.3)
                    await self._page.keyboard.press('Enter')
                    await asyncio.sleep(0.4)
                    self._log(f"Selected {label} via tab")
                    return True
            except:
                pass

            self._log(f"All DOB strategies failed for {label}")
            return False

        except Exception as e:
            self._log(f"DOB error for {label}: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _fill_registration_form(self) -> bool:
        try:
            self._log("=" * 40)
            self._log("FILLING REGISTRATION FORM")
            self._log("=" * 40)
            self._log(f"Email: {self._email}")

            await self._page.wait_for_selector('input[name="email"]', timeout=15000)
            await self._page.locator('input[name="email"]').fill(self._email)
            await self._human_pause()

            # Generate username
            consonants = 'bcdfghjklmnpqrstvwxyz'
            vowels = 'aeiou'
            username = ''
            for _ in range(random.randint(8, 12)):
                username += random.choice(vowels if random.random() < 0.35 else consonants)
            self._username = username
            display_name = self._username[:15]

            self._log(f"Display name: {display_name}")
            try:
                await self._page.wait_for_selector('input[name="global_name"]', timeout=5000)
                await self._page.locator('input[name="global_name"]').fill(display_name)
            except:
                pass
            await self._human_pause()

            self._log(f"Username: {self._username}")
            await self._page.locator('input[name="username"]').fill(self._username)
            await self._human_pause()

            # Generate password
            first = random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')
            body = ''
            for _ in range(random.randint(8, 11)):
                body += random.choice(vowels if random.random() < 0.35 else consonants)
            specials = '!@#$%&*'
            self._password = first + body + random.choice(specials) + str(random.randint(1, 99))

            self._log("Filling password")
            await self._page.locator('input[name="password"]').fill(self._password)
            await self._human_pause()

            # DOB
            month_val = random.randint(1, 12)
            day_val = str(random.randint(1, 28))
            year_val = str(random.randint(1990, 1999))
            months = ['January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December']
            month_name = months[month_val - 1]
            self._log(f"DOB: {month_name} {day_val}, {year_val}")

            await self._select_dob("Month", month_name)
            await self._human_pause()
            await self._select_dob("Day", day_val)
            await self._human_pause()
            await self._select_dob("Year", year_val)
            await self._human_pause()

            # ── ToS Checkbox (aggressive detection) ──────────────
            self._log("Checking ToS checkbox...")
            tos_checked = False

            # Strategy 1: Playwright locators
            for sel in [
                'input[type="checkbox"]',
                'div[role="checkbox"]',
                '[class*="checkbox"]',
                'label:has-text("Terms of Service")',
                'label:has-text("terms of service")',
                'label:has-text("agree")',
                'label:has-text("I have read")',
                'label:has-text("read and agree")',
            ]:
                cb = self._page.locator(sel)
                if await cb.count() > 0:
                    await cb.first.click()
                    tos_checked = True
                    self._log(f"✓ ToS via '{sel}'")
                    break

            # Strategy 2: Find by text "Terms of Service" and click parent
            if not tos_checked:
                try:
                    tos_text = self._page.get_by_text("Terms of Service", exact=False)
                    if await tos_text.count() > 0:
                        # Click the parent label or container
                        el = tos_text.first
                        parent = self._page.locator(':has-text("Terms of Service")').first
                        await parent.click()
                        tos_checked = True
                        self._log("✓ ToS via text-content click")
                except Exception as e:
                    self._log(f"ToS text click error: {e}")

            # Strategy 3: Find by text "I have read" 
            if not tos_checked:
                try:
                    read_text = self._page.get_by_text("I have read", exact=False)
                    if await read_text.count() > 0:
                        await read_text.first.click()
                        tos_checked = True
                        self._log("✓ ToS via 'I have read' text click")
                except:
                    pass

            # Strategy 4: JS — find ANY element with checkbox-related text
            if not tos_checked:
                try:
                    result = await self._page.evaluate("""() => {
                        // Find element containing "Terms of Service" text
                        const walker = document.createTreeWalker(document.body, 4, null);
                        let node;
                        while (node = walker.nextNode()) {
                            if (node.textContent.includes('Terms of Service') || 
                                node.textContent.includes('terms of service') ||
                                node.textContent.includes('I have read')) {
                                // Click the parent that's clickable
                                let el = node.parentElement;
                                for (let i = 0; i < 10; i++) {
                                    if (!el) break;
                                    const cs = window.getComputedStyle(el);
                                    if (cs.cursor === 'pointer' || el.tagName === 'LABEL' || el.tagName === 'A') {
                                        el.click();
                                        return 'clicked ' + el.tagName;
                                    }
                                    el = el.parentElement;
                                }
                                // Fallback: click the text node's parent
                                node.parentElement.click();
                                return 'clicked parent';
                            }
                        }
                        // Last resort: click any unchecked checkbox-like element
                        const checkboxes = document.querySelectorAll('[class*="checkbox"], [role="checkbox"]');
                        for (const cb of checkboxes) {
                            const checked = cb.getAttribute('aria-checked');
                            if (checked !== 'true') {
                                cb.click();
                                return 'clicked checkbox element';
                            }
                        }
                        return 'nothing found';
                    }""")
                    if result and 'clicked' in str(result):
                        tos_checked = True
                        self._log(f"✓ ToS checkbox {result}")
                except Exception as e:
                    self._log(f"JS ToS error: {e}")

            if tos_checked:
                self._log("✓ ToS checkbox checked")
                await asyncio.sleep(0.3)
            else:
                self._log("No ToS checkbox found — proceeding anyway")

            # ── Create Account Button (aggressive) ─────────────
            self._log("Clicking Create Account...")
            create_clicked = False

            for sel in [
                'button:has-text("Create Account")',
                'button:has-text("Continue")',
                'button:has-text("Sign Up")',
                'button:has-text("Create")',
                'button[type="submit"]',
                '[type="submit"]',
            ]:
                try:
                    btn = self._page.locator(sel)
                    if await btn.count() > 0:
                        await btn.first.click()
                        create_clicked = True
                        self._log(f"✓ Clicked via '{sel}'")
                        break
                except:
                    pass

            if not create_clicked:
                try:
                    btn = self._page.get_by_role("button", name="Create Account")
                    if await btn.count() > 0:
                        await btn.first.click()
                        create_clicked = True
                        self._log("✓ Clicked via get_by_role")
                except:
                    pass

            if not create_clicked:
                try:
                    btn = self._page.get_by_role("button", name="Continue")
                    if await btn.count() > 0:
                        await btn.first.click()
                        create_clicked = True
                        self._log("✓ Clicked Continue button")
                except:
                    pass

            if not create_clicked:
                # JS fallback
                result = await self._page.evaluate("""() => {
                    const btns = document.querySelectorAll('button, [role="button"], a[class*="button"]');
                    for (const btn of btns) {
                        const t = btn.textContent.toLowerCase();
                        if (t.includes('create account') || t.includes('sign up') || t.includes('continue')) {
                            btn.click();
                            return 'clicked ' + t.trim();
                        }
                    }
                    const submit = document.querySelector('[type="submit"]');
                    if (submit) { submit.click(); return 'clicked submit'; }
                    // Submit the form directly
                    const form = document.querySelector('form');
                    if (form) { form.requestSubmit(); return 'form submitted'; }
                    return 'failed';
                }""")
                if result and result != 'failed':
                    create_clicked = True
                    self._log(f"✓ JS button click: {result}")

            if create_clicked:
                self._log("Create Account clicked — waiting...")
            else:
                self._log("WARNING: Could not click Create Account!", level="error")
                # Try pressing Enter on the password field as last resort
                try:
                    await self._page.locator('input[name="password"]').press('Enter')
                    self._log("Pressed Enter on password field")
                    create_clicked = True
                except:
                    pass

            await asyncio.sleep(3)
            await self.capture_screenshot()

            return True

        except Exception as e:
            self._log(f"Form filling error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

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


async def run_discord_automation():
    bot = DiscordAutomation(headless=True)
    try:
        await bot.initialize()
        success = await bot.start_discord_signup()
        if success:
            print("✓ Discord automation completed")
        else:
            print("✗ Discord automation failed")
        await asyncio.sleep(5)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(run_discord_automation())
