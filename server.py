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
        self._activity_log: list = []  # Real activity log - never lies, never fakes
        self._email = ""
        self._username = ""
        self._password = ""

    def _log(self, message: str, level: str = "info") -> None:
        """Add a truthful entry to the activity log. Never fake, never lie."""
        import time as _time
        entry = {
            "time": _time.strftime("%H:%M:%S"),
            "timestamp": _time.time(),
            "level": level,
            "message": message
        }
        self._activity_log.append(entry)
        # Keep last 200 entries
        if len(self._activity_log) > 200:
            self._activity_log = self._activity_log[-200:]
        print(f"[{entry['time']}] [{level.upper()}] {message}", flush=True)

    def get_activity_log(self) -> list:
        """Return the real activity log."""
        return self._activity_log

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
        self._password = self._generate_password()  # Always generate strong password
        self._log(f"Config: Email: {self._email}, Username: {self._username}, Password set: {bool(self._password)}")
    
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
        """Generate a strong password that looks natural.
        Format: Capital letter start + random lowercase blend + special chars + digits
        Example: Jxhaishdbd!3, Kqmvtpwle@7, Bznhcxfwoj#9
        """
        # Start with a capital letter
        first = random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')
        # Random lowercase blend (8-11 chars, consonant-heavy like username)
        consonants = 'bcdfghjklmnpqrstvwxyz'
        vowels = 'aeiou'
        body_len = random.randint(8, 11)
        body = ''
        for _ in range(body_len):
            if random.random() < 0.35:
                body += random.choice(vowels)
            else:
                body += random.choice(consonants)
        # Add 1-2 special chars and 1-2 digits at the end
        specials = '!@#$%&*'
        tail = random.choice(specials) + str(random.randint(1, 99))
        return first + body + tail

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
            form_filled_successfully = await asyncio.wait_for(self._fill_registration_form(), timeout=60)
            if form_filled_successfully:
                self._log("Registration form filled, checking for captcha...")
                success = await self._solve_hcaptcha_if_present()
                if not success:
                    self._log("Captcha solving failed.", level="error")
            else:
                self._log("Registration form filling failed.", level="error")
                success = False
        except asyncio.TimeoutError:
            self._log("Form filling timed out after 60 seconds")
            success = False
        
        await self.capture_screenshot()
        
        return success

    async def _solve_hcaptcha_if_present(self) -> bool:
        """Detect and solve hCaptcha with robust multi-method detection."""
        try:
            self._log("Checking for hCaptcha...")
            

            
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
                    self._log(f"hCaptcha iframe detected (attempt {attempt+1})")
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
                    self._log(f"hCaptcha div detected (attempt {attempt+1})")
                    break
                
                # Method 3: Check for hcaptcha response textarea (means it loaded)
                textarea = await self._page.query_selector(
                    'textarea[name="h-captcha-response"], '
                    'textarea[name="g-recaptcha-response"]'
                )
                if textarea:
                    captcha_found = True
                    self._log(f"hCaptcha textarea detected (attempt {attempt+1})")
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
                    self._log(f"hCaptcha detected via JS ({js_detected}, attempt {attempt+1})")
                    break
                
                await asyncio.sleep(0.5)
            
            if not captcha_found:
                self._log("No hCaptcha detected after 10s - might have passed without captcha")
                # Check if we're on a success page or error page
                current_url = self._page.url
                if 'channels' in current_url or 'app' in current_url:
                    self._log("Redirected to app - registration succeeded without captcha!")
                    return True
                return True  # No captcha = success
            
            # Wait a moment for the captcha to fully render
            await asyncio.sleep(2)
            
            # First, check if there's a checkbox to click before the challenge appears
            try:
                checkbox_frame = self._page.frame_locator('iframe[src*="hcaptcha"], iframe[title*="Widget containing checkbox"], iframe[title*="hCaptcha"]')
                checkbox = checkbox_frame.locator('#checkbox, [role="checkbox"]')
                if await checkbox.count() > 0:
                    self._log("Clicking hCaptcha checkbox...")
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
                        self._log("hCaptcha passed with just checkbox click!")
                        return True
            except Exception as e:
                self._log(f"Checkbox click attempt: {e}")
            
            # Solve with loop: re-detect captcha after clicking Create Account
            config = captcha_solver.SolverConfig(
                headless=False,
                clip_confidence_threshold=0.45,
                max_challenge_rounds=5,
                timeout=35,
                rate_limit_min_delay=0.05,
                rate_limit_max_delay=0.15,
                min_solve_time_per_round=1.2,
                ollama_timeout=20,
                ollama_num_ctx=1024,
                ollama_temperature=0.05,
            )
            
            max_captcha_loops = 5  # Handle up to 5 consecutive captchas
            master_solver = captcha_solver.MasterSolver(config)
            
            for captcha_attempt in range(1, max_captcha_loops + 1):
                self._log(f"Captcha attempt {captcha_attempt}/{max_captcha_loops} - Attempting MasterSolver...")
                success = await master_solver.solve(self._page)
                
                if not success:
                    self._log(f"hCaptcha solve FAILED on attempt {captcha_attempt}")
                    await master_solver.close()
                    return False
                
                self._log(f"hCaptcha SOLVED on attempt {captcha_attempt}!")
                await asyncio.sleep(1.5)
                
                # Click "Create Account" after solving
                self._log("Clicking Create Account after captcha solve...")
                try:
                    create_btn = self._page.locator('button:has-text("Create Account"), button:has-text("create account"), button[type="submit"]:has-text("Create"), button:has-text("Register"), button:has-text("Sign Up")')
                    if await create_btn.count() > 0:
                        await create_btn.first.click()
                        self._log("Clicked Create Account button.")
                    else:
                        submit_btn = self._page.locator('button[type="submit"]')
                        if await submit_btn.count() > 0:
                            await submit_btn.first.click()
                            self._log("Clicked submit button (fallback).")
                        else:
                            self._log("No Create Account button found, pressing Enter...")
                            await self._page.keyboard.press("Enter")
                except Exception as btn_err:
                    self._log(f"Error clicking Create Account: {btn_err}")
                
                # Scan for 15 seconds to detect if a new captcha appears
                self._log("Scanning for new captcha (15s window)...")
                new_captcha = False
                scan_start = asyncio.get_event_loop().time()
                scan_duration = 15  # seconds
                
                # All possible hCaptcha iframe selectors
                captcha_selectors = [
                    "iframe[src*='newassets.hcaptcha.com/captcha']",
                    "iframe[src*='hcaptcha.com/captcha']",
                    "iframe[src*='imgs.hcaptcha.com']",
                    "iframe[title*='hCaptcha challenge']",
                    "iframe[title*='hcaptcha challenge']",
                    "iframe[data-hcaptcha-widget-id]",
                    "iframe[src*='hcaptcha'][style*='width']",
                ]
                
                while (asyncio.get_event_loop().time() - scan_start) < scan_duration:
                    # Check if page navigated away (success!)
                    try:
                        current_url = self._page.url
                        if any(kw in current_url for kw in ['verify', 'confirm', 'welcome', 'home', 'dashboard', 'app']):
                            self._log(f"Page navigated to: {current_url} - signup successful!")
                            await master_solver.close()
                            return True
                    except:
                        pass
                    
                    # Check ALL possible captcha iframe selectors
                    for sel in captcha_selectors:
                        try:
                            challenge_iframe = self._page.locator(sel)
                            challenge_count = await challenge_iframe.count()
                            
                            if challenge_count > 0:
                                for i in range(challenge_count):
                                    try:
                                        frame = challenge_iframe.nth(i)
                                        box = await frame.bounding_box()
                                        if box and box['width'] > 100 and box['height'] > 100:
                                            is_visible = await frame.is_visible()
                                            if is_visible:
                                                new_captcha = True
                                                self._log(f"New active captcha detected! (selector: {sel}, size: {box['width']:.0f}x{box['height']:.0f})")
                                                break
                                    except:
                                        continue
                            if new_captcha:
                                break
                        except:
                            continue
                    
                    if new_captcha:
                        break
                    
                    # Also check for any large overlay/modal that appeared (captcha container)
                    try:
                        overlay = self._page.locator("div[style*='position: fixed'], div[style*='position:fixed'], .hcaptcha-box, #hcaptcha, .captcha-container")
                        if await overlay.count() > 0:
                            for i in range(await overlay.count()):
                                box = await overlay.nth(i).bounding_box()
                                if box and box['width'] > 200 and box['height'] > 200:
                                    # Check if it contains an iframe
                                    inner_frames = await overlay.nth(i).locator("iframe").count()
                                    if inner_frames > 0:
                                        new_captcha = True
                                        self._log(f"Captcha overlay/container detected! (size: {box['width']:.0f}x{box['height']:.0f})")
                                        break
                    except:
                        pass
                    
                    if new_captcha:
                        break
                    
                    await asyncio.sleep(1.5)  # Poll every 1.5s
                
                if not new_captcha:
                    self._log(f"No captcha appeared after {scan_duration}s scan - proceeding!")
                    await master_solver.close()
                    return True
                
                self._log(f"Re-solving captcha (loop {captcha_attempt + 1})...")
                await asyncio.sleep(0.5)
            
            self._log(f"Exhausted {max_captcha_loops} captcha attempts")
            await master_solver.close()
            return False
        except Exception as e:
            self._log(f"hCaptcha solve error: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _select_dob(self, label: str, option_text: str) -> bool:
        """Select DOB dropdown. Discord uses custom React-Select components.
        The dropdowns show placeholder text 'Month', 'Day', 'Year' and have NO
        role=combobox or aria-label. We find them by their visible text."""
        try:
            self._log(f"Selecting {label}: {option_text}")
            
            # Strategy 1: Find the placeholder/value text and click its parent container
            # Discord's React-Select renders: container > control > valueContainer > placeholder
            # The placeholder div contains exactly "Month", "Day", or "Year"
            success = await self._page.evaluate(f"""
                async () => {{
                    // Find the element showing the placeholder text
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    let targetEl = null;
                    while (node = walker.nextNode()) {{
                        if (node.textContent.trim() === '{label}') {{
                            // Make sure it's a leaf text node in the DOB area
                            const parent = node.parentElement;
                            if (parent && parent.offsetParent !== null && 
                                !parent.querySelector('input[name="email"]')) {{
                                targetEl = parent;
                                break;
                            }}
                        }}
                    }}
                    
                    if (!targetEl) return 'no_element';
                    
                    // Walk up to find the clickable control container
                    // React-Select structure: wrapper > control (clickable) > valueContainer > placeholder
                    let clickTarget = targetEl;
                    for (let i = 0; i < 5; i++) {{
                        clickTarget = clickTarget.parentElement;
                        if (!clickTarget) break;
                        // The control div usually has a min-height and cursor:pointer
                        const style = window.getComputedStyle(clickTarget);
                        if (style.cursor === 'pointer' || 
                            clickTarget.getAttribute('tabindex') !== null ||
                            clickTarget.className.includes('control') ||
                            clickTarget.className.includes('css-')) {{
                            break;
                        }}
                    }}
                    
                    if (!clickTarget) clickTarget = targetEl;
                    
                    // Click to open the dropdown
                    clickTarget.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
                    
                    // Wait for dropdown menu to appear
                    await new Promise(r => setTimeout(r, 600));
                    
                    // Look for the option in the dropdown menu
                    // React-Select renders options with id containing 'option'
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
                    
                    // If not found, try scrolling the menu list
                    const menuList = document.querySelector(
                        '[class*="MenuList"], [class*="menuList"], [id*="listbox"], [role="listbox"]'
                    );
                    if (menuList) {{
                        for (let scroll = 0; scroll < 30; scroll++) {{
                            menuList.scrollTop += 200;
                            await new Promise(r => setTimeout(r, 100));
                            const opts = document.querySelectorAll(
                                '[id*="option"], [role="option"], [class*="option"]'
                            );
                            for (const opt of opts) {{
                                if (opt.textContent.trim() === '{option_text}') {{
                                    opt.scrollIntoView({{block: 'nearest'}});
                                    opt.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                                    opt.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                                    opt.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                                    return 'selected_after_scroll';
                                }}
                            }}
                        }}
                    }}
                    
                    return 'option_not_found';
                }}
            """)
            
            if success and 'selected' in str(success):
                self._log(f"Selected {label}: {option_text} via JS ({success})")
                await asyncio.sleep(0.4)
                return True
            
            self._log(f"JS method result for {label}: {success}")
            
            # Strategy 2: Use Playwright's text locator to click, then type to filter
            try:
                # Click on the placeholder text directly
                placeholder = self._page.get_by_text(label, exact=True)
                if await placeholder.count() > 0:
                    await placeholder.first.click()
                    await asyncio.sleep(0.5)
                    # Type to filter the dropdown
                    await self._page.keyboard.type(option_text, delay=30)
                    await asyncio.sleep(0.4)
                    await self._page.keyboard.press('Enter')
                    await asyncio.sleep(0.4)
                    self._log(f"Selected {label}: {option_text} via text click+type")
                    return True
            except Exception as e2:
                self._log(f"Strategy 2 failed for {label}: {e2}")
            
            # Strategy 3: Tab from password field to reach DOB dropdowns
            try:
                idx = {"Month": 0, "Day": 1, "Year": 2}.get(label, 0)
                password_field = self._page.locator('input[name="password"]')
                if await password_field.count() > 0:
                    await password_field.click()
                    await asyncio.sleep(0.2)
                    # Tab to the correct dropdown
                    for _ in range(idx + 1):
                        await self._page.keyboard.press('Tab')
                        await asyncio.sleep(0.15)
                    # Open with space/enter and type
                    await self._page.keyboard.press('Space')
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.type(option_text, delay=30)
                    await asyncio.sleep(0.3)
                    await self._page.keyboard.press('Enter')
                    await asyncio.sleep(0.4)
                    self._log(f"Selected {label}: {option_text} via tab navigation")
                    return True
            except Exception as e3:
                self._log(f"Strategy 3 failed for {label}: {e3}")
            
            self._log(f"All DOB strategies failed for {label}: {option_text}")
            return False
            
        except Exception as e:
            self._log(f"DOB selection error for {label}: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _fill_registration_form(self) -> bool:
        try:
            self._log("Navigating to Discord registration page...")
            await self._page.goto('https://discord.com/register', wait_until='networkidle')
            await asyncio.sleep(3)
            
            self._log(f"Filling email: {self._email}")
            await self._page.wait_for_selector('input[name="email"]', timeout=15000)
            await self._page.locator('input[name="email"]').fill(self._email)
            await self._human_pause()
            
            display_name = self._username[:15] if len(self._username) > 15 else self._username
            self._log(f"Filling display name: {display_name}")
            await self._page.wait_for_selector('input[name="global_name"]', timeout=10000)
            await self._page.locator('input[name="global_name"]').fill(display_name)
            await self._human_pause()
            
            self._log(f"Filling username: {self._username}")
            await self._page.locator('input[name="username"]').fill(self._username)
            await self._human_pause()
            
            self._log("Filling password")
            await self._page.locator('input[name="password"]').fill(self._password)
            await self._human_pause()
            
            # DOB - year always 1990-1999 (under 2000, guarantees 18+)
            month_val = random.randint(1, 12)
            day_val = str(random.randint(1, 28))
            year_val = str(random.randint(1990, 1999))
            months = ['January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December']
            month_name = months[month_val - 1]
            self._log(f"Selecting DOB: {month_name} {day_val}, {year_val}")
            
            await self._select_dob("Month", month_name)
            await self._human_pause()
            
            await self._select_dob("Day", day_val)
            await self._human_pause()
            
            await self._select_dob("Year", year_val)
            await self._human_pause()
            
            # Click Create Account with multiple fallback methods
            self._log("Clicking Create Account button")
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
                self._log("ERROR: Could not click Create Account button!")
                return False
            
            self._log("Create Account clicked, waiting for captcha...")
            await asyncio.sleep(3)
            
            # Take screenshot to see what happened
            await self.capture_screenshot()
            
            # Return True - captcha solving is handled by start_discord_signup()
            return True
            
        except Exception as e:
            self._log(f"Form filling error: {e}")
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
