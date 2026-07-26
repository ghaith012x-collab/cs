"""
CAPTCHA Solver — Pixel-Similarity & Multi-Strategy Solver.
No heavy ML models needed. Uses lightweight pixel analysis.

Strategy Flow (tried in order):
  0. Click checkbox, wait for auto-pass (token appears without grid)
  1. Extract challenge text — only proceed if it's a real grid instruction
  2. Extract tiles from iframe (DOM first, then screenshot split)
  3a. Pixel-similarity: compare each tile against the "average" tile
      → Tiles that differ significantly = matching tiles
  3b. CLIP model (optional, only if pixel analysis is inconclusive)
  4. Click matching tiles, submit, check for token
  5. If all fails, return None (no crash)
"""

import asyncio
import io
import random
import re
import time
from typing import Callable, Optional
from math import sqrt

from PIL import Image


# ── Pixel Similarity (No ML needed) ──────────────────────

def _tile_signature(img: Image.Image) -> list[float]:
    """Compute a lightweight signature for a tile image.
    
    Uses color histogram (reduced bins) + average brightness + edge density.
    Fast to compute, no ML needed.
    """
    # Resize to small for speed
    small = img.resize((32, 32), Image.LANCZOS)
    
    # 1. Average brightness
    gray = small.convert('L')
    avg_brightness = sum(gray.getdata()) / (32 * 32)
    
    # 2. Color histogram (simplified: average R, G, B)
    pixels = list(small.getdata())
    r_avg = sum(p[0] for p in pixels) / len(pixels)
    g_avg = sum(p[1] for p in pixels) / len(pixels)
    b_avg = sum(p[2] for p in pixels) / len(pixels)
    
    # 3. Variance (texture complexity)
    variance = sum((p[0] - r_avg)**2 + (p[1] - g_avg)**2 + (p[2] - b_avg)**2 
                   for p in pixels) / len(pixels)
    
    # 4. Edge density (using simple horizontal gradient)
    edge_sum = 0
    for y in range(32):
        for x in range(31):
            edge_sum += abs(gray.getpixel((x+1, y)) - gray.getpixel((x, y)))
    edge_density = edge_sum / (32 * 31)
    
    return [avg_brightness / 255, r_avg / 255, g_avg / 255, b_avg / 255, 
            variance / 50000, edge_density / 50]


def _signature_distance(sig1: list[float], sig2: list[float]) -> float:
    """Euclidean distance between two tile signatures."""
    return sqrt(sum((a - b) ** 2 for a, b in zip(sig1, sig2)))


def find_matching_tiles_by_similarity(tiles: list[Image.Image], 
                                       threshold: float = 0.15) -> list[int]:
    """Find tiles that are significantly different from the majority.
    
    Logic:
    1. Compute signatures for all tiles
    2. Find the "average" signature (median of each component)
    3. Tiles far from the average = matching tiles
    4. If ALL tiles are similar, return empty (no challenge detected)
    
    This works because hCaptcha grid challenges typically have:
    - A few tiles containing the target object (different)
    - Most tiles showing "empty" backgrounds (similar to each other)
    """
    if len(tiles) < 3:
        return list(range(len(tiles)))  # Not enough tiles, click all
    
    sigs = [_tile_signature(t) for t in tiles]
    n_dims = len(sigs[0])
    
    # Compute median signature (more robust than mean)
    median_sig = []
    for dim in range(n_dims):
        vals = sorted(s[dim] for s in sigs)
        median_sig.append(vals[len(vals) // 2])
    
    # Distance of each tile from median
    distances = [_signature_distance(s, median_sig) for s in sigs]
    avg_dist = sum(distances) / len(distances)
    
    # Adaptive threshold: tiles farther than avg_dist * 1.5 are matches
    # OR use fixed threshold for very uniform grids
    adaptive_threshold = max(threshold, avg_dist * 1.2)
    
    matching = [i for i, d in enumerate(distances) if d > adaptive_threshold]
    
    # If too many matches (> 70% of tiles), probably no real challenge
    # Return empty to trigger fallback strategy
    if len(matching) > len(tiles) * 0.7:
        return []
    
    return matching


# ── Challenge Text Parsing ────────────────────────────────

# These are ACTUAL grid challenge patterns (not widget headers)
_GRID_CHALLENGE_PATTERNS = [
    r"select all images? (?:containing|with|that have|that show|that display|of)\s+(?:a\s+|an\s+)?(.+?)(?:\s+below|\.|$)",
    r"select all (?:the\s+)?(.+?)(?:\s+images|\s+pictures|\s+below|\.|$)",
    r"click on (?:the\s+)?(?:matching\s+)?(.+?)(?:\s+images|\.|$)",
    r"please select (?:the\s+)?(.+?)(?:\s+below|\.|$)",
    r"choose all (?:the\s+)?(.+?)(?:\s+below|\.|$)",
    r"identify (?:the\s+)?(.+?)(?:\s+in|\s+below|\.|$)",
    r"which (?:images?|pictures?|ones?) (?:are|contain|show|have)\s+(?:a\s+|an\s+)?(.+?)(?:\?|\.|$)",
]

# Widget header texts that should NOT be treated as challenges
_SKIP_TEXTS = [
    "create an account", "security check", "verify you're human", 
    "complete the security check", "powered by", "hcaptcha",
    "checkbox", "challenge", "access denied", "security",
    "please try again", "your browser", "enabled", "cookies",
]


def extract_target_objects(challenge_text: str) -> list[str]:
    """Extract target object(s) from the hCaptcha challenge text.
    
    Returns empty list if the text is not a real grid challenge.
    """
    if not challenge_text:
        return []
    
    text = challenge_text.lower().strip()
    
    # Skip widget headers / non-challenge text
    for skip in _SKIP_TEXTS:
        if skip in text:
            return []  # Not a real challenge
    
    # Match grid challenge patterns
    for pattern in _GRID_CHALLENGE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            objects = [g.strip().rstrip(".,?!") for g in groups if g.strip()]
            cleaned = []
            for obj in objects:
                obj = re.sub(r'\ba\s+', '', obj)
                obj = re.sub(r'\ban\s+', '', obj)
                obj = re.sub(r'\bthe\s+', '', obj)
                obj = obj.strip()
                if obj and len(obj) > 1:
                    cleaned.append(obj)
            if cleaned:
                return cleaned
    
    return []  # Not a recognizable challenge


# ── Tile Extraction ───────────────────────────────────────

def split_grid_screenshot(screenshot_bytes: bytes, grid_size: int = 3) -> list[Image.Image]:
    """Split a captcha grid screenshot into individual tile images."""
    img = Image.open(io.BytesIO(screenshot_bytes))
    w, h = img.size
    
    margin_x = int(w * 0.02)
    margin_y = int(h * 0.02)
    tile_w = (w - 2 * margin_x) // grid_size
    tile_h = (h - 2 * margin_y) // grid_size
    
    if tile_w < 20 or tile_h < 20:
        return []  # Too small, invalid screenshot
    
    tiles = []
    for row in range(grid_size):
        for col in range(grid_size):
            left = margin_x + col * tile_w
            top = margin_y + row * tile_h
            right = left + tile_w
            bottom = top + tile_h
            tile = img.crop((left, top, right, bottom))
            tile = tile.resize((128, 128), Image.LANCZOS)  # Smaller for speed
            tiles.append(tile)
    
    return tiles


# ═══════════════════════════════════════════════════════════
# DRAG/JIGSAW PUZZLE SOLVER (Arkose Labs Funcaptcha)
# ═══════════════════════════════════════════════════════════

def find_drag_piece_position(screenshot_bytes: bytes) -> int:
    """
    Find the horizontal offset to drag the puzzle piece in a Funcaptcha.
    
    Strategy:
    1. Take screenshot of the captcha iframe
    2. The bottom portion has a track with the draggable piece
       The piece has IMAGE content (high variance), the track is plain
    3. The top portion has the puzzle image with a GAP
       The gap shows where the piece should go (smooth/low variance)
    4. Find piece's center on the track
    5. Find gap's center in the image
    6. Return the pixel offset = gap_center - piece_center
    
    Returns pixel offset to drag right, or None if detection fails.
    """
    try:
        from PIL import Image
        import io
        
        img = Image.open(io.BytesIO(screenshot_bytes))
        w, h = img.size
        
        if w < 100 or h < 100:
            return None
        
        gray = img.convert('L')
        pixels = gray.load()
        
        # ── Find the track area (bottom 25-40% of widget) ──
        track_top = int(h * 0.60)
        track_bot = int(h * 0.95)
        
        # ── Find the image area (top 5-60% of widget) ──
        img_top = int(h * 0.05)
        img_bot = int(h * 0.55)
        
        # ── Scan columns to find piece position ──
        # The piece creates high vertical variance (has image content)
        # The track has low variance (plain background)
        piece_scores = []
        for x in range(w):
            variance = 0
            for y in range(track_top + 2, track_bot):
                diff = abs(pixels[x, y] - pixels[x, y-1])
                variance += diff
            piece_scores.append(variance)
        
        # Normalize scores
        if not piece_scores:
            return None
        max_score = max(piece_scores)
        if max_score < 10:
            return None
        piece_scores = [s / max_score for s in piece_scores]
        
        # Find clusters of high-variance columns (the piece) 
        # in the left half of the track
        piece_left = None
        piece_right = None
        in_piece = False
        for x in range(1, w // 2):
            if piece_scores[x] > 0.3 and not in_piece:
                piece_left = x
                in_piece = True
            elif piece_scores[x] < 0.15 and in_piece:
                piece_right = x
                in_piece = False
                break
        if in_piece and piece_left:
            piece_right = w // 2 - 1
        
        if not piece_left or not piece_right:
            # Fallback: look for the piece by finding its distinct edges
            for x in range(1, w // 2):
                if abs(piece_scores[x] - piece_scores[x-1]) > 0.2:
                    if not piece_left:
                        piece_left = x
                    else:
                        piece_right = x
                        break
        
        if not piece_left or not piece_right:
            # Last fallback: piece is usually at 15-25% of track width
            piece_center = int(w * 0.2)
        else:
            piece_center = (piece_left + piece_right) // 2
        
        # ── Scan image area to find the gap ──
        # The gap has LOW vertical variance (plain/missing region)
        # The image has HIGH vertical variance (has content)
        gap_scores = []
        for x in range(w):
            variance = 0
            for y in range(img_top + 2, img_bot):
                diff = abs(pixels[x, y] - pixels[x, y-1])
                variance += diff
            gap_scores.append(variance)
        
        # Find the gap - a cluster of LOW variance in the right half
        # surrounded by HIGH variance (the image content)
        min_gap_score = min(gap_scores[w//2:])
        threshold = min_gap_score * 1.5
        
        gap_left = None
        gap_right = None
        in_gap = False
        for x in range(w // 2, w):
            if gap_scores[x] < threshold and not in_gap:
                gap_left = x
                in_gap = True
            elif gap_scores[x] > threshold * 2 and in_gap:
                gap_right = x
                break
        
        if in_gap and gap_left:
            gap_right = w - 1
        
        if gap_left and gap_right:
            gap_center = (gap_left + gap_right) // 2
        else:
            # Fallback: look for gap in the middle-right
            gap_center = int(w * 0.65)
        
        offset = gap_center - piece_center
        
        # Sanity check: offset should be positive (drag right) and reasonable
        if offset < 10 or offset > w * 0.8:
            # Try alternative approach
            return None
        
        return offset
        
    except Exception as e:
        print(f"[DragSolver] Error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None

# ═══════════════════════════════════════════════════════════
# Main Vision Solver
# ═══════════════════════════════════════════════════════════

class VisionSolver:
    """Pixel-similarity captcha solver. No ML model needed.
    
    Strategy 0: Click checkbox, wait for auto-pass
    Strategy 1: Pixel similarity — compare tile signatures
    Strategy 2: Brute force — click all tiles
    Strategy 3: CLIP (optional, only if available)
    """
    
    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self._clip_loaded = False
        self._stats = {
            "total_challenges": 0,
            "solved": 0,
            "failed": 0,
            "strategy_used": "",
        }

    async def ensure_model_loaded(self) -> bool:
        """CLIP model not needed by default, but available if loaded."""
        # By default, return True — pixel-similarity doesn't need CLIP
        return True

    async def solve_captcha(self, page, iframe=None) -> Optional[str]:
        """Full captcha solving flow.
        
        Returns hCaptcha token string, or None if failed.
        NEVER crashes the caller.
        """
        self._stats["total_challenges"] += 1
        self._log("[Vision] Starting captcha solve...")
        
        try:
            # Step 1: Find iframe if not provided
            if not iframe:
                iframe = await self._find_captcha_iframe(page)
                if not iframe:
                    self._log("[Vision] No hCaptcha iframe found", level="warn")
                    return None
            
            # Step 2: Check for existing token first
            token = await self._try_extract_token(page)
            if token:
                self._log("[Vision] ✓ Token already present — no solving needed")
                self._stats["solved"] += 1
                return token
            
            # Step 3: Click checkbox
            await self._click_checkbox(page, iframe)
            
            # Step 4: Wait and check for auto-pass (token appears without grid)
            await asyncio.sleep(2)
            token = await self._try_extract_token(page)
            if token:
                self._log("[Vision] ✓ Auto-pass! Token obtained without grid")
                self._stats["solved"] += 1
                self._stats["strategy_used"] = "auto_pass"
                return token
            
            # Step 5: Wait for challenge to fully load
            await asyncio.sleep(2)
            
            # Step 6: Check for token again (sometimes appears after full load)
            token = await self._try_extract_token(page)
            if token:
                self._log("[Vision] ✓ Token appeared after challenge load")
                self._stats["solved"] += 1
                self._stats["strategy_used"] = "delayed_auto_pass"
                return token
            
            # Step 7: Get challenge text — validate it's a real grid challenge
            challenge_text = await self._get_challenge_text(page, iframe)
            targets = extract_target_objects(challenge_text) if challenge_text else []
            
            if targets:
                target = targets[0]
                self._log(f"[Vision] Grid challenge: \"{challenge_text[:60]}...\" → target: '{target}'")
            else:
                if challenge_text:
                    self._log(f"[Vision] Challenge text not a grid instruction: \"{challenge_text[:60]}...\"")
                    self._log("[Vision] Trying pixel-similarity approach...")
                else:
                    self._log("[Vision] No challenge text found — trying pixel approach...")
                target = ""
            
            # Step 8: Extract tiles
            tiles = await self._get_tiles(page, iframe)
            if not tiles or len(tiles) < 2:
                self._log("[Vision] Could not extract tiles from captcha", level="warn")
                # Last try: check if captcha passed already
                token = await self._try_extract_token(page)
                return token or None
            
            self._log(f"[Vision] Extracted {len(tiles)} tiles from grid")
            
            # Step 9: Find matching tiles using pixel similarity
            matching_indices = find_matching_tiles_by_similarity(tiles)
            
            if matching_indices:
                self._log(f"[Vision] Pixel analysis found {len(matching_indices)} matching tiles: {matching_indices}")
                self._stats["strategy_used"] = "pixel_similarity"
            else:
                self._log("[Vision] Pixel analysis inconclusive — trying all tiles")
                matching_indices = list(range(len(tiles)))
                self._stats["strategy_used"] = "all_tiles"
            
            # Step 10: Click matching tiles
            clicked = await self._click_tiles(page, iframe, matching_indices)
            self._log(f"[Vision] Clicked {clicked} tiles")
            
            # Step 11: Submit
            await self._click_submit(page, iframe)
            
            # Step 12: Wait and extract token
            await asyncio.sleep(2)
            token = await self._try_extract_token(page)
            
            if token:
                self._log(f"[Vision] ✓ SOLVED! Token: {token[:25]}...")
                self._stats["solved"] += 1
            else:
                self._log("[Vision] ✗ Captcha not solved — no token found", level="warn")
                self._stats["failed"] += 1
            
            return token
            
        except Exception as e:
            self._log(f"[Vision] Solver error (caught): {e}", level="error")
            import traceback
            traceback.print_exc()
            return None

    async def _find_captcha_iframe(self, page):
        """Find hCaptcha iframe."""
        try:
            for attempt in range(20):
                iframe_el = await page.query_selector('iframe[src*="hcaptcha.com"], iframe[title*="hCaptcha"]')
                if iframe_el:
                    self._log(f"[Vision] Iframe found (attempt {attempt+1})")
                    return iframe_el
                await asyncio.sleep(0.5)
        except:
            pass
        return None

    async def _click_checkbox(self, page, iframe):
        """Click the hCaptcha checkbox."""
        self._log("[Vision] Clicking checkbox...")
        try:
            # Try clicking via iframe evaluate
            result = await iframe.evaluate("""() => {
                const cb = document.querySelector('#checkbox, [role="checkbox"], .checkbox');
                if (cb) { cb.click(); return true; }
                // Try clicking the whole iframe as fallback
                return false;
            }""")
            if result:
                self._log("[Vision] Checkbox clicked via iframe JS")
                return
        except:
            pass
        
        # Fallback: click the iframe element directly
        try:
            await iframe.click()
            self._log("[Vision] Checkbox clicked via iframe.click()")
        except:
            self._log("[Vision] Could not click checkbox", level="warn")

    async def _get_challenge_text(self, page, iframe):
        """Extract challenge text from the captcha."""
        try:
            # Try iframe text
            text = await iframe.evaluate("""() => {
                const els = document.querySelectorAll(
                    '.challenge-text, .task-text, .prompt-text, ' +
                    '.header-text, [class*="prompt"], [class*="task"], ' +
                    'h1, h2, .title, strong, [class*="challenge"]'
                );
                for (const el of els) {
                    if (el.offsetParent !== null) {
                        const t = el.textContent.trim();
                        if (t.length > 5 && t.length < 200) return t;
                    }
                }
                // Full body text (filtered)
                const body = document.body ? document.body.innerText.trim() : '';
                return body.length < 300 ? body : '';
            }""")
            if text:
                return text.strip()
        except:
            pass
        
        # Try parent page
        try:
            text = await page.evaluate("""() => {
                const el = document.querySelector('[class*="captcha"], [class*="challenge"]');
                return el ? el.textContent.trim() : '';
            }""")
            if text:
                return text.strip()
        except:
            pass
        
        return None

    async def _get_tiles(self, page, iframe):
        """Extract tile images from the captcha grid."""
        tiles = []
        
        # Strategy A: DOM tile extraction with page.screenshot()
        try:
            tile_data = await iframe.evaluate("""() => {
                const selectors = '.task-image, [class*="image"], [role="button"] > div, ' +
                                  '.grid-item, .cell, td, img[class*="task"], ' +
                                  '.image-grid > div, [class*="tile"]';
                const els = document.querySelectorAll(selectors);
                const result = [];
                els.forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width > 30 && r.height > 30 && 
                        r.width < 500 && r.height < 500) {
                        result.push({x: r.x, y: r.y, w: r.width, h: r.height});
                    }
                });
                return JSON.stringify(result);
            }""")
            
            if tile_data:
                import json
                boxes = json.loads(tile_data)
                if len(boxes) >= 2:
                    iframe_box = await iframe.bounding_box()
                    if iframe_box:
                        for box in boxes:
                            clip = {
                                'x': iframe_box['x'] + box['x'],
                                'y': iframe_box['y'] + box['y'],
                                'width': box['w'],
                                'height': box['h']
                            }
                            tile_bytes = await page.screenshot(clip=clip)
                            tile_img = Image.open(io.BytesIO(tile_bytes))
                            tile_img = tile_img.resize((128, 128), Image.LANCZOS)
                            tiles.append(tile_img)
                        if tiles:
                            self._log(f"[Vision] Extracted {len(tiles)} tiles via DOM")
                            return tiles
        except Exception as e:
            self._log(f"[Vision] DOM extraction skipped: {e}", level="warn")
        
        # Strategy B: Iframe screenshot + grid split
        try:
            grid_bytes = await iframe.screenshot()
            img = Image.open(io.BytesIO(grid_bytes))
            w, h = img.size
            
            if w > 50 and h > 50:
                # Detect grid size from aspect ratio
                aspect = w / h
                if 0.8 <= aspect <= 1.2:
                    grid_size = 3  # Square-ish = 3x3
                elif aspect > 1.5:
                    grid_size = 4  # Wide = 4x3 or 4x4
                else:
                    grid_size = 3
                
                tiles = split_grid_screenshot(grid_bytes, grid_size)
                if tiles:
                    self._log(f"[Vision] Extracted {len(tiles)} tiles via grid split ({grid_size}x{grid_size})")
                    return tiles
        except Exception as e:
            self._log(f"[Vision] Grid split failed: {e}", level="warn")
        
        return []

    async def _click_tiles(self, page, iframe, indices):
        """Click tiles by index. Returns number clicked."""
        clicked = 0
        
        # Method 1: Click via iframe evaluate (DOM)
        try:
            for idx in indices:
                ok = await iframe.evaluate(f"""() => {{
                    const tiles = document.querySelectorAll(
                        '.task-image, [class*="image"], [role="button"] > div, ' +
                        '.grid-item, .cell, td, img[class*="task"], ' +
                        '.image-grid > div, [class*="tile"]'
                    );
                    const visible = [];
                    tiles.forEach(t => {{
                        const r = t.getBoundingClientRect();
                        if (r.width > 30 && r.height > 30 && r.width < 500) visible.push(t);
                    }});
                    if (visible[{idx}]) {{
                        visible[{idx}].scrollIntoView({{block: 'nearest'}});
                        visible[{idx}].click();
                        return true;
                    }}
                    return false;
                }}""")
                if ok:
                    clicked += 1
                    await asyncio.sleep(0.2)
        except:
            pass
        
        if clicked >= len(indices):
            return clicked
        
        # Method 2: Click by page coordinates (if iframe bounding box available)
        try:
            box = await iframe.bounding_box()
            if box:
                grid_size = 3  # Assume 3x3
                tile_w = box['width'] / grid_size
                tile_h = box['height'] / grid_size
                margin = min(tile_w, tile_h) * 0.05
                
                for idx in indices:
                    if idx >= grid_size * grid_size:
                        continue
                    row = idx // grid_size
                    col = idx % grid_size
                    x = box['x'] + col * tile_w + tile_w / 2
                    y = box['y'] + row * tile_h + tile_h / 2
                    await page.mouse.click(x, y)
                    clicked += 1
                    await asyncio.sleep(0.2)
        except:
            pass
        
        return clicked

    async def _click_submit(self, page, iframe):
        """Click the submit/verify button."""
        try:
            await iframe.evaluate("""() => {
                const btn = document.querySelector(
                    'button[type="submit"], .submit-btn, #submit, ' +
                    '[class*="submit"], [class*="verify"], ' +
                    'button:not([class*="checkbox"])'
                );
                if (btn) { btn.click(); return; }
                // Try any button
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.offsetParent !== null) { b.click(); return; }
                }
            }""")
            self._log("[Vision] Submit clicked")
        except:
            self._log("[Vision] Could not click submit", level="warn")

    async def _try_extract_token(self, page) -> Optional[str]:
        """Try to extract hCaptcha token. Returns None if not found."""
        try:
            # Check textarea
            token = await page.evaluate("""() => {
                const ta = document.querySelector('textarea[name="h-captcha-response"]');
                if (ta && ta.value && ta.value.length > 20) return ta.value;
                // Check hcaptcha API
                if (window.hcaptcha && window.hcaptcha.getResponse) {
                    const r = window.hcaptcha.getResponse();
                    if (r && r.length > 20) return r;
                }
                return '';
            }""")
            if token:
                return token
        except:
            pass
        return None

    async def set_token_on_page(self, page, token: str) -> bool:
        """Inject a solved token into the page."""
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
                self._log("[Vision] ✓ Token injected")
                return True
        except:
            pass
        return False

    async def solve_drag_captcha(self, page, iframe=None) -> bool:
        """
        Solve a Funcaptcha drag/jigsaw puzzle.
        
        Strategy:
        1. Find the captcha element (iframe or div)
        2. Screenshot it
        3. Analyze pixels to find piece position and target gap
        4. Drag the piece to align with gap
        
        Returns True if solved.
        """
        self._stats["total_challenges"] += 1
        self._log("[DragSolver] Starting drag captcha solve...")
        
        try:
            # Find the captcha iframe
            if not iframe:
                for attempt in range(15):
                    for sel in [
                        'iframe[src*="funcaptcha"]',
                        'iframe[src*="arkose"]',
                        'iframe[title*="captcha"]',
                        'iframe[src*="captcha"]',
                        '[id*="funcaptcha"]',
                        '[class*="funcaptcha"]',
                        '[class*="Challenge"]',
                    ]:
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                iframe = el
                                self._log(f"[DragSolver] Found captcha element via: {sel}")
                                break
                        except:
                            pass
                    if iframe:
                        break
                    await asyncio.sleep(1)
            
            if not iframe:
                # Try looking for a div-based captcha widget directly
                for attempt in range(10):
                    try:
                        has_captcha = await page.evaluate("""() => {
                            const body = document.body.innerText.toLowerCase();
                            if (body.includes('captcha') && body.includes('drag') || 
                                body.includes('drag the')) return true;
                            // Check for Arkose-specific elements
                            const els = document.querySelectorAll(
                                '[id*="arkose"], [class*="funcaptcha"], ' +
                                '[class*="challenge"], iframe[src*="arkose"]'
                            );
                            return els.length > 0;
                        }""")
                        if has_captcha:
                            self._log("[DragSolver] Captcha detected on page (text/class check)")
                            # Try to find any iframe with size > 200px
                            iframe_info = await page.evaluate("""() => {
                                const frames = document.querySelectorAll('iframe');
                                for (const f of frames) {
                                    const r = f.getBoundingClientRect();
                                    if (r.width > 200 && r.height > 100) {
                                        return {x: r.x, y: r.y, w: r.width, h: r.height, sel: f.tagName};
                                    }
                                }
                                return null;
                            }""")
                            if iframe_info:
                                self._log(f"[DragSolver] Found iframe {iframe_info['w']}x{iframe_info['h']}")
                                iframe = await page.query_selector(f'iframe:nth-of-type({1})')
                                # If we couldn't find the exact iframe, we'll screenshot the area
                    except:
                        pass
                    await asyncio.sleep(0.5)
                
                if not iframe:
                    self._log("[DragSolver] No captcha iframe found", level="warn")
                    return False
            
            # Take screenshot of the captcha area
            await asyncio.sleep(1)
            
            box = None
            try:
                box = await iframe.bounding_box()
            except:
                pass
            
            if not box:
                self._log("[DragSolver] Could not get iframe bounding box", level="warn")
                return False
            
            self._log(f"[DragSolver] Captcha area: {box['w']:.0f}x{box['h']:.0f}")
            
            # Screenshot the captcha area
            try:
                clip = {'x': box['x'], 'y': box['y'], 'width': box['w'], 'height': box['h']}
                captcha_bytes = await page.screenshot(clip=clip)
            except:
                captcha_bytes = await iframe.screenshot()
            
            if not captcha_bytes or len(captcha_bytes) < 1000:
                self._log("[DragSolver] Screenshot too small", level="warn")
                return False
            
            # Find the drag offset using pixel analysis
            offset = find_drag_piece_position(captcha_bytes)
            
            if offset is None:
                self._log("[DragSolver] Could not determine drag offset — trying alternative")
                # Alternative: try scanning the track more aggressively
                offset = None
                # Try brute force: piece starts at ~20% width, gap at ~65%
                for piece_pct in [0.2, 0.15, 0.25, 0.3]:
                    for gap_pct in [0.65, 0.6, 0.7, 0.55, 0.75]:
                        test_offset = int(box['w'] * (gap_pct - piece_pct))
                        if 30 < test_offset < box['w'] * 0.7:
                            offset = test_offset
                            self._log(f"[DragSolver] Using estimated offset: {offset}px ({gap_pct-piece_pct:.0%})")
                            break
                    if offset:
                        break
            
            if offset is None or offset < 10 or offset > box['w'] * 0.8:
                self._log(f"[DragSolver] Invalid offset: {offset}", level="warn")
                return False
            
            self._log(f"[DragSolver] Calculated drag offset: {offset}px")
            
            # Find the drag handle position
            # The piece is typically in the bottom-left of the widget
            handle_x = box['x'] + box['w'] * 0.15
            handle_y = box['y'] + box['h'] * 0.85
            
            end_x = handle_x + offset
            end_y = handle_y
            
            # Perform the drag with smooth mouse movement
            self._log(f"[DragSolver] Dragging from ({handle_x:.0f},{handle_y:.0f}) → ({end_x:.0f},{end_y:.0f})")
            
            try:
                await page.mouse.move(handle_x, handle_y)
                await asyncio.sleep(0.15)
                await page.mouse.down()
                await asyncio.sleep(0.08)
                
                # Smooth drag in steps (for natural look + better capture)
                steps = 25
                for i in range(1, steps + 1):
                    progress = i / steps
                    # Ease-in-out for natural feel
                    eased = progress * progress * (3 - 2 * progress)  # smoothstep
                    x = handle_x + offset * eased
                    y = handle_y + random.uniform(-1, 1)  # slight jitter for realism
                    await page.mouse.move(x, y)
                    await asyncio.sleep(0.015 + random.uniform(0, 0.01))
                    if i % 5 == 0:
                        await asyncio.sleep(0.05)
                
                await asyncio.sleep(0.15)
                await page.mouse.up()
                self._log("[DragSolver] ✓ Drag completed")
                
            except Exception as e:
                self._log(f"[DragSolver] Drag failed: {e}", level="warn")
                return False
            
            # Wait for the captcha to verify the solution
            await asyncio.sleep(2)
            
            # Check if solved by looking for success indicators
            solved = await page.evaluate("""() => {
                // Check for token in Arkose/Funcaptcha response
                const textarea = document.querySelector('textarea[name="fc-token"]');
                if (textarea && textarea.value && textarea.value.length > 10) return 'fc_token';
                // Check for generic captcha response
                const ta2 = document.querySelector('textarea[name="g-recaptcha-response"]');
                if (ta2 && ta2.value && ta2.value.length > 10) return 'recaptcha';
                // Look for success class/hiding
                const challenge = document.querySelector('[class*="challenge"], [class*="Challenge"]');
                if (challenge && challenge.style.display === 'none') return 'hidden';
                return '';
            }""")
            
            if solved:
                self._log(f"[DragSolver] ✓ SOLVED! ({solved})")
                self._stats["solved"] += 1
                self._stats["strategy_used"] = "drag"
                return True
            else:
                self._log("[DragSolver] ✗ Not solved — trying again with adjusted position...")
                # Try a couple more times with slight adjustments
                for attempt in range(2):
                    offset_adjustment = int(offset * (-0.1 + 0.2 * attempt))
                    new_offset = offset + offset_adjustment
                    if new_offset < 10:
                        continue
                    try:
                        await page.mouse.move(handle_x, handle_y)
                        await asyncio.sleep(0.1)
                        await page.mouse.down()
                        await asyncio.sleep(0.05)
                        end_x2 = handle_x + new_offset
                        for i in range(1, 16):
                            x = handle_x + new_offset * (i / 16)
                            await page.mouse.move(x, handle_y + random.uniform(-0.5, 0.5))
                            await asyncio.sleep(0.02)
                        await page.mouse.up()
                        await asyncio.sleep(1.5)
                        
                        solved2 = await page.evaluate("""() => {
                            const ta = document.querySelector('textarea[name="fc-token"]');
                            if (ta && ta.value && ta.value.length > 10) return true;
                            return false;
                        }""")
                        if solved2:
                            self._log(f"[DragSolver] ✓ Solved on attempt {attempt+2}!")
                            self._stats["solved"] += 1
                            self._stats["strategy_used"] = "drag_retry"
                            return True
                    except:
                        pass
                
                self._log("[DragSolver] ✗ Failed to solve drag captcha", level="error")
                self._stats["failed"] += 1
                return False
            
        except Exception as e:
            self._log(f"[DragSolver] Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    def get_stats(self) -> dict:
        return dict(self._stats)


# ── Backward Compatibility ────────────────────────────────
SolverAPI = VisionSolver
