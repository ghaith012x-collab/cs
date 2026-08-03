import asyncio
import base64
import json
import os
import random
import socket
import time
from typing import Optional

from playwright.async_api import async_playwright

from captcha_solver import (
    NoCaptchaAI,
    extract_hcaptcha_sitekey,
    extract_funcaptcha_task,
    read_hcaptcha_token,
    set_hcaptcha_token_on_page,
    solve_funcaptcha_pixels,
)
from duckmail import TempMail


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

PAST_CAPTCHA_KEYWORDS = ['/channels', '/verify', '/welcome', '/login', '@me', 'discord.com/app']


class DiscordAutomation:
    def __init__(self, headless: bool = False, email: str = ""):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._screenshots: list = []
        self._activity_log: list = []
        self._email = (email or os.environ.get("ACCOUNT_EMAIL", "")).strip()
        self._username = ""
        self._password = ""
        self._solver = NoCaptchaAI(log=self._log)
        self._mail: Optional[TempMail] = None

    def _log(self, message: str, level: str = "info") -> None:
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "timestamp": time.time(),
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

        # No hardcoded email - use the configured email, or fall back to a
        # fresh duckmail.sbs address (mail.tm fallback) when none is provided.
        if not self._email:
            self._log("[Mail] No email configured - creating duckmail.sbs inbox...")
            try:
                self._mail = TempMail(log=self._log)
                self._email = await self._mail.create_inbox()
            except Exception as e:
                self._log(f"[Mail] inbox creation error: {e}", level="error")
                self._email = ""

        if not self._email:
            self._log("[FAIL] No email available - aborting signup", level="error")
            return False

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
                self._log("[OK] Form filled - now solving captcha...")
                success = await self._solve_hcaptcha_if_present()
                if success:
                    self._log("[OK] CAPTCHA SOLVED! Registration submitted.")
                    # Auto-verify: complete Discord email verification via duckmail.sbs
                    await self._verify_account_email()
                else:
                    self._log("[FAIL] Captcha solving failed", level="error")
            else:
                self._log("[FAIL] Form filling failed", level="error")
                success = False

        except Exception as e:
            self._log(f"Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            success = False

        await self.capture_screenshot()
        return success

    async def _verify_account_email(self) -> bool:
        """Wait for the Discord verification email and open its link (best effort)."""
        if not self._mail:
            return False
        try:
            link = await self._mail.wait_for_verification_link(timeout=240)
            if not link:
                self._log("[Mail] No verification link found yet - account may still be created", level="warn")
                return False
            self._log(f"[Mail] Opening verification link: {link[:80]}...")
            await self._page.goto(link, wait_until='domcontentloaded', timeout=20000)
            await asyncio.sleep(5)
            # Discord shows a verification success page (or redirects to login)
            try:
                page_text = await self._page.evaluate(
                    "() => document.body.innerText.substring(0, 300)")
            except Exception:
                page_text = ""
            if any(w in (page_text or "").lower()
                   for w in ('verified', 'success', 'confirmation', 'you\'re all set')):
                self._log("[Mail] [OK] Email verification completed")
            await self.capture_screenshot()
            self._log("[Mail] [OK] Verification link opened")
            return True
        except Exception as e:
            self._log(f"[Mail] verification error: {e}", level="warn")
            return False

    async def _past_captcha(self) -> bool:
        """True when the page has moved past the captcha into Discord."""
        try:
            cur_url = self._page.url
            return any(k in cur_url for k in PAST_CAPTCHA_KEYWORDS)
        except:
            return False

    async def _extract_sitekey_with_retry(self, timeout: float = 20.0,
                                          poll: float = 3.0) -> str:
        """Wait for the hCaptcha widget, polling for its sitekey.

        The captcha iframe mounts before its src URL carries the sitekey, so
        extracting too early fails. Poll every `poll` seconds for up to
        `timeout` seconds. Last resort: Discord's well-known register sitekey.
        """
        deadline = time.time() + timeout
        attempts = 0
        while True:
            attempts += 1
            sitekey = await extract_hcaptcha_sitekey(self._page)
            if sitekey:
                self._log(f"[Captcha] Sitekey ready (attempt {attempts}): {sitekey[:16]}...")
                return sitekey
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._log(f"[Captcha] Sitekey not ready yet (attempt {attempts}) - "
                      f"retrying in {int(min(poll, remaining))}s", level="warn")
            await asyncio.sleep(min(poll, remaining))
        self._log("[Captcha] Sitekey never appeared - using known Discord register sitekey",
                  level="warn")
        return "f5561ba9-8f1e-40ca-9b8b-a0bce722149b"

    async def _click_form_submit(self) -> bool:
        """Click Create Account / Continue after the captcha token is in place."""
        try:
            result = await self._page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (btn.offsetParent === null) continue;
                    const t = btn.textContent.toLowerCase().trim();
                    if (t.includes('create account') || t.includes('continue') || t.includes('sign up')) {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return t.slice(0, 24);
                    }
                }
                const submit = document.querySelector('[type="submit"]');
                if (submit && submit.offsetParent !== null) {
                    submit.click();
                    return 'submit_btn';
                }
                const form = document.querySelector('form');
                if (form) {
                    if (form.requestSubmit) { form.requestSubmit(); return 'requestSubmit'; }
                    form.submit();
                    return 'form_submit';
                }
                return '';
            }""")
            if result:
                self._log(f"[Captcha] [OK] Submit clicked: {result}")
                return True
        except Exception as e:
            self._log(f"[Captcha] submit click error: {e}", level="warn")
        return False

    async def _solve_hcaptcha_if_present(self) -> bool:
        """Detect and solve hCaptcha via NoCaptchaAI (nocaptchaai.com).

        Token API: sitekey + pageurl -> hCaptcha token (usually < 5s).
        """
        try:
            self._log("[Captcha] Checking for hCaptcha...")

            if await self._past_captcha():
                self._log(f"[Captcha] Already past captcha - at {self._page.url[:50]}")
                return True

            # Find the captcha iframe
            iframe = None
            for attempt in range(25):
                try:
                    iframe_el = await asyncio.wait_for(
                        self._page.query_selector('iframe[src*="hcaptcha.com"]'),
                        timeout=2.0
                    )
                except:
                    iframe_el = None
                if iframe_el:
                    self._log(f"[Captcha] hCaptcha iframe found (attempt {attempt+1})")
                    iframe = iframe_el
                    break
                if await self._past_captcha():
                    self._log(f"[Captcha] Page navigated to {self._page.url[:50]} - no captcha needed")
                    return True
                await asyncio.sleep(0.5)

            if not iframe:
                # No hCaptcha iframe - check for FunCAPTCHA (Arkose) instead
                try:
                    if await self._past_captcha():
                        self._log(f"[Captcha] Registration went through - at {self._page.url[:50]}")
                        return True
                    page_text = await self._page.evaluate(
                        "() => document.body.innerText.substring(0, 500)")
                    has_captcha_text = ('captcha' in page_text.lower()
                                        or 'security' in page_text.lower()
                                        or 'verify' in page_text.lower())
                    if has_captcha_text:
                        self._log("[Captcha] FunCAPTCHA detected - pixel tile solver...")
                        return await self._solve_funcaptcha()
                    self._log(f"[Captcha] No captcha indicators on page: {self._page.url[:40]}")
                except Exception as e:
                    self._log(f"[Captcha] Captcha check error: {e}", level="warn")
                return True

            await asyncio.sleep(1)
            try:
                await self.capture_screenshot()
            except:
                pass

            if not self._solver.configured:
                self._log("[Captcha] [FAIL] No API_KEY set - NoCaptchaAI unavailable "
                          "(set API_KEY to your nocaptchaai.com key)", level="error")
                return False

            # Wait for the hCaptcha widget to finish loading, then extract the
            # sitekey. The iframe appears before its src carries the sitekey,
            # so retry every 3s for up to 20s instead of failing immediately.
            sitekey = await self._extract_sitekey_with_retry(timeout=20.0, poll=3.0)
            if not sitekey:
                self._log("[Captcha] Could not extract sitekey from iframe", level="error")
                return False

            self._log(f"[NoCaptchaAI] Solving hCaptcha (sitekey {sitekey[:16]}...)")
            for attempt in range(3):
                try:
                    token = await asyncio.wait_for(
                        self._solver.solve_hcaptcha(sitekey, self._page.url),
                        timeout=100.0,
                    )
                except asyncio.TimeoutError:
                    token = None
                    self._log("[NoCaptchaAI] Solve timed out", level="warn")

                if not token:
                    break

                await set_hcaptcha_token_on_page(self._page, token)
                await self._click_form_submit()
                await asyncio.sleep(3)
                if await self._past_captcha():
                    self._log("[NoCaptchaAI] [OK] Captcha passed - Discord accepted the token")
                    return True
                if await read_hcaptcha_token(self._page):
                    self._log("[NoCaptchaAI] [OK] Token verified on page")
                    await self._click_form_submit()
                    return True
                self._log(f"[NoCaptchaAI] Token attempt {attempt+1} did not advance - retrying",
                          level="warn")
                await asyncio.sleep(2)

            self._log("[NoCaptchaAI] [FAIL] Could not solve hCaptcha", level="error")
            return False

        except Exception as e:
            self._log(f"[Captcha] Flow error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _solve_funcaptcha(self) -> bool:
        """Solve FunCAPTCHA tile challenges with the offline pixel solver."""
        try:
            task = await extract_funcaptcha_task(self._page)
            if task:
                self._log(f"[FunCAPTCHA] Challenge: {task[:80]}")
            solved = await solve_funcaptcha_pixels(self._page, log=self._log)
            if solved:
                await self._click_form_submit()
                return True
            self._log("[FunCAPTCHA] [FAIL] Could not solve challenge", level="error")
            return False
        except Exception as e:
            self._log(f"[FunCAPTCHA] Error: {e}", level="error")
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
                count = await asyncio.wait_for(placeholder.count(), timeout=2.0)
                if count > 0:
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
                count = await asyncio.wait_for(pw.count(), timeout=2.0)
                if count > 0:
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

            # ── ToS Checkbox — FIND THE CORRECT ONE (Terms of Service, not newsletter) ─
            self._log("Checking ToS checkbox...")
            tos_checked = False

            try:
                tos_checked = await self._page.evaluate("""() => {
                    // Helper: find nearest checkbox sibling/parent
                    function findNearestCheckbox(el) {
                        // Check siblings
                        let sib = el.previousElementSibling || el.nextElementSibling;
                        while (sib) {
                            if (sib.tagName === 'INPUT' && sib.type === 'checkbox') return sib;
                            if (sib.getAttribute('role') === 'checkbox') return sib;
                            if (sib.querySelector('input[type="checkbox"]')) return sib.querySelector('input[type="checkbox"]');
                            if (sib.matches('[class*="checkbox"]')) return sib;
                            sib = sib.nextElementSibling || sib.previousElementSibling;
                        }
                        // Check parents
                        let p = el.parentElement;
                        for (let i = 0; i < 5 && p; i++) {
                            const cb = p.querySelector('input[type="checkbox"], [role="checkbox"], [class*="checkbox"]');
                            if (cb && cb !== el) return cb;
                            p = p.parentElement;
                        }
                        return null;
                    }

                    // STRATEGY 1: Find checkbox near "Terms of Service" text (MOST ACCURATE)
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                    let node;
                    while (node = walker.nextNode()) {
                        const t = node.textContent.trim().toLowerCase();
                        const tosKeywords = ['terms of service', 'terms of use', 'terms & conditions', 
                                            'terms and conditions', 'i have read', 'read and agree'];
                        const hasTos = tosKeywords.some(k => t.includes(k));
                        if (!hasTos) continue;
                        
                        // Found text containing ToS reference — find its checkbox
                        const cb = findNearestCheckbox(node.parentElement);
                        if (cb) {
                            cb.scrollIntoView({block: 'center'});
                            cb.click();
                            // Force checked
                            if (cb.type === 'checkbox') cb.checked = true;
                            cb.dispatchEvent(new Event('change', { bubbles: true }));
                            cb.dispatchEvent(new Event('input', { bubbles: true }));
                            return 'tos_text_' + t.slice(0, 30);
                        }
                    }

                    // STRATEGY 2: Find checkbox with label for="..." reference to ToS
                    const labels = document.querySelectorAll('label');
                    for (const lbl of labels) {
                        const t = lbl.textContent.toLowerCase();
                        const tosKeywords = ['terms of service', 'terms of use', 'i have read', 'read and agree'];
                        const hasTos = tosKeywords.some(k => t.includes(k));
                        if (!hasTos) continue;
                        
                        const forId = lbl.getAttribute('for');
                        if (forId) {
                            const cb = document.getElementById(forId);
                            if (cb) { cb.click(); cb.checked = true; return 'label_for_' + forId; }
                        }
                        // Check inside label
                        const innerCb = lbl.querySelector('input[type="checkbox"], [role="checkbox"]');
                        if (innerCb) { innerCb.click(); innerCb.checked = true; return 'label_inner_cb'; }
                        // Click label itself
                        lbl.click();
                        return 'label_click';
                    }

                    // STRATEGY 3: Last checkbox on the page (Discord puts ToS last)
                    const allCheckboxes = document.querySelectorAll('input[type="checkbox"]:not([checked])');
                    if (allCheckboxes.length > 0) {
                        // Pick the LAST unchecked visible checkbox (most likely ToS)
                        const lastCb = allCheckboxes[allCheckboxes.length - 1];
                        if (lastCb.offsetParent !== null) {
                            lastCb.scrollIntoView({block: 'center'});
                            lastCb.click();
                            lastCb.checked = true;
                            lastCb.dispatchEvent(new Event('change', { bubbles: true }));
                            lastCb.dispatchEvent(new Event('input', { bubbles: true }));
                            return 'last_checkbox_' + allCheckboxes.length;
                        }
                    }

                    // STRATEGY 4: role=checkbox or class-based
                    const roles = document.querySelectorAll('[role="checkbox"]:not([aria-checked="true"])');
                    for (const r of roles) {
                        if (r.offsetParent !== null) {
                            r.click(); r.setAttribute('aria-checked', 'true');
                            return 'role_checkbox';
                        }
                    }

                    // STRATEGY 5: Any visible unchecked checkbox
                    const anyCb = document.querySelectorAll('input[type="checkbox"]');
                    for (const cb of anyCb) {
                        if (!cb.checked && cb.offsetParent !== null) {
                            cb.click(); cb.checked = true;
                            return 'any_checkbox';
                        }
                    }

                    return 'not_found';
                }""")
                if tos_checked and tos_checked != 'not_found':
                    self._log(f"[OK] ToS checked via JS: {tos_checked}")
                else:
                    self._log("ToS checkbox not found by JS - proceeding")
                    tos_checked = False
            except Exception as e:
                self._log(f"ToS JS evaluate error: {e}")

            if tos_checked:
                self._log("[OK] ToS checkbox checked")
                await asyncio.sleep(0.3)
            else:
                self._log("No ToS checkbox found - proceeding anyway")

            # ── Create Account Button (PURE JS evaluate) ────────
            self._log("Clicking Create Account...")
            create_clicked = False

            try:
                result = await self._page.evaluate("""() => {
                    // Try visible buttons first
                    const btns = document.querySelectorAll('button');
                    for (const btn of btns) {
                        if (btn.offsetParent === null) continue;
                        const t = btn.textContent.toLowerCase().trim();
                        if (t.includes('create account') || t.includes('sign up') || t.includes('continue')) {
                            btn.scrollIntoView({block: 'center'});
                            btn.click();
                            return 'button_' + t.slice(0, 20);
                        }
                        if (btn.getAttribute('type') === 'submit' && !t.includes(' ')) {
                            // Don't click generic submit if there's a better match
                        }
                    }
                    // Try submit buttons
                    const submit = document.querySelector('[type="submit"]');
                    if (submit && submit.offsetParent !== null) {
                        submit.scrollIntoView({block: 'center'});
                        submit.click();
                        return 'submit_btn';
                    }
                    // Try role buttons
                    const roles = document.querySelectorAll('[role="button"]');
                    for (const r of roles) {
                        if (r.offsetParent === null) continue;
                        const t = r.textContent.toLowerCase();
                        if (t.includes('create account') || t.includes('sign up') || t.includes('continue')) {
                            r.click();
                            return 'role_' + t.slice(0, 20);
                        }
                    }
                    // Try form submit directly
                    const form = document.querySelector('form');
                    if (form) {
                        if (form.requestSubmit) {
                            form.requestSubmit();
                            return 'form_requestSubmit';
                        }
                        form.submit();
                        return 'form_submit';
                    }
                    return 'failed';
                }""")
                if result and result != 'failed':
                    create_clicked = True
                    self._log(f"[OK] Account button clicked: {result}")
                else:
                    self._log("Could not find Create Account button via JS", level="warn")
                    # Last resort: Press Enter on password field
                    try:
                        pw = self._page.locator('input[name="password"]')
                        await asyncio.wait_for(pw.press('Enter'), timeout=2.0)
                        self._log("Pressed Enter on password")
                        create_clicked = True
                    except:
                        pass
            except Exception as e:
                self._log(f"Create Account JS error: {e}", level="warn")

            if create_clicked:
                self._log("Create Account clicked - waiting...")
            else:
                self._log("WARNING: Could not click Create Account!", level="error")

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
        if self._mail:
            try:
                await self._mail.close()
            except Exception:
                pass
            self._mail = None
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
            print("[OK] Discord automation completed")
        else:
            print("[FAIL] Discord automation failed")
        await asyncio.sleep(5)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(run_discord_automation())
