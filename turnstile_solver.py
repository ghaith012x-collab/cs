import asyncio
import base64
import io
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
import torch
import open_clip
from PIL import Image
from playwright.async_api import async_playwright, Page, BrowserContext

from captcha_solver import SolverConfig, ClipModel, HumanMouse, STEALTH_SCRIPT


@dataclass
class TurnstileSolverConfig(SolverConfig):
    # Add any specific configurations for Turnstile here if needed
    pass


class TurnstileSolver:
    def __init__(self, config: TurnstileSolverConfig = None):
        self.config = config if config else TurnstileSolverConfig()
        self._log_messages = []

    def _log(self, message: str, level: str = "info") -> None:
        timestamp = time.strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] [{level.upper()}] {message}"
        self._log_messages.append(log_entry)
        print(log_entry, flush=True)

    async def _inject_stealth(self, page: Page):
        await page.add_init_script(STEALTH_SCRIPT)
        self._log("Injected stealth script.")

    async def _detect_turnstile_iframe(self, page: Page) -> Optional[Page]:
        self._log("Attempting to detect Cloudflare Turnstile iframe...")
        iframe_selector = 'iframe[src*="challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile"]'
        
        for attempt in range(int(self.config.timeout / 0.5)): # Wait up to timeout seconds
            iframe_element = await page.query_selector(iframe_selector)
            if iframe_element:
                self._log(f"Turnstile iframe detected on attempt {attempt + 1}.")
                return await iframe_element.content_frame()
            await asyncio.sleep(0.5)
        
        self._log("Turnstile iframe not detected within timeout.", level="warn")
        return None

    async def _solve_challenge(self, iframe_page: Page) -> bool:
        self._log("Attempting to solve Turnstile challenge...")
        
        # Look for the checkbox or the main challenge element
        checkbox_selector = 'input[type="checkbox"], #challenge-stage'
        
        try:
            # Wait for the challenge to be visible and interactive
            await iframe_page.wait_for_selector(checkbox_selector, state='visible', timeout=self.config.timeout * 1000)
            self._log("Turnstile challenge element visible.")

            # Try to click the checkbox if it exists
            checkbox = await iframe_page.query_selector('input[type="checkbox"]')
            if checkbox:
                self._log("Clicking Turnstile checkbox...")
                # Use human-like mouse movement for clicking
                box = await checkbox.bounding_box()
                if box:
                    await HumanMouse.move_and_click(iframe_page, box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                else:
                    await checkbox.click()
                self._log("Checkbox clicked.")
                await asyncio.sleep(random.uniform(2, 4)) # Wait for challenge to process
            else:
                self._log("No explicit checkbox found, waiting for passive challenge completion.")
                # If no checkbox, it's likely a passive challenge, just wait.
                await asyncio.sleep(random.uniform(5, 10)) # Longer wait for passive challenges

            # Verify if the challenge is solved by checking for a success indicator
            # This might be a hidden input with a token, or the iframe disappearing/redirecting
            # For now, we'll check for the presence of the 'cf-turnstile-response' input
            response_input = await iframe_page.query_selector('input[name="cf-turnstile-response"]')
            if response_input and await response_input.get_attribute('value'):
                self._log("Turnstile challenge solved successfully (response token found).")
                return True
            else:
                self._log("Turnstile challenge not solved (no response token or empty).", level="warn")
                return False

        except Exception as e:
            self._log(f"Error during Turnstile challenge solving: {e}", level="error")
            return False

    async def solve(self, page: Page) -> bool:
        self._log("Starting Cloudflare Turnstile solver.")
        await self._inject_stealth(page)

        for round_num in range(self.config.max_challenge_rounds):
            self._log(f"Attempting Turnstile solve round {round_num + 1}/{self.config.max_challenge_rounds}")
            iframe_page = await self._detect_turnstile_iframe(page)
            if iframe_page:
                if await self._solve_challenge(iframe_page):
                    self._log("Cloudflare Turnstile solved!")
                    return True
                else:
                    self._log("Turnstile challenge failed in this round, retrying...", level="warn")
            else:
                self._log("Turnstile iframe not found, assuming no challenge or already solved.")
                # If iframe is not found, it might mean the challenge was already passed passively
                # or it hasn't appeared yet. We can try to proceed.
                return True # Assume success if no iframe is found after waiting
            
            await asyncio.sleep(random.uniform(1, 3)) # Small delay before next round

        self._log("Failed to solve Cloudflare Turnstile after multiple attempts.", level="error")
        return False

    async def close(self):
        # No specific resources to close for this solver, but keep for consistency
        pass

