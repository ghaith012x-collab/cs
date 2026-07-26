"""
CAPTCHA Solver — Custom AI Vision Solver using CLIP.
No external APIs. Runs a CLIP model locally for zero-shot image classification.

Architecture:
  - CLIP (openai/clip-vit-base-patch32) for classifying captcha tiles
  - Runs on CPU with ONNX or PyTorch
  - Thread-pool for non-blocking inference
  - Extracts challenge text, parses target, classifies tiles, clicks matches

Flow:
  1. Detect hCaptcha iframe on page
  2. Click checkbox to trigger challenge
  3. Extract challenge text ("Select all images containing a bus")
  4. Parse target object from text
  5. Split iframe screenshot into grid tiles
  6. For each tile: CLIP inference — "a photo of [target]" vs "a photo of something else"
  7. Click tiles that match
  8. Submit and extract token
"""

import asyncio
import base64
import io
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from PIL import Image


# ── CLIP Model (lazy-loaded) ──────────────────────────────

_CLIP_MODEL = None
_CLIP_PROCESSOR = None
_CLIP_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_CLIP_LOADED = False


def _load_clip_sync():
    """Load CLIP model (blocking, runs in thread pool)."""
    global _CLIP_MODEL, _CLIP_PROCESSOR, _CLIP_LOADED
    if _CLIP_LOADED:
        return
    try:
        from transformers import CLIPProcessor, CLIPModel
        import torch

        model_name = os.environ.get("CLIP_MODEL", "openai/clip-vit-base-patch32")
        print(f"[CLIP] Loading model: {model_name}...", flush=True)
        t0 = time.time()

        _CLIP_MODEL = CLIPModel.from_pretrained(
            model_name,
            torch_dtype=torch.float32,
            # Force CPU (no GPU in most containers)
        )
        _CLIP_PROCESSOR = CLIPProcessor.from_pretrained(model_name)

        elapsed = time.time() - t0
        params = sum(p.numel() for p in _CLIP_MODEL.parameters())
        print(f"[CLIP] Loaded {model_name} ({params/1e6:.0f}M params) in {elapsed:.1f}s", flush=True)
        _CLIP_LOADED = True

    except ImportError:
        print(f"[CLIP] transformers/torch not installed! Install with: pip install transformers torch", flush=True)
        _CLIP_LOADED = False
    except Exception as e:
        print(f"[CLIP] Failed to load model: {e}", flush=True)
        _CLIP_LOADED = False


async def load_clip():
    """Ensure CLIP model is loaded (async, runs in thread pool)."""
    if _CLIP_LOADED:
        return True
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_CLIP_EXECUTOR, _load_clip_sync)
    return _CLIP_LOADED


async def classify_tile(tile_img: Image.Image, target: str) -> float:
    """Classify whether a tile image contains the target object.
    
    Returns confidence score 0.0-1.0 for "contains target".
    """
    global _CLIP_MODEL, _CLIP_PROCESSOR

    if not _CLIP_LOADED:
        await load_clip()
        if not _CLIP_LOADED:
            return 0.5  # Random guess if model not loaded

    def _infer():
        labels = [
            f"a photo of a {target}",
            f"a photo of {target}",
            f"a photo of something else",
            f"a photo of an object that is not a {target}",
        ]
        inputs = _CLIP_PROCESSOR(
            images=tile_img,
            text=labels,
            return_tensors="pt",
            padding=True,
            truncation=True,
        )
        import torch
        with torch.no_grad():
            outputs = _CLIP_MODEL(**inputs)
        # logits_per_image: (1, num_labels)
        probs = outputs.logits_per_image.softmax(dim=1)
        # Sum probabilities for positive labels (first 2) vs negative (last 2)
        pos = probs[0, :2].sum().item()
        neg = probs[0, 2:].sum().item()
        return pos / (pos + neg + 1e-8)

    loop = asyncio.get_event_loop()
    confidence = await loop.run_in_executor(_CLIP_EXECUTOR, _infer)
    return confidence


# ── Challenge Text Parsing ────────────────────────────────

# Common hCaptcha challenge patterns
_CHALLENGE_PATTERNS = [
    r"select all images? (?:containing|with|that have|that show)\s+(?:a\s+|an\s+)?(.+?)(?:\.|$)",
    r"select all (?:the\s+)?(.+?)(?:\s+below|$)",
    r"click on (?:the\s+)?(?:matching\s+)?(.+?)(?:\.|$)",
    r"which (?:image|picture|one) (?:is|contains|shows)\s+(?:a\s+|an\s+)?(.+?)(?:\?|\.|$)",
    r"please drag (.+?) to (.+?)(?:\.|$)",
    r"identify (?:the\s+)?(.+?)(?:\.|$)",
    r"choose (?:the\s+)?(.+?)(?:\s+images|$)",
    r"what (?:is|are)\s+(?:a\s+|an\s+)?(.+?)(?:\?|$)",
]


def extract_target_objects(challenge_text: str) -> list[str]:
    """Extract target object(s) from the hCaptcha challenge text.
    
    Examples:
      "Select all images containing a bus" → ["bus"]
      "Please drag the spaceship to the star" → ["spaceship", "star"]
      "Which image is the odd one out?" → ["odd one out"]
    """
    text = challenge_text.lower().strip()

    for pattern in _CHALLENGE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            objects = [g.strip().rstrip(".") for g in groups if g.strip()]
            # Clean up
            cleaned = []
            for obj in objects:
                # Remove articles
                obj = re.sub(r'\ba\s+', '', obj)
                obj = re.sub(r'\ban\s+', '', obj)
                obj = re.sub(r'\bthe\s+', '', obj)
                obj = obj.strip()
                if obj:
                    cleaned.append(obj)
            if cleaned:
                return cleaned

    # Fallback: just return the whole text as a single target
    return [text]


# ── Tile Extraction ───────────────────────────────────────

def split_grid_screenshot(screenshot_bytes: bytes, grid_size: int = 3) -> list[Image.Image]:
    """Split a captcha grid screenshot into individual tile images.
    
    Args:
        screenshot_bytes: PNG bytes of the grid area
        grid_size: 3 for 3x3 grid, 4 for 4x4, etc.
    
    Returns:
        List of PIL Images, one per tile (row-major order)
    """
    img = Image.open(io.BytesIO(screenshot_bytes))
    w, h = img.size

    # Calculate tile dimensions (with slight margin cropping)
    margin_x = int(w * 0.02)
    margin_y = int(h * 0.02)
    tile_w = (w - 2 * margin_x) // grid_size
    tile_h = (h - 2 * margin_y) // grid_size

    tiles = []
    for row in range(grid_size):
        for col in range(grid_size):
            left = margin_x + col * tile_w
            top = margin_y + row * tile_h
            right = left + tile_w
            bottom = top + tile_h
            tile = img.crop((left, top, right, bottom))
            # Resize to CLIP's expected size (224x224)
            tile = tile.resize((224, 224), Image.LANCZOS)
            tiles.append(tile)

    return tiles


# ── Main Vision Solver ────────────────────────────────────

class VisionSolver:
    """CLIP-based vision solver for hCaptcha challenges.
    
    No external APIs. Uses a locally-loaded CLIP model to classify
    captcha grid tiles.
    """

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self._clip_loaded = False
        self._stats = {
            "total_challenges": 0,
            "solved": 0,
            "failed": 0,
            "tiles_classified": 0,
            "tiles_clicked": 0,
        }

    async def ensure_model_loaded(self) -> bool:
        """Load the CLIP model if not already loaded."""
        if not self._clip_loaded:
            self._log("[Vision] Loading CLIP model...")
            loaded = await load_clip()
            self._clip_loaded = loaded
            if loaded:
                self._log("[Vision] ✓ CLIP model loaded successfully")
            else:
                self._log("[Vision] ✗ CLIP model failed to load!", level="error")
        return self._clip_loaded

    async def solve_captcha(self, page, iframe=None) -> Optional[str]:
        """Full captcha solving flow.
        
        Args:
            page: Playwright page object
            iframe: Optional iframe element containing the captcha
        
        Returns:
            hCaptcha token string, or None if failed
        """
        self._stats["total_challenges"] += 1
        self._log("[Vision] Starting captcha solve...")

        # 1. Ensure model is loaded
        if not await self.ensure_model_loaded():
            self._log("[Vision] CLIP not available — cannot solve", level="error")
            return None

        # 2. Find the hCaptcha iframe if not provided
        if not iframe:
            iframe = await self._find_captcha_iframe(page)
            if not iframe:
                self._log("[Vision] No hCaptcha iframe found", level="error")
                return None

        # 3. Click checkbox to trigger challenge
        await self._click_checkbox(iframe)

        # 4. Wait for challenge to load
        await asyncio.sleep(2)

        # 5. Get challenge text
        challenge_text = await self._get_challenge_text(page, iframe)
        if not challenge_text:
            self._log("[Vision] Could not extract challenge text", level="error")
            return None

        targets = extract_target_objects(challenge_text)
        self._log(f"[Vision] Challenge: \"{challenge_text[:60]}...\" → targets: {targets}")

        # 6. Get the grid tiles
        tiles = await self._get_tiles(page, iframe)
        if not tiles or len(tiles) == 0:
            self._log("[Vision] No tiles found in captcha", level="error")
            return None

        self._log(f"[Vision] Found {len(tiles)} tiles")

        # 7. Classify each tile
        target = targets[0] if targets else "object"
        matching_indices = []
        classification_log = []

        for i, tile_img in enumerate(tiles):
            self._log(f"[Vision] Classifying tile {i+1}/{len(tiles)}...")
            confidence = await classify_tile(tile_img, target)
            is_match = confidence >= 0.65  # Threshold
            self._stats["tiles_classified"] += 1

            label = "✓ MATCH" if is_match else "✗ no"
            classification_log.append(f"Tile {i+1}: {confidence:.2f} — {label}")
            self._log(f"[Vision]   Tile {i+1}: {label} (conf={confidence:.2f})")

            if is_match:
                matching_indices.append(i)

        self._log(f"[Vision] Classification results ({len(matching_indices)} matches):")
        for line in classification_log:
            self._log(f"[Vision]   {line}")

        # 8. Click matching tiles
        if matching_indices:
            clicked = await self._click_tiles(page, iframe, matching_indices, len(tiles))
            self._stats["tiles_clicked"] += clicked
            self._log(f"[Vision] Clicked {clicked} tiles")
        else:
            self._log("[Vision] No matching tiles found — captcha may be unsolvable", level="warn")
            return None

        # 9. Click submit/verify
        await self._click_submit(page, iframe)

        # 10. Wait for and extract token
        await asyncio.sleep(2)
        token = await self._extract_token(page)

        if token:
            self._log(f"[Vision] ✓ SOLVED! Token: {token[:25]}...")
            self._stats["solved"] += 1
        else:
            self._log("[Vision] ✗ Solved but no token found", level="warn")
            self._stats["failed"] += 1

        return token

    async def _find_captcha_iframe(self, page):
        """Find the hCaptcha iframe element on the page."""
        self._log("[Vision] Looking for hCaptcha iframe...")
        for attempt in range(30):
            iframe_el = await page.query_selector(
                'iframe[src*="hcaptcha.com"], iframe[title*="hCaptcha"], '
                'iframe[title*="checkbox"], iframe[title*="Widget"]'
            )
            if iframe_el:
                self._log(f"[Vision] hCaptcha iframe found (attempt {attempt+1})")
                return iframe_el
            # Also check via JS
            has_hcaptcha = await page.evaluate("""() => {
                const f = document.querySelector('iframe[src*="hcaptcha"]');
                if (f) return true;
                const s = document.querySelector('script[src*="hcaptcha"]');
                return !!s;
            }""")
            if has_hcaptcha:
                self._log(f"[Vision] hCaptcha detected via JS (attempt {attempt+1})")
                break
            await asyncio.sleep(0.5)
        else:
            return None

        # After detecting hCaptcha, wait for iframe to appear
        for _ in range(15):
            iframe_el = await page.query_selector('iframe[src*="hcaptcha.com"]')
            if iframe_el:
                return iframe_el
            await asyncio.sleep(0.5)
        return None

    async def _click_checkbox(self, iframe):
        """Click the hCaptcha checkbox to trigger the challenge."""
        self._log("[Vision] Clicking hCaptcha checkbox...")
        try:
            # Use Playwright's frame locator
            from playwright.async_api import Page
            page = iframe._page if hasattr(iframe, '_page') else None
            if page:
                frame = page.frame_locator('iframe[src*="hcaptcha.com"]')
                checkbox = frame.locator('#checkbox')
                if await checkbox.count() > 0:
                    await checkbox.first.click()
                    self._log("[Vision] Checkbox clicked via frame locator")
                    return

            # Fallback: click via JS
            await iframe.evaluate("""() => {
                const cb = document.querySelector('#checkbox');
                if (cb) { cb.click(); return true; }
                return false;
            }""")
            self._log("[Vision] Checkbox clicked via JS")
        except Exception as e:
            self._log(f"[Vision] Checkbox click error: {e}", level="warn")
            # Try parent page evaluate
            try:
                page = iframe._page if hasattr(iframe, '_page') else None
                if page:
                    await page.evaluate("""() => {
                        const f = document.querySelector('iframe[src*="hcaptcha"]');
                        if (f) {
                            try {
                                const doc = f.contentDocument || f.contentWindow.document;
                                const cb = doc.querySelector('#checkbox');
                                if (cb) cb.click();
                            } catch(e) {}
                        }
                    }""")
                    self._log("[Vision] Checkbox clicked via parent page JS")
            except:
                pass

    async def _get_challenge_text(self, page, iframe):
        """Extract the challenge text from the captcha."""
        self._log("[Vision] Extracting challenge text...")
        try:
            # Try from the iframe first
            text = await iframe.evaluate("""() => {
                const el = document.querySelector('.challenge-text, .task-text, [class*="challenge"], [class*="prompt"], .header, h1, h2, .title');
                return el ? el.textContent.trim() : null;
            }""")
            if text:
                return text
        except:
            pass

        # Try from parent page (challenge text sometimes appears outside iframe)
        try:
            text = await page.evaluate("""() => {
                const el = document.querySelector('.hcaptcha-challenge-text, .challenge-text, [class*="captcha"] strong');
                return el ? el.textContent.trim() : null;
            }""")
            if text:
                return text
        except:
            pass

        # Try the iframe's full text content
        try:
            text = await iframe.evaluate("""() => document.body ? document.body.innerText.trim() : null""")
            if text and len(text) < 200:
                return text
        except:
            pass

        return None

    async def _get_tiles(self, page, iframe):
        """Get individual tile images from the captcha grid.
        
        Strategy:
        1. Try to extract individual tile elements from the iframe DOM
        2. Fallback: take iframe screenshot and split into grid
        """
        self._log("[Vision] Capturing grid tiles...")

        # Strategy 1: Find tile elements in iframe
        try:
            tile_count = await iframe.evaluate("""() => {
                const tiles = document.querySelectorAll('.task-image, [class*="image"], [role="button"] > div, .grid > div, .cell, td, img[class*="task"]');
                return tiles.length;
            }""")
            if tile_count > 0:
                self._log(f"[Vision] Found {tile_count} tile elements in iframe DOM")
                # Get bounding boxes
                tile_boxes = await iframe.evaluate("""() => {
                    const tiles = document.querySelectorAll('.task-image, [class*="image"], [role="button"] > div, .grid > div, .cell, td, img[class*="task"]');
                    const boxes = [];
                    tiles.forEach(t => {
                        const r = t.getBoundingClientRect();
                        if (r.width > 30 && r.height > 30) {
                            boxes.push({x: r.x, y: r.y, w: r.width, h: r.height});
                        }
                    });
                    return boxes;
                }""")
                if tile_boxes and len(tile_boxes) >= 3:
                    self._log(f"[Vision] Got {len(tile_boxes)} tile bounding boxes")
                    # Screenshot each tile individually using page.screenshot with absolute coords
                    tiles = []
                    iframe_box = await iframe.bounding_box()
                    if iframe_box:
                        for box in tile_boxes:
                            clip = {
                                'x': iframe_box['x'] + box['x'],
                                'y': iframe_box['y'] + box['y'],
                                'width': box['w'],
                                'height': box['h']
                            }
                            tile_bytes = await page.screenshot(clip=clip)
                            tile_img = Image.open(io.BytesIO(tile_bytes))
                            tile_img = tile_img.resize((224, 224), Image.LANCZOS)
                            tiles.append(tile_img)
                        if tiles:
                            return tiles
                    else:
                        self._log("[Vision] Could not get iframe bounding box", level="warn")
        except Exception as e:
            self._log(f"[Vision] DOM tile extraction error: {e}", level="warn")

        # Strategy 2: Take iframe screenshot and split into grid
        self._log("[Vision] Taking iframe screenshot and splitting grid...")
        try:
            grid_bytes = await iframe.screenshot()
            # Determine grid size from aspect ratio
            img = Image.open(io.BytesIO(grid_bytes))
            w, h = img.size
            # hCaptcha grids are typically 3x3 (square-ish) or 4x4
            aspect = w / h
            if aspect > 1.2:
                grid_size = 3
            elif aspect < 0.8:
                grid_size = 3
            else:
                grid_size = 3  # Default 3x3

            self._log(f"[Vision] Grid: {grid_size}x{grid_size} ({w}x{h}px)")
            tiles = split_grid_screenshot(grid_bytes, grid_size)
            if tiles:
                return tiles
        except Exception as e:
            self._log(f"[Vision] Grid split error: {e}", level="warn")

        return []

    async def _click_tiles(self, page, iframe, indices, total_tiles):
        """Click the matching tiles in the captcha grid."""
        self._log(f"[Vision] Clicking {len(indices)} matching tiles...")

        clicked = 0
        try:
            # Try clicking via DOM if we have tile elements
            for idx in indices:
                clicked_ok = await iframe.evaluate(f"""() => {{
                    const tiles = document.querySelectorAll('.task-image, [class*="image"], [role="button"] > div, .grid > div, .cell, td, img[class*="task"]');
                    let visible = [];
                    tiles.forEach(t => {{
                        const r = t.getBoundingClientRect();
                        if (r.width > 30 && r.height > 30) visible.push(t);
                    }});
                    if (visible[{idx}]) {{
                        visible[{idx}].click();
                        return true;
                    }}
                    return false;
                }}""")
                if clicked_ok:
                    clicked += 1
                    await asyncio.sleep(0.3)
        except:
            pass

        # Fallback: click by grid position (row-major)
        if clicked == 0:
            self._log("[Vision] Fallback: clicking by grid position...")
            grid_size = int(total_tiles ** 0.5)
            for idx in indices:
                row = idx // grid_size
                col = idx % grid_size
                try:
                    # Calculate click position within the iframe
                    tile_w = 100.0 / grid_size
                    tile_h = 100.0 / grid_size
                    x_pct = (col + 0.5) * tile_w
                    y_pct = (row + 0.5) * tile_h

                    box = await iframe.bounding_box()
                    if box:
                        x = box['x'] + (box['width'] * x_pct / 100)
                        y = box['y'] + (box['height'] * y_pct / 100)
                        await page.mouse.click(x, y)
                        clicked += 1
                        await asyncio.sleep(0.3)
                except:
                    pass

        # If still nothing, try the "select all" button
        if clicked == 0:
            self._log("[Vision] Trying Select All button...", level="warn")
            try:
                await iframe.evaluate("""() => {
                    const btn = document.querySelector('button:has-text("Select All")');
                    if (btn) btn.click();
                }""")
                clicked = 1
            except:
                pass

        return clicked

    async def _click_submit(self, page, iframe):
        """Click the submit/verify button."""
        self._log("[Vision] Clicking submit/verify...")
        try:
            await iframe.evaluate("""() => {
                const btn = document.querySelector('button[type="submit"], .submit, #submit, button:has-text("Verify"), button:has-text("Next")');
                if (btn) { btn.click(); return true; }
                return false;
            }""")
            self._log("[Vision] Submit clicked")
        except Exception as e:
            self._log(f"[Vision] Submit click error: {e}", level="warn")

    async def _extract_token(self, page) -> Optional[str]:
        """Extract the hCaptcha token from the page."""
        self._log("[Vision] Extracting token...")

        # Check textarea
        token = await page.evaluate("""() => {
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            return ta && ta.value && ta.value.length > 20 ? ta.value : '';
        }""")
        if token:
            return token

        # Check hcaptcha response
        token = await page.evaluate("""() => {
            if (window.hcaptcha && window.hcaptcha.getResponse) {
                const r = window.hcaptcha.getResponse();
                if (r && r.length > 20) return r;
            }
            return '';
        }""")
        if token:
            return token

        # Wait a bit more and try again
        await asyncio.sleep(1)
        token = await page.evaluate("""() => {
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            return ta && ta.value && ta.value.length > 20 ? ta.value : '';
        }""")
        return token or None

    async def set_token_on_page(self, page, token: str) -> bool:
        """Inject a solved token into the page's hCaptcha textarea."""
        try:
            result = await page.evaluate(f"""() => {{
                const ta = document.querySelector('textarea[name="h-captcha-response"]');
                if (ta) {{
                    ta.value = '{token}';
                    ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                    ta.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }}
                return false;
            }}""")
            if result:
                self._log("[Vision] ✓ Token injected into page")
                return True
            self._log("[Vision] Could not find hCaptcha textarea", level="warn")
            return False
        except Exception as e:
            self._log(f"[Vision] Token injection error: {e}", level="error")
            return False

    def get_stats(self) -> dict:
        return dict(self._stats)


# ── Legacy Alias ──────────────────────────────────────────

# Old code imported SolverAPI — keep backward compat
SolverAPI = VisionSolver
