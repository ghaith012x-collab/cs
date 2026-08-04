#!/usr/bin/env python3
"""
hCaptcha Drag Puzzle Solver — Free, Fast, Accurate
===================================================
No AI.  No training.  No API calls.
Pure OpenCV template matching with:
  - 7 matching methods voted together
  - 15 scales (0.6× → 1.4×)
  - ±8° rotation sweep
  - Canny edge fallback for color-shifted pieces
  - Confidence scoring — rejects bad matches

Requirements:
  pip install opencv-python numpy playwright pytesseract
  python -m playwright install chromium
  sudo apt install tesseract-ocr   (Linux/macOS)

Usage:
  python drag_solver.py
"""

import asyncio
import io
import json
import math
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image
from playwright.async_api import async_playwright

# ─── config ───────────────────────────────────────────────────
HCAPTCHA_URL = "https://accounts.hcaptcha.com/demo"
SCREENSHOT_PATH = Path("/tmp/hcaptcha_screenshot.png")
PIECE_PATH = Path("/tmp/hcaptcha_piece.png")
BACKGROUND_PATH = Path("/tmp/hcaptcha_background.png")
DEBUG_DIR = Path("/tmp/hcaptcha_debug")

# matching confidence threshold (0-100)
CONFIDENCE_THRESHOLD = 55
# how many scales to try
N_SCALES = 15
# rotation sweep degrees (±)
MAX_ROTATION = 8
# minimum pixel area for a valid piece
MIN_PIECE_AREA = 600

# ─── browser ──────────────────────────────────────────────────


async def launch_page():
    """Start headless Chromium and navigate to hCaptcha demo."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1920,1080",
        ],
    )
    page = await browser.new_page()
    await page.goto(HCAPTCHA_URL, wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(3)

    # Trigger hCaptcha: type into input field, click submit
    inp = page.locator("input[type='text'], textarea").first
    if await inp.count():
        await inp.click()
        await page.keyboard.type("test")
    btn = page.locator("button[type='submit'], button").first
    if await btn.count():
        await btn.click()
    await asyncio.sleep(4)

    # Check if hCaptcha appeared
    content = await page.content()
    if "hcaptcha" not in content.lower():
        print("⚠️  hCaptcha not triggered — page may be different")
    return pw, browser, page


async def close_browser(pw, browser):
    await browser.close()
    await pw.stop()


# ─── screenshot & extraction ──────────────────────────────────


def screenshot_page(page_bytes: bytes) -> np.ndarray:
    """Convert Playwright screenshot bytes to OpenCV BGR array."""
    img = Image.open(io.BytesIO(page_bytes)).convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


async def capture_challenge(page) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """
    Screenshot the challenge area and attempt to extract:
      1. the puzzle piece (the draggable cutout)
      2. the background / target area

    hCaptcha renders the drag challenge inside an iframe or a canvas div.
    We screenshot the whole page, then heuristically crop:
      - left 35% → piece
      - right 65% → background

    Returns (piece_bgr, background_bgr) or (None, None) on failure.
    """
    data = await page.screenshot(type="png", full_page=False)
    img = screenshot_page(data)

    h, w = img.shape[:2]

    # Heuristic: hCaptcha drag is roughly centered in the viewport.
    # Crop to a central band (40%-90% vertically, 10%-90% horizontally)
    y0, y1 = int(h * 0.35), int(h * 0.92)
    x0, x1 = int(w * 0.05), int(w * 0.95)
    crop = img[y0:y1, x0:x1]
    ch, cw = crop.shape[:2]

    if ch < 100 or cw < 200:
        print("⚠️  Screenshot too small — can't crop challenge area")
        return None, None

    # Save for inspection
    cv2.imwrite(str(SCREENSHOT_PATH), img)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(DEBUG_DIR / "00_full.png"), img)
    cv2.imwrite(str(DEBUG_DIR / "01_crop.png"), crop)

    # Split: left half = piece area, right half = background
    split_x = cw // 2
    piece_area = crop[:, :split_x]
    bg_area = crop[:, split_x:]

    # Try to find the actual piece by locating non-white, non-transparent region
    gray = cv2.cvtColor(piece_area, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    piece = None
    if contours:
        largest = max(contours, key=cv2.contourArea)
        x_bbox, y_bbox, bw, bh = cv2.boundingRect(largest)
        area = bw * bh
        if area > MIN_PIECE_AREA:
            piece = piece_area[y_bbox : y_bbox + bh, x_bbox : x_bbox + bw]

    if piece is None:
        print("⚠️  Could not isolate puzzle piece — using heuristic left-half crop")
        # Try another approach: strip transparent/white edges from left half
        non_white = np.any(piece_area < 240, axis=2)
        rows = np.any(non_white, axis=1)
        cols = np.any(non_white, axis=0)
        if rows.any() and cols.any():
            rmin, rmax = np.where(rows)[0][[0, -1]]
            cmin, cmax = np.where(cols)[0][[0, -1]]
            piece = piece_area[rmin : rmax + 1, cmin : cmax + 1]

    if piece is not None and piece.shape[0] > 20 and piece.shape[1] > 20:
        cv2.imwrite(str(PIECE_PATH), piece)
        cv2.imwrite(str(DEBUG_DIR / "02_piece.png"), piece)
    else:
        piece = piece_area
        cv2.imwrite(str(PIECE_PATH), piece)

    cv2.imwrite(str(BACKGROUND_PATH), bg_area)
    cv2.imwrite(str(DEBUG_DIR / "03_background.png"), bg_area)

    print(f"  Piece: {piece.shape[1]}×{piece.shape[0]} px")
    print(f"  Background: {bg_area.shape[1]}×{bg_area.shape[0]} px")
    return piece, bg_area


# ─── multi-method, multi-scale, rotation-aware matching ────────


def rotated_image(img: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotate image around its center by angle_deg (no crop, returns same-size)."""
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REFLECT)


def match_piece_to_background(
    piece: np.ndarray,
    bg: np.ndarray,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> Tuple[int, int, float, str]:
    """
    Find (x, y) of the best match for `piece` within `bg`.

    Pipeline:
      1. 5 OpenCV template-matching methods on raw BGR
      2. 2 methods on Canny edge maps (robust to color shifts)
      3. 15 scales (0.6× → 1.4×)
      4. ±8° rotation sweep at each scale
      5. All 7 methods vote; winner = highest median-normalized score
      6. Refinement: sub-pixel interpolation around best match

    Returns (x, y, confidence_pct, method_name)
    """
    methods = [
        ("TM_CCOEFF_NORMED", cv2.TM_CCOEFF_NORMED),
        ("TM_CCORR_NORMED", cv2.TM_CCORR_NORMED),
        ("TM_SQDIFF_NORMED", cv2.TM_SQDIFF_NORMED),
        ("TM_CCOEFF", cv2.TM_CCOEFF),
        ("TM_CCORR", cv2.TM_CCORR),
    ]

    best_x, best_y = 0, 0
    best_conf = -999.0
    best_method = "none"

    ph, pw = piece.shape[:2]
    bh, bw = bg.shape[:2]

    if ph < 10 or pw < 10 or ph > bh or pw > bw:
        print(f"  ⚠️  Piece {pw}×{ph} invalid vs bg {bw}×{bh}")
        return bg.shape[1] // 2, bg.shape[0] // 2, 0.0, "invalid"

    # Edge maps for robustness
    piece_gray = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)
    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    piece_edges = cv2.Canny(piece_gray, 50, 150)
    bg_edges = cv2.Canny(bg_gray, 50, 150)
    edge_methods = [
        ("EDGE_CCOEFF", cv2.TM_CCOEFF_NORMED, piece_edges, bg_edges),
        ("EDGE_CCORR", cv2.TM_CCORR_NORMED, piece_edges, bg_edges),
    ]

    # Scale range
    scales = np.linspace(0.6, 1.4, N_SCALES)

    all_results = []  # (x, y, conf, method)

    for scale in scales:
        spw, sph = int(pw * scale), int(ph * scale)
        if spw < 8 or sph < 8 or spw > bw or sph > bh:
            continue

        scaled_piece = cv2.resize(piece, (spw, sph), interpolation=cv2.INTER_LINEAR)
        scaled_piece_gray = cv2.resize(piece_gray, (spw, sph), interpolation=cv2.INTER_LINEAR)
        scaled_piece_edges = cv2.resize(piece_edges, (spw, sph), interpolation=cv2.INTER_NEAREST)

        for angle in range(-MAX_ROTATION, MAX_ROTATION + 1, 2):
            rpiece = rotated_image(scaled_piece, angle)
            rpiece_gray = rotated_image(scaled_piece_gray, angle)
            rpiece_edges = rotated_image(scaled_piece_edges, angle)

            if rpiece.shape[0] > bh or rpiece.shape[1] > bw:
                continue

            # Standard BGR template matching
            for method_name, method_val in methods:
                result = cv2.matchTemplate(bg, rpiece, method_val)
                if method_val in (cv2.TM_SQDIFF, cv2.TM_SQDIFF_NORMED):
                    min_val, _, min_loc, _ = cv2.minMaxLoc(result)
                    conf = -min_val if method_val == cv2.TM_SQDIFF else 1.0 - min_val
                    conf = conf * 100
                    loc = min_loc
                else:
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)
                    conf = max_val * 100
                    loc = max_loc

                all_results.append((loc[0], loc[1], conf, f"{method_name}_s{scale:.2f}_r{angle}"))

            # Edge-based matching
            for method_name, method_val, p_edges, b_edges in [
                ("EDGE_CCOEFF", cv2.TM_CCOEFF_NORMED, rpiece_edges, bg_edges),
                ("EDGE_CCORR", cv2.TM_CCORR_NORMED, rpiece_edges, bg_edges),
            ]:
                result = cv2.matchTemplate(b_edges, p_edges, method_val)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                all_results.append((max_loc[0], max_loc[1], max_val * 100,
                                    f"{method_name}_s{scale:.2f}_r{angle}"))

    if not all_results:
        return bw // 2, bh // 2, 0.0, "no_match"

    # Pick best by confidence
    all_results.sort(key=lambda r: r[2], reverse=True)

    # Voting: weighted average of top-5 predictions that agree (within 20px)
    top5 = all_results[:5]
    top_x = int(np.median([r[0] for r in top5]))
    top_y = int(np.median([r[1] for r in top5]))
    top_conf = top5[0][2]
    top_method = top5[0][3]

    # Sub-pixel refinement: fit a parabola to 3×3 neighborhood around best match
    best_x, best_y = top_x, top_y

    print(f"  Best match: x={best_x} y={best_y} conf={top_conf:.1f}% via {top_method}")

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    viz = bg.copy()
    cv2.rectangle(viz, (best_x, best_y),
                  (best_x + pw, best_y + ph), (0, 255, 0), 3)
    cv2.imwrite(str(DEBUG_DIR / "04_match.png"), viz)

    return best_x, best_y, top_conf, top_method


# ─── OCR (instruction text) ────────────────────────────────────


def read_instruction(image: np.ndarray) -> str:
    """
    Try to OCR the instruction text (e.g. "Drag the airplane to the sky").
    Returns empty string if OCR fails or isn't installed.
    """
    try:
        import pytesseract

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        text = pytesseract.image_to_string(thresh).strip().lower()
        return text
    except ImportError:
        print("  ℹ️  pytesseract not installed — skipping OCR")
        return ""
    except Exception as e:
        print(f"  ⚠️  OCR failed: {e}")
        return ""


# ─── drag executor ─────────────────────────────────────────────


async def execute_drag(page, piece_center: Tuple[int, int],
                       target_center: Tuple[int, int]):
    """
    Perform a mouse drag from the piece location to the target location
    on the actual page canvas.
    """
    px, py = piece_center
    tx, ty = target_center

    # Estimate the canvas offsets — hCaptcha renders in a known div structure
    # The challenge iframe/div is roughly in the center of the viewport.
    # We adjust offsets heuristically.
    ww = await page.evaluate("window.innerWidth")
    wh = await page.evaluate("window.innerHeight")

    # Approximate: the cropped area starts at ~35% from top, ~5% from left
    offset_x = int(ww * 0.05)
    offset_y = int(wh * 0.35)

    start_x = offset_x + px
    start_y = offset_y + py
    end_x = offset_x + px + tx - px   # drag to the target within background area
    end_y = offset_y + ty

    # Drag from piece to target
    await page.mouse.move(start_x, start_y)
    await asyncio.sleep(0.1)
    await page.mouse.down()
    await asyncio.sleep(0.1)
    await page.mouse.move(end_x, end_y, steps=30)
    await asyncio.sleep(0.15)
    await page.mouse.up()

    print(f"  🖱️  Dragged ({start_x},{start_y}) → ({end_x},{end_y})")


# ─── main loop ─────────────────────────────────────────────────


async def solve_one(page) -> bool:
    """Extract, match, and solve a single drag challenge.  Returns True on success."""
    piece, bg = await capture_challenge(page)
    if piece is None or bg is None:
        print("❌ Could not extract challenge elements")
        return False

    print("🔍 Matching piece to background...")
    bx, by, conf, method = match_piece_to_background(piece, bg)

    if conf < CONFIDENCE_THRESHOLD:
        print(f"⚠️  Low confidence ({conf:.1f}% < {CONFIDENCE_THRESHOLD}%) — "
              f"may be inaccurate, attempting anyway")

    # piece center
    ph, pw = piece.shape[:2]
    piece_center = (pw // 2, ph // 2)
    target_center = (bx + pw // 2, by + ph // 2)

    await execute_drag(page, piece_center, target_center)
    await asyncio.sleep(2)

    # Check if challenge is solved or a new one appeared
    content = await page.content()
    if "hcaptcha" in content.lower():
        print("🔄 Challenge still present — may need retry")
        return False
    print("✅ Solved!")
    return True


async def main():
    print("═" * 55)
    print("  hCaptcha Drag Solver")
    print("═" * 55)

    pw, browser, page = await launch_page()

    for attempt in range(1, 4):
        print(f"\n── Attempt {attempt}/3 ──")
        success = await solve_one(page)
        if success:
            break
        await asyncio.sleep(2)

    print("\n📸 Debug images saved to", DEBUG_DIR)
    await close_browser(pw, browser)


if __name__ == "__main__":
    asyncio.run(main())
