#!/usr/bin/env python3
"""
Install the trained "brains" from the Kaggle training notebook.

Downloads these files from the notebook output into ./models/:
  model_grid.pth       - 33-class tile grid classifier (ResNet18)
  model_drag.pth       - drag position regressor (ResNet18, normalized x,y)
  motion_params.json   - human mouse-behavior stats

Credentials are read from the environment (injected by Freebuff API Keys):
  KAGGLE_USERNAME  (optional; defaults to "ghaith012x")
  KAGGLE_KEY       (required)

Usage:
  python fetch_brains.py              # fetch missing brains
  python fetch_brains.py --force      # re-download everything
  python fetch_brains.py --check      # just report what's installed

The notebook must have a *saved version with output* on Kaggle for the
download to succeed.  If it was never saved ("Save & Run All (Commit)" with
output enabled), the Kaggle API returns 404 and this script prints a clear
message telling you what to do — no brain files are created.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KAGGLE_OWNER = "ghaith012x"
# Override with KAGGLE_KERNEL_SLUG=<owner/slug> if you run the new
# hcaptcha_superbrain.ipynb as a different Kaggle notebook.
KAGGLE_KERNEL = os.environ.get("KAGGLE_KERNEL_SLUG", "notebookfcf0d1c9e3")
KERNEL_SLUG = f"{KAGGLE_OWNER}/{KAGGLE_KERNEL}"

# Expected brain files -> friendly label
BRAINS = {
    "model_grid.pth": "tile grid classifier",
    "model_drag.pth": "drag position regressor",
    "motion_params.json": "human mouse-behavior stats",
}

MODELS_DIR = Path(__file__).resolve().parent / "models"


def _credential() -> tuple[str, str] | None:
    """Return (username, key) from env, or None if the key is missing."""
    key = (os.environ.get("KAGGLE_KEY") or "").strip()
    if not key:
        return None
    username = (os.environ.get("KAGGLE_USERNAME") or KAGGLE_OWNER).strip()
    return username, key


def _auth_header(cred: tuple[str, str]) -> dict:
    token = base64.b64encode(f"{cred[0]}:{cred[1]}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def installed_status() -> dict:
    """Return {filename: bool} for each brain file."""
    return {name: (MODELS_DIR / name).exists() for name in BRAINS}


def _download_via_cli(dest: Path, cred: tuple[str, str]) -> list[str]:
    """Use the `kaggle` CLI to pull the notebook output. Returns file names."""
    env = dict(os.environ)
    env["KAGGLE_USERNAME"] = cred[0]
    env["KAGGLE_KEY"] = cred[1]
    cmd = ["kaggle", "kernels", "output", KERNEL_SLUG, "-p", str(dest)]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"kaggle CLI exited {proc.returncode}")
    return [p.name for p in dest.iterdir() if p.is_file()]


def _download_via_api(dest: Path, cred: tuple[str, str]) -> list[str]:
    """Direct HTTP fallback against the Kaggle REST API."""
    base = f"https://www.kaggle.com/api/v1/kernels/output/{KERNEL_SLUG}"
    req = urllib.request.Request(f"{base}/files", headers=_auth_header(cred))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            listing = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Kaggle API HTTP {e.code}") from e

    if not isinstance(listing, list):
        raise RuntimeError("Unexpected Kaggle API response (no output listing)")

    saved = []
    for entry in listing:
        name = entry.get("fileName") if isinstance(entry, dict) else str(entry)
        if not name:
            continue
        f_req = urllib.request.Request(
            f"{base}/files/{urllib.parse.quote(name)}", headers=_auth_header(cred)
        )
        with urllib.request.urlopen(f_req, timeout=300) as resp:
            (dest / name).write_bytes(resp.read())
        saved.append(name)
    return saved


def fetch_brains(force: bool = False, quiet: bool = False) -> dict:
    """Download missing brain files into models/. Returns {filename: ok}."""
    def log(msg: str):
        if not quiet:
            print(msg, flush=True)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    status = installed_status()

    if not force and all(status.values()):
        log("[brains] All brains already installed — nothing to do.")
        return status

    cred = _credential()
    if cred is None:
        log("[brains] KAGGLE_KEY not set. Add your Kaggle API key in "
            "Freebuff → API Keys (name: KAGGLE_KEY), then start the app again.")
        return status

    missing = [name for name, ok in status.items() if force or not ok]
    log(f"[brains] Fetching {len(missing)} brain(s) from Kaggle "
        f"({KERNEL_SLUG})...")

    # Download to a temp dir so partial failures don't leave broken files.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            try:
                names = _download_via_cli(tmp_path, cred)
            except FileNotFoundError:
                log("[brains] kaggle CLI not found — using direct API.")
                names = _download_via_api(tmp_path, cred)
            except RuntimeError:
                log("[brains] kaggle CLI failed — trying direct API.")
                names = _download_via_api(tmp_path, cred)
        except Exception as e:
            log(f"[brains] Download failed: {e}")
            log("[brains] Tip: open the notebook on Kaggle and save a version "
                "WITH output enabled ('Save & Run All (Commit)'). Until a "
                "saved version exists, there is no output to download.")
            return status

        found = {n for n in names if n in BRAINS}
        if not found:
            log(f"[brains] Downloaded files don't match expected brains: {names}")
            return status

        for name in sorted(found):
            (tmp_path / name).replace(MODELS_DIR / name)
            log(f"[brains] Installed {name} ({BRAINS[name]})")

    status = installed_status()
    done = sum(1 for ok in status.values() if ok)
    if done == len(BRAINS):
        log("[brains] ✅ All brains installed. The solver now knows what is what.")
    else:
        log(f"[brains] {done}/{len(BRAINS)} brains installed: {status}")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Re-download all brains even if present")
    parser.add_argument("--check", action="store_true",
                        help="Only report installed status")
    args = parser.parse_args()

    if args.check:
        status = installed_status()
        for name, ok in status.items():
            size = (MODELS_DIR / name).stat().st_size if ok else 0
            print(f"  {'✅' if ok else '❌'} {name}  "
                  f"({BRAINS[name]})" + (f"  [{size:,} bytes]" if ok else ""))
        return 0

    fetch_brains(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
