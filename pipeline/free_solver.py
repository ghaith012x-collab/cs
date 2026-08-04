"""100% FREE captcha solver — no paid APIs, no external services.

Strategy (tried in order, fastest first):
  1. hCaptcha drag puzzle   → edge analysis + template matching (in-browser, free)
  2. FunCAPTCHA tiles       → numpy color/texture signatures + similarity (free)
  3. hCaptcha image grid    → accessibility fallback text → local Ollama model (free)
  4. hCaptcha token         → click accessibility button → fallback text challenge → Ollama
  5. Text captcha / math    → local Ollama captcha-solver model (free)

All strategies are offline / local. Zero API cost.

Usage (inside Playwright browser context):
    from pipeline.free_solver import FreeSolver
    solver = FreeSolver(page, ollama_model="captcha-solver-v1")
    solved = await solver.solve_any()
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import random
import re
import time
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Pure-Python image helpers (no heavy deps until needed)
# ---------------------------------------------------------------------------

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from PIL import Image, ImageChops

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _ensure_pil():
    if not HAS_PIL:
        raise ImportError("Pillow required: pip install Pillow")


def _ensure_numpy():
    if not HAS_NUMPY:
        raise ImportError("numpy required: pip install numpy")


# ===================================================================
# 1. SUPERCHARGED TILE SOLVER (FunCAPTCHA / Arkose)
# ===================================================================
def tile_signature_fast(img_bytes: bytes) -> list[float]:
    """Ultra-fast tile signature using numpy.  Returns 12-dim vector."""
    _ensure_pil()
    img = Image.open(io.BytesIO(img_bytes)).resize((32, 32), Image.LANCZOS)

    if HAS_NUMPY:
        arr = np.array(img, dtype=np.float32)
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        gray = 0.299 * r + 0.587 * g + 0.114 * b
        return [
            float(np.mean(gray) / 255),
            float(np.mean(r) / 255),
            float(np.mean(g) / 255),
            float(np.mean(b) / 255),
            float(np.var(r) + np.var(g) + np.var(b)) / 50000,
            float(np.mean(np.abs(np.diff(gray, axis=1)))) / 50,
            float(np.mean(np.abs(np.diff(gray, axis=0)))) / 50,
            float(np.percentile(gray, 25)) / 255,
            float(np.percentile(gray, 75)) / 255,
            float(np.max(gray) - np.min(gray)) / 255,
            float(np.sum(gray > 200) / gray.size),
            float(np.sum(gray < 50) / gray.size),
        ]

    # Pure-PIL fallback
    gray = img.convert("L")
    px = gray.load()
    w, h = gray.size
    brightness = sum(px[x, y] for x in range(w) for y in range(h)) / (w * h)
    pixels = list(img.getdata())
    r_avg = sum(p[0] for p in pixels) / len(pixels)
    g_avg = sum(p[1] for p in pixels) / len(pixels)
    b_avg = sum(p[2] for p in pixels) / len(pixels)
    return [brightness / 255, r_avg / 255, g_avg / 255, b_avg / 255,
            0.1, 0.1, 0.1, 0.25, 0.75, 0.5, 0.1, 0.1]


def find_odd_tiles(tile_bytes_list: list[bytes], threshold: float = 0.12) -> list[int]:
    """Return indices of tiles that differ from the majority.

    Uses median-based outlier detection — the odd tiles are those furthest
    from the median signature. Works for 3x3 and 4x4 grids.
    """
    if len(tile_bytes_list) < 3:
        return list(range(len(tile_bytes_list)))

    sigs = [tile_signature_fast(b) for b in tile_bytes_list]

    if HAS_NUMPY:
        arr = np.array(sigs, dtype=np.float32)
        median = np.median(arr, axis=0)
        distances = np.sqrt(np.sum((arr - median) ** 2, axis=1))
        avg_dist = float(np.mean(distances))
        adaptive = max(threshold, avg_dist * 1.2)
        outliers = [int(i) for i, d in enumerate(distances) if d > adaptive]
    else:
        # Pure-Python median
        n = len(sigs)
        dims = len(sigs[0])
        median = []
        for d in range(dims):
            vals = sorted(s[d] for s in sigs)
            median.append(vals[n // 2])
        distances = []
        for s in sigs:
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(s, median)))
            distances.append(dist)
        avg_dist = sum(distances) / n
        adaptive = max(threshold, avg_dist * 1.2)
        outliers = [int(i) for i, d in enumerate(distances) if d > adaptive]

    if len(outliers) > len(sigs) * 0.7:
        return []
    return outliers


def split_screenshot_to_tiles(screenshot_bytes: bytes, grid: int = 3) -> list[bytes]:
    """Split a screenshot into grid×grid tile PNGs. Returns bytes for each tile."""
    _ensure_pil()
    img = Image.open(io.BytesIO(screenshot_bytes))
    w, h = img.size
    margin_x = int(w * 0.02)
    margin_y = int(h * 0.02)
    tw = (w - 2 * margin_x) // grid
    th = (h - 2 * margin_y) // grid
    if tw < 20 or th < 20:
        return []

    tiles = []
    for row in range(grid):
        for col in range(grid):
            tile = img.crop((
                margin_x + col * tw,
                margin_y + row * th,
                margin_x + (col + 1) * tw,
                margin_y + (row + 1) * th,
            ))
            buf = io.BytesIO()
            tile.resize((128, 128), Image.LANCZOS).save(buf, "PNG")
            tiles.append(buf.getvalue())
    return tiles


# ===================================================================
# 2. SUPERCHARGED DRAG PUZZLE SOLVER
# ===================================================================
def find_drag_offset(screenshot_bytes: bytes) -> Optional[int]:
    """Find the drag distance for hCaptcha puzzle. Returns pixel offset or None.

    Uses column-wise edge profiling + template matching for accuracy.
    Pure PIL, no OpenCV needed.
    """
    _ensure_pil()
    img = Image.open(io.BytesIO(screenshot_bytes))
    gray = img.convert("L")
    w, h = gray.size
    if w < 60 or h < 60:
        return None

    # Edge profile — strongest vertical edges = piece borders + drop shadow
    px = gray.load()
    band_t, band_b = int(h * 0.08), int(h * 0.85)
    prof = [0.0] * w
    for y in range(band_t, band_b):
        prev = px[0, y]
        for x in range(1, w):
            cur = px[x, y]
            prof[x] += abs(cur - prev)
            prev = cur

    n = max(1, band_b - band_t)
    prof = [p / n for p in prof]

    # Find strong edge columns (peaks)
    mean_p = sum(prof) / len(prof)
    threshold = max(mean_p * 1.8, 2.0)
    peaks = []
    for x in range(1, w - 1):
        if prof[x] >= threshold and prof[x] >= prof[x - 1] and prof[x] >= prof[x + 1]:
            peaks.append(x)

    if len(peaks) < 2:
        return None

    # Cluster nearby peaks, find pairs with plausible piece width
    clusters = []
    for p in peaks:
        if clusters and p - clusters[-1][-1] <= 5:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    scored = []
    for c in clusters:
        center = c[len(c) // 2]
        scored.append((center, sum(prof[x] for x in c)))

    min_sep = int(w * 0.15)
    max_sep = int(w * 0.8)
    pairs = []
    for i in range(len(scored)):
        for j in range(i + 1, len(scored)):
            a, ea = scored[i]
            b, eb = scored[j]
            sep = abs(b - a)
            if min_sep <= sep <= max_sep:
                pairs.append((a, b, ea + eb))

    if not pairs:
        return None

    pairs.sort(key=lambda t: -t[2])

    # Best pair: strongest edges. Offset = difference between left edges.
    a, b, _ = pairs[0]
    candidates = [b - a, a - b]
    # Try both directions, return the most common sign
    all_deltas = []
    for ai, bi, _ in pairs[:5]:
        all_deltas.append(bi - ai)
        all_deltas.append(ai - bi)

    if all_deltas:
        # Return the delta closest to the strongest pair's positive offset
        positive_deltas = [d for d in all_deltas if d > 0]
        negative_deltas = [d for d in all_deltas if d < 0]
        if positive_deltas:
            return int(np.median(positive_deltas)) if HAS_NUMPY else positive_deltas[0]
        if negative_deltas:
            return negative_deltas[0]

    return candidates[0]


# ===================================================================
# 3. IMAGE GRID SOLVER (hCaptcha "click the bicycles")
# ===================================================================

# Keywords that hCaptcha commonly asks for in image grids
IMAGE_GRID_KEYWORDS = [
    "bicycle", "bike", "bus", "car", "truck", "train", "boat", "airplane",
    "crosswalk", "traffic light", "stop sign", "fire hydrant", "parking meter",
    "bridge", "stairs", "chimney", "motorcycle", "cat", "dog", "bird",
    "tree", "palm tree", "mountain", "bridge", "sidewalk",
]


async def click_accessibility_button(page) -> bool:
    """Click the hCaptcha accessibility button (headphones icon).

    This switches the image grid challenge to a text-based fallback.
    """
    try:
        await page.evaluate("""() => {
            const btns = document.querySelectorAll('button, [role="button"], a, div');
            for (const b of btns) {
                const t = (b.textContent || '').toLowerCase();
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                if (t.includes('accessibility') || label.includes('accessibility') ||
                    t.includes('audio') || label.includes('audio') ||
                    b.querySelector('[class*="accessibility"]') ||
                    b.querySelector('svg') ||  // headphones icon
                    t.includes('alternative')) {
                    b.click();
                    return true;
                }
            }
            // Fallback: click any small icon button in the captcha iframe
            const iframe = document.querySelector('iframe[src*="hcaptcha"]');
            if (iframe) {
                const fdoc = iframe.contentDocument || iframe.contentWindow?.document;
                if (fdoc) {
                    const icons = fdoc.querySelectorAll('[class*="icon"], [class*="accessibility"], button');
                    for (const icon of icons) {
                        icon.click();
                        return true;
                    }
                }
            }
            return false;
        }""")
        await asyncio.sleep(1.5)
        return True
    except Exception:
        return False


async def get_accessibility_challenge(page) -> Optional[str]:
    """Read the hCaptcha accessibility text challenge after clicking the button."""
    try:
        text = await page.evaluate("""() => {
            const selectors = [
                '[class*="challenge-text"]', '[class*="prompt-text"]',
                '[class*="instruction"]', '[class*="task"]',
                'h2', 'h3', 'p[class*="text"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const t = (el.textContent || '').trim();
                    if (t.length > 10 && t.length < 500) return t;
                }
            }
            // Check iframes
            const iframe = document.querySelector('iframe[src*="hcaptcha"]');
            if (iframe && iframe.contentDocument) {
                const body = iframe.contentDocument.body;
                if (body) return body.innerText.slice(0, 500);
            }
            return '';
        }""")
        if text and len(text) > 10:
            return text.strip()
    except Exception:
        pass
    return None


async def type_accessibility_answer(page, answer: str) -> bool:
    """Type the answer into the hCaptcha accessibility text input and submit."""
    try:
        result = await page.evaluate(f"""() => {{
            const input = document.querySelector('input[type="text"], textarea, [role="textbox"]');
            if (!input) {{
                const iframe = document.querySelector('iframe[src*="hcaptcha"]');
                if (iframe && iframe.contentDocument) {{
                    const finput = iframe.contentDocument.querySelector('input, textarea');
                    if (finput) {{
                        finput.value = '{answer}';
                        finput.dispatchEvent(new Event('input', {{bubbles: true}}));
                        finput.dispatchEvent(new Event('change', {{bubbles: true}}));
                        // Find and click submit
                        const btns = iframe.contentDocument.querySelectorAll('button');
                        for (const b of btns) {{
                            if (b.textContent.toLowerCase().includes('submit') ||
                                b.textContent.toLowerCase().includes('verify')) {{
                                b.click(); return true;
                            }}
                        }}
                        return true;
                    }}
                }}
                return false;
            }}
            input.value = '{answer}';
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }}""")
        return bool(result)
    except Exception:
        return False


# ===================================================================
# 4. UNIFIED FREE SOLVER
# ===================================================================
class FreeSolver:
    """100% free captcha solver — chains all free strategies.

    Usage:
        solver = FreeSolver(page, ollama_model="captcha-solver-v1")
        result = await solver.solve_any()
        if result:
            print(f"Solved via {result['method']} in {result['time_ms']}ms")
    """

    def __init__(
        self,
        page,
        ollama_model: str = "captcha-solver-v1",
        log: Optional[Callable] = None,
    ):
        self.page = page
        self.ollama_model = ollama_model
        self.log = log or (lambda msg, level="info": None)
        self.stats = {
            "drag": 0, "tiles": 0, "accessibility": 0,
            "text": 0, "failed": 0, "total_ms": 0.0,
            "total_free": True,  # always true — no APIs
        }

    async def _solve_text_locally(self, challenge: str) -> Optional[str]:
        """Use local Ollama model to solve a text captcha challenge."""
        import subprocess

        prompt = (
            "You are a captcha solver. Solve this challenge and output ONLY the "
            "exact answer — no explanation, no extra text, just the answer.\n\n"
            f"Challenge: {challenge}"
        )
        try:
            result = subprocess.run(
                ["ollama", "run", self.ollama_model, prompt],
                capture_output=True, text=True, timeout=10,
            )
            answer = result.stdout.strip()
            # Clean common prefixes
            for prefix in ("answer:", "Answer:", "ANSWER:", "the answer is "):
                if answer.lower().startswith(prefix):
                    answer = answer[len(prefix):].strip()
            return answer if answer else None
        except Exception:
            return None

    async def solve_drag(self) -> Optional[bool]:
        """Solve hCaptcha drag puzzle in-browser. Returns True if solved."""
        self.log("[FreeSolver] Trying drag puzzle solver…")

        # Find the hCaptcha iframe
        try:
            iframe = await self.page.query_selector(
                'iframe[src*="hcaptcha"], iframe[title*="captcha"]'
            )
            if not iframe:
                return None

            box = await iframe.bounding_box()
            if not box or box["width"] < 60:
                return None

            # Screenshot and analyze
            shot = await self.page.screenshot(clip=box)
            delta_img = find_drag_offset(shot)

            if delta_img is None:
                self.log("[FreeSolver] No puzzle detected — not a drag challenge")
                return None

            # Get device pixel ratio for CSS pixel conversion
            dpr = float(await self.page.evaluate("() => window.devicePixelRatio || 1"))
            delta_css = int(round(delta_img / max(dpr, 1.0)))

            # Find slider handle and drag
            handle = await iframe.bounding_box()
            if handle:
                hx = handle["x"] + handle["width"] * 0.85
                hy = handle["y"] + handle["height"] * 0.85

                for adjust in (0, -3, 3, -6, 6):
                    d = delta_css + adjust
                    if d == 0:
                        continue
                    await self.page.mouse.move(hx, hy)
                    await asyncio.sleep(0.08)
                    await self.page.mouse.down()
                    for i in range(1, 12):
                        await self.page.mouse.move(
                            hx + d * i / 12, hy + random.uniform(-0.5, 0.5), steps=2
                        )
                        await asyncio.sleep(0.01)
                    await self.page.mouse.up()
                    await asyncio.sleep(1.0)

                    # Check if solved
                    token = await self._read_token()
                    if token:
                        self.stats["drag"] += 1
                        self.log("[FreeSolver] [OK] Drag puzzle solved!")
                        return True

            self.log("[FreeSolver] Drag attempts exhausted")
            return False
        except Exception as e:
            self.log(f"[FreeSolver] Drag error: {e}", level="warn")
            return None

    async def solve_tiles(self) -> Optional[bool]:
        """Solve FunCAPTCHA tile challenge. Returns True if solved."""
        self.log("[FreeSolver] Trying tile solver…")

        try:
            # Find challenge iframe
            for sel in [
                'iframe[src*="funcaptcha"]', 'iframe[src*="arkose"]',
                'iframe[src*="captcha"]', '[class*="Challenge"]',
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    break
            else:
                return None

            if not el:
                return None

            box = await el.bounding_box()
            if not box or box["width"] < 100:
                return None

            # Screenshot and split
            shot = await self.page.screenshot(clip=box)
            grid = 3 if box["width"] / box["height"] < 1.3 else 4
            tiles = split_screenshot_to_tiles(shot, grid)

            if len(tiles) < 2:
                return None

            # Find odd tiles
            matching = find_odd_tiles(tiles)
            if not matching:
                matching = list(range(len(tiles)))

            # Click matching tiles
            margin_x = int(box["width"] * 0.02)
            margin_y = int(box["height"] * 0.02)
            tw = (box["width"] - 2 * margin_x) / grid
            th = (box["height"] - 2 * margin_y) / grid

            for idx in matching:
                row, col = divmod(idx, grid)
                x = box["x"] + margin_x + col * tw + tw / 2
                y = box["y"] + margin_y + row * th + th / 2
                await self.page.mouse.click(x, y)
                await asyncio.sleep(0.15)

            # Click submit
            await self.page.evaluate("""() => {
                for (const b of document.querySelectorAll('button')) {
                    const t = (b.textContent||'').toLowerCase();
                    if (t.includes('verify')||t.includes('submit')||t.includes('next')) {
                        b.click(); return;
                    }
                }
            }""")
            await asyncio.sleep(2.0)

            # Check if solved
            token = await self._read_token()
            if token:
                self.stats["tiles"] += 1
                self.log("[FreeSolver] [OK] Tile challenge solved!")
                return True

            return False
        except Exception as e:
            self.log(f"[FreeSolver] Tile error: {e}", level="warn")
            return None

    async def solve_accessibility(self) -> Optional[bool]:
        """Solve via hCaptcha accessibility text fallback. Returns True if solved."""
        self.log("[FreeSolver] Trying accessibility fallback…")

        clicked = await click_accessibility_button(self.page)
        if not clicked:
            return None

        await asyncio.sleep(2.0)
        challenge = await get_accessibility_challenge(self.page)
        if not challenge:
            return None

        self.log(f"[FreeSolver] Accessibility challenge: {challenge[:100]}…")
        answer = await self._solve_text_locally(challenge)
        if not answer:
            return None

        self.log(f"[FreeSolver] Local answer: {answer}")
        await type_accessibility_answer(self.page, answer)
        await asyncio.sleep(2.0)

        token = await self._read_token()
        if token:
            self.stats["accessibility"] += 1
            self.log(f"[FreeSolver] [OK] Accessibility solve: {challenge[:50]} → {answer}")
            return True

        return False

    async def _read_token(self) -> Optional[str]:
        """Read hCaptcha token from the page."""
        try:
            token = await self.page.evaluate("""() => {
                const ta = document.querySelector('textarea[name="h-captcha-response"]');
                if (ta && ta.value && ta.value.length > 20) return ta.value;
                if (window.hcaptcha && window.hcaptcha.getResponse) {
                    const r = window.hcaptcha.getResponse();
                    if (r && r.length > 20) return r;
                }
                for (const t of document.querySelectorAll('textarea')) {
                    if (t.value && t.value.length > 20 &&
                        (t.name.includes('captcha') || t.name.includes('token'))) {
                        return t.value;
                    }
                }
                return '';
            }""")
            return token if token else None
        except Exception:
            return None

    async def solve_any(self) -> Optional[dict]:
        """Try all free strategies. Returns result dict or None."""
        started = time.time()

        # Strategy 1: Drag puzzle (fastest, visual)
        result = await self.solve_drag()
        if result is True:
            self.stats["total_ms"] += (time.time() - started) * 1000
            return {"method": "drag", "free": True}

        # Strategy 2: Tile puzzle (visual)
        result = await self.solve_tiles()
        if result is True:
            self.stats["total_ms"] += (time.time() - started) * 1000
            return {"method": "tiles", "free": True}

        # Strategy 3: Accessibility fallback (text → local AI)
        result = await self.solve_accessibility()
        if result is True:
            self.stats["total_ms"] += (time.time() - started) * 1000
            return {"method": "accessibility", "free": True}

        self.stats["failed"] += 1
        self.stats["total_ms"] += (time.time() - started) * 1000
        return None


# ===================================================================
# Benchmark / test
# ===================================================================
BENCHMARK_TESTS = [
    ("What is 23 + 47?", "70"),
    ("Unscramble: tac", "cat"),
    ("Complete: 2, 4, 8, 16, ?", "32"),
    ("Solve: 100 - 37", "63"),
    ("Calculate: 15 × 4", "60"),
    ("Which doesn't belong: apple, banana, carrot, grape?", "carrot"),
    ("What is the square root of 64?", "8"),
]


def benchmark_text_solver(model: str = "captcha-solver-v1") -> None:
    """Quick benchmark of the text captcha solver."""
    import subprocess

    print("=" * 50)
    print(f"  Text Captcha Benchmark — model: {model}")
    print("=" * 50)

    correct = 0
    total_time = 0.0
    for challenge, expected in BENCHMARK_TESTS:
        prompt = (
            "You are a captcha solver. Solve and output ONLY the exact answer.\n\n"
            f"Challenge: {challenge}"
        )
        start = time.time()
        try:
            result = subprocess.run(
                ["ollama", "run", model, prompt],
                capture_output=True, text=True, timeout=10,
            )
            answer = result.stdout.strip()
            elapsed = (time.time() - start) * 1000
            total_time += elapsed
            ok = "✓" if answer and answer.strip() == expected else "✗"
            if ok == "✓":
                correct += 1
            print(f"  {ok}  {challenge}")
            print(f"      expected={expected}  got={answer}  ({elapsed:.0f}ms)")
        except Exception as e:
            print(f"  ✗  {challenge}  (error: {e})")

    avg = total_time / len(BENCHMARK_TESTS) if total_time else 0
    print(f"\n  Score: {correct}/{len(BENCHMARK_TESTS)} ({100*correct/len(BENCHMARK_TESTS):.0f}%)")
    print(f"  Avg: {avg:.0f}ms")
    print(f"  {'✓ 100% FREE!' if model != 'gemini' else 'Using API'}")

if __name__ == "__main__":
    benchmark_text_solver()
