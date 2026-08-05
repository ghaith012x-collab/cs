# ═══════════════════════════════════════════════════════════════
# hCaptcha SuperBrains — Kaggle Trainer (v3, bulletproof)
# ==============================================================
# Trains the two "brains" the solver needs, from REAL data when
# available and SYNTHETIC data as a guaranteed fallback:
#
#   model_grid.pth      33-class tile grid classifier (ResNet18)
#   model_drag.pth      drag position regressor (ResNet18, fc=2)
#   motion_params.json  human mouse-behavior stats
#
# Format is exactly what solver.py loads:
#   model_grid.pth -> {"state_dict": <fp16>, "classes": [...]}
#   model_drag.pth -> {"state_dict": <fp16>}
#
# Weights are saved as float16 so each file is ~22.7 MB — under the
# 25 MB limit — and load_state_dict() casts them back up on load.
#
# HOW TO RUN ON KAGGLE (READ THIS — IT MATTERS):
#   1. New Notebook -> Settings: Accelerator = GPU T4 x2, Internet = ON
#   2. Add Input -> search "aneeshtickoo/hcaptcha-dataset" -> Add
#      (if Internet is OFF, this attached dataset is the ONLY data
#       source, so you MUST add it)
#   3. Paste/upload this notebook
#   4. Click "Save & Run All (Commit)"  ← NOT just "Run".
#      Only a committed run produces downloadable output files.
#   5. Wait ~30–60 min (watch the log — it prints progress loudly)
#   6. Open the "Output" tab -> download model_grid.pth,
#      model_drag.pth, motion_params.json -> put them in models/
#
# Tunables (env vars):
#   EPOCHS_GRID (12)  EPOCHS_DRAG (10)  BATCH_GRID (128)  BATCH_DRAG (64)
# ═══════════════════════════════════════════════════════════════

# %% [markdown]
# # 🧠 hCaptcha SuperBrains (v3 — bulletproof)
#
# | Output | What it is | Size |
# |---|---|---|
# | `model_grid.pth` | 33-class tile classifier (ResNet18, fp16) | ~22.7 MB |
# | `model_drag.pth` | drag position regressor (ResNet18, fc=2, fp16) | ~22.7 MB |
# | `motion_params.json` | human mouse-behavior stats | < 1 KB |
#
# **Before running, in Settings:** Accelerator = **GPU T4 x2**, Internet = **ON**.
# **Add Input:** `aneeshtickoo/hcaptcha-dataset` (required if Internet is off).
# **Run with:** **Save & Run All (Commit)** — not just "Run".
#
# Every step prints loud progress markers (STEP 1/7 ... STEP 7/7). If you
# see them, it's working. If you don't, your kernel/GPU settings are wrong.

# %%
import json
import math
import os
import random
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torchvision
from PIL import Image, ImageEnhance, ImageFilter
from torchvision import models, transforms

OUT = Path("/kaggle/working")
DATA = OUT / "superbrain_data"
for d in ["grid_train", "grid_val", "slider", "motion"]:
    (DATA / d).mkdir(parents=True, exist_ok=True)

EPOCHS_GRID = int(os.environ.get("EPOCHS_GRID", "12"))
EPOCHS_DRAG = int(os.environ.get("EPOCHS_DRAG", "10"))
BATCH_GRID = int(os.environ.get("BATCH_GRID", "128"))
BATCH_DRAG = int(os.environ.get("BATCH_DRAG", "64"))

torch.manual_seed(42)
np.random.seed(42)
random.seed(42)
torch.backends.cudnn.benchmark = True

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
IS_CUDA = device.type == "cuda"


def step(msg: str) -> None:
    print(f"\n═══ STEP: {msg} ═══", flush=True)


print("=" * 56, flush=True)
print("ENVIRONMENT CHECK", flush=True)
print("=" * 56, flush=True)
print(f"  torch      : {torch.__version__}", flush=True)
print(f"  torchvision: {torchvision.__version__}", flush=True)
print(f"  GPU        : {torch.cuda.get_device_name(0) if IS_CUDA else 'NONE (CPU — enable T4!)'}",
      flush=True)
if IS_CUDA:
    print(f"  VRAM       : {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB",
          flush=True)
net_ok = False
try:
    with urllib.request.urlopen("https://github.com", timeout=10) as r:
        net_ok = r.status == 200
except Exception:
    net_ok = False
print(f"  Internet   : {'ON' if net_ok else 'OFF'}  (attached dataset works either way)",
      flush=True)
print("=" * 56, flush=True)

# ═══════════════════════════════════════════════════════════════
# SOURCE 1: real hCaptcha tile data (attached dataset > GitHub)
# ═══════════════════════════════════════════════════════════════
step("1/7  Collect tile data (attached Kaggle dataset first)")

IMG_SUFFIXES = (".jpg", ".jpeg", ".png")


def find_attached_dataset() -> Path | None:
    """Return the attached Kaggle dataset folder, if it has tile images."""
    kaggle_input = Path("/kaggle/input")
    if not kaggle_input.exists():
        return None
    for sub in sorted(kaggle_input.iterdir()):
        hits = [p for p in sub.rglob("*")
                if p.suffix.lower() in IMG_SUFFIXES and "checkpoint" not in str(p)]
        if hits:
            print(f"  [data] Attached dataset: {sub.name} ({len(hits)} images)", flush=True)
            return sub
    return None


def fetch_github_tiles() -> Path | None:
    """Download the xtekky/hcaptcha-dataset tarball directly (no git needed)."""
    url = "https://codeload.github.com/xtekky/hcaptcha-dataset/tar.gz/refs/heads/main"
    dest = DATA / "xtekky_hcaptcha"
    try:
        print(f"  [data] Downloading xtekky/hcaptcha-dataset ...", flush=True)
        tmp = DATA / "xtekky.tar.gz"
        urllib.request.urlretrieve(url, tmp)
        with tarfile.open(tmp, "r:gz") as t:
            t.extractall(DATA)
        # repo extracts as DATA/hcaptcha-dataset-main
        for cand in DATA.glob("hcaptcha-dataset*"):
            if cand.is_dir():
                return cand
    except Exception as e:
        print(f"  [data] GitHub download failed: {e}", flush=True)
    return None


def organize_grid(source: Path) -> list[str]:
    """Copy images into train/val per class; return sorted class names."""
    class_dirs = [d for d in source.rglob("*") if d.is_dir()]
    found = []
    for d in class_dirs:
        imgs = [p for p in d.iterdir() if p.suffix.lower() in IMG_SUFFIXES]
        if len(imgs) >= 3:
            found.append((d, imgs))
    found.sort(key=lambda t: t[0].name)
    if not found:
        print("  [data] No class folders found (need folders each holding images).",
              flush=True)
        return []

    classes = []
    for d, imgs in found:
        cls = d.name
        classes.append(cls)
        random.shuffle(imgs)
        n_train = max(1, int(len(imgs) * 0.85))
        for i, p in enumerate(imgs):
            split = "train" if i < n_train else "val"
            dest_dir = DATA / f"grid_{split}" / cls
            dest_dir.mkdir(parents=True, exist_ok=True)
            try:
                img = Image.open(p).convert("RGB").resize((224, 224), Image.LANCZOS)
                img.save(dest_dir / f"{cls}_{i:04d}.jpg", quality=92)
            except Exception:
                continue
    total = sum(len(list((DATA / "grid_train" / c).glob("*"))) for c in classes)
    print(f"  [data] {len(classes)} classes, {total} train images", flush=True)
    return sorted(classes)


grid_classes: list[str] = []
source = find_attached_dataset()
if source is not None:
    grid_classes = organize_grid(source)
if not grid_classes and net_ok:
    gh = fetch_github_tiles()
    if gh is not None:
        grid_classes = organize_grid(gh)
if grid_classes:
    print(f"  ✅ Classifier data ready: {len(grid_classes)} classes", flush=True)
else:
    print("  ⚠️  NO labeled tile data. Possible causes:", flush=True)
    print("      - No dataset attached AND Internet OFF", flush=True)
    print("      - Attached dataset has no images in class folders", flush=True)
    print("      Classifier will be SKIPPED. Drag brain will still train "
          "on synthetic puzzles (if any tiles exist) or be skipped too.",
          flush=True)

# ═══════════════════════════════════════════════════════════════
# SOURCE 2: real slider puzzles (best effort, never fatal)
# ═══════════════════════════════════════════════════════════════
step("2/7  Collect slider puzzle data (best effort)")

slider_train: list = []
if net_ok:
    try:
        from huggingface_hub import snapshot_download
        SLIDER = DATA / "slider_raw"
        snapshot_download("nfsn/SliderCaptcha", local_dir=str(SLIDER),
                          repo_type="dataset",
                          allow_patterns=["train/images/*", "train/labels/*",
                                          "valid/images/*", "valid/labels/*",
                                          "test/images/*", "test/labels/*"])
        for split_name in ["train", "valid", "test"]:
            img_dir = SLIDER / split_name / "images"
            lbl_dir = SLIDER / split_name / "labels"
            if not img_dir.exists():
                continue
            for img_path in sorted(img_dir.glob("*.jpg")):
                lbl_path = lbl_dir / f"{img_path.stem}.txt"
                if not lbl_path.exists():
                    continue
                parts = lbl_path.read_text().strip().split()
                if len(parts) >= 3:
                    slider_train.append((str(img_path), float(parts[1]),
                                         float(parts[2])))
        print(f"  [data] Real slider samples: {len(slider_train)}", flush=True)
    except Exception as e:
        print(f"  [data] Slider download failed ({e})", flush=True)
else:
    print("  [data] Internet OFF — skipping slider download", flush=True)

# ═══════════════════════════════════════════════════════════════
# SOURCE 3: human mouse motion (best effort, tuned defaults fallback)
# ═══════════════════════════════════════════════════════════════
step("3/7  Collect human mouse-motion stats")

motion_stats = {
    "mean_velocity": 0.33, "std_velocity": 0.12, "mean_pause": 16.0,
    "count": 0, "mean_points": 30.0, "mean_accel": 0.02,
}
if net_ok:
    try:
        from huggingface_hub import snapshot_download
        import base64
        MOTION = DATA / "motion_raw"
        snapshot_download("Capycap-AI/CaptchaSolve30k", local_dir=str(MOTION),
                          repo_type="dataset", allow_patterns=["train.jsonl"])
        velocities, pauses, point_counts, accels = [], [], [], []
        with open(MOTION / "train.jsonl") as f:
            for line_num, line in enumerate(f):
                if line_num > 5000:
                    break
                try:
                    row = json.loads(line)
                    for key in ["raw", "mousedata", "mouseStream", "stream"]:
                        if key in row:
                            raw_bytes = base64.b64decode(row[key])
                            pts = []
                            for i in range(0, len(raw_bytes) - 6, 6):
                                x = int.from_bytes(raw_bytes[i:i+2], "little", signed=True)
                                y = int.from_bytes(raw_bytes[i+2:i+4], "little", signed=True)
                                t = int.from_bytes(raw_bytes[i+4:i+6], "little")
                                pts.append((x, y, t))
                            if len(pts) > 3:
                                for i in range(1, len(pts)):
                                    dx = pts[i][0] - pts[i-1][0]
                                    dy = pts[i][1] - pts[i-1][1]
                                    dt = max(1, pts[i][2] - pts[i-1][2])
                                    velocities.append(math.hypot(dx, dy) / dt)
                                    if dt > 30:
                                        pauses.append(dt)
                                point_counts.append(len(pts))
                                for i in range(2, len(pts)):
                                    v1 = math.hypot(pts[i-1][0]-pts[i-2][0],
                                                    pts[i-1][1]-pts[i-2][1]) / max(1, pts[i-1][2]-pts[i-2][2])
                                    v2 = math.hypot(pts[i][0]-pts[i-1][0],
                                                    pts[i][1]-pts[i-1][1]) / max(1, pts[i][2]-pts[i-1][2])
                                    accels.append(abs(v2 - v1) / max(1, pts[i][2]-pts[i-1][2]))
                            break
                except Exception:
                    continue
        if velocities:
            motion_stats.update({
                "mean_velocity": float(np.mean(velocities)),
                "std_velocity": float(np.std(velocities)),
                "mean_pause": float(np.mean(pauses)) if pauses else 16.0,
                "mean_points": float(np.mean(point_counts)),
                "mean_accel": float(np.mean(accels)) if accels else 0.02,
                "count": len(velocities),
            })
            print(f"  [data] Motion stats from {len(point_counts)} real streams",
                  flush=True)
    except Exception as e:
        print(f"  [data] Motion download failed ({e}) — using tuned defaults",
              flush=True)
else:
    print("  [data] Internet OFF — using tuned defaults", flush=True)
with open(OUT / "motion_params.json", "w") as f:
    json.dump(motion_stats, f, indent=2)
print(f"  ✅ motion_params.json written ({len(json.dumps(motion_stats))} bytes)",
      flush=True)

# ═══════════════════════════════════════════════════════════════
# SYNTHETIC DRAG PUZZLES — guaranteed training data for the regressor
# ═══════════════════════════════════════════════════════════════
step("4/7  Generate synthetic drag puzzles")

def make_synthetic_puzzles(count: int, seed: int) -> list:
    """Paste a random tile (piece) into a blurred tile (bg) at a random
    spot with slight rotation/scale. Returns (PIL image, (nx, ny))."""
    rng = random.Random(seed)
    pool = []
    for split in ["train", "val"]:
        base = DATA / f"grid_{split}"
        if base.exists():
            for p in base.rglob("*"):
                if p.suffix.lower() in IMG_SUFFIXES:
                    pool.append(p)
    if not pool:
        return []
    out = []
    for _ in range(count):
        bg_p, piece_p = rng.choice(pool), rng.choice(pool)
        bg = Image.open(bg_p).convert("RGB").resize((224, 224), Image.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=rng.uniform(1.5, 3.5)))
        piece = Image.open(piece_p).convert("RGB")
        piece = piece.resize((rng.randint(64, 92), rng.randint(64, 92)), Image.LANCZOS)
        piece = piece.rotate(rng.uniform(-12, 12), expand=False,
                             fillcolor=(128, 128, 128))
        if rng.random() < 0.5:
            piece = piece.transpose(Image.FLIP_LEFT_RIGHT)
        pw, ph = piece.size
        cx = rng.randint(pw // 2, 224 - pw // 2)
        cy = rng.randint(ph // 2, 224 - ph // 2)
        bg.paste(piece, (cx - pw // 2, cy - ph // 2))
        if rng.random() < 0.7:
            bg = ImageEnhance.Brightness(bg).enhance(rng.uniform(0.85, 1.15))
            bg = ImageEnhance.Contrast(bg).enhance(rng.uniform(0.9, 1.1))
        out.append((bg, (cx / 224.0, cy / 224.0)))
    return out


synthetic = make_synthetic_puzzles(4000, 7)
print(f"  [data] Synthetic drag puzzles: {len(synthetic)}", flush=True)

# ═══════════════════════════════════════════════════════════════
# MODEL 1: Tile Grid Classifier (33 classes, ResNet18)
# ═══════════════════════════════════════════════════════════════
step("5/7  Train Tile Grid Classifier")

def make_resnet18(n_out: int) -> nn.Module:
    """ResNet18 with pretrained weights when downloadable, else from scratch."""
    try:
        model = models.resnet18(weights=models.ResNet18_Weights.IMAGENET1K_V1)
    except Exception as e:
        print(f"  [train] Pretrained weights unavailable ({e}) — from scratch",
              flush=True)
        model = models.resnet18(weights=None)
    model.fc = nn.Linear(model.fc.in_features, n_out)
    return model.to(device)


def train_classifier() -> None:
    train_root = DATA / "grid_train"
    if not train_root.exists() or not any(train_root.iterdir()):
        print("  ⚠️  No labeled tile data — classifier SKIPPED "
              "(solver will use heuristics).", flush=True)
        return

    tf = transforms.Compose([
        transforms.RandomResizedCrop(224, scale=(0.65, 1.0)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(10),
        transforms.ColorJitter(0.2, 0.2, 0.2, 0.05),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])
    vtf = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])
    tr_ds = torchvision.datasets.ImageFolder(str(train_root), tf)
    vl_ds = torchvision.datasets.ImageFolder(str(DATA / "grid_val"), vtf)
    tr_ld = torch.utils.data.DataLoader(tr_ds, BATCH_GRID, shuffle=True,
                                        num_workers=2, pin_memory=IS_CUDA)
    vl_ld = torch.utils.data.DataLoader(vl_ds, BATCH_GRID, shuffle=False,
                                        num_workers=2, pin_memory=IS_CUDA)
    print(f"  [train] train: {len(tr_ds)}  val: {len(vl_ds)}  "
          f"classes: {len(tr_ds.classes)}", flush=True)

    model = make_resnet18(len(tr_ds.classes))
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3, weight_decay=0.02)
    warm = torch.optim.lr_scheduler.LinearLR(opt, start_factor=0.1, total_iters=2)
    cos = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, EPOCHS_GRID - 2))
    sched = torch.optim.lr_scheduler.SequentialLR(opt, [warm, cos], milestones=[2])
    crit = nn.CrossEntropyLoss(label_smoothing=0.1)
    scaler = torch.cuda.amp.GradScaler() if IS_CUDA else None

    best_acc, best_state = 0.0, None
    for ep in range(EPOCHS_GRID):
        model.train()
        t0, tot, cor, run = time.time(), 0, 0, 0.0
        for x, y in tr_ld:
            x, y = x.to(device), y.to(device)
            lam = 1.0
            if random.random() < 0.5:
                lam = float(np.random.beta(0.2, 0.2))
                idx = torch.randperm(x.size(0), device=x.device)
                x = lam * x + (1 - lam) * x[idx]
                y2 = y[idx]
            opt.zero_grad()
            if IS_CUDA:
                with torch.cuda.amp.autocast():
                    logits = model(x)
                    loss = lam * crit(logits, y) + (1 - lam) * crit(logits, y2)
                scaler.scale(loss).backward()
                scaler.step(opt)
                scaler.update()
            else:
                logits = model(x)
                loss = lam * crit(logits, y) + (1 - lam) * crit(logits, y2)
                loss.backward()
                opt.step()
            run += loss.item() * x.size(0)
            tot += y.size(0)
            cor += (logits.argmax(1) == y).sum().item()
        sched.step()
        model.eval()
        vc = vt = 0
        with torch.no_grad():
            for x, y in vl_ld:
                x, y = x.to(device), y.to(device)
                if IS_CUDA:
                    with torch.cuda.amp.autocast():
                        vc += (model(x).argmax(1) == y).sum().item()
                else:
                    vc += (model(x).argmax(1) == y).sum().item()
                vt += y.size(0)
        acc = vc / vt
        if acc > best_acc:
            best_acc = acc
            best_state = {k: v.detach().cpu() for k, v in model.state_dict().items()}
        print(f"  [train] ep {ep+1:2d}/{EPOCHS_GRID}  train {100*cor/tot:5.1f}%  "
              f"val {100*acc:5.1f}%  {time.time()-t0:.0f}s", flush=True)
    if best_state is not None:
        save_path = OUT / "model_grid.pth"
        torch.save({"state_dict": {k: v.half() for k, v in best_state.items()},
                    "classes": list(tr_ds.classes)}, save_path)
        print(f"  ✅ model_grid.pth saved (best val {100*best_acc:.1f}%)",
              flush=True)
    else:
        print("  ⚠️  No checkpoint saved.", flush=True)


train_classifier()

# ═══════════════════════════════════════════════════════════════
# MODEL 2: Drag Position Regressor (ResNet18, fc=2)
# ═══════════════════════════════════════════════════════════════
step("6/7  Train Drag Position Regressor")

class DragDataset(torch.utils.data.Dataset):
    def __init__(self, items, augment=False):
        self.items = items
        self.tf = transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.ColorJitter(0.15, 0.15, 0.15) if augment
            else transforms.Lambda(lambda x: x),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        img, (x, y) = self.items[idx]
        if not isinstance(img, Image.Image):
            img = Image.open(img).convert("RGB")
        return self.tf(img), torch.tensor([x, y], dtype=torch.float32)


def train_drag() -> None:
    real_items = [(p, (x, y)) for p, x, y in slider_train]
    all_items = real_items + synthetic
    if len(all_items) < 40:
        print(f"  ⚠️  Only {len(all_items)} samples (<40) — drag brain SKIPPED.",
              flush=True)
        return
    rng = random.Random(42)
    rng.shuffle(all_items)
    n_train = int(len(all_items) * 0.85)
    tr = DragDataset(all_items[:n_train], augment=True)
    vl = DragDataset(all_items[n_train:])
    tr_ld = torch.utils.data.DataLoader(tr, BATCH_DRAG, shuffle=True,
                                        num_workers=2, pin_memory=IS_CUDA)
    vl_ld = torch.utils.data.DataLoader(vl, BATCH_DRAG, shuffle=False,
                                        num_workers=2, pin_memory=IS_CUDA)
    print(f"  [train] train: {len(tr)}  val: {len(vl)}", flush=True)

    model = make_resnet18(2)
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3, weight_decay=0.01)
    warm = torch.optim.lr_scheduler.LinearLR(opt, start_factor=0.1, total_iters=2)
    cos = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, EPOCHS_DRAG - 2))
    sched = torch.optim.lr_scheduler.SequentialLR(opt, [warm, cos], milestones=[2])
    crit = nn.SmoothL1Loss()
    scaler = torch.cuda.amp.GradScaler() if IS_CUDA else None

    best_loss, best_state = float("inf"), None
    for ep in range(EPOCHS_DRAG):
        model.train()
        t0, run = time.time(), 0.0
        for x, y in tr_ld:
            x, y = x.to(device), y.to(device)
            opt.zero_grad()
            if IS_CUDA:
                with torch.cuda.amp.autocast():
                    loss = crit(model(x), y)
                scaler.scale(loss).backward()
                scaler.step(opt)
                scaler.update()
            else:
                loss = crit(model(x), y)
                loss.backward()
                opt.step()
            run += loss.item() * x.size(0)
        sched.step()
        model.eval()
        vl_loss = 0.0
        with torch.no_grad():
            for x, y in vl_ld:
                x, y = x.to(device), y.to(device)
                if IS_CUDA:
                    with torch.cuda.amp.autocast():
                        vl_loss += crit(model(x), y).item() * x.size(0)
                else:
                    vl_loss += crit(model(x), y).item() * x.size(0)
        vl_loss /= len(vl)
        if vl_loss < best_loss:
            best_loss = vl_loss
            best_state = {k: v.detach().cpu() for k, v in model.state_dict().items()}
        print(f"  [train] ep {ep+1:2d}/{EPOCHS_DRAG}  train {run/len(tr):.4f}  "
              f"val {vl_loss:.4f}  {time.time()-t0:.0f}s", flush=True)
    if best_state is not None:
        save_path = OUT / "model_drag.pth"
        torch.save({"state_dict": {k: v.half() for k, v in best_state.items()}},
                   save_path)
        print(f"  ✅ model_drag.pth saved (best val loss {best_loss:.4f})",
              flush=True)
    else:
        print("  ⚠️  No checkpoint saved.", flush=True)


train_drag()

# ═══════════════════════════════════════════════════════════════
# VERIFY — reload exactly like solver.py and run a forward pass
# ═══════════════════════════════════════════════════════════════
step("7/7  Verify output files (reload like solver.py)")

def verify(path: Path, kind: str) -> None:
    if not path.exists():
        print(f"  ❌ {path.name}: MISSING", flush=True)
        return
    size_mb = path.stat().st_size / 1e6
    raw = torch.load(path, map_location="cpu", weights_only=False)
    if isinstance(raw, dict) and "state_dict" in raw:
        state, classes = raw["state_dict"], raw.get("classes")
    else:
        state, classes = raw, None
    n_out = len(classes) if classes else 2
    m = models.resnet18(weights=None)
    m.fc = nn.Linear(m.fc.in_features, n_out)
    m.load_state_dict(state)
    m.eval()
    with torch.no_grad():
        m(torch.randn(1, 3, 224, 224))
    ok = "✅" if size_mb < 25.0 else "⚠️ OVER 25MB"
    print(f"  {ok} {path.name}  {size_mb:.1f} MB  (loads + forward pass OK, "
          f"{'classifier' if classes else 'regressor'} mode)", flush=True)


verify(OUT / "model_grid.pth", "grid")
verify(OUT / "model_drag.pth", "drag")

print("\n" + "=" * 56, flush=True)
print("DONE — download these from the Output tab", flush=True)
print("(if you used 'Run All' instead of 'Save & Run All (Commit)',", flush=True)
print(" click Commit and re-run — outputs only download after a commit)",
      flush=True)
print("=" * 56, flush=True)
for name in ["model_grid.pth", "model_drag.pth", "motion_params.json"]:
    p = OUT / name
    if p.exists():
        print(f"  ✅ {name}  ({p.stat().st_size / 1e6:.1f} MB)", flush=True)
    else:
        print(f"  ❌ {name}  (not produced)", flush=True)
print("=" * 56, flush=True)
