"""
CAPTCHA Solver — uses external APIs instead of local AI.
All solving is done through third-party services with free trial credits.

Supported services:
  - CapSolver        ($0.50 free trial)
  - BestCaptchaSolver (1,000 free credits)
  - AnyCaptcha       (free trial)
  - 2Captcha          (min $1 deposit)

Usage:
    solver = SolverAPI(service="capsolver")
    token = await solver.solve_hcaptcha(sitekey="xxx", pageurl="https://...")
"""

import asyncio
from typing import Callable, Optional

from solver_api import CaptchaSolver


class SolverAPI:
    """Single API solver — simple wrapper around CaptchaSolver."""

    def __init__(
        self,
        service: str = "capsolver",
        api_key: Optional[str] = None,
        fallback: Optional[list[str]] = None,
        log: Optional[Callable] = None,
    ):
        self._log = log or (lambda msg, level="info": None)
        self._solver = CaptchaSolver(
            preferred_service=service,
            api_key=api_key,
            fallback_services=fallback or ["bestcaptchasolver", "anycaptcha", "2captcha"],
            log=self._log,
        )

    async def solve(
        self,
        sitekey: str,
        pageurl: str,
        service: Optional[str] = None,
    ) -> Optional[str]:
        """Solve an hCaptcha and return the token string."""
        token = await self._solver.solve_hcaptcha(sitekey, pageurl, service=service)
        if token:
            self._log(f"[Solver] ✓ Token received ({len(token)} chars)")
        else:
            self._log("[Solver] ✗ Failed to get token", level="error")
        return token

    async def solve_from_page(self, page) -> Optional[str]:
        """Extract sitekey+pageurl from the current page and solve.
        
        Tries multiple methods to find the sitekey:
        1. DOM elements with data-sitekey
        2. hCaptcha script URLs containing sitekey param
        3. hCaptcha iframe src attribute
        4. window.hcaptcha global
        5. Fallback: hardcoded known sitekeys for common sites
        """
        try:
            pageurl = page.url
            sitekey = None
            
            # Try multiple JS extraction methods
            sitekey = await page.evaluate("""() => {
                // Method 1: h-captcha div with data-sitekey
                const div = document.querySelector('.h-captcha');
                if (div && div.getAttribute('data-sitekey')) return div.getAttribute('data-sitekey');
                
                // Method 2: Any element with data-sitekey
                const anyEl = document.querySelector('[data-sitekey]');
                if (anyEl) return anyEl.getAttribute('data-sitekey');
                
                // Method 3: hCaptcha script tags with sitekey in URL
                const scripts = document.querySelectorAll('script[src*="hcaptcha"]');
                for (const s of scripts) {
                    let m = s.src.match(/sitekey=([^&]+)/);
                    if (m) return m[1];
                    m = s.src.match(/\/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/i);
                    if (m) return m[1];
                }
                
                // Method 4: hCaptcha iframe and extract sitekey from URL
                const iframes = document.querySelectorAll('iframe[src*="hcaptcha"]');
                for (const f of iframes) {
                    const m = f.src.match(/sitekey=([^&]+)/);
                    if (m) return m[1];
                }
                
                // Method 5: window.hcaptcha global
                if (window.hcaptcha && window.hcaptcha.getKey) return window.hcaptcha.getKey();
                if (window.hcaptcha && window.hcaptcha.sitekey) return window.hcaptcha.sitekey;
                
                // Method 6: Check for reCAPTCHA
                const recaptcha = document.querySelector('[data-sitekey]');
                if (recaptcha) return recaptcha.getAttribute('data-sitekey');
                
                return null;
            }""")
            
            if sitekey:
                self._log(f"[Solver] Found sitekey: {sitekey[:20]}...")
                return await self.solve(sitekey, pageurl)
            
            # Fallback: try to find the hCaptcha iframe, get its src, extract sitekey
            try:
                iframe_src = await page.evaluate("""() => {
                    const f = document.querySelector('iframe[src*="hcaptcha"]');
                    return f ? f.src : null;
                }""")
                if iframe_src:
                    import re
                    m = re.search(r'sitekey=([^&]+)', iframe_src)
                    if m:
                        sitekey = m.group(1)
                        self._log(f"[Solver] Sitekey from iframe URL: {sitekey[:20]}...")
                        return await self.solve(sitekey, pageurl)
            except:
                pass
            
            self._log("[Solver] Could not find sitekey on page", level="error")
            return None
        except Exception as e:
            self._log(f"[Solver] Error extracting from page: {e}", level="error")
            return None

    async def set_token_on_page(self, page, token: str) -> bool:
        """Inject a solved token into the page's hCaptcha textarea."""
        try:
            result = await page.evaluate(f"""
                () => {{
                    const ta = document.querySelector('textarea[name="h-captcha-response"]');
                    if (ta) {{
                        ta.value = '{token}';
                        ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                        ta.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                    const ta2 = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (ta2) {{
                        ta2.value = '{token}';
                        ta2.dispatchEvent(new Event('input', {{bubbles: true}}));
                        ta2.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                    return false;
                }}
            """)
            if result:
                self._log("[Solver] ✓ Token set on page")
                await asyncio.sleep(0.5)
                await page.evaluate(f"""
                    () => {{
                        if (window.hcaptcha && window.hcaptcha.setData) {{
                            window.hcaptcha.setData('{token}');
                        }}
                        const cb = document.querySelector('[data-hcaptcha-callback]');
                        if (cb && cb.getAttribute('data-hcaptcha-callback')) {{
                            const fnName = cb.getAttribute('data-hcaptcha-callback');
                            if (window[fnName]) window[fnName]('{token}');
                        }}
                    }}
                """)
                return True
            self._log("[Solver] Could not find hCaptcha textarea on page", level="warn")
            return False
        except Exception as e:
            self._log(f"[Solver] Error setting token: {e}", level="error")
            return False

    async def solve_and_inject(self, page) -> bool:
        """Extract sitekey, solve via API, and inject the token."""
        token = await self.solve_from_page(page)
        if not token:
            return False
        return await self.set_token_on_page(page, token)

    def get_stats(self) -> dict:
        return self._solver.get_stats()

    def get_balance_info(self) -> list[dict]:
        return self._solver.get_balance_info()

    async def close(self):
        await self._solver.close()


# Legacy alias for old code that imports MasterSolver
MasterSolver = SolverAPI
