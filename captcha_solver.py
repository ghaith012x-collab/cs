"""
CAPTCHA SOLVER — NoCaptchaAI-first (nocaptchaai.com) with offline pixel fallbacks.
No Gemini. No NopeCHA. No local AI models.

Strategy Flow:
  hCaptcha (Discord): NoCaptchaAI HCaptchaTaskProxyless token API —
      sitekey + pageurl -> hCaptcha token (typically < 5s).
      API key comes from the API_KEY environment variable.
  hCaptcha drag puzzle: solved IN-BROWSER by dragging the slider (the token
      APIs hang forever on this challenge type).
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
import random
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
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def _post(self, endpoint: str, payload: dict,
                    timeout: float = 30.0) -> dict:
        url = f"{NOCAPTCHAAI_BASE}/{endpoint}"
        body = dict(payload)
        body.setdefault("clientKey", self._key)
        try:
            # Reuse one session: a fresh TLS handshake per poll adds real
            # latency to the getTaskResult loop.
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            async with self._session.post(
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
                             timeout: float = 85.0,
                             poll: float = 1.0) -> Optional[str]:
        """Solve hCaptcha. Returns the h-captcha-response token or None.

        Polls every second and logs a heartbeat every 15s so a slow solve
        never looks frozen. Most hCaptcha tokens solve in under 10s.
        """
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
        self._log(f"[NoCaptchaAI] Task created (id {task_id}) - polling for solution...")

        started = time.time()
        deadline = started + timeout
        last_heartbeat = started
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
                    elapsed = int(time.time() - started)
                    self._log(f"[NoCaptchaAI] [OK] hCaptcha token after {elapsed}s "
                              f"({len(token)} chars)")
                    return token
                self._log("[NoCaptchaAI] ready but empty solution", level="error")
                self.stats["failed"] += 1
                return None
            if status in ("failed", "error"):
                self._log(f"[NoCaptchaAI] task failed: {result}", level="error")
                self.stats["failed"] += 1
                return None
            if time.time() - last_heartbeat >= 15:
                last_heartbeat = time.time()
                self._log(f"[NoCaptchaAI] Still solving (task {task_id}, "
                          f"{int(time.time() - started)}s elapsed)...")
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

_SITEKEY_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)


def _is_valid_sitekey(value: str) -> bool:
    """hCaptcha sitekeys are UUIDs - reject partial or garbage extractions.

    A mid-load iframe src or an empty hash can produce values like 'a9b5fb0'
    which would hang the solving API forever. Only full UUIDs are accepted.
    """
    v = (value or "").strip()
    return bool(_SITEKEY_RE.match(v))


async def extract_hcaptcha_sitekey(page) -> str:
    """Pull the hCaptcha sitekey from every possible source.

    Discord sets a data-sitekey attribute immediately, so we check that first.
    Fall back to iframe src hash fragments, a full iframe scan, and the
    hcaptcha JS global object. Only well-formed UUID sitekeys are accepted -
    partial/garbage extractions are skipped so a bad sitekey never reaches
    the solving API.
    """
    # Strategy 1: [data-sitekey] on the parent page (Discord always has this)
    try:
        sk = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            return el ? el.getAttribute('data-sitekey') : '';
        }""")
        if _is_valid_sitekey(str(sk)):
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
        if m and _is_valid_sitekey(m.group(1)):
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
        if _is_valid_sitekey(sitekey):
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
        if _is_valid_sitekey(str(sk)):
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


# ── hCaptcha puzzle (drag) in-browser solver ───────────────
#
# Discord sometimes shows hCaptcha's PUZZLE challenge: a scene image with a
# cutout hole and a floating piece that you drag into place with a slider.
# External token APIs often hang forever on this challenge type, so we solve
# it directly in the browser: screenshot the puzzle, locate the piece/hole
# outlines with edge analysis, then drag the slider until the challenge
# passes. Never faked - success is verified against the real hCaptcha token.


def _puzzle_edge_profile(img: Image.Image) -> list[float]:
    """Column-wise edge strength for the puzzle band of a screenshot."""
    gray = img.convert('L')
    w, h = gray.size
    px = gray.load()
    best: list[float] = []
    for band_top, band_bot in ((int(h * 0.08), int(h * 0.85)),
                               (int(h * 0.05), int(h * 0.95))):
        prof = [0.0] * w
        for y in range(band_top, band_bot):
            prev = px[0, y]
            for x in range(1, w):
                cur = px[x, y]
                prof[x] += abs(cur - prev)
                prev = cur
        n = max(1, band_bot - band_top)
        prof = [p / n for p in prof]
        if not best or max(prof) > max(best):
            best = prof
    return best


def _find_outline_pairs(img: Image.Image,
                        max_pairs: int = 16) -> list[tuple]:
    """Locate all plausible rectangular outline pairs (left, right, strength)
    for the puzzle piece and its hole. Pure PIL edge analysis, O(w*h).

    Thresholds relax in steps so faint hole outlines are still found on
    noisy scene backgrounds. Returns the full candidate set (no dedupe) so
    the caller can cluster by width and pick the piece/hole pair.
    """
    prof = _puzzle_edge_profile(img)
    w = len(prof)
    if w < 40:
        return []
    mean = sum(prof) / len(prof)
    peaks = []
    for threshold in (mean * 2.0 + 1.5, mean * 1.3 + 0.8, mean * 1.05 + 0.4):
        found = []
        for x in range(1, w - 1):
            if prof[x] >= threshold and prof[x] >= prof[x - 1] and prof[x] >= prof[x + 1]:
                found.append((x, prof[x]))
        if len(found) >= 2:
            peaks = found
            break
    if not peaks:
        return []
    peaks.sort(key=lambda p: -p[1])
    kept = []
    for x, s in peaks:
        if all(abs(x - kx) > 3 for kx, _ in kept):
            kept.append((x, s))
        if len(kept) >= 12:
            break
    kept.sort()
    min_w = int(w * 0.22)
    max_w = int(w * 0.85)
    pairs = []
    for i in range(len(kept)):
        for j in range(i + 1, len(kept)):
            lx, ls = kept[i]
            rx, rs = kept[j]
            if min_w <= rx - lx <= max_w:
                pairs.append((lx, rx, ls + rs))
    pairs.sort(key=lambda p: -p[2])
    return pairs[:max_pairs]


def _puzzle_deltas(img: Image.Image) -> list[int]:
    """Candidate horizontal drag distances (px), most likely first.

    The piece and its hole have the SAME width (it is a cutout), so pairs are
    clustered by width and the largest cluster is taken - it contains both the
    piece and the hole. The strongest edge-separated pair in the cluster is
    assumed to be the piece (brighter border + drop shadow), giving the
    correct drag direction first; other assignments stay as fallbacks.
    """
    w = img.size[0]
    pairs = _find_outline_pairs(img)
    if len(pairs) < 2:
        return []

    # 1) Cluster pairs by similar width.
    clusters = []
    for p in pairs:
        pw = p[1] - p[0]
        for cl in clusters:
            if abs(cl['w'] - pw) <= int(w * 0.05):
                cl['pairs'].append(p)
                n = len(cl['pairs'])
                cl['w'] = (cl['w'] * (n - 1) + pw) / n
                break
        else:
            clusters.append({'w': float(pw), 'pairs': [p]})
    clusters.sort(key=lambda c: (-len(c['pairs']), -max(p[2] for p in c['pairs'])))
    members = sorted(clusters[0]['pairs'], key=lambda p: -p[2])

    # 2) From the cluster, keep the strongest pairs with separated edges.
    keep = []
    for p in members:
        if all(abs(p[0] - k[0]) > 15 and abs(p[1] - k[1]) > 15 for k in keep):
            keep.append(p)
        if len(keep) >= 3:
            break
    if len(keep) < 2:
        return []

    # 3) Deltas between the kept outlines (piece assumed = strongest).
    cands = []
    for i in range(len(keep)):
        for j in range(len(keep)):
            if i == j:
                continue
            d = keep[j][0] - keep[i][0]
            if d != 0 and abs(d) < w * 0.8:
                cands.append((-keep[i][2], abs(d), d))
    cands.sort()
    seen, out = set(), []
    for _s, _a, d in cands:
        if d not in seen:
            seen.add(d)
            out.append(d)
        if len(out) >= 5:
            break
    return out


async def _drag_handle(page, start_x: float, start_y: float, delta: int,
                       steps: int = 16) -> None:
    """Humanized mouse drag on the slider handle."""
    await page.mouse.move(start_x, start_y)
    await asyncio.sleep(random.uniform(0.06, 0.14))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.05, 0.10))
    for i in range(1, steps + 1):
        await page.mouse.move(
            start_x + delta * i / steps,
            start_y + random.uniform(-0.8, 0.8),
            steps=2,
        )
        await asyncio.sleep(random.uniform(0.004, 0.02))
    await asyncio.sleep(random.uniform(0.06, 0.14))
    await page.mouse.up()


async def _challenge_solved(page, iframe) -> bool:
    """True once hCaptcha accepted the solve (token present / UI hidden)."""
    try:
        if await read_hcaptcha_token(page):
            return True
    except Exception:
        pass
    try:
        frame = await iframe.content_frame()
        if frame:
            hidden = await frame.evaluate("""() => {
                const chal = document.querySelector('[class*="challenge"], [class*="Challenge"]');
                if (!chal) return false;
                const cs = getComputedStyle(chal);
                return cs.display === 'none' || cs.visibility === 'hidden';
            }""")
            if hidden:
                return True
    except Exception:
        pass
    return False


async def _probe_drag_dom(iframe) -> dict:
    """Find the slider handle (iframe-relative center) and puzzle area box."""
    try:
        frame = await iframe.content_frame()
        if not frame:
            return {}
        handle = await frame.evaluate("""() => {
            for (const el of document.querySelectorAll('*')) {
                if (el.children.length > 4) continue;
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.width > 600 || r.height < 12 || r.height > 140) continue;
                if (['grab', 'grabbing', 'move', 'ew-resize'].includes(cs.cursor)) {
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }
            }
            return null;
        }""")
        area = await frame.evaluate("""() => {
            const el = document.querySelector('canvas, img[src], [class*="puzzle"], [class*="task-image"]');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            if (r.width < 40 || r.height < 40) return null;
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        }""")
        return {"handle": handle, "area": area}
    except Exception:
        return {}


async def solve_hcaptcha_drag(page, iframe, log=None,
                              max_attempts: int = 6) -> bool:
    """Solve the hCaptcha puzzle drag challenge directly in the browser.

    Screenshots the puzzle, computes the drag offset from the piece/hole
    outlines, then drags the slider. Verified against the real token - no
    faking. Returns True only when hCaptcha actually accepted the solve.
    """
    log = log or (lambda msg, level="info": None)
    try:
        iframe_box = await iframe.bounding_box()
        if not iframe_box or iframe_box['width'] < 60 or iframe_box['height'] < 60:
            log("[Drag] Challenge iframe too small to solve", level="error")
            return False

        probe = await _probe_drag_dom(iframe)
        area = probe.get("area")
        if area and area.get("w", 0) >= 40:
            shot_box = {
                'x': iframe_box['x'] + area['x'],
                'y': iframe_box['y'] + area['y'],
                'width': area['w'],
                'height': area['h'],
            }
        else:
            shot_box = iframe_box

        handle = probe.get("handle")
        if handle:
            hx = iframe_box['x'] + handle['x']
            hy = iframe_box['y'] + handle['y']
        else:
            hx = iframe_box['x'] + iframe_box['width'] * 0.85
            hy = iframe_box['y'] + iframe_box['height'] * 0.85
            log("[Drag] Slider handle not found - dragging from slider area", level="warn")

        for attempt in range(1, max_attempts + 1):
            shot = await page.screenshot(clip=shot_box)
            img = Image.open(io.BytesIO(shot))
            deltas = _puzzle_deltas(img)
            if not deltas:
                log(f"[Drag] Attempt {attempt}: no piece/hole outlines found - retrying",
                    level="warn")
                await asyncio.sleep(1.2)
                continue
            delta = deltas[0]
            log(f"[Drag] Attempt {attempt}/{max_attempts}: offset {delta:+d}px "
                f"(candidates {deltas})")
            # Drag the estimate, then fine-tune around it: outline detection
            # can land on the inner vs outer border edge (+/- a few px).
            for adjust in (0, -4, 4):
                d = delta + adjust
                if d == 0:
                    continue
                await _drag_handle(page, hx, hy, d)
                for check in range(2):
                    await asyncio.sleep(1.0)
                    if await _challenge_solved(page, iframe):
                        log("[Drag] [OK] Puzzle solved - hCaptcha passed!")
                        return True
            log(f"[Drag] Attempt {attempt} did not pass - retrying", level="warn")
            probe = await _probe_drag_dom(iframe)
            handle = probe.get("handle")
            if handle:
                hx = iframe_box['x'] + handle['x']
                hy = iframe_box['y'] + handle['y']
            await asyncio.sleep(0.8)

        log("[Drag] [FAIL] Could not solve puzzle after retries", level="error")
        return False
    except Exception as e:
        log(f"[Drag] solver error: {e}", level="error")
        import traceback
        traceback.print_exc()
        return False
