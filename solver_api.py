"""
Unified CAPTCHA Solving API — wraps multiple services with free trial credits.

Supported Services (all offer free trial credits / free tier):
  1. CapSolver        — ~$0.50 free trial (https://dashboard.capsolver.com)
  2. BestCaptchaSolver — 1,000 free credits (https://bestcaptchasolver.com)
  3. AnyCaptcha       — Free trial/demo   (https://anycaptcha.com)
  4. 2Captcha         — ~$0.06/solve      (https://2captcha.com — min deposit $1)

Usage:
    solver = CaptchaSolver(api_key="CAP-xxx", service="capsolver")
    token = await solver.solve_hcaptcha(sitekey="xxx", pageurl="https://...")
    print(f"Token: {token}")
"""

import asyncio
import json
import os
import time
from typing import Callable, Optional

import aiohttp


# ── Known sites that use hCaptcha challenges ──────────────
# (sitekey → site name lookup for better logging)

KNOWN_SITES = {
    "discord.com": "Discord",
    "accounts.hcaptcha.com": "hCaptcha Demo",
    "openai.com": "OpenAI",
    "roblox.com": "Roblox",
    "origin.com": "EA Origin",
}


# ── API Wrappers ──────────────────────────────────────────

class CapSolverAPI:
    """CapSolver (capsolver.com) — ~$0.50 free trial credit.
    
    API: https://api.capsolver.com/createTask
    Cost: ~$0.004/solve for hCaptcha (125 solves with $0.50 free trial)
    Docs: https://docs.capsolver.com/guide/captcha/HcaptchaTask.html
    """

    BASE = "https://api.capsolver.com"

    @staticmethod
    async def solve_hcaptcha(
        session: aiohttp.ClientSession,
        api_key: str,
        sitekey: str,
        pageurl: str,
        proxy: Optional[str] = None,
        log: Optional[Callable] = None,
    ) -> Optional[str]:
        log = log or (lambda *a: None)
        task = {
            "type": "HCaptchaTaskProxyLess" if not proxy else "HCaptchaTask",
            "websiteURL": pageurl,
            "websiteKey": sitekey,
        }
        if proxy:
            parts = proxy.split("@")[-1].split(":")
            task["proxy"] = proxy

        try:
            async with session.post(
                f"{CapSolverAPI.BASE}/createTask",
                json={"clientKey": api_key, "task": task},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()
                task_id = data.get("taskId")
                if not task_id:
                    log(f"[CapSolver] Create failed: {data.get('errorDescription', str(data))}", level="error")
                    return None
                log(f"[CapSolver] Task created: {task_id}")

            # Poll for result
            for _ in range(60):  # up to 60s
                await asyncio.sleep(2)
                async with session.post(
                    f"{CapSolverAPI.BASE}/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                    status = data.get("status", "")
                    if status == "ready":
                        token = data.get("solution", {}).get("gRecaptchaResponse", "")
                        if token:
                            log(f"[CapSolver] Solved! Token: {token[:20]}...")
                            return token
                        log("[CapSolver] Ready but no token", level="warn")
                        return None
                    elif status == "failed":
                        log(f"[CapSolver] Failed: {data}", level="error")
                        return None
                    # else "processing" — keep polling
            log("[CapSolver] Timeout waiting for result", level="error")
            return None
        except Exception as e:
            log(f"[CapSolver] Error: {e}", level="error")
            return None


class BestCaptchaSolverAPI:
    """BestCaptchaSolver (bestcaptchasolver.com) — 1,000 free credits.
    
    API: https://bestcaptchasolver.com/api/
    Cost: ~$0.001/solve (1,000 solves with free credits)
    The free credits work for image-based captchas.
    """

    BASE = "https://bestcaptchasolver.com/api"

    @staticmethod
    async def solve_hcaptcha(
        session: aiohttp.ClientSession,
        api_key: str,
        sitekey: str,
        pageurl: str,
        log: Optional[Callable] = None,
    ) -> Optional[str]:
        log = log or (lambda *a: None)
        try:
            async with session.post(
                f"{BestCaptchaSolverAPI.BASE}/captcha/hcaptcha",
                json={
                    "api_key": api_key,
                    "page_url": pageurl,
                    "site_key": sitekey,
                    "proxy": "",  # optional proxy
                    "affiliate_id": "",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()
                if data.get("error"):
                    log(f"[BCS] Error: {data['error']}", level="error")
                    return None
                captcha_id = data.get("id")
                log(f"[BCS] ID: {captcha_id}")

            # Poll
            for _ in range(60):
                await asyncio.sleep(2)
                async with session.get(
                    f"{BestCaptchaSolverAPI.BASE}/captcha/hcaptcha",
                    params={"id": captcha_id, "api_key": api_key},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                    if data.get("gresponse"):
                        token = data["gresponse"]
                        log(f"[BCS] Solved! Token: {token[:20]}...")
                        return token
                    if data.get("error"):
                        log(f"[BCS] Error: {data['error']}", level="error")
                        return None
            log("[BCS] Timeout", level="error")
            return None
        except Exception as e:
            log(f"[BCS] Error: {e}", level="error")
            return None


class AnyCaptchaAPI:
    """AnyCaptcha (anycaptcha.com) — free trial available.
    
    API: Standard Anticaptcha-compatible JSON API.
    Uses the same format as CapMonster / Anticaptcha.
    """

    BASE = "https://api.anycaptcha.com"

    @staticmethod
    async def solve_hcaptcha(
        session: aiohttp.ClientSession,
        api_key: str,
        sitekey: str,
        pageurl: str,
        log: Optional[Callable] = None,
    ) -> Optional[str]:
        log = log or (lambda *a: None)
        try:
            async with session.post(
                f"{AnyCaptchaAPI.BASE}/createTask",
                json={
                    "clientKey": api_key,
                    "task": {
                        "type": "HCaptchaTaskProxyless",
                        "websiteURL": pageurl,
                        "websiteKey": sitekey,
                    },
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()
                task_id = data.get("taskId")
                if not task_id:
                    log(f"[AnyCaptcha] Create failed: {data}", level="error")
                    return None
                log(f"[AnyCaptcha] Task: {task_id}")

            for _ in range(60):
                await asyncio.sleep(2)
                async with session.post(
                    f"{AnyCaptchaAPI.BASE}/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                    if data.get("status") == "ready":
                        token = data.get("solution", {}).get("gRecaptchaResponse", "")
                        if token:
                            log(f"[AnyCaptcha] Solved! Token: {token[:20]}...")
                            return token
                        return None
            log("[AnyCaptcha] Timeout", level="error")
            return None
        except Exception as e:
            log(f"[AnyCaptcha] Error: {e}", level="error")
            return None


class TwoCaptchaAPI:
    """2Captcha (2captcha.com) — min $1 deposit, ~$0.006/solve.
    
    API: https://api.2captcha.com/in.php
    Cheapest per-solve pricing among all services.
    """

    BASE = "https://api.2captcha.com"

    @staticmethod
    async def solve_hcaptcha(
        session: aiohttp.ClientSession,
        api_key: str,
        sitekey: str,
        pageurl: str,
        log: Optional[Callable] = None,
    ) -> Optional[str]:
        log = log or (lambda *a: None)
        try:
            async with session.post(
                f"{TwoCaptchaAPI.BASE}/in.php",
                data={
                    "key": api_key,
                    "method": "hcaptcha",
                    "sitekey": sitekey,
                    "pageurl": pageurl,
                    "json": 1,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()
                if data.get("status") != 1:
                    log(f"[2Captcha] Create failed: {data}", level="error")
                    return None
                captcha_id = str(data.get("request", ""))
                log(f"[2Captcha] ID: {captcha_id}")

            for _ in range(90):
                await asyncio.sleep(2)
                async with session.get(
                    f"{TwoCaptchaAPI.BASE}/res.php",
                    params={
                        "key": api_key,
                        "action": "get",
                        "id": captcha_id,
                        "json": 1,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                    if data.get("status") == 1:
                        token = data.get("request", "")
                        if token and len(token) > 20:
                            log(f"[2Captcha] Solved! Token: {token[:20]}...")
                            return token
                    elif data.get("request") and "CAPCHA_NOT_READY" in str(data.get("request", "")):
                        continue
                    else:
                        log(f"[2Captcha] Failed: {data}", level="error")
                        return None
            log("[2Captcha] Timeout", level="error")
            return None
        except Exception as e:
            log(f"[2Captcha] Error: {e}", level="error")
            return None


class BrightDataAPI:
    """Bright Data CAPTCHA solving — try as proxy credentials first, then API.
    
    Bright Data doesn't have a simple sitekey→token API.
    Instead, use it as:
      1. PROXY MODE: Route Playwright through Bright Data's Web Unlocker proxy
         (requires zone credentials: brd-customer-XXX-zone-YYY:password)
      2. API MODE: Fetch the page through Bright Data's request API
         (requires an API token + Web Unlocker zone name)
    
    The API_KEY env var should contain zone credentials in format:
      brd-customer-{CUSTOMER_ID}-zone-{ZONE_NAME}:{ZONE_PASSWORD}
    
    Or a Bright Data API token (UUID format) — you MUST also create
    a Web Unlocker zone in Bright Data dashboard.
    """

    BASE = "https://api.brightdata.com"

    @staticmethod
    async def solve_hcaptcha(
        session: aiohttp.ClientSession,
        api_key: str,
        sitekey: str,
        pageurl: str,
        log: Optional[Callable] = None,
    ) -> Optional[str]:
        log = log or (lambda *a: None)
        
        # If key is in zone credentials format (brd-customer-XXX-zone-YYY:password)
        # use it as proxy auth; otherwise use as API token
        is_proxy_creds = "brd-customer-" in api_key.lower()
        is_uuid = len(api_key) == 36 and api_key.count("-") == 4
        
        if is_proxy_creds:
            # Credentials format: brd-customer-CID-zone-ZONE:PASS
            # Use as proxy credentials — this won't give us a token directly
            # but Playwright can route through it for auto-solving
            log("[BrightData] Detected proxy credentials — these work when routing Playwright through the proxy")
            log(f"[BrightData] To use: set Playwright proxy to http://{api_key[:25]}...@brd.superproxy.io:22225")
            log("[BrightData] Proxy mode can't extract tokens directly — try CapSolver instead", level="warn")
            return None
        
        if is_uuid:
            # UUID format: API token — needs a Web Unlocker zone to work
            log("[BrightData] Detected API token (UUID format)")
            log("[BrightData] This token needs a Web Unlocker zone created in Bright Data dashboard")
            
            # Try the request API using the token as zone name (likely to fail if zone doesn't exist)
            try:
                url = pageurl.split("?")[0]
                log(f"[BrightData] Trying Web Unlocker API with zone={api_key[:8]}...")
                
                async with session.post(
                    f"{BrightDataAPI.BASE}/request",
                    json={
                        "zone": api_key,
                        "url": url,
                        "format": "raw",
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status == 200:
                        body = await r.text()
                        import re
                        token_match = re.search(
                            r'<textarea[^>]*name="h-captcha-response"[^>]*>([^<]+)</textarea>',
                            body
                        )
                        if token_match:
                            token = token_match.group(1).strip()
                            if token and len(token) > 20:
                                log(f"[BrightData] Token found!")
                                return token
                        log("[BrightData] No token in response HTML", level="warn")
                        return None
                    else:
                        err = await r.text()
                        log(f"[BrightData] API error: {err[:120]}", level="error")
                        log("[BrightData] ⚠️ To fix: go to Bright Data dashboard → Web Access APIs → create a Web Unlocker zone, then set API_KEY='brd-customer-CID-zone-ZONE:PASS'", level="error")
                        return None
            except Exception as e:
                log(f"[BrightData] Error: {e}", level="error")
                return None
        
        # Unknown format
        log(f"[BrightData] Unknown API key format (starts with {api_key[:8]}...)", level="warn")
        log("[BrightData] Expected: brd-customer-CID-zone-NAME:PASS or a UUID API token", level="warn")
        return None


# ── Unified Solver ───────────────────────────────────────

SERVICE_MAP = {
    "brightdata": BrightDataAPI,
    "capsolver": CapSolverAPI,
    "bestcaptchasolver": BestCaptchaSolverAPI,
    "anycaptcha": AnyCaptchaAPI,
    "2captcha": TwoCaptchaAPI,
}

SERVICE_FREE_CREDITS = {
    "brightdata": "Bright Data Web Unlocker (paid, best success rate)",
    "capsolver": "$0.50 free trial (~125 solves)",
    "bestcaptchasolver": "1,000 free credits (~1,000 solves)",
    "anycaptcha": "Free trial (amount varies)",
    "2captcha": "Min $1 deposit (~$0.006/solve)",
}

SERVICE_ENV_VARS = {
    "brightdata": "API_KEY",
    "capsolver": "CAPSOLVER_API_KEY",
    "bestcaptchasolver": "BESTCAPTCHASOLVER_API_KEY",
    "anycaptcha": "ANYCAPTCHA_API_KEY",
    "2captcha": "TWOCAPTCHA_API_KEY",
}


class CaptchaSolver:
    """Unified CAPTCHA solving API with automatic fallback between services."""

    def __init__(
        self,
        preferred_service: str = "capsolver",
        api_key: Optional[str] = None,
        fallback_services: Optional[list[str]] = None,
        log: Optional[Callable] = None,
    ):
        self.preferred = preferred_service.lower()
        self.api_key = api_key or os.environ.get(SERVICE_ENV_VARS.get(self.preferred, ""), "")
        self.fallback = fallback_services or ["bestcaptchasolver", "anycaptcha", "2captcha"]
        self._log = log or (lambda *a: None)
        self._session: Optional[aiohttp.ClientSession] = None
        self._stats = {"total_solves": 0, "success": 0, "failed": 0, "by_service": {}}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def solve_hcaptcha(
        self,
        sitekey: str,
        pageurl: str,
        service: Optional[str] = None,
    ) -> Optional[str]:
        """Solve an hCaptcha challenge and return the token.
        
        Auto-prioritizes services that have API keys configured.
        Tries the preferred (or specified) service first, then falls back.
        """
        s = await self._get_session()
        
        # Build ordered list: configured services first, then preferred, then fallback
        services_to_try = []
        
        if service:
            services_to_try = [service.lower()]
        else:
            # Check which services have keys configured
            configured = []
            for svc_name in SERVICE_ENV_VARS:
                key = os.environ.get(SERVICE_ENV_VARS[svc_name], "")
                if key:
                    configured.append(svc_name)
            
            # Ordered: configured services first, then preferred, then fallbacks
            for cfg_svc in configured:
                if cfg_svc not in services_to_try:
                    services_to_try.append(cfg_svc)
            if self.preferred not in services_to_try:
                services_to_try.append(self.preferred)
            for fb in self.fallback:
                if fb not in services_to_try:
                    services_to_try.append(fb)
        
        for svc in services_to_try:
            svc_key = os.environ.get(SERVICE_ENV_VARS.get(svc, ""), "")
            if not svc_key:
                self._log(f"[Solver] No API key for {svc}, skipping", level="warn")
                continue
            
            handler_cls = SERVICE_MAP.get(svc)
            if not handler_cls:
                self._log(f"[Solver] Unknown service: {svc}", level="error")
                continue
            
            self._log(f"[Solver] Trying {svc}...")
            token = await handler_cls.solve_hcaptcha(
                s, svc_key, sitekey, pageurl, log=self._log
            )
            
            self._stats["total_solves"] += 1
            self._stats["by_service"][svc] = self._stats["by_service"].get(svc, 0) + 1
            
            if token:
                self._stats["success"] += 1
                return token
            
            self._stats["failed"] += 1
        
        self._log("[Solver] All services failed", level="error")
        return None

    def get_stats(self) -> dict:
        return dict(self._stats)

    def get_balance_info(self) -> list[dict]:
        """Return info about which services have API keys configured."""
        info = []
        for svc_name, env_var in SERVICE_ENV_VARS.items():
            key = os.environ.get(env_var, "")
            credits = SERVICE_FREE_CREDITS.get(svc_name, "Unknown")
            configured = bool(key)
            info.append({
                "name": svc_name,
                "env_var": env_var,
                "configured": configured,
                "free_credits": credits,
                "key_preview": key[:8] + "..." if configured and len(key) > 8 else "",
            })
        return info

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
