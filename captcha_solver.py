"""
CAPTCHA SOLVER — NoCaptchaAI-first (nocaptchaai.com) with offline pixel fallbacks.
No Gemini. No NopeCHA. No local AI models.

Strategy Flow:
  hCaptcha (Discord): NoCaptchaAI HCaptchaTaskProxyless token API —
      sitekey + pageurl -> hCaptcha token (typically < 5s).
      API key comes from the API_KEY environment variable.
  FunCAPTCHA (Arkose): offline pixel-similarity tile solver (no API needed).

NoCaptchaAI is 2captcha-compatible:
  POST /createTask     {"clientKey", "task": {...}} -> {"errorId":0, "taskId":"..."}
  POST /getTaskResult  {"clientKey", "taskId"}      -> {"errorId":0,"status":"ready","solution":{...}}
  POST /getBalance     {"clientKey"}                -> {"errorId":0,"balance":0.0,...}
"""

import asyncio
import io
import json
import os
import re
import time
from math import sqrt
from typing import Callable, Optional

import aiohttp
from PIL import Image

NOCAPTCHAAI_BASE = "https://api.nocaptchaai.com"

# Browser fingerprint sent with hCaptcha tasks (improves solve accuracy).
SOLVER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _api_key() -> str:
    """NoCaptchaAI API key from the environment (API_KEY)."""
    return (os.environ.get("API_KEY") or "").strip()


# ── NoCaptchaAI Client ────────────────────────────────────

class NoCaptchaAI:
    """Async client for the NoCaptchaAI API (2captcha-compatible)."""

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self._key = _api_key()
        self.stats = {"calls": 0, "ok": 0, "failed": 0}

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def _post(self, endpoint: str, payload: dict,
                    timeout: float = 30.0) -> dict:
        url = f"{NOCAPTCHAAI_BASE}/{endpoint}"
        body = dict(payload)
        body.setdefault("clientKey", self._key)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    return await resp.json(content_type=None)
        except Exception as e:
            self._log(f"[NoCaptchaAI] {endpoint} error: {e}", level="error")
            return {}

    async def create_task(self, task: dict) -> Optional[str]:
        """Create a solving task. Returns the taskId or None."""
        if not self._key:
            self._log("[NoCaptchaAI] No API_KEY set", level="warn")
            return None
        self.stats["calls"] += 1
        data = await self._post("createTask", {"task": task})
        error_id = data.get("errorId")
        if error_id not in (0, None):
            self._log(f"[NoCaptchaAI] createTask error {error_id}: "
                      f"{data.get('error', data)}", level="error")
            self.stats["failed"] += 1
            return None
        task_id = data.get("taskId")
        if isinstance(task_id, str) and task_id:
            return task_id
        self._log(f"[NoCaptchaAI] createTask odd response: {data}", level="error")
        self.stats["failed"] += 1
        return None

    async def get_task_result(self, task_id: str) -> Optional[dict]:
        data = await self._post("getTaskResult", {"taskId": task_id})
        error_id = data.get("errorId")
        if error_id not in (0, None):
            self._log(f"[NoCaptchaAI] getTaskResult error {error_id}: "
                      f"{data.get('error', data)}", level="error")
            return None
        return data

    async def solve_hcaptcha(self, sitekey: str, pageurl: str,
                             timeout: float = 120.0,
                             poll: float = 2.0) -> Optional[str]:
        """Solve hCaptcha. Returns the h-captcha-response token or None."""
        self._log(f"[NoCaptchaAI] hCaptcha task (sitekey {sitekey[:12]}...)")
        task = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": pageurl,
            "websiteKey": sitekey,
            "userAgent": SOLVER_UA,
        }
        task_id = await self.create_task(task)
        if not task_id:
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(poll)
            result = await self.get_task_result(task_id)
            if not result:
                continue
            status = result.get("status")
            if status == "ready":
                solution = result.get("solution") or {}
                token = (solution.get("gRecaptchaResponse")
                         or solution.get("token") or "")
                if isinstance(token, str) and len(token) > 20:
                    self.stats["ok"] += 1
                    self._log(f"[NoCaptchaAI] [OK] hCaptcha token ({len(token)} chars)")
                    return token
                self._log("[NoCaptchaAI] ready but empty solution", level="error")
                self.stats["failed"] += 1
                return None
            if status in ("failed", "error"):
                self._log(f"[NoCaptchaAI] task failed: {result}", level="error")
                self.stats["failed"] += 1
                return None
        self._log(f"[NoCaptchaAI] hCaptcha task timed out after {int(timeout)}s",
                  level="warn")
        self.stats["failed"] += 1
        return None

    async def get_balance(self) -> Optional[dict]:
        """Fetch the account balance. Returns the raw API dict or None."""
        data = await self._post("getBalance", {})
        if data.get("errorId") not in (0, None):
            return None
        return data


# ── hCaptcha sitekey extraction (DOM, no extensions) ──────

async def extract_hcaptcha_sitekey(page) -> str:
    """Pull the hCaptcha sitekey from every possible source.

    Discord sets a data-sitekey attribute immediately, so we check that first.
    Fall back to iframe src hash fragments, a full iframe scan, and the
    hcaptcha JS global object if it exists.
    """
    # Strategy 1: [data-sitekey] on the parent page (Discord always has this)
    try:
        sk = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            return el ? el.getAttribute('data-sitekey') : '';
        }""")
        if sk and len(str(sk).strip()) > 5:
            return str(sk).strip()
    except Exception:
        pass
    # Strategy 2: sitekey in any hcaptcha iframe src hash fragment
    try:
        src = await page.evaluate("""() => {
            const f = document.querySelector('iframe[src*="hcaptcha.com"]');
            return f ? f.src : '';
        }""")
        m = re.search(r"sitekey=([^&]+)", src or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    # Strategy 3: scan every iframe for a sitekey in the src
    try:
        sitekey = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            for (const f of iframes) {
                const src = f.src || '';
                const m = src.match(/sitekey=([^&#]+)/);
                if (m) return m[1];
            }
            return '';
        }""")
        if sitekey:
            return sitekey.strip()
    except Exception:
        pass
    # Strategy 4: check the hcaptcha JS global object
    try:
        sk = await page.evaluate("""() => {
            if (window.hcaptcha && window.hcaptcha.getSitekey) {
                try { return window.hcaptcha.getSitekey(); } catch(e) {}
            }
            return '';
        }""")
        if sk and len(str(sk).strip()) > 5:
            return str(sk).strip()
    except Exception:
        pass
    return ""


# ── DOM token helpers ─────────────────────────────────────

async def read_hcaptcha_token(page) -> Optional[str]:
    """Read the current hCaptcha response token from the page."""
    try:
        token = await page.evaluate("""() => {
            const ta = document.querySelector('textarea[name="h-captcha-response"]');
            if (ta && ta.value && ta.value.length > 20) return ta.value;
            if (window.hcaptcha && window.hcaptcha.getResponse) {
                const r = window.hcaptcha.getResponse();
                if (r && r.length > 20) return r;
            }
            return '';
        }""")
        if token:
            return token
    except Exception:
        pass
    return None


async def set_hcaptcha_token_on_page(page, token: str) -> bool:
    """Inject a solved hCaptcha token into the form textarea."""
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
        return bool(result)
    except Exception:
        return False


# ── FunCAPTCHA challenge text (for logging) ───────────────

async def extract_funcaptcha_task(page, iframe=None) -> str:
    """Read the FunCAPTCHA challenge instruction text from the page."""
    try:
        if iframe:
            text = await iframe.evaluate("""() => {
                const els = document.querySelectorAll('[class*="challenge"], [class*="prompt"], [class*="instruction"], [class*="header"], h1, h2, [class*="title"]');
                for (const el of els) {
                    const t = (el.textContent || '').trim();
                    if (t.length > 6 && t.length < 200) return t;
                }
                return document.body ? document.body.innerText.slice(0, 300) : '';
            }""")
            if text and len(str(text).strip()) > 5:
                return str(text).strip()
    except Exception:
        pass
    try:
        text = await page.evaluate("""() => {
            const el = document.querySelector('[class*="challenge"], [class*="prompt"], [class*="instruction"], [class*="header"]');
            return el ? el.textContent.trim().slice(0, 200) : '';
        }""")
        if text and len(str(text).strip()) > 5:
            return str(text).strip()
    except Exception:
        pass
    return ""


# ── Offline tile similarity (no API, no ML) ───────────────

def _tile_signature(img: Image.Image) -> list[float]:
    """Lightweight color/texture signature for a tile image."""
    small = img.resize((32, 32), Image.LANCZOS)
    gray = small.convert('L')
    avg_brightness = sum(gray.getdata()) / (32 * 32)
    pixels = list(small.getdata())
    r_avg = sum(p[0] for p in pixels) / len(pixels)
    g_avg = sum(p[1] for p in pixels) / len(pixels)
    b_avg = sum(p[2] for p in pixels) / len(pixels)
    variance = sum((p[0] - r_avg)**2 + (p[1] - g_avg)**2 + (p[2] - b_avg)**2
                   for p in pixels) / len(pixels)
    edge_sum = 0
    for y in range(32):
        for x in range(31):
            edge_sum += abs(gray.getpixel((x + 1, y)) - gray.getpixel((x, y)))
    edge_density = edge_sum / (32 * 31)
    return [avg_brightness / 255, r_avg / 255, g_avg / 255, b_avg / 255,
            variance / 50000, edge_density / 50]


def _signature_distance(sig1: list[float], sig2: list[float]) -> float:
    return sqrt(sum((a - b) ** 2 for a, b in zip(sig1, sig2)))


def find_matching_tiles_by_similarity(tiles: list[Image.Image],
                                      threshold: float = 0.15) -> list[int]:
    """Tiles that differ significantly from the majority (offline fallback)."""
    if len(tiles) < 3:
        return list(range(len(tiles)))
    sigs = [_tile_signature(t) for t in tiles]
    n_dims = len(sigs[0])
    median_sig = []
    for dim in range(n_dims):
        vals = sorted(s[dim] for s in sigs)
        median_sig.append(vals[len(vals) // 2])
    distances = [_signature_distance(s, median_sig) for s in sigs]
    avg_dist = sum(distances) / len(distances)
    adaptive_threshold = max(threshold, avg_dist * 1.2)
    matching = [i for i, d in enumerate(distances) if d > adaptive_threshold]
    if len(matching) > len(tiles) * 0.7:
        return []
    return matching


def split_grid_screenshot(screenshot_bytes: bytes,
                          grid_size: int = 3) -> list[Image.Image]:
    """Split a challenge screenshot into a square grid of tile images."""
    img = Image.open(io.BytesIO(screenshot_bytes))
    w, h = img.size
    margin_x = int(w * 0.02)
    margin_y = int(h * 0.02)
    tile_w = (w - 2 * margin_x) // grid_size
    tile_h = (h - 2 * margin_y) // grid_size
    if tile_w < 20 or tile_h < 20:
        return []
    tiles = []
    for row in range(grid_size):
        for col in range(grid_size):
            left = margin_x + col * tile_w
            top = margin_y + row * tile_h
            tile = img.crop((left, top, left + tile_w, top + tile_h))
            tile = tile.resize((128, 128), Image.LANCZOS)
            tiles.append(tile)
    return tiles


# ── FunCAPTCHA tile solver (offline) ──────────────────────

FUNCAPTCHA_SELECTORS = [
    'iframe[src*="funcaptcha"]', 'iframe[src*="arkose"]',
    'iframe[title*="captcha"]', 'iframe[src*="captcha"]',
    '[id*="funcaptcha"]', '[class*="funcaptcha"]',
    '[class*="Challenge"]',
]


async def solve_funcaptcha_pixels(page, iframe=None,
                                  log: Optional[Callable] = None) -> bool:
    """Solve a FunCAPTCHA/Arkose tile challenge offline via pixel similarity.

    Returns True when a solved state (fc-token present / challenge hidden)
    is detected after clicking the matching tiles.
    """
    log = log or (lambda msg, level="info": None)

    # 1) Locate the challenge element
    if iframe is None:
        for sel in FUNCAPTCHA_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    iframe = el
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)
    if not iframe:
        log("[FunCAPTCHA] No challenge element found", level="warn")
        return False
    await asyncio.sleep(1)

    clicked = 0

    # 2) Preferred: extract tile boxes from the DOM
    tile_boxes = []
    try:
        data = await iframe.evaluate("""() => {
            const selectors = '.task-image, [class*="image"], [role="button"] > div, ' +
                              '.grid-item, .cell, td, img[class*="task"], ' +
                              '.image-grid > div, [class*="tile"]';
            const els = document.querySelectorAll(selectors);
            const out = [];
            els.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 30 && r.height > 30 && r.width < 500 && r.height < 500) {
                    out.push({x: r.x, y: r.y, w: r.width, h: r.height});
                }
            });
            return JSON.stringify(out);
        }""")
        if data:
            boxes = json.loads(data)
            if len(boxes) >= 2:
                ibox = await iframe.bounding_box()
                if ibox:
                    tile_boxes = [{'x': ibox['x'] + b['x'], 'y': ibox['y'] + b['y'],
                                   'w': b['w'], 'h': b['h']} for b in boxes]
    except Exception:
        pass

    if len(tile_boxes) >= 2:
        tiles = []
        valid = []
        for i, b in enumerate(tile_boxes):
            try:
                clip = {'x': b['x'], 'y': b['y'], 'width': b['w'], 'height': b['h']}
                shot = await page.screenshot(clip=clip)
                tiles.append(Image.open(io.BytesIO(shot)).resize((128, 128), Image.LANCZOS))
                valid.append(i)
            except Exception:
                tiles.append(None)
        if len(valid) >= 2:
            sig_tiles = [tiles[i] for i in valid]
            local = find_matching_tiles_by_similarity(sig_tiles)
            matching = [valid[i] for i in local] if local else []
            if not matching:
                log("[FunCAPTCHA] No standout tiles - clicking all", level="warn")
                matching = valid
            for idx in matching:
                b = tile_boxes[idx]
                try:
                    await page.mouse.click(b['x'] + b['w'] / 2, b['y'] + b['h'] / 2)
                    clicked += 1
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            log(f"[FunCAPTCHA] Clicked {clicked} tiles (DOM boxes)")

    # 3) Fallback: grid split of the whole challenge area
    if clicked == 0:
        try:
            box = await iframe.bounding_box()
            if box and box['width'] >= 100 and box['height'] >= 100:
                clip = {'x': box['x'], 'y': box['y'],
                        'width': box['width'], 'height': box['height']}
                shot = await page.screenshot(clip=clip)
                img = Image.open(io.BytesIO(shot))
                w, h = img.size
                grid = 4 if (w / h) > 1.5 else 3
                tiles = split_grid_screenshot(shot, grid)
                if len(tiles) >= 2:
                    matching = find_matching_tiles_by_similarity(tiles)
                    if not matching:
                        matching = list(range(len(tiles)))
                    margin_x = int(box['width'] * 0.02)
                    margin_y = int(box['height'] * 0.02)
                    tile_w = (box['width'] - 2 * margin_x) / grid
                    tile_h = (box['height'] - 2 * margin_y) / grid
                    for idx in matching:
                        row, col = divmod(idx, grid)
                        x = box['x'] + margin_x + col * tile_w + tile_w / 2
                        y = box['y'] + margin_y + row * tile_h + tile_h / 2
                        try:
                            await page.mouse.click(x, y)
                            clicked += 1
                        except Exception:
                            pass
                        await asyncio.sleep(0.25)
                    log(f"[FunCAPTCHA] Clicked {clicked} tiles (grid split)")
        except Exception as e:
            log(f"[FunCAPTCHA] grid split error: {e}", level="warn")

    if clicked == 0:
        log("[FunCAPTCHA] No tiles to click", level="warn")
        return False

    # 4) Submit the challenge
    try:
        await iframe.evaluate("""() => {
            const btns = document.querySelectorAll('button, [role="button"], [type="submit"]');
            for (const b of btns) {
                const t = (b.textContent || '').toLowerCase();
                if (b.offsetParent !== null &&
                    (t.includes('verify') || t.includes('submit') ||
                     t.includes('continue') || t.includes('done'))) {
                    b.click();
                    return;
                }
            }
        }""")
    except Exception:
        pass
    await asyncio.sleep(2.5)

    # 5) Check for a solved state
    try:
        solved = await page.evaluate("""() => {
            const fc = document.querySelector('textarea[name="fc-token"]');
            if (fc && fc.value && fc.value.length > 10) return 'fc-token';
            const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
            if (ta && ta.value && ta.value.length > 10) return 'recaptcha';
            const ch = document.querySelector('[class*="challenge" i], [class*="Challenge"]');
            if (ch && getComputedStyle(ch).display === 'none') return 'hidden';
            return '';
        }""")
    except Exception:
        solved = ""

    if solved:
        log(f"[FunCAPTCHA] SOLVED ({solved})")
        return True
    log("[FunCAPTCHA] No token after click - challenge may still be up", level="warn")
    return False
