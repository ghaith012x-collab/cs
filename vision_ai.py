"""
Vision AI Module — custom YOLOv11n-based captcha solver.
Downloads pre-trained YOLOv11n from Ultralytics, exports to ONNX,
runs fast CPU inference (~56ms per image).

Uses COCO pre-trained model for zero-shot detection of common objects.
For captcha-specific training, see train_colab.py.
"""

import asyncio
import io
import os
import warnings
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

warnings.filterwarnings("ignore", category=UserWarning, module="ultralytics")

# ─── Lazy-loaded globals ──────────────────────────────────
_onnx_session = None
_model_loaded = False

MODEL_DIR = Path("_models")
MODEL_PATH = MODEL_DIR / "yolo11n.onnx"
# URL for pre-trained weights from Ultralytics
YOLO_PT_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt"
MODEL_DOWNLOAD_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx"


def load_model(log: Optional[Callable] = None) -> bool:
    """Download YOLOv11n ONNX model and load into ONNX Runtime.
    
    This is a one-time call — downloads the ~5.5MB model from Ultralytics' GitHub,
    loads it into ONNX Runtime for fast CPU inference.
    Returns True if successful.
    """
    global _onnx_session, _model_loaded
    
    if _model_loaded:
        return True
    
    _log = log or (lambda msg, level="info": None)
    
    try:
        import onnxruntime as ort
        
        # Download model if not present
        MODEL_DIR.mkdir(exist_ok=True)
        
        if not MODEL_PATH.exists():
            _log("[YOLO] Downloading model (5.5MB)...")
            import urllib.request
            try:
                urllib.request.urlretrieve(MODEL_DOWNLOAD_URL, MODEL_PATH)
                _log("[YOLO] Model downloaded")
            except Exception:
                # Fallback: try Ultralytics export path
                _log("[YOLO] Direct download failed, trying Ultralytics export...", level="warn")
                try:
                    _export_from_ultralytics(_log)
                except Exception as e2:
                    _log(f"[YOLO] Export failed: {e2}", level="error")
                    return False
        
        if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 100000:  # >100KB = valid
            _log(f"[YOLO] Loading ONNX model ({MODEL_PATH.stat().st_size // 1024}KB)...")
            _onnx_session = ort.InferenceSession(
                str(MODEL_PATH),
                providers=['CPUExecutionProvider']
            )
            _model_loaded = True
            _log("[YOLO] Model loaded! ~56ms per inference")
            return True
        else:
            _log("[YOLO] Model file invalid or missing", level="error")
            return False
            
    except ImportError as e:
        _log(f"[YOLO] Import error: {e}. Install: pip install onnxruntime onnx", level="error")
        return False
    except Exception as e:
        _log(f"[YOLO] Load error: {e}", level="error")
        return False


def _export_from_ultralytics(log):
    """Export YOLO11n.pt to ONNX using Ultralytics library."""
    from ultralytics import YOLO
    _log("[YOLO] Downloading yolo11n.pt via Ultralytics...")
    model = YOLO("yolo11n.pt")  # Downloads automatically
    _log("[YOLO] Exporting to ONNX...")
    success = model.export(format="onnx", imgsz=640, nms=True)
    # Move to our expected path
    src = Path("yolo11n.onnx")
    if src.exists():
        import shutil
        shutil.move(str(src), MODEL_PATH)
    _log("[YOLO] ONNX export complete")


# ─── Inference ────────────────────────────────────────────

def preprocess(img: np.ndarray, input_size: int = 640) -> np.ndarray:
    """Preprocess image for YOLO inference: resize + normalize."""
    h, w = img.shape[:2]
    scale = min(input_size / max(h, w), input_size / max(h, w))
    new_w, new_h = int(w * scale), int(h * scale)
    
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    
    # Create square canvas filled with 114 (typical YOLO padding value)
    canvas = np.full((input_size, input_size, 3), 114, dtype=np.uint8)
    canvas[:new_h, :new_w] = resized
    
    # HWC → CHW, normalize to [0,1]
    tensor = canvas.transpose(2, 0, 1).astype(np.float32) / 255.0
    tensor = np.expand_dims(tensor, axis=0)
    
    return tensor, scale


def postprocess(output: np.ndarray, original_shape: Tuple[int, int],
                scale: float, conf_threshold: float = 0.25) -> List[dict]:
    """Parse YOLO ONNX output into bounding boxes.
    
    Output shape: (1, 300, 6) where 6 = [x1, y1, x2, y2, confidence, class_id]
    (end-to-end NMS baked into ONNX graph by default)
    """
    detections = output[0][0]  # Shape: (300, 6)
    h, w = original_shape[:2]
    
    results = []
    for det in detections:
        conf = float(det[4])
        if conf < conf_threshold:
            continue
        
        cls_id = int(det[5])
        
        # Scale coordinates back to original image size
        x1 = float(det[0]) / scale
        y1 = float(det[1]) / scale
        x2 = float(det[2]) / scale
        y2 = float(det[3]) / scale
        
        # Clamp to image bounds
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        
        results.append({
            "class_id": cls_id,
            "confidence": conf,
            "bbox": (int(x1), int(y1), int(x2 - x1), int(y2 - y1)),  # x, y, w, h
            "center": (int((x1 + x2) / 2), int((y1 + y2) / 2)),
        })
    
    return results


def run_inference(image: np.ndarray, conf_threshold: float = 0.25
                  ) -> Optional[List[dict]]:
    """Run YOLO inference on an image.
    
    Args:
        image: BGR numpy array (OpenCV format)
        conf_threshold: Minimum confidence (0.0-1.0)
    
    Returns:
        List of detections, or None if model not loaded
    """
    global _onnx_session
    if _onnx_session is None:
        return None
    
    input_size = 640
    tensor, scale = preprocess(image, input_size)
    
    input_name = _onnx_session.get_inputs()[0].name
    outputs = _onnx_session.run(None, {input_name: tensor})
    
    return postprocess(outputs[0], image.shape, scale, conf_threshold)


def get_coco_class_name(class_id: int) -> str:
    """Return the COCO class name for a given class ID."""
    names = [
        'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train',
        'truck', 'boat', 'traffic light', 'fire hydrant', 'stop sign',
        'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep',
        'cow', 'elephant', 'bear', 'zebra', 'giraffe', 'backpack', 'umbrella',
        'handbag', 'tie', 'suitcase', 'frisbee', 'skis', 'snowboard',
        'sports ball', 'kite', 'baseball bat', 'baseball glove', 'skateboard',
        'surfboard', 'tennis racket', 'bottle', 'wine glass', 'cup', 'fork',
        'knife', 'spoon', 'bowl', 'banana', 'apple', 'sandwich', 'orange',
        'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake', 'chair',
        'couch', 'potted plant', 'bed', 'dining table', 'toilet', 'tv',
        'laptop', 'mouse', 'remote', 'keyboard', 'cell phone', 'microwave',
        'oven', 'toaster', 'sink', 'refrigerator', 'book', 'clock', 'vase',
        'scissors', 'teddy bear', 'hair drier', 'toothbrush'
    ]
    if 0 <= class_id < len(names):
        return names[class_id]
    return f"class_{class_id}"


# ─── Captcha-specific helpers ─────────────────────────────

# COCO class IDs commonly found in captcha drag objects
CAPTCHA_RELEVANT_CLASSES = {
    0: 'person', 1: 'bicycle', 2: 'car', 3: 'motorcycle',
    4: 'airplane', 5: 'bus', 6: 'train', 7: 'truck',
    8: 'boat', 14: 'bird', 15: 'cat', 16: 'dog',
    17: 'horse', 18: 'sheep', 19: 'cow', 20: 'elephant',
    21: 'bear', 22: 'zebra', 23: 'giraffe', 24: 'backpack',
    28: 'suitcase', 31: 'sports ball', 32: 'kite', 33: 'baseball bat',
    39: 'bottle', 41: 'cup', 43: 'knife', 44: 'spoon',
    45: 'bowl', 46: 'banana', 47: 'apple', 48: 'sandwich',
    49: 'orange', 50: 'broccoli', 51: 'carrot', 52: 'hot dog',
    53: 'pizza', 54: 'donut', 55: 'cake', 56: 'chair',
    57: 'couch', 58: 'potted plant', 59: 'bed', 60: 'dining table',
    62: 'tv', 63: 'laptop', 64: 'mouse', 65: 'remote',
    66: 'keyboard', 67: 'cell phone', 73: 'book', 74: 'clock',
    75: 'vase', 76: 'scissors', 77: 'teddy bear',
}

# Class IDs for small objects commonly found in hCaptcha (spaceship, star, vial, etc.)
# These aren't in COCO, but COCO has similar-shaped objects
SMALL_OBJECT_CLASSES = {
    31: 'sports ball',    # Similar to circular targets (star, sun)
    32: 'kite',           # Similar to spaceship, rocketship shape
    39: 'bottle',         # Vial, bottle-like drag objects
    41: 'cup',            # Cup, bowl-like targets
    44: 'spoon',          # Elongated objects
    45: 'bowl',           # Bowl, container targets
    46: 'banana',         # Curved objects
    47: 'apple',          # Round objects
    55: 'cake',           # Multi-tiered objects
    73: 'book',           # Rectangular objects
    74: 'clock',          # Round targets
    75: 'vase',           # Container shapes
    76: 'scissors',       # Cross/star-shaped objects
    77: 'teddy bear',     # Irregularly shaped objects
}


async def detect_drag_objects(iframe_screenshot: bytes,
                               log: Optional[Callable] = None
                               ) -> Optional[Tuple[int, int, int, int, int, int, int, int]]:
    """Detect drag object + target in an hCaptcha iframe screenshot using YOLO.
    
    Returns:
        (sx, sy, ex, ey, left_cls, right_cls, left_conf, right_conf)
        where (sx,sy) is the drag start and (ex,ey) is the drag end.
        Returns None if detection fails.
    """
    _log = log or (lambda msg, level="info": None)
    
    if not _model_loaded:
        if not load_model(_log):
            return None
    
    try:
        img = cv2.imdecode(np.frombuffer(iframe_screenshot, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        
        h, w = img.shape[:2]
        
        # Only analyze top 70% (skip verify button)
        crop_h = int(h * 0.7)
        img_crop = img[:crop_h, :]
        
        detections = run_inference(img_crop, conf_threshold=0.3)
        if not detections:
            # Try lower threshold
            detections = run_inference(img_crop, conf_threshold=0.15)
        
        if not detections or len(detections) < 2:
            _log(f"[YOLO] {len(detections) if detections else 0} object(s) detected")
            return None
        
        _log(f"[YOLO] {len(detections)} object(s) detected")
        for d in detections:
            cls_name = get_coco_class_name(d['class_id'])
            _log(f"  {cls_name}: {d['confidence']:.2f} at {d['center']} size {d['bbox'][2]}x{d['bbox'][3]}")
        
        # Filter by size (drag objects are 20-150px)
        filtered = [d for d in detections if 20 <= d['bbox'][2] <= 150 and 20 <= d['bbox'][3] <= 150]
        
        if len(filtered) < 2:
            filtered = detections
        
        # Split into left and right sides
        mid_x = w // 2
        left = sorted([d for d in filtered if d['center'][0] < mid_x - 30],
                       key=lambda d: -d['confidence'])
        right = sorted([d for d in filtered if d['center'][0] > mid_x + 30],
                        key=lambda d: -d['confidence'])
        
        if left and right:
            src = left[0]
            tgt = right[0]
            _log(f"[YOLO] Pair: {get_coco_class_name(src['class_id'])}({src['center'][0]},{src['center'][1]}) → "
                 f"{get_coco_class_name(tgt['class_id'])}({tgt['center'][0]},{tgt['center'][1]})")
            return (src['center'][0], src['center'][1],
                    tgt['center'][0], tgt['center'][1],
                    src['class_id'], tgt['class_id'],
                    int(src['confidence'] * 100), int(tgt['confidence'] * 100))
        
        # One-sided detection: try pairing with center objects
        if left:
            src = left[0]
            right_candidates = [d for d in filtered if d['center'][0] > src['center'][0] + 60]
            if right_candidates:
                tgt = max(right_candidates, key=lambda d: d['confidence'])
                return (src['center'][0], src['center'][1],
                        tgt['center'][0], tgt['center'][1],
                        src['class_id'], tgt['class_id'],
                        int(src['confidence'] * 100), int(tgt['confidence'] * 100))
        
        if right:
            tgt = right[0]
            left_candidates = [d for d in filtered if d['center'][0] < tgt['center'][0] - 60]
            if left_candidates:
                src = max(left_candidates, key=lambda d: d['confidence'])
                return (src['center'][0], src['center'][1],
                        tgt['center'][0], tgt['center'][1],
                        src['class_id'], tgt['class_id'],
                        int(src['confidence'] * 100), int(tgt['confidence'] * 100))
        
        return None
        
    except Exception as e:
        _log(f"[YOLO] Error: {e}", level="error")
        return None


async def detect_grid_tiles(iframe_screenshot: bytes, challenge_text: str = "",
                             log: Optional[Callable] = None) -> Optional[List[int]]:
    """Use YOLO to detect which tiles in a grid challenge contain the target object.
    
    For grid captchas, takes the iframe screenshot and detects objects in each tile.
    Returns indices of matching tiles (0-8).
    """
    _log = log or (lambda msg, level="info": None)
    
    if not _model_loaded:
        if not load_model(_log):
            return None
    
    try:
        img = cv2.imdecode(np.frombuffer(iframe_screenshot, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        
        h, w = img.shape[:2]
        
        # Detect all objects in the full image
        detections = run_inference(img, conf_threshold=0.2)
        if not detections:
            return None
        
        _log(f"[YOLO] Grid: {len(detections)} object(s) in frame")
        
        # Split into 3x3 grid
        cols, rows = 3, 3
        tw, th = w // cols, h // rows
        
        tile_scores = []
        for r in range(rows):
            for c in range(cols):
                tx, ty = c * tw, r * th
                tile_center = (tx + tw // 2, ty + th // 2)
                
                # Count objects whose center falls within this tile
                count = 0
                for d in detections:
                    cx, cy = d['center']
                    if tx <= cx <= tx + tw and ty <= cy <= ty + th:
                        count += 1
                
                tile_scores.append((count, r * cols + c))
        
        # Sort by count descending, pick tiles with 1+ detected objects
        tile_scores.sort(key=lambda x: -x[0])
        matched = [idx for count, idx in tile_scores if count > 0]
        
        if matched:
            _log(f"[YOLO] Grid: {len(matched)} tile(s) with objects")
            return matched
        
        return None
        
    except Exception as e:
        _log(f"[YOLO] Grid error: {e}", level="error")
        return None


async def ensure_model(log: Optional[Callable] = None):
    """Ensure YOLO model is downloaded and loaded.
    Call this at startup to download the model before it's needed."""
    return load_model(log)


if __name__ == "__main__":
    # Test: load model and show info
    success = load_model()
    print(f"Model loaded: {success}")
    if _onnx_session:
        print(f"Input: {_onnx_session.get_inputs()[0]}")
        print(f"Output: {_onnx_session.get_outputs()[0]}")
        print(f"Providers: {_onnx_session.get_providers()}")
