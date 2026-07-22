import asyncio
import base64
import io
import math
import os
import random
import time
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
import torch
from PIL import Image
from playwright.async_api import Page, BrowserContext, Frame

# Import existing components from the repository
from captcha_solver import SolverConfig, HumanMouse, STEALTH_SCRIPT

@dataclass
class TurnstileSolverConfig(SolverConfig):
    """Configuration for the advanced Turnstile solver."""
    max_retries: int = 5
    retry_backoff_base: float = 1.5
    interactive_timeout: int = 60
    token_check_interval: float = 0.5
    human_delay_min: float = 0.5
    human_delay_max: float = 2.0

class AdvancedStealth:
    """Advanced browser fingerprint evasion and spoofing."""
    
    @staticmethod
    def get_enhanced_stealth_script():
        """Returns an enhanced stealth script with Canvas and WebGL spoofing."""
        return STEALTH_SCRIPT + """
        (() => {
            // --- Canvas Fingerprint Spoofing (Stable Noise) ---
            const originalGetImageData = ImageData.prototype.getImageData;
            const canvasSeed = Math.random(); // Per-session stable seed
            
            // We don't want to break functionality, just add stable, subtle noise
            // if we were to spoof. However, the existing STEALTH_SCRIPT says:
            // "Canvas: DO NOT add noise. Stable fingerprint is better."
            // For Turnstile, we will follow this but ensure no automation artifacts exist.
            
            // --- WebGL Spoofing ---
            const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(param) {
                // Return common high-end GPU values to look like a real user
                if (param === 37445) return 'Google Inc. (NVIDIA)';
                if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameterOrig.call(this, param);
            };

            // --- Advanced Timing Evasion ---
            const originalPerformanceNow = performance.now;
            performance.now = function() {
                // Add tiny jitter to performance.now to prevent timing attacks
                return originalPerformanceNow.apply(this, arguments) + (Math.random() * 0.1);
            };

            // --- Prevent detection of common automation properties ---
            delete Object.getPrototypeOf(navigator).webdriver;
            
            // --- Mock chrome object if missing ---
            if (!window.chrome) {
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
            }
        })();
        """

class TurnstileSolver:
    """Production-grade Cloudflare Turnstile solver."""
    
    def __init__(self, config: TurnstileSolverConfig = None):
        self.config = config if config else TurnstileSolverConfig()
        self._log_entries = []

    def _log(self, message: str, level: str = "info"):
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] [TURNSTILE] [{level.upper()}] {message}"
        self._log_entries.append(entry)
        print(entry, flush=True)

    async def _wait_for_turnstile_iframe(self, page: Page) -> Optional[Frame]:
        """Find the Turnstile iframe using multiple fallback selectors."""
        selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[title*="Cloudflare security challenge"]',
            'div.cf-turnstile iframe',
            '#cf-turnstile-wrapper iframe'
        ]
        
        self._log("Searching for Turnstile iframe...")
        for _ in range(20): # 10 seconds
            for selector in selectors:
                try:
                    iframe_element = await page.query_selector(selector)
                    if iframe_element:
                        frame = await iframe_element.content_frame()
                        if frame:
                            self._log(f"Found Turnstile iframe via: {selector}")
                            return frame
                except Exception:
                    continue
            await asyncio.sleep(0.5)
        
        self._log("Turnstile iframe not found.", "warn")
        return None

    async def _simulate_human_interaction(self, page: Page, frame: Frame):
        """Simulate realistic human interactions with the Turnstile widget."""
        try:
            # 1. Move mouse to the widget area with natural entropy
            container = await page.query_selector('div.cf-turnstile, #cf-turnstile-wrapper, iframe[src*="challenges.cloudflare.com"]')
            if not container:
                return

            box = await container.bounding_box()
            if not box:
                return

            # Target the "Verify you are human" checkbox area
            # Usually located in the middle-left of the widget
            target_x = box['x'] + (box['width'] * random.uniform(0.1, 0.3))
            target_y = box['y'] + (box['height'] * random.uniform(0.4, 0.6))

            self._log(f"Moving mouse to widget at ({target_x}, {target_y})")
            await HumanMouse.move_and_click(page, target_x, target_y)
            
            # 2. Simulate focus/blur events in the frame
            await frame.evaluate("""() => {
                const body = document.body;
                body.dispatchEvent(new Event('mouseenter'));
                body.dispatchEvent(new Event('mouseover'));
                body.focus();
            }""")
            
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
            # 3. Look for the actual checkbox inside the iframe
            checkbox = await frame.query_selector('input[type="checkbox"], #challenge-stage, .ctp-checkbox-container')
            if checkbox:
                cbox = await checkbox.bounding_box()
                if cbox:
                    self._log("Found interactive checkbox inside iframe, clicking...")
                    # Coordinates are relative to the viewport, so we need to add frame offset
                    # but Playwright's mouse.click on a locator handles this.
                    await checkbox.click(delay=random.uniform(50, 150))
            
        except Exception as e:
            self._log(f"Error during human interaction simulation: {e}", "warn")

    async def _check_for_token(self, page: Page) -> Optional[str]:
        """Check for the presence of the Turnstile response token."""
        return await page.evaluate("""() => {
            // Check common locations for the token
            const turnstileInput = document.querySelector('input[name="cf-turnstile-response"]');
            if (turnstileInput && turnstileInput.value) return turnstileInput.value;
            
            // Check for any hidden input that looks like a Turnstile token
            const allInputs = document.querySelectorAll('input[type="hidden"]');
            for (const input of allInputs) {
                if (input.value && input.value.length > 100 && (input.name.includes('cf-') || input.id.includes('cf-'))) {
                    return input.value;
                }
            }
            return null;
        }""")

    async def solve(self, page: Page) -> bool:
        """Main solve method with advanced evasion and multi-round logic."""
        self._log("Starting advanced Turnstile solver...")
        
        # Inject enhanced stealth
        await page.add_init_script(AdvancedStealth.get_enhanced_stealth_script())
        
        for attempt in range(self.config.max_retries):
            self._log(f"Solve attempt {attempt + 1}/{self.config.max_retries}")
            
            frame = await self._wait_for_turnstile_iframe(page)
            if not frame:
                # If no iframe, check if we already have a token (passive solve)
                token = await self._check_for_token(page)
                if token:
                    self._log("Passive Turnstile solve detected!")
                    return True
                
                # Wait a bit and retry
                await asyncio.sleep(2)
                continue

            # Check if it's already solved
            if await self._check_for_token(page):
                self._log("Turnstile already solved.")
                return True

            # Perform human-like interaction
            await self._simulate_human_interaction(page, frame)
            
            # Wait for solve with exponential backoff for checking
            timeout = self.config.interactive_timeout
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                token = await self._check_for_token(page)
                if token:
                    self._log("Turnstile SOLVED! Token extracted.")
                    # Human-like delay after solving
                    await asyncio.sleep(random.uniform(self.config.human_delay_min, self.config.human_delay_max))
                    return True
                
                # Check for "Failure" or "Expired" states in the iframe
                status = await frame.evaluate("""() => {
                    const text = document.body.innerText.toLowerCase();
                    if (text.includes('failed') || text.includes('error')) return 'failed';
                    if (text.includes('expired')) return 'expired';
                    return 'pending';
                }""")
                
                if status == 'failed':
                    self._log("Turnstile reported failure, retrying round...", "warn")
                    break
                if status == 'expired':
                    self._log("Turnstile challenge expired, refreshing...", "warn")
                    await page.reload()
                    break
                    
                await asyncio.sleep(self.config.token_check_interval)
            
            # Backoff before next major retry attempt
            wait_time = self.config.retry_backoff_base ** attempt
            self._log(f"Waiting {wait_time:.2f}s before next attempt...")
            await asyncio.sleep(wait_time)

        self._log("Failed to solve Turnstile after all attempts.", "error")
        return False

    async def close(self):
        """Cleanup resources."""
        pass

# Convenience function for integration
async def solve_turnstile(page: Page) -> bool:
    solver = TurnstileSolver()
    return await solver.solve(page)
