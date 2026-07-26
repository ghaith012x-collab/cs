"""
Setup script: Download NopeCHA extension (free hCaptcha solver).
Uses the regular Chromium build which works better in headless mode.

NopeCHA gives 100 free solves per day — no API key or account needed.
"""

import os
import sys
import zipfile
from pathlib import Path

import requests

EXTENSIONS_DIR = Path("extensions")
# Use the regular Chromium build (with UI framework) - works better in headless
EXTENSION_URL = "https://github.com/NopeCHALLC/nopecha-extension/releases/latest/download/chromium.zip"


def download_and_extract():
    print("[Setup] Downloading NopeCHA extension (free hCaptcha solver)...")
    EXTENSIONS_DIR.mkdir(exist_ok=True)

    zip_path = EXTENSIONS_DIR / "nopecha.zip"
    extract_to = EXTENSIONS_DIR / "nopecha"

    # Clean previous
    if extract_to.exists():
        import shutil
        shutil.rmtree(extract_to)

    # Download
    try:
        r = requests.get(EXTENSION_URL, timeout=60, allow_redirects=True)
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            f.write(r.content)
        print(f"[Setup] Downloaded {len(r.content) / 1024:.1f} KB")
    except Exception as e:
        print(f"[Setup] Download failed: {e}")
        return False

    # Extract
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_to)
        print(f"[Setup] Extracted to {extract_to}")

        # Verify manifest
        manifest = extract_to / "manifest.json"
        if manifest.exists():
            print(f"[Setup] ✓ Extension ready at {extract_to}")
            # List contents
            files = [f.name for f in extract_to.iterdir()]
            print(f"[Setup] Files: {files}")
            return True
        print(f"[Setup] ✗ Manifest not found")
        return False
    except Exception as e:
        print(f"[Setup] Extract failed: {e}")
        return False
    finally:
        if zip_path.exists():
            zip_path.unlink()


if __name__ == "__main__":
    success = download_and_extract()
    sys.exit(0 if success else 1)
