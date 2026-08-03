"""
CAPTCHA SOLVER — NopeCHA-first solver with Gemini Vision + pixel fallbacks.
No local AI models (no CLIP, no torch, no YOLO).

Strategy Flow (tried in order):
  NopeCHA Token API (primary): submit sitekey + pageurl -> hCaptcha token in
      ~15s. Spoofs human activity (no email/phone verification needed).
  NopeCHA Recognition API: FunCAPTCHA tile grids (task text + screenshot).
  Gemini Vision API: numbered contact sheet -> which tiles to click
      (used only when no NOPECHA_KEY is configured).
  Pixel-similarity fallback: works with no API key at all.
"""

import asyncio
import base64
import io
import json
import math
import os
import random
import re
import time
from typing import Callable, Optional
from math import sqrt

import aiohttp
from PIL import Image, ImageDraw


# ── Gemini Configuration ──────────────────────────────────

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Tried in order until one answers.
GEMINI_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash"]


def _api_key() -> str:
    """Gemini API key from the environment (API_KEY, fallback GEMINI_API_KEY)."""
    return (os.environ.get("API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()


def _parse_json_text(text: Optional[str]):
    """Extract the first JSON object from a model response. Returns None on failure."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ── Gemini Vision Client ──────────────────────────────────

class GeminiVision:
    """Minimal async client for Gemini generateContent (vision)."""

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self._key = _api_key()
        self.stats = {"calls": 0, "ok": 0, "failed": 0}

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def generate(self, prompt: str,
                       images: list[Image.Image] | None = None,
                       json_mode: bool = False,
                       timeout: float = 45.0) -> Optional[str]:
        """Send text + inline images to Gemini. Returns raw text or None."""
        if not self._key:
            self._log("[Gemini] No API_KEY set — skipping Gemini call", level="warn")
            return None

        parts = [{"text": prompt}]
        for img in (images or []):
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(buf.getvalue()).decode("utf-8"),
                }
            })

        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.1},
        }
        if json_mode:
            body["generationConfig"]["response_mime_type"] = "application/json"

        self.stats["calls"] += 1
        last_err = None
        async with aiohttp.ClientSession() as session:
            for model in GEMINI_MODELS:
                url = f"{GEMINI_BASE}/models/{model}:generateContent?key={self._key}"
                try:
                    async with session.post(
                        url, json=body,
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status != 200:
                            msg = data.get("error", {}).get("message", "")
                            last_err = f"{model} {resp.status}: {msg}"
                            # 400 with INVALID_ARGUMENT usually means model mismatch -> try next
                            continue
                        try:
                            text = data["candidates"][0]["content"]["parts"][0]["text"]
                            self.stats["ok"] += 1
                            return text
                        except Exception:
                            last_err = f"{model}: empty response"
                except Exception as e:
                    last_err = f"{model}: {e}"
        self.stats["failed"] += 1
        self._log(f"[Gemini] All models failed — {last_err}", level="error")
        return None


# ── NopeCHA Configuration ──────────────────────────────────

NOPECHA_BASE = "https://api.nopecha.com"
# Incomplete-job error code returned while a job is still being processed.
NOPECHA_INCOMPLETE = 14


def _nopecha_key() -> str:
    """NopeCHA API key from the environment (NOPECHA_KEY, fallback NOPECHA_API_KEY)."""
    return (os.environ.get("NOPECHA_KEY") or os.environ.get("NOPECHA_API_KEY") or "").strip()


class NopeCHA:
    """Async client for the NopeCHA API (token + recognition jobs).

    Free tier gives 100 credits/day (hCaptcha token = 20 credits, recognition = 1).
    Token jobs spoof human activity, so generated Discord accounts usually
    skip email/phone verification entirely.

    Auth: `Authorization: Basic <API_KEY>` (or a `key` field in body/query).
    """

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self._key = _nopecha_key()
        self.stats = {"calls": 0, "ok": 0, "failed": 0}

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Basic {self._key}",
        }

    async def _submit(self, endpoint: str, payload: dict,
                      timeout: float = 30.0) -> Optional[str]:
        """Submit a job. Returns the job id (string) or None on failure."""
        if not self._key:
            self._log("[NopeCHA] No NOPECHA_KEY set", level="warn")
            return None
        url = f"{NOPECHA_BASE}/v1/{endpoint}"
        payload = dict(payload)
        payload.setdefault("key", self._key)
        self.stats["calls"] += 1
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    data = await resp.json(content_type=None)
                    job_id = data.get("data")
                    if resp.status == 200 and isinstance(job_id, str) and job_id:
                        self.stats["ok"] += 1
                        return job_id
                    self._log(f"[NopeCHA] submit {endpoint} failed: {resp.status} {data}",
                              level="error")
                    self.stats["failed"] += 1
                    return None
        except Exception as e:
            self._log(f"[NopeCHA] submit {endpoint} error: {e}", level="error")
            self.stats["failed"] += 1
            return None

    async def _retrieve(self, endpoint: str, job_id: str,
                        timeout: float = 90.0, poll: float = 1.0) -> Optional[dict]:
        """Poll a job until it resolves. Returns the full response body or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            url = f"{NOPECHA_BASE}/v1/{endpoint}?id={job_id}&key={self._key}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, headers=self._headers(),
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status == 200 and data.get("data") not in (None, ""):
                            self.stats["ok"] += 1
                            return data
                        err = data.get("error", {})
                        code = err.get("code") if isinstance(err, dict) else None
                        if code is not None and code != NOPECHA_INCOMPLETE:
                            self._log(f"[NopeCHA] job {job_id} error code {code}: {err}",
                                      level="error")
                            self.stats["failed"] += 1
                            return None
            except Exception as e:
                self._log(f"[NopeCHA] retrieve error: {e}", level="warn")
            await asyncio.sleep(poll)
        self._log(f"[NopeCHA] job {job_id} timed out after {int(timeout)}s", level="warn")
        self.stats["failed"] += 1
        return None

    async def solve_hcaptcha_token(self, sitekey: str, pageurl: str,
                                   timeout: float = 90.0) -> Optional[str]:
        """Solve hCaptcha via the Token API. Returns the h-captcha-response token."""
        self._log(f"[NopeCHA] hCaptcha token job (sitekey {sitekey[:12]}...)")
        job_id = await self._submit("token/hcaptcha",
                                    {"sitekey": sitekey, "url": pageurl})
        if not job_id:
            return None
        data = await self._retrieve("token/hcaptcha", job_id, timeout=timeout)
        token = data.get("data") if data else None
        if isinstance(token, str) and len(token) > 20:
            self._log(f"[NopeCHA] ✓ hCaptcha token obtained ({len(token)} chars)")
            return token
        self._log("[NopeCHA] hCaptcha token solve failed", level="error")
        self.stats["failed"] += 1
        return None

    async def solve_funcaptcha_tiles(self, task: str, image_b64: str,
                                     timeout: float = 45.0) -> Optional[list[int]]:
        """Solve a FunCAPTCHA 3x2 tile challenge. Returns 0-based indices to click."""
        self._log(f"[NopeCHA] FunCAPTCHA recognition job: {task[:60]}")
        job_id = await self._submit("recognition/funcaptcha",
                                    {"task": task, "image_data": [image_b64]})
        if not job_id:
            return None
        data = await self._retrieve("recognition/funcaptcha", job_id, timeout=timeout)
        flags = data.get("data") if data else None
        if isinstance(flags, list) and flags:
            indices = [i for i, f in enumerate(flags) if f]
            self._log(f"[NopeCHA] ✓ FunCAPTCHA tiles: {indices}")
            return indices
        self._log("[NopeCHA] FunCAPTCHA solve failed", level="error")
        self.stats["failed"] += 1
        return None

    async def get_credit(self) -> Optional[dict]:
        """Fetch free-tier credit status (plan, credit remaining, ttl)."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{NOPECHA_BASE}/v1/status?key={self._key}",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    return await resp.json(content_type=None)
        except Exception:
            return None


# ── hCaptcha sitekey / params extraction (DOM, no extensions) ──

async def extract_hcaptcha_sitekey(page) -> str:
    """Pull the hCaptcha sitekey out of the captcha iframe src."""
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
    return ""


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


# ── Pixel Similarity (offline fallback, no ML) ───────────

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


# ── Challenge Text Parsing ───────────────────────────────

_GRID_CHALLENGE_PATTERNS = [
    r"select all images? (?:containing|with|that have|that show|that display|of)\s+(?:a\s+|an\s+)?(.+?)(?:\s+below|\.|$)",
    r"select all (?:the\s+)?(.+?)(?:\s+images|\s+pictures|\s+below|\.|$)",
    r"click on (?:the\s+)?(?:matching\s+)?(.+?)(?:\s+images|\.|$)",
    r"please select (?:the\s+)?(.+?)(?:\s+below|\.|$)",
    r"choose all (?:the\s+)?(.+?)(?:\s+below|\.|$)",
    r"identify (?:the\s+)?(.+?)(?:\s+in|\s+below|\.|$)",
    r"which (?:images?|pictures?|ones?) (?:are|contain|show|have)\s+(?:a\s+|an\s+)?(.+?)(?:\?|\.|$)",
]

_SKIP_TEXTS = [
    "create an account", "security check", "verify you're human",
    "complete the security check", "powered by", "hcaptcha",
    "checkbox", "challenge", "access denied", "security",
    "please try again", "your browser", "enabled", "cookies",
]


def extract_target_objects(challenge_text: str) -> list[str]:
    """Extract target object(s) from the hCaptcha challenge text."""
    if not challenge_text:
        return []
    text = challenge_text.lower().strip()
    for skip in _SKIP_TEXTS:
        if skip in text:
            return []
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
    return []


# ── Tile Extraction ───────────────────────────────────────

def split_grid_screenshot(screenshot_bytes: bytes, grid_size: int = 3) -> list[Image.Image]:
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


def _build_numbered_grid(tiles: list[Image.Image], cols: int = 3) -> Image.Image:
    """Compose tiles into one numbered contact sheet so Gemini can answer with indices."""
    size = 160
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * size, rows * size), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        x, y = c * size, r * size
        t = tile.convert("RGB").resize((size - 24, size - 24))
        sheet.paste(t, (x + 12, y + 12))
        draw.rectangle([x + 4, y + 4, x + 30, y + 26], fill=(220, 38, 38))
        draw.text((x + 9, y + 6), str(i + 1), fill=(255, 255, 255))
    return sheet


# ── Drag/Jigsaw Fallback (offline, no ML) ────────────────

def find_drag_piece_position(screenshot_bytes: bytes) -> Optional[int]:
    """Pixel-based estimate of the horizontal drag offset (fallback only)."""
    try:
        img = Image.open(io.BytesIO(screenshot_bytes))
        w, h = img.size
        if w < 100 or h < 100:
            return None
        gray = img.convert('L')
        pixels = gray.load()
        track_top = int(h * 0.60)
        track_bot = int(h * 0.95)
        img_top = int(h * 0.05)
        img_bot = int(h * 0.55)

        piece_scores = []
        for x in range(w):
            variance = 0
            for y in range(track_top + 2, track_bot):
                variance += abs(pixels[x, y] - pixels[x, y - 1])
            piece_scores.append(variance)
        max_score = max(piece_scores) if piece_scores else 0
        if max_score < 10:
            return None
        piece_scores = [s / max_score for s in piece_scores]

        piece_left = piece_right = None
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
            for x in range(1, w // 2):
                if abs(piece_scores[x] - piece_scores[x - 1]) > 0.2:
                    if not piece_left:
                        piece_left = x
                    else:
                        piece_right = x
                        break
        piece_center = (piece_left + piece_right) // 2 if (piece_left and piece_right) else int(w * 0.2)

        gap_scores = []
        for x in range(w):
            variance = 0
            for y in range(img_top + 2, img_bot):
                variance += abs(pixels[x, y] - pixels[x, y - 1])
            gap_scores.append(variance)
        min_gap_score = min(gap_scores[w // 2:]) if gap_scores[w // 2:] else 1
        threshold = min_gap_score * 1.5
        gap_left = gap_right = None
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
        gap_center = (gap_left + gap_right) // 2 if (gap_left and gap_right) else int(w * 0.65)

        offset = gap_center - piece_center
        if offset < 10 or offset > w * 0.8:
            return None
        return offset
    except Exception as e:
        print(f"[DragSolver] Error: {e}", flush=True)
        return None


# ── Main Vision Solver ────────────────────────────────────

class VisionSolver:
    """Gemini-powered captcha solver with offline pixel fallbacks.

    Strategy 0: Click checkbox, wait for auto-pass
    Strategy 1: Gemini Vision — numbered contact sheet -> matching tiles
    Strategy 2: Pixel similarity (offline)
    Strategy 3: Brute force — click all tiles
    """

    def __init__(self, log: Optional[Callable] = None):
        self._log = log or (lambda msg, level="info": None)
        self._gemini = GeminiVision(log=self._log)
        self._stats = {
            "total_challenges": 0,
            "solved": 0,
            "failed": 0,
            "strategy_used": "",
            "api_key_set": self._gemini.configured,
        }

    async def ensure_model_loaded(self) -> bool:
        """No local model — just confirm an API key is available (or fallback mode)."""
        if self._gemini.configured:
            self._log("[Gemini] API key detected — Gemini Vision ready")
            return True
        self._log("[Gemini] No API_KEY found — using offline pixel fallback", level="warn")
        return False

    # ── Gemini helpers ─────────────────────────────────

    async def _ask_which_tiles(self, instruction: str,
                               tiles: list[Image.Image]) -> Optional[list[int]]:
        """Ask Gemini which numbered tiles match the instruction."""
        if not self._gemini.configured or not tiles:
            return None
        sheet = _build_numbered_grid(tiles)
        prompt = (
            "You are solving an image captcha. The instruction is: "
            f"'{instruction}'. Below is a grid of tile images numbered "
            f"1..{len(tiles)} left-to-right, top-to-bottom. "
            "Return ONLY a JSON object like {\"tiles\":[1,4,7]} with the "
            "numbers of every tile that matches the instruction. "
            "If no tile matches, return {\"tiles\":[]}."
        )
        text = await self._gemini.generate(prompt, [sheet], json_mode=True)
        data = _parse_json_text(text)
        if not data or not isinstance(data.get("tiles"), list):
            return None
        indices = []
        for n in data["tiles"]:
            try:
                idx = int(n) - 1
                if 0 <= idx < len(tiles):
                    indices.append(idx)
            except Exception:
                continue
        return indices

    async def _ask_drag_offset(self, screenshot_bytes: bytes) -> Optional[int]:
        """Ask Gemini how many pixels to drag the puzzle piece right."""
        if not self._gemini.configured:
            return None
        img = Image.open(io.BytesIO(screenshot_bytes))
        prompt = (
            "This is a drag/puzzle slider captcha. The puzzle piece is on the "
            "left track and the target gap is in the image above it. "
            "Return ONLY a JSON object like {\"offset_px\":150} with the "
            "horizontal distance in pixels to drag the piece to the right so "
            "it aligns with the gap. Estimate carefully from the image."
        )
        text = await self._gemini.generate(prompt, [img], json_mode=True)
        data = _parse_json_text(text)
        if not data:
            return None
        try:
            return max(0, int(data.get("offset_px", 0)))
        except Exception:
            return None

    # ── Main solve flow ────────────────────────────────

    async def solve_captcha(self, page, iframe=None) -> Optional[str]:
        """Full hCaptcha solving flow. Returns token string or None. Never crashes."""
        self._stats["total_challenges"] += 1
        self._log("[Vision] Starting captcha solve...")

        try:
            if not iframe:
                iframe = await self._find_captcha_iframe(page)
                if not iframe:
                    self._log("[Vision] No hCaptcha iframe found", level="warn")
                    return None

            token = await self._try_extract_token(page)
            if token:
                self._log("[Vision] ✓ Token already present — no solving needed")
                self._stats["solved"] += 1
                return token

            await self._click_checkbox(page, iframe)

            await asyncio.sleep(2)
            token = await self._try_extract_token(page)
            if token:
                self._log("[Vision] ✓ Auto-pass! Token obtained without grid")
                self._stats["solved"] += 1
                self._stats["strategy_used"] = "auto_pass"
                return token

            await asyncio.sleep(2)
            token = await self._try_extract_token(page)
            if token:
                self._log("[Vision] ✓ Token appeared after challenge load")
                self._stats["solved"] += 1
                self._stats["strategy_used"] = "delayed_auto_pass"
                return token

            challenge_text = await self._get_challenge_text(page, iframe)
            targets = extract_target_objects(challenge_text) if challenge_text else []
            instruction = targets[0] if targets else (challenge_text or "").strip()
            if targets:
                self._log(f"[Vision] Grid challenge → target: '{instruction}'")
            else:
                self._log("[Vision] No grid instruction parsed — trying vision anyway...")

            tiles = await self._get_tiles(page, iframe)
            if not tiles or len(tiles) < 2:
                self._log("[Vision] Could not extract tiles from captcha", level="warn")
                token = await self._try_extract_token(page)
                return token or None

            self._log(f"[Vision] Extracted {len(tiles)} tiles from grid")

            # Strategy 1: Gemini Vision
            matching_indices = await self._ask_which_tiles(instruction, tiles)
            if matching_indices is not None:
                self._log(f"[Vision] Gemini selected {len(matching_indices)} tiles: {[i+1 for i in matching_indices]}")
                self._stats["strategy_used"] = "gemini_vision"
            else:
                # Strategy 2: pixel similarity
                matching_indices = find_matching_tiles_by_similarity(tiles)
                if matching_indices:
                    self._log(f"[Vision] Pixel fallback selected {len(matching_indices)} tiles")
                    self._stats["strategy_used"] = "pixel_similarity"
                else:
                    self._log("[Vision] No matches found — clicking all tiles")
                    matching_indices = list(range(len(tiles)))
                    self._stats["strategy_used"] = "all_tiles"

            clicked = await self._click_tiles(page, iframe, matching_indices)
            self._log(f"[Vision] Clicked {clicked} tiles")

            await self._click_submit(page, iframe)

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

    # ── iframe / checkbox / text helpers ────────────────

    async def _find_captcha_iframe(self, page):
        try:
            for attempt in range(20):
                iframe_el = await page.query_selector(
                    'iframe[src*="hcaptcha.com"], iframe[title*="hCaptcha"]'
                )
                if iframe_el:
                    self._log(f"[Vision] Iframe found (attempt {attempt+1})")
                    return iframe_el
                await asyncio.sleep(0.5)
        except:
            pass
        return None

    async def _click_checkbox(self, page, iframe):
        self._log("[Vision] Clicking checkbox...")
        try:
            result = await iframe.evaluate("""() => {
                const cb = document.querySelector('#checkbox, [role="checkbox"], .checkbox');
                if (cb) { cb.click(); return true; }
                return false;
            }""")
            if result:
                self._log("[Vision] Checkbox clicked via iframe JS")
                return
        except:
            pass
        try:
            await iframe.click()
            self._log("[Vision] Checkbox clicked via iframe.click()")
        except:
            self._log("[Vision] Could not click checkbox", level="warn")

    async def _get_challenge_text(self, page, iframe):
        try:
            text = await iframe.evaluate("""() => {
                const els = document.querySelectorAll(
                    '.challenge-text, .task-text, .prompt-text, .header-text, ' +
                    '[class*="prompt"], [class*="task"], h1, h2, .title, strong, [class*="challenge"]'
                );
                for (const el of els) {
                    if (el.offsetParent !== null) {
                        const t = el.textContent.trim();
                        if (t.length > 5 && t.length < 200) return t;
                    }
                }
                const body = document.body ? document.body.innerText.trim() : '';
                return body.length < 300 ? body : '';
            }""")
            if text:
                return text.strip()
        except:
            pass
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
        tiles = []
        try:
            tile_data = await iframe.evaluate("""() => {
                const selectors = '.task-image, [class*="image"], [role="button"] > div, ' +
                                  '.grid-item, .cell, td, img[class*="task"], ' +
                                  '.image-grid > div, [class*="tile"]';
                const els = document.querySelectorAll(selectors);
                const result = [];
                els.forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width > 30 && r.height > 30 && r.width < 500 && r.height < 500) {
                        result.push({x: r.x, y: r.y, w: r.width, h: r.height});
                    }
                });
                return JSON.stringify(result);
            }""")
            if tile_data:
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

        try:
            grid_bytes = await iframe.screenshot()
            img = Image.open(io.BytesIO(grid_bytes))
            w, h = img.size
            if w > 50 and h > 50:
                aspect = w / h
                grid_size = 4 if aspect > 1.5 else 3
                tiles = split_grid_screenshot(grid_bytes, grid_size)
                if tiles:
                    self._log(f"[Vision] Extracted {len(tiles)} tiles via grid split ({grid_size}x{grid_size})")
                    return tiles
        except Exception as e:
            self._log(f"[Vision] Grid split failed: {e}", level="warn")

        return []

    async def _click_tiles(self, page, iframe, indices):
        clicked = 0
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

        try:
            box = await iframe.bounding_box()
            if box:
                grid_size = 3
                tile_w = box['width'] / grid_size
                tile_h = box['height'] / grid_size
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
        try:
            await iframe.evaluate("""() => {
                const btn = document.querySelector(
                    'button[type="submit"], .submit-btn, #submit, ' +
                    '[class*="submit"], [class*="verify"], ' +
                    'button:not([class*="checkbox"])'
                );
                if (btn) { btn.click(); return; }
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.offsetParent !== null) { b.click(); return; }
                }
            }""")
            self._log("[Vision] Submit clicked")
        except:
            self._log("[Vision] Could not click submit", level="warn")

    async def _try_extract_token(self, page) -> Optional[str]:
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
        except:
            pass
        return None

    async def set_token_on_page(self, page, token: str) -> bool:
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

    # ── Drag solver (Gemini first, pixel fallback) ─────

    async def solve_drag_captcha(self, page, iframe=None) -> bool:
        """Solve a Funcaptcha drag/jigsaw puzzle. Returns True if solved."""
        self._stats["total_challenges"] += 1
        self._log("[DragSolver] Starting drag captcha solve...")

        try:
            if not iframe:
                for attempt in range(15):
                    for sel in [
                        'iframe[src*="funcaptcha"]', 'iframe[src*="arkose"]',
                        'iframe[title*="captcha"]', 'iframe[src*="captcha"]',
                        '[id*="funcaptcha"]', '[class*="funcaptcha"]',
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
                self._log("[DragSolver] No captcha iframe found", level="warn")
                return False

            await asyncio.sleep(1)
            try:
                box = await iframe.bounding_box()
            except:
                box = None
            if not box:
                self._log("[DragSolver] Could not get iframe bounding box", level="warn")
                return False

            self._log(f"[DragSolver] Captcha area: {box['w']:.0f}x{box['h']:.0f}")

            try:
                clip = {'x': box['x'], 'y': box['y'], 'width': box['w'], 'height': box['h']}
                captcha_bytes = await page.screenshot(clip=clip)
            except:
                captcha_bytes = await iframe.screenshot()

            if not captcha_bytes or len(captcha_bytes) < 1000:
                self._log("[DragSolver] Screenshot too small", level="warn")
                return False

            # Gemini first, pixel fallback second
            offset = await self._ask_drag_offset(captcha_bytes)
            if offset is not None:
                self._log(f"[DragSolver] Gemini suggested offset: {offset}px")
            else:
                offset = find_drag_piece_position(captcha_bytes)
                if offset is not None:
                    self._log(f"[DragSolver] Pixel fallback offset: {offset}px")

            if offset is None or offset < 10 or offset > box['w'] * 0.9:
                for piece_pct in [0.2, 0.15, 0.25, 0.3]:
                    for gap_pct in [0.65, 0.6, 0.7, 0.55, 0.75]:
                        test_offset = int(box['w'] * (gap_pct - piece_pct))
                        if 30 < test_offset < box['w'] * 0.7:
                            offset = test_offset
                            self._log(f"[DragSolver] Using estimated offset: {offset}px")
                            break
                    if offset:
                        break

            if offset is None or offset < 10 or offset > box['w'] * 0.9:
                self._log(f"[DragSolver] Invalid offset: {offset}", level="warn")
                return False

            self._log(f"[DragSolver] Calculated drag offset: {offset}px")

            handle_x = box['x'] + box['w'] * 0.15
            handle_y = box['y'] + box['h'] * 0.85
            end_x = handle_x + offset
            end_y = handle_y

            self._log(f"[DragSolver] Dragging ({handle_x:.0f},{handle_y:.0f}) → ({end_x:.0f},{end_y:.0f})")

            try:
                await page.mouse.move(handle_x, handle_y)
                await asyncio.sleep(0.15)
                await page.mouse.down()
                await asyncio.sleep(0.08)
                steps = 25
                for i in range(1, steps + 1):
                    progress = i / steps
                    eased = progress * progress * (3 - 2 * progress)
                    x = handle_x + offset * eased
                    y = handle_y + random.uniform(-1, 1)
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

            await asyncio.sleep(2)

            solved = await page.evaluate("""() => {
                const textarea = document.querySelector('textarea[name="fc-token"]');
                if (textarea && textarea.value && textarea.value.length > 10) return 'fc_token';
                const ta2 = document.querySelector('textarea[name="g-recaptcha-response"]');
                if (ta2 && ta2.value && ta2.value.length > 10) return 'recaptcha';
                const challenge = document.querySelector('[class*="challenge"], [class*="Challenge"]');
                if (challenge && challenge.style.display === 'none') return 'hidden';
                return '';
            }""")

            if solved:
                self._log(f"[DragSolver] ✓ SOLVED! ({solved})")
                self._stats["solved"] += 1
                self._stats["strategy_used"] = "drag"
                return True

            self._log("[DragSolver] ✗ Not solved — retrying with adjusted position...")
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
        stats = dict(self._stats)
        stats["gemini"] = dict(self._gemini.stats)
        return stats


# ── Backward Compatibility ────────────────────────────────
SolverAPI = VisionSolver
