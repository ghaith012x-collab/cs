"""Improved hCaptcha Solver"""

import sys
import re
import time
import random
import base64
import asyncio
import warnings
import logging
import urllib.request
import urllib.error
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any, Final, Protocol, Union
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from threading import RLock
from collections import Counter
import traceback
import math

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('captchahub')

# =============================================================================
# DEPENDENCY CHECKING
# =============================================================================

def _optional_import(module: str) -> Any:
    """Safely import a module if available, return None otherwise."""
    try:
        return __import__(module)
    except ImportError:
        return None

# Core ML imports
np = _optional_import('numpy')
cv2 = _optional_import('cv2')
torch = _optional_import('torch')
PIL = _optional_import('PIL') # Pillow for CLIP image processing

# Browser automation
_playwright_async_api = _optional_import('playwright.async_api')
_async_playwright = getattr(_playwright_async_api, 'async_playwright', None) if _playwright_async_api else None

_selenium_pkg = _optional_import('selenium')
webdriver = getattr(_selenium_pkg, 'webdriver', None) if _selenium_pkg else None
_By = getattr(_selenium_pkg.webdriver.common.by, 'By', None) if _selenium_pkg else None
_Options = getattr(_selenium_pkg.webdriver.chrome.options, 'Options', None) if _selenium_pkg else None
_ActionChains = getattr(_selenium_pkg.webdriver.common.action_chains, 'ActionChains', None) if _selenium_pkg else None

# Model imports
ultralytics_pkg = _optional_import('ultralytics')
transformers_pkg = _optional_import('transformers')
onnxruntime = _optional_import('onnxruntime')

# Safe attribute access
YOLO = getattr(ultralytics_pkg, 'YOLO', None) if ultralytics_pkg else None
CLIPModel = getattr(transformers_pkg, 'CLIPModel', None) if transformers_pkg else None
CLIPProcessor = getattr(transformers_pkg, 'CLIPProcessor', None) if transformers_pkg else None
AutoProcessor = getattr(transformers_pkg, 'AutoProcessor', None) if transformers_pkg else None

# Availability flags
PLAYWRIGHT_AVAILABLE: Final[bool] = _async_playwright is not None
SELENIUM_AVAILABLE: Final[bool] = webdriver is not None and _By is not None and _Options is not None
ULTRALYTICS_AVAILABLE: Final[bool] = YOLO is not None
TRANSFORMERS_AVAILABLE: Final[bool] = transformers_pkg is not None
ONNX_AVAILABLE: Final[bool] = onnxruntime is not None

# Check essential dependencies
if np is None:
    warnings.warn("numpy not installed. Install: pip install numpy")
if cv2 is None:
    warnings.warn("opencv-python not installed. Install: pip install opencv-python")
if PIL is None:
    warnings.warn("Pillow not installed. Install: pip install Pillow")
if torch is None:
    warnings.warn("torch not installed. Install: pip install torch")
if not TRANSFORMERS_AVAILABLE:
    warnings.warn("transformers not installed. CLIP functionality will be disabled. Install: pip install transformers")

# =============================================================================
# ENUMS
# =============================================================================

class ChallengeType(Enum):
    """Types of CAPTCHA challenges supported."""
    UNKNOWN = auto()
    HCAPTCHA_IMAGE_LABEL = auto()
    SLIDER_CAPTCHA = auto()
    SHAPE_MATCHING = auto()
    OBJECT_ALIGNMENT = auto()
    DRAG_AND_DROP = auto()

class BackendType(Enum):
    """Available automation backends."""
    AUTO = "auto"
    PLAYWRIGHT = "playwright"
    SELENIUM = "selenium"

class ConfidenceLevel(Enum):
    """Confidence level for classification decisions."""
    LOW = "low"       # < 0.3 - likely incorrect
    MEDIUM = "medium" # 0.3-0.7 - needs verification  
    HIGH = "high"     # > 0.7 - reliable

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass(slots=True)
class SolverConfig:
    """
    Configuration for CAPTCHA solver.
    
    Attributes:
        browser_type: Browser to use ('chromium', 'firefox', 'webkit')
        headless: Run browser in headless mode
        stealth: Enable stealth mode
        user_data_dir: Custom browser profile directory
        clip_confidence_threshold: Confidence threshold for CLIP (0-1)
        yolo_confidence_threshold: Confidence threshold for YOLO (0-1)
        iou_threshold: IoU threshold for NMS (0-1)
        max_challenge_rounds: Maximum solve attempts for hCaptcha rounds
        challenge_timeout: Timeout for challenge detection (seconds)
        viewport_width: Browser viewport width
        viewport_height: Browser viewport height
        human_like_mouse: Enable human-like mouse movements
        timeout: Page load timeout (seconds)
        model_dir: Directory for ML models
        yolo_model_name: YOLO model to use (yolov8n, yolo11s, yolo11m)
        viewport_jitter: Add random viewport jitter for stealth
        rate_limit_min_delay: Minimum delay between actions for rate limiting (seconds)
        rate_limit_max_delay: Maximum delay between actions for rate limiting (seconds)
        min_solve_time_per_round: Minimum time to spend solving a single challenge round (seconds)
        max_concurrent_sessions: Maximum concurrent browser sessions
    """
    browser_type: str = "chromium"
    headless: bool = False
    stealth: bool = True
    user_data_dir: Optional[str] = None
    clip_confidence_threshold: float = 0.75 # Higher default for CLIP-first
    yolo_confidence_threshold: float = 0.25 # Lower default for YOLO as secondary
    iou_threshold: float = 0.45
    max_challenge_rounds: int = 4 # Support up to 4 rounds
    challenge_timeout: int = 30
    viewport_width: int = 1920
    viewport_height: int = 1080
    human_like_mouse: bool = True
    timeout: int = 30
    model_dir: Optional[Path] = None
    yolo_model_name: str = "yolo11s"
    viewport_jitter: bool = True
    rate_limit_min_delay: float = 0.5 # Min delay between clicks
    rate_limit_max_delay: float = 1.5 # Max delay between clicks
    min_solve_time_per_round: float = 8.0 # Minimum 8 seconds per round
    max_concurrent_sessions: int = 1

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not 0 < self.clip_confidence_threshold <= 1:
            raise ValueError("clip_confidence_threshold must be between 0 and 1")
        if not 0 < self.yolo_confidence_threshold <= 1:
            raise ValueError("yolo_confidence_threshold must be between 0 and 1")
        if not 0 < self.iou_threshold <= 1:
            raise ValueError("iou_threshold must be between 0 and 1")
        if self.max_challenge_rounds <= 0:
            raise ValueError("max_challenge_rounds must be positive")
        if self.viewport_width <= 0 or self.viewport_height <= 0:
            raise ValueError("Viewport dimensions must be positive")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.browser_type not in ('chromium', 'firefox', 'webkit'):
            raise ValueError("browser_type must be chromium, firefox, or webkit")
        if self.yolo_model_name not in ('yolov8n', 'yolo11s', 'yolo11m', 'yolo11l'):
            raise ValueError("yolo_model_name must be yolov8n, yolo11s, yolo11m, or yolo11l")
        if self.model_dir is None:
            object.__setattr__(self, 'model_dir', Path.home() / ".captcha_solver" / "models")
        if not 0 <= self.rate_limit_min_delay <= self.rate_limit_max_delay:
            raise ValueError("rate_limit_min_delay must be less than or equal to rate_limit_max_delay")
        if self.min_solve_time_per_round <= 0:
            raise ValueError("min_solve_time_per_round must be positive")

    def with_model_dir(self, model_dir: Path) -> 'SolverConfig':
        """Return a new config with a different model directory."""
        return replace(self, model_dir=model_dir)

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass(slots=True)
class BoundingBox:
    """Represents a detected object bounding box."""
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float = 0.0
    label: str = ""

    @property
    def width(self) -> float:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0, self.y2 - self.y1)

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    @property
    def area(self) -> float:
        return self.width * self.height

@dataclass
class ClassificationResult:
    """
    Result of classifying a CAPTCHA tile.
    
    Contains all scores from different models for debugging and analysis.
    """
    label: str
    confidence: float
    confidence_level: ConfidenceLevel
    clip_score: float = 0.0
    yolo_score: float = 0.0
    # Removed quality_score and heuristic_score as per design

@dataclass
class ChallengeState:
    """State of a detected CAPTCHA challenge."""
    challenge_type: ChallengeType = ChallengeType.UNKNOWN
    target_label: str = ""
    instruction_text: str = ""
    grid_cells: List[BoundingBox] = field(default_factory=list)
    solved: bool = False
    attempts: int = 0
    challenge_round: int = 0 # New: Track challenge rounds

# Removed EvaluationMetrics as per design

# =============================================================================
# CUSTOM EXCEPTIONS
# =============================================================================

class CaptchaHubError(Exception):
    """Base exception for CaptchaHub."""
    pass

class BrowserNotAvailableError(CaptchaHubError):
    """Browser automation library not installed."""
    pass

class ModelNotAvailableError(CaptchaHubError):
    """ML model not available."""
    pass

class ChallengeDetectionError(CaptchaHubError):
    """Failed to detect challenge."""
    pass

class SolveTimeoutError(CaptchaHubError):
    """Challenge solve timed out."""
    pass

class DownloadError(CaptchaHubError):
    """Failed to download model or file."""
    pass

class ResourceCleanupError(CaptchaHubError):
    """Failed to clean up browser resources."""
    pass

# =============================================================================
# FILE DOWNLOADER
# =============================================================================

class ModelDownloader:
    """Safe model downloader with integrity verification."""

    MODEL_SOURCES: Final[Dict[str, str]] = {
        'yolov8n': 'https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov8n.pt',
        'yolo11s': 'https://github.com/ultralytics/assets/releases/download/v0.0.0/yolo11s.pt',
        'yolo11m': 'https://github.com/ultralytics/assets/releases/download/v0.0.0/yolo11m.pt',
    }

    @staticmethod
    def download(url: str, dest: Path, chunk_size: int = 8192, timeout: int = 60) -> bool:
        """Download file with progress tracking."""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            request = urllib.request.Request(url, headers={'User-Agent': 'CaptchaHub/1.0'})
            
            with urllib.request.urlopen(request, timeout=timeout) as response:
                with open(dest, 'wb') as f:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
            
            logger.info(f"Downloaded model to {dest}")
            return True
            
        except urllib.error.URLError as e:
            logger.error(f"Download failed: {e}")
            if dest.exists():
                dest.unlink()
            return False
        except (IOError, OSError) as e:
            logger.error(f"File error: {e}")
            if dest.exists():
                dest.unlink()
            return False

    @classmethod
    def get_yolo_model_path(cls, model_dir: Path, model_name: str = "yolo11s") -> Path:
        """Get path for YOLO model."""
        return model_dir / f"{model_name}.pt"

# =============================================================================
# PREPROCESSOR MODULE
# =============================================================================

class Preprocessor:
    """
    Multi-representation image preprocessor.
    
    Creates multiple image representations for ensemble inference.
    """

    @staticmethod
    def create_crops(image: Any) -> Dict[str, Any]:
        """
        Create multiple image representations for CLIP evaluation.
        
        Returns dict with:
        - original: Raw image
        - center_crop: 80% center crop
        - padded: Padded version (e.g., 10% padding)
        """
        if image is None:
            return {}

        h, w, _ = image.shape
        crops = {'original': image.copy()}

        # Center crop (e.g., 80% of image)
        center_h, center_w = int(h * 0.8), int(w * 0.8)
        start_h, start_w = (h - center_h) // 2, (w - center_w) // 2
        center_crop = image[start_h:start_h + center_h, start_w:start_w + center_w]
        crops['center_crop'] = center_crop

        # Padded version (e.g., 10% padding)
        pad_h, pad_w = int(h * 0.1), int(w * 0.1)
        padded_image = cv2.copyMakeBorder(image, pad_h, pad_h, pad_w, pad_w, cv2.BORDER_CONSTANT, value=[0, 0, 0])
        crops['padded'] = padded_image
        
        return crops

# =============================================================================
# EMBEDDING MODEL MODULE (CLIP)
# =============================================================================

class EmbeddingModel:
    """
    Vision-language embedding model for semantic similarity (CLIP).
    
    Uses CLIP to compute image and text embeddings for comparison.
    """

    def __init__(self, config: SolverConfig):
        self.config = config
        self._model = None
        self._processor = None
        self._model_lock = RLock()
        self._ready = False
        self._load_model()
        self.prompt_templates = [
            "a photo of a {}",
            "a {} in the image",
            "an image containing {}",
            "picture of a {}",
            "this is a {}"
        ]

    def _load_model(self) -> None:
        """Load CLIP model."""
        if not TRANSFORMERS_AVAILABLE or CLIPModel is None or CLIPProcessor is None:
            logger.warning("Transformers or CLIP models not available, embedding model disabled.")
            return

        try:
            with self._model_lock:
                if self._ready:
                    return
                    
                model_name = "openai/clip-vit-base-patch32"
                self._model = CLIPModel.from_pretrained(model_name)
                self._processor = CLIPProcessor.from_pretrained(model_name)
                self._ready = True
                logger.info("CLIP embedding model loaded.")
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning(f"CLIP load failed: {e}")

    def semantic_similarity(self, image: Any, target_label: str) -> float:
        """
        Compute semantic similarity between image and text using multiple prompt templates.
        
        Returns averaged cosine similarity between embeddings (0-1).
        """
        if not self._ready or self._model is None or self._processor is None:
            return 0.0
        if not target_label or image is None or PIL is None:
            return 0.0

        try:
            from PIL import Image
            
            # Generate multiple prompts
            prompts = [template.format(target_label) for template in self.prompt_templates]

            # Process image and texts
            inputs = self._processor(
                text=prompts,
                images=Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)),
                return_tensors="pt", padding=True
            )
            outputs = self._model(**inputs)
            
            # Normalize embeddings
            image_emb = outputs.image_embeds / outputs.image_embeds.norm(dim=-1, keepdim=True)
            text_emb = outputs.text_embeds / outputs.text_embeds.norm(dim=-1, keepdim=True)
            
            # Compute cosine similarity for each prompt and average
            similarities = (image_emb * text_emb).sum(dim=-1).tolist()
            avg_similarity = sum(similarities) / len(similarities)

            return max(0.0, min(1.0, avg_similarity))
        except (ImportError, RuntimeError, ValueError) as e:
            logger.debug(f"CLIP similarity error: {e}")
            return 0.0

# =============================================================================
# YOLO DETECTOR MODULE (SECONDARY)
# =============================================================================

class YOLODetector:
    """
    YOLO-based object detector, now secondary to CLIP.
    """

    # Expanded hCaptcha label aliases
    HCAPTCHA_ALIASES: Final[Dict[str, str]] = {
        'motorbus': 'bus',
        'vehicles': 'vehicle',
        'traffic lights': 'traffic light',
        'traffic light': 'traffic light',
        'crosswalk': 'pedestrian crossing',
        'crosswalks': 'pedestrian crossing',
        'seaplane': 'seaplane',
        'chimney': 'chimney',
        'chimneys': 'chimney',
        'stairs': 'stairs',
        'staircase': 'stairs',
        'motorized vehicles': 'vehicle',
        'motorized vehicle': 'vehicle',
        'bicycle': 'bicycle',
        'bicycles': 'bicycle',
        'boat': 'boat',
        'boats': 'boat',
        'airplane': 'airplane',
        'airplanes': 'airplane',
        'fire hydrant': 'fire hydrant',
        'fire hydrants': 'fire hydrant',
        'bus': 'bus',
        'buses': 'bus',
        'car': 'car',
        'cars': 'car',
        'truck': 'truck',
        'trucks': 'truck',
        'motorcycle': 'motorcycle',
        'motorcycles': 'motorcycle',
        'train': 'train',
        'trains': 'train',
        'person': 'person',
        'people': 'person',
        'tree': 'tree',
        'trees': 'tree',
        'building': 'building',
        'buildings': 'building',
        'mountain': 'mountain',
        'mountains': 'mountain',
        'bridge': 'bridge',
        'bridges': 'bridge',
        'road': 'road',
        'roads': 'road',
        'sign': 'sign',
        'signs': 'sign',
        'lamp': 'lamp',
        'lamps': 'lamp',
        'street light': 'street light',
        'street lights': 'street light',
        'parking meter': 'parking meter',
        'parking meters': 'parking meter',
        'bench': 'bench',
        'benches': 'bench',
        'cat': 'cat',
        'cats': 'cat',
        'dog': 'dog',
        'dogs': 'dog',
        'horse': 'horse',
        'horses': 'horse',
        'sheep': 'sheep',
        'sheeps': 'sheep',
        'cow': 'cow',
        'cows': 'cow',
        'elephant': 'elephant',
        'elephants': 'elephant',
        'bear': 'bear',
        'bears': 'bear',
        'zebra': 'zebra',
        'zebras': 'zebra',
        'giraffe': 'giraffe',
        'giraffes': 'giraffe',
        'backpack': 'backpack',
        'backpacks': 'backpack',
        'umbrella': 'umbrella',
        'umbrellas': 'umbrella',
        'handbag': 'handbag',
        'handbags': 'handbag',
        'tie': 'tie',
        'ties': 'tie',
        'suitcase': 'suitcase',
        'suitcases': 'suitcase',
        'frisbee': 'frisbee',
        'frisbees': 'frisbee',
        'ski': 'ski',
        'skis': 'ski',
        'snowboard': 'snowboard',
        'snowboards': 'snowboard',
        'sports ball': 'sports ball',
        'sports balls': 'sports ball',
        'kite': 'kite',
        'kites': 'kite',
        'baseball bat': 'baseball bat',
        'baseball bats': 'baseball bat',
        'baseball glove': 'baseball glove',
        'baseball gloves': 'baseball glove',
        'skateboard': 'skateboard',
        'skateboards': 'skateboard',
        'surfboard': 'surfboard',
        'surfboards': 'surfboard',
        'tennis racket': 'tennis racket',
        'tennis rackets': 'tennis racket',
        'bottle': 'bottle',
        'bottles': 'bottle',
        'wine glass': 'wine glass',
        'wine glasses': 'wine glass',
        'cup': 'cup',
        'cups': 'cup',
        'fork': 'fork',
        'forks': 'fork',
        'knife': 'knife',
        'knives': 'knife',
        'spoon': 'spoon',
        'spoons': 'spoon',
        'bowl': 'bowl',
        'bowls': 'bowl',
        'banana': 'banana',
        'bananas': 'banana',
        'apple': 'apple',
        'apples': 'apple',
        'sandwich': 'sandwich',
        'sandwiches': 'sandwich',
        'orange': 'orange',
        'oranges': 'orange',
        'broccoli': 'broccoli',
        'carrots': 'carrot',
        'hot dog': 'hot dog',
        'hot dogs': 'hot dog',
        'pizza': 'pizza',
        'pizzas': 'pizza',
        'donut': 'donut',
        'donuts': 'donut',
        'cake': 'cake',
        'cakes': 'cake',
        'chair': 'chair',
        'chairs': 'chair',
        'couch': 'couch',
        'couches': 'couch',
        'potted plant': 'potted plant',
        'potted plants': 'potted plant',
        'bed': 'bed',
        'beds': 'bed',
        'dining table': 'dining table',
        'dining tables': 'dining table',
        'toilet': 'toilet',
        'toilets': 'toilet',
        'tv': 'tv',
        'tvs': 'tv',
        'laptop': 'laptop',
        'laptops': 'laptop',
        'mouse': 'mouse',
        'mice': 'mouse',
        'remote': 'remote',
        'remotes': 'remote',
        'keyboard': 'keyboard',
        'keyboards': 'keyboard',
        'cell phone': 'cell phone',
        'cell phones': 'cell phone',
        'microwave': 'microwave',
        'microwaves': 'microwave',
        'oven': 'oven',
        'ovens': 'oven',
        'toaster': 'toaster',
        'toasters': 'toaster',
        'sink': 'sink',
        'sinks': 'sink',
        'refrigerator': 'refrigerator',
        'refrigerators': 'refrigerator',
        'book': 'book',
        'books': 'book',
        'clock': 'clock',
        'clocks': 'clock',
        'vase': 'vase',
        'vases': 'vase',
        'scissors': 'scissors',
        'teddy bear': 'teddy bear',
        'teddy bears': 'teddy bear',
        'hair drier': 'hair drier',
        'hair driers': 'hair drier',
        'toothbrush': 'toothbrush',
        'toothbrushes': 'toothbrush',
    }

    def __init__(
        self,
        config: SolverConfig,
    ):
        self.config = config
        self.device = "cuda" if torch is not None and torch.cuda.is_available() else "cpu"
        self.model = None
        self._model_lock = RLock()
        self._ready = False
        if ULTRALYTICS_AVAILABLE:
            self._load_model()

    def _load_model(self) -> None:
        """Load YOLO model."""
        try:
            model_path = ModelDownloader.get_yolo_model_path(self.config.model_dir, self.config.yolo_model_name)
            if not model_path.exists():
                model_source = ModelDownloader.MODEL_SOURCES.get(self.config.yolo_model_name)
                if model_source:
                    logger.info(f"Downloading {self.config.yolo_model_name} to {model_path}")
                    if not ModelDownloader.download(model_source, model_path):
                        logger.warning(f"Failed to download YOLO model {self.config.yolo_model_name}. YOLO detection will be disabled.")
                        return

            with self._model_lock:
                if not self._ready:
                    self.model = YOLO(str(model_path))
                    self.model.to(self.device)
                    self._ready = True
                    logger.info(f"YOLO model loaded: {model_path}")
        except (ImportError, OSError, RuntimeError) as e:
            logger.warning(f"YOLO load failed: {e}. YOLO detection will be disabled.")
            self._ready = False

    def detect(self, image: Any, target_label: str) -> float:
        """
        Detect objects in image and return a confidence score for the target_label.
        Returns 1.0 if target_label is detected with high confidence, 0.0 otherwise.
        """
        if not self._ready or self.model is None or image is None:
            return 0.0
        
        # Normalize target_label using aliases for YOLO detection
        normalized_target_label = self.HCAPTCHA_ALIASES.get(target_label.lower(), target_label.lower())

        results = self.model(image, verbose=False, conf=self.config.yolo_confidence_threshold)
        
        for result in results:
            if result.boxes is None:
                continue
            for box in result.boxes:
                cls_id = int(box.cls[0])
                label = result.names.get(cls_id, f"class_{cls_id}").lower()
                # Check if the detected label matches the normalized target label
                if label == normalized_target_label:
                    return float(box.conf[0]) # Return YOLO's confidence if detected
        return 0.0

# =============================================================================
# SCORER MODULE (CLIP-FIRST ENSEMBLE)
# =============================================================================

class Scorer:
    """
    Ensemble scorer for CAPTCHA tiles, prioritizing CLIP.
    """

    def __init__(self, config: SolverConfig):
        self.config = config
        self.clip_model = EmbeddingModel(config)
        self.yolo_detector = YOLODetector(config)
        # Removed _metrics and _vote_history as per design

    def classify_tile(self, image: Any, target_label: str) -> ClassificationResult:
        """
        Classify a single tile using CLIP-first ensemble approach.
        """
        if image is None:
            return ClassificationResult(label=target_label, confidence=0.0, confidence_level=ConfidenceLevel.LOW)

        # 1. Multi-crop ensemble for CLIP
        crops = Preprocessor.create_crops(image)
        clip_scores = []
        for crop_name, crop_image in crops.items():
            score = self.clip_model.semantic_similarity(crop_image, target_label)
            if score > 0:
                clip_scores.append(score)
        
        avg_clip_score = sum(clip_scores) / len(clip_scores) if clip_scores else 0.0

        # 2. YOLO as secondary confidence booster
        yolo_score = self.yolo_detector.detect(image, target_label) if self.yolo_detector._ready else 0.0

        # Combine scores (CLIP-first weighting)
        # Weights: CLIP (primary) ~0.8, YOLO (booster) ~0.2
        # If YOLO detects the object, it boosts the CLIP score, otherwise it doesn't penalize.
        final_confidence = avg_clip_score
        if yolo_score > self.config.yolo_confidence_threshold:
            # Simple boosting: if YOLO is confident, give a small boost to CLIP score
            final_confidence = min(1.0, avg_clip_score + (yolo_score * 0.1)) # Boost by up to 10% of YOLO's confidence

        # Determine confidence level (adaptive thresholding will be applied later in solver)
        confidence_level = ConfidenceLevel.LOW
        if final_confidence >= self.config.clip_confidence_threshold:
            confidence_level = ConfidenceLevel.HIGH
        elif final_confidence >= (self.config.clip_confidence_threshold * 0.6): # A dynamic medium threshold
            confidence_level = ConfidenceLevel.MEDIUM

        return ClassificationResult(
            label=target_label,
            confidence=final_confidence,
            confidence_level=confidence_level,
            clip_score=avg_clip_score,
            yolo_score=yolo_score
        )

# =============================================================================
# CHALLENGE DETECTOR
# =============================================================================

class ChallengeDetector:
    """DOM-based CAPTCHA challenge detection."""

    HCAPTCHA_SELECTORS: Final[List[str]] = [
        'iframe[src*="hcaptcha.com"]',
        'iframe[src*="captcha.hcaptcha.com"]',
    ]

    PROMPT_SELECTORS: Final[List[str]] = [
        '.prompt-text',
        '.challenge-title',
        '.instruction',
        'h1',
        'div.challenge-header',
        'div.challenge-text',
    ]

    SUBMIT_SELECTORS: Final[List[str]] = [
        '.button-submit',
        '#submit-button',
        'button[type="submit"]',
        'div.button-frame > button',
    ]

    def __init__(self, config: SolverConfig):
        self.config = config

    async def detect_playwright(self, page: Any) -> Optional[ChallengeState]:
        """Detect hCaptcha challenge using Playwright."""
        state = ChallengeState()

        try:
            for selector in self.HCAPTCHA_SELECTORS:
                frame = page.locator(selector).first
                if await frame.count() > 0:
                    state.challenge_type = ChallengeType.HCAPTCHA_IMAGE_LABEL
                    content_frame = await frame.content_frame()
                    if content_frame:
                        # Wait for prompt to be visible
                        for prompt_sel in self.PROMPT_SELECTORS:
                            try:
                                await content_frame.locator(prompt_sel).wait_for(state='visible', timeout=self.config.challenge_timeout * 1000 / 2)
                                prompt_element = content_frame.locator(prompt_sel).first
                                if await prompt_element.count() > 0:
                                    state.instruction_text = await prompt_element.inner_text()
                                    state.target_label = self._extract_target(state.instruction_text)
                                    if state.target_label:
                                        break # Found prompt, break from prompt selectors loop
                            except Exception as e:
                                logger.debug(f"Prompt selector {prompt_sel} failed: {e}")
                        
                        # Get grid cells
                        cells = content_frame.locator('.task-image')
                        cell_count = await cells.count()
                        for i in range(cell_count):
                            cell_locator = cells.nth(i)
                            box = await cell_locator.bounding_box()
                            if box:
                                state.grid_cells.append(BoundingBox(
                                    x1=box['x'], y1=box['y'], x2=box['x']+box['width'], y2=box['y']+box['height']
                                ))

                    if state.target_label and state.grid_cells:
                        return state
        except Exception as e:
            logger.debug(f"Detection error: {e}")

        return None

    def detect_selenium(self, driver: Any) -> Optional[ChallengeState]:
        """Detect hCaptcha challenge using Selenium."""
        state = ChallengeState()

        try:
            for selector in self.HCAPTCHA_SELECTORS:
                frames = driver.find_elements(_By.CSS_SELECTOR, selector)
                if frames:
                    state.challenge_type = ChallengeType.HCAPTCHA_IMAGE_LABEL
                    driver.switch_to.default_content()
                    driver.switch_to.frame(frames[0])
                    try:
                        for prompt_sel in self.PROMPT_SELECTORS:
                            try:
                                prompt = driver.find_element(_By.CSS_SELECTOR, prompt_sel)
                                state.instruction_text = prompt.text
                                state.target_label = self._extract_target(state.instruction_text)
                                if state.target_label:
                                    break
                            except Exception:
                                continue
                        
                        # Get grid cells for Selenium
                        cells = driver.find_elements(_By.CSS_SELECTOR, '.task-image')
                        for cell in cells:
                            loc = cell.location
                            size = cell.size
                            state.grid_cells.append(BoundingBox(
                                x1=loc['x'], y1=loc['y'], x2=loc['x']+size['width'], y2=loc['y']+size['height']
                            ))

                    finally:
                        driver.switch_to.default_content()
                    
                    if state.target_label and state.grid_cells:
                        return state
        except Exception as e:
            logger.error(f"Detection error: {e}\n{traceback.format_exc()}")

        return None

    def _extract_target(self, prompt: str) -> str:
        """Extract target object from challenge instruction using robust patterns and aliases."""
        if not prompt:
            return ""

        prompt_lower = prompt.lower()

        # Comprehensive patterns for various hCaptcha prompt formats
        patterns = [
            r'(?:click|select) each image containing (?:a|an)?\s+([\w\s]+?)(?:\s*\.|\s*$)',
            r'(?:click|select) all images with (?:a|an)?\s+([\w\s]+?)(?:\s*\.|\s*$)',
            r'(?:click|select) on the image of (?:a|an)?\s+([\w\s]+?)(?:\s*\.|\s*$)',
            r'(?:click|select) (?:the|each) ([\w\s]+?)(?:\s*\.|\s*$)',
            r'select all squares with (?:a|an)?\s+([\w\s]+?)(?:\s*\.|\s*$)',
            r'find all images of (?:a|an)?\s+([\w\s]+?)(?:\s*\.|\s*$)',
            r'please click each image containing a ([\w\s]+?)(?:\s*\.|\s*$)',
            r'which images contain a ([\w\s]+?)(?:\s*\.|\s*$)',
        ]

        target = ""
        for pattern in patterns:
            match = re.search(pattern, prompt_lower)
            if match:
                target = match.group(1).strip().rstrip('.').replace('s$', '') # Remove plural 's'
                break
        
        if not target:
            # Fallback: check for direct matches from aliases if no pattern matched
            for alias_key, alias_value in YOLODetector.HCAPTCHA_ALIASES.items():
                if alias_key in prompt_lower:
                    target = alias_value
                    break

        # Apply alias mapping to the extracted target
        return YOLODetector.HCAPTCHA_ALIASES.get(target, target)

# =============================================================================
# BROWSER SOLVERS
# =============================================================================

class PlaywrightSolver:
    """Playwright-based CAPTCHA solver."""

    def __init__(self, config: SolverConfig):
        self.config = config
        self._scorer = Scorer(config)
        self._detector = ChallengeDetector(config)
        self._playwright = None
        self._browser = None
        self._context = None
        self._initialized = False
        self._last_action_time = 0.0

    async def __aenter__(self) -> 'PlaywrightSolver':
        await self.initialize()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    async def initialize(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise BrowserNotAvailableError(
                "Playwright not installed. Run: pip install playwright && playwright install"
            )

        if self._initialized:
            return

        try:
            self._playwright = await _async_playwright().start()
            browser_type = getattr(self._playwright, self.config.browser_type)
            args = StealthPatcher.get_stealth_args(self.config)

            if self.config.user_data_dir:
                self._context = await browser_type.launch_persistent_context(
                    user_data_dir=self.config.user_data_dir, **args
                )
            else:
                self._browser = await browser_type.launch(**args)
                self._context = await self._browser.new_context(
                    viewport={'width': self.config.viewport_width, 'height': self.config.viewport_height},
                    user_agent=random.choice(StealthPatcher.USER_AGENTS)
                )

            await self._context.add_init_script(StealthPatcher.STEALTH_SCRIPT)
            self._initialized = True
            logger.info("Playwright initialized.")

        except Exception as e:
            logger.error(f"Playwright initialization failed: {e}")
            await self._safe_close()
            raise

    async def _safe_close(self) -> None:
        """Safely close all browser resources."""
        try:
            if self._context and not getattr(self._context, 'is_closed', lambda: False)():
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

    async def _apply_rate_limit(self) -> None:
        """Apply a random delay to simulate human-like interaction."""
        delay = random.uniform(self.config.rate_limit_min_delay, self.config.rate_limit_max_delay)
        await asyncio.sleep(delay)
        self._last_action_time = time.time()

    async def solve(self, url: str) -> bool:
        """Solve CAPTCHA at given URL, handling multiple challenge rounds."""
        page = None
        try:
            page = await self._context.new_page()
            await page.goto(url, wait_until='networkidle', timeout=self.config.timeout * 1000)
            await self._apply_rate_limit()

            # Initial checkbox click
            if await self._click_checkbox(page):
                await self._apply_rate_limit()

            for round_num in range(1, self.config.max_challenge_rounds + 1):
                logger.info(f"Starting hCaptcha challenge round {round_num}/{self.config.max_challenge_rounds}")
                round_start_time = time.time()

                state = await self._detector.detect_playwright(page)
                if state is None or not state.target_label or not state.grid_cells:
                    logger.info(f"No hCaptcha challenge detected or already solved after round {round_num-1}.")
                    return True # Challenge solved or not present
                
                state.challenge_round = round_num
                logger.info(f"Challenge target for round {round_num}: '{state.target_label}'")

                if not await self._solve_image_challenge(page, state):
                    logger.warning(f"Failed to solve hCaptcha image challenge in round {round_num}.")
                    # If a round fails, it might mean the challenge is unsolvable or a new one appeared
                    # We can retry the detection to see if a new challenge is presented.
                    continue # Try next round
                
                # Ensure minimum solve time per round
                elapsed_time = time.time() - round_start_time
                if elapsed_time < self.config.min_solve_time_per_round:
                    sleep_needed = self.config.min_solve_time_per_round - elapsed_time
                    logger.info(f"Waiting for {sleep_needed:.2f} seconds to meet minimum solve time for round {round_num}.")
                    await asyncio.sleep(sleep_needed)
                
                await self._apply_rate_limit() # Apply delay after submission

                # After submission, check if challenge is still present
                # This is the retry logic for multiple rounds
                re_check_state = await self._detector.detect_playwright(page)
                if re_check_state is None or not re_check_state.target_label:
                    logger.info(f"hCaptcha challenge successfully solved after {round_num} rounds.")
                    return True
                else:
                    logger.info(f"hCaptcha challenge still present after round {round_num}. Proceeding to next round.")

            logger.error(f"Failed to solve hCaptcha after {self.config.max_challenge_rounds} rounds.")
            return False

        except asyncio.TimeoutError:
            logger.error("Page load or challenge detection timed out.")
            return False
        except Exception as e:
            logger.error(f"Solve error: {e}\n{traceback.format_exc()}")
            return False
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _click_checkbox(self, page: Any) -> bool:
        """Click the initial hCaptcha checkbox."""
        try:
            frame = None
            for selector in self._detector.HCAPTCHA_SELECTORS:
                frame = page.locator(selector).first
                if await frame.count() > 0:
                    break
            if not frame:
                return False

            content_frame = await frame.content_frame()
            if not content_frame:
                return False

            # Wait for the checkbox to be visible and enabled
            checkbox_locator = content_frame.locator('button, [role="button"]').filter(has_text=re.compile(r'I am human|I am not a robot', re.IGNORECASE)).first
            if await checkbox_locator.count() > 0:
                await checkbox_locator.wait_for(state='visible', timeout=self.config.challenge_timeout * 1000 / 2)
                await self._human_click(content_frame, checkbox_locator)
                return True
        except Exception as e:
            logger.debug(f"Error clicking checkbox: {e}")
        return False

    async def _solve_image_challenge(self, page: Any, state: ChallengeState) -> bool:
        """Solve image grid challenge using ensemble intelligence and adaptive thresholding."""
        try:
            frame = None
            for selector in self._detector.HCAPTCHA_SELECTORS:
                frame = page.locator(selector).first
                if await frame.count() > 0:
                    break
            if not frame:
                return False

            content_frame = await frame.content_frame()
            if not content_frame:
                return False

            cells = content_frame.locator('.task-image')
            cell_count = await cells.count()

            if cell_count == 0:
                logger.warning("No image tiles found for the challenge.")
                return False

            tile_scores: List[Tuple[int, float, Any]] = [] # (index, confidence, locator)
            for i in range(cell_count):
                cell = cells.nth(i)
                box = await cell.bounding_box()
                if not box:
                    continue

                screenshot = await content_frame.screenshot(
                    clip={'x': box['x'], 'y': box['y'],
                          'width': box['width'], 'height': box['height']}
                )

                img_array = np.frombuffer(screenshot, np.uint8)
                image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                if image is None:
                    logger.warning(f"Could not decode image for tile {i}.")
                    continue

                result = self._scorer.classify_tile(image, state.target_label)
                tile_scores.append((i, result.confidence, cell))
                logger.debug(f"Tile {i} - Target: '{state.target_label}', CLIP Score: {result.clip_score:.2f}, YOLO Score: {result.yolo_score:.2f}, Final Confidence: {result.confidence:.2f}")

            # Adaptive Thresholding
            confidences = [score for _, score, _ in tile_scores]
            if not confidences:
                return False

            threshold = self.config.clip_confidence_threshold # Default to config threshold

            # Check for low score scenario (all scores uniformly low)
            if max(confidences) < 0.5 and len(confidences) > 1:
                # Pick top N tiles, e.g., top 30% or at least 1
                num_to_pick = max(1, math.ceil(len(confidences) * 0.3))
                sorted_tiles = sorted(tile_scores, key=lambda x: x[1], reverse=True)
                to_click_tiles = sorted_tiles[:num_to_pick]
                logger.info(f"Adaptive thresholding: All scores low, picking top {num_to_pick} tiles.")
            else:
                # Attempt bimodal thresholding if scores are not uniformly low
                # Simple bimodal detection: look for a significant gap in sorted scores
                sorted_confidences = sorted(confidences)
                gaps = [sorted_confidences[i+1] - sorted_confidences[i] for i in range(len(sorted_confidences)-1)]
                if gaps:
                    max_gap = max(gaps)
                    if max_gap > 0.2: # A significant gap
                        gap_idx = gaps.index(max_gap)
                        threshold = (sorted_confidences[gap_idx] + sorted_confidences[gap_idx+1]) / 2
                        logger.info(f"Adaptive thresholding: Detected bimodal distribution, setting threshold to {threshold:.2f}.")
                
                to_click_tiles = [(idx, conf, cell) for idx, conf, cell in tile_scores if conf >= threshold]
                logger.info(f"Adaptive thresholding: Using threshold {threshold:.2f}, selected {len(to_click_tiles)} tiles.")

            if not to_click_tiles:
                logger.warning("No tiles selected for clicking based on confidence. Selecting highest scoring tile as fallback.")
                # Fallback: if no tiles meet the threshold, click the highest scoring one
                highest_scoring_tile = max(tile_scores, key=lambda x: x[1])
                to_click_tiles = [highest_scoring_tile]

            for idx, conf, cell_locator in to_click_tiles:
                await self._human_click(content_frame, cell_locator)
                await self._apply_rate_limit()

            submit = None
            for selector in self._detector.SUBMIT_SELECTORS:
                submit = content_frame.locator(selector).first
                if await submit.count() > 0:
                    break

            if submit:
                await self._human_click(content_frame, submit)
                await self._apply_rate_limit()
            else:
                logger.warning("Submit button not found.")
                return False

            return True

        except Exception as e:
            logger.error(f"Image solve error: {e}\n{traceback.format_exc()}")
            return False

    async def _human_click(self, frame: Any, locator: Any) -> None:
        """Simulate human-like mouse click."""
        try:
            box = await locator.bounding_box()
            if not box:
                await locator.click(timeout=5000)
                return

            x = box['x'] + random.uniform(box['width'] * 0.25, box['width'] * 0.75)
            y = box['y'] + random.uniform(box['height'] * 0.25, box['height'] * 0.75)

            steps = random.randint(8, 20) if self.config.human_like_mouse else 1
            await frame.mouse.move(x, y, steps=steps)
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await frame.mouse.down()
            await asyncio.sleep(random.uniform(0.03, 0.1))
            await frame.mouse.up()
        except Exception:
            try:
                await locator.click(force=True)
            except Exception:
                pass

    async def close(self) -> None:
        await self._safe_close()
        self._initialized = False

# =============================================================================
# SELENIUM SOLVER
# =============================================================================

class SeleniumSolver:
    """Selenium-based CAPTCHA solver with rate limiting."""

    def __init__(self, config: SolverConfig):
        self.config = config
        self._scorer = Scorer(config)
        self._detector = ChallengeDetector(config)
        self._driver = None
        self._last_action_time = 0.0

    def _apply_rate_limit(self) -> None:
        """Apply a random delay to simulate human-like interaction."""
        delay = random.uniform(self.config.rate_limit_min_delay, self.config.rate_limit_max_delay)
        time.sleep(delay)
        self._last_action_time = time.time()

    def solve(self, url: str) -> bool:
        """Solve CAPTCHA at given URL, handling multiple challenge rounds."""
        try:
            if self._driver is None:
                self._initialize_driver()
            
            self._driver.get(url)
            self._apply_rate_limit()

            # Initial checkbox click
            if self._click_checkbox():
                self._apply_rate_limit()

            for round_num in range(1, self.config.max_challenge_rounds + 1):
                logger.info(f"Starting hCaptcha challenge round {round_num}/{self.config.max_challenge_rounds}")
                round_start_time = time.time()

                state = self._detector.detect_selenium(self._driver)
                if state is None or not state.target_label or not state.grid_cells:
                    logger.info(f"No hCaptcha challenge detected or already solved after round {round_num-1}.")
                    return True # Challenge solved or not present
                
                state.challenge_round = round_num
                logger.info(f"Challenge target for round {round_num}: '{state.target_label}'")

                if not self._solve_image_challenge(state):
                    logger.warning(f"Failed to solve hCaptcha image challenge in round {round_num}.")
                    continue # Try next round
                
                # Ensure minimum solve time per round
                elapsed_time = time.time() - round_start_time
                if elapsed_time < self.config.min_solve_time_per_round:
                    sleep_needed = self.config.min_solve_time_per_round - elapsed_time
                    logger.info(f"Waiting for {sleep_needed:.2f} seconds to meet minimum solve time for round {round_num}.")
                    time.sleep(sleep_needed)
                
                self._apply_rate_limit() # Apply delay after submission

                # After submission, check if challenge is still present
                re_check_state = self._detector.detect_selenium(self._driver)
                if re_check_state is None or not re_check_state.target_label:
                    logger.info(f"hCaptcha challenge successfully solved after {round_num} rounds.")
                    return True
                else:
                    logger.info(f"hCaptcha challenge still present after round {round_num}. Proceeding to next round.")

            logger.error(f"Failed to solve hCaptcha after {self.config.max_challenge_rounds} rounds.")
            return False

        except Exception as e:
            logger.error(f"Selenium solve error: {e}\n{traceback.format_exc()}")
            return False

    def _initialize_driver(self) -> None:
        """Initialize Selenium WebDriver."""
        if not SELENIUM_AVAILABLE:
            raise BrowserNotAvailableError("Selenium not installed. Run: pip install selenium")
        
        options = StealthPatcher.get_selenium_options(self.config)
        self._driver = webdriver.Chrome(options=options)
        self._driver.set_page_load_timeout(self.config.timeout)

    def _click_checkbox(self) -> bool:
        """Click the initial hCaptcha checkbox."""
        try:
            for selector in self._detector.HCAPTCHA_SELECTORS:
                frames = self._driver.find_elements(_By.CSS_SELECTOR, selector)
                if frames:
                    self._driver.switch_to.default_content()
                    self._driver.switch_to.frame(frames[0])
                    try:
                        checkbox = self._driver.find_element(_By.CSS_SELECTOR, 'button, [role="button"]')
                        if checkbox.is_displayed() and checkbox.is_enabled():
                            self._human_click(checkbox)
                            return True
                    except Exception:
                        pass
                    finally:
                        self._driver.switch_to.default_content()
        except Exception as e:
            logger.debug(f"Error clicking checkbox (Selenium): {e}")
        return False

    def _solve_image_challenge(self, state: ChallengeState) -> bool:
        """Solve image grid challenge using ensemble intelligence and adaptive thresholding (Selenium)."""
        try:
            for selector in self._detector.HCAPTCHA_SELECTORS:
                frames = self._driver.find_elements(_By.CSS_SELECTOR, selector)
                if frames:
                    self._driver.switch_to.default_content()
                    self._driver.switch_to.frame(frames[0])
                    break
            else:
                return False

            try:
                cells = self._driver.find_elements(_By.CSS_SELECTOR, '.task-image')
                if not cells:
                    logger.warning("No image tiles found for the challenge (Selenium).")
                    return False

                tile_scores: List[Tuple[int, float, Any]] = [] # (index, confidence, element)
                for i, cell in enumerate(cells):
                    # Get screenshot of the element
                    screenshot_b64 = cell.screenshot_as_base64
                    img_array = np.frombuffer(base64.b64decode(screenshot_b64), np.uint8)
                    image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                    if image is None:
                        logger.warning(f"Could not decode image for tile {i} (Selenium).")
                        continue

                    result = self._scorer.classify_tile(image, state.target_label)
                    tile_scores.append((i, result.confidence, cell))
                    logger.debug(f"Tile {i} (Selenium) - Target: '{state.target_label}', CLIP Score: {result.clip_score:.2f}, YOLO Score: {result.yolo_score:.2f}, Final Confidence: {result.confidence:.2f}")

                # Adaptive Thresholding (same logic as PlaywrightSolver)
                confidences = [score for _, score, _ in tile_scores]
                if not confidences:
                    return False

                threshold = self.config.clip_confidence_threshold # Default to config threshold

                if max(confidences) < 0.5 and len(confidences) > 1:
                    num_to_pick = max(1, math.ceil(len(confidences) * 0.3))
                    sorted_tiles = sorted(tile_scores, key=lambda x: x[1], reverse=True)
                    to_click_tiles = sorted_tiles[:num_to_pick]
                    logger.info(f"Adaptive thresholding (Selenium): All scores low, picking top {num_to_pick} tiles.")
                else:
                    sorted_confidences = sorted(confidences)
                    gaps = [sorted_confidences[i+1] - sorted_confidences[i] for i in range(len(sorted_confidences)-1)]
                    if gaps:
                        max_gap = max(gaps)
                        if max_gap > 0.2:
                            gap_idx = gaps.index(max_gap)
                            threshold = (sorted_confidences[gap_idx] + sorted_confidences[gap_idx+1]) / 2
                            logger.info(f"Adaptive thresholding (Selenium): Detected bimodal distribution, setting threshold to {threshold:.2f}.")
                    
                    to_click_tiles = [(idx, conf, cell) for idx, conf, cell in tile_scores if conf >= threshold]
                    logger.info(f"Adaptive thresholding (Selenium): Using threshold {threshold:.2f}, selected {len(to_click_tiles)} tiles.")

                if not to_click_tiles:
                    logger.warning("No tiles selected for clicking based on confidence (Selenium). Selecting highest scoring tile as fallback.")
                    highest_scoring_tile = max(tile_scores, key=lambda x: x[1])
                    to_click_tiles = [highest_scoring_tile]

                for idx, conf, cell_element in to_click_tiles:
                    self._human_click(cell_element)
                    self._apply_rate_limit()

                submit = None
                for selector in self._detector.SUBMIT_SELECTORS:
                    try:
                        submit = self._driver.find_element(_By.CSS_SELECTOR, selector)
                        if submit.is_displayed() and submit.is_enabled():
                            break
                    except Exception:
                        continue

                if submit:
                    self._human_click(submit)
                    self._apply_rate_limit()
                else:
                    logger.warning("Submit button not found (Selenium).")
                    return False

                return True
            finally:
                self._driver.switch_to.default_content()

        except Exception as e:
            logger.error(f"Image solve error (Selenium): {e}\n{traceback.format_exc()}")
            return False

    def _human_click(self, element: Any) -> None:
        """Simulate human-like mouse click for Selenium."""
        try:
            if self.config.human_like_mouse and _ActionChains is not None:
                action = _ActionChains(self._driver)
                # Move to element center first
                action.move_to_element(element).perform()
                # Random offset within element
                size = element.size
                x_offset = random.uniform(size['width'] * 0.25, size['width'] * 0.75) - size['width'] / 2
                y_offset = random.uniform(size['height'] * 0.25, size['height'] * 0.75) - size['height'] / 2
                action.move_by_offset(x_offset, y_offset).click().perform()
            else:
                element.click()
        except Exception:
            element.click() # Fallback to direct click

    def close(self) -> None:
        """Clean up Selenium driver."""
        if self._driver:
            try:
                self._driver.quit()
            except Exception as e:
                logger.warning(f"Error closing Selenium driver: {e}")
            self._driver = None

# =============================================================================
# STEALTH PATCHER
# =============================================================================

class StealthPatcher:
    """Provides browser stealth scripts and arguments."""

    STEALTH_SCRIPT: Final[str] = """
    (() => {
        // Override webdriver property
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        
        // Override plugins array
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        
        // Override languages
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        
        // Mock Chrome properties
        window.chrome = { runtime: {}, autocomplete: {} };
        
        // Override WebGL vendor/renderer
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            const vendorMap = {
                37445: 'Intel Inc.',
                37446: 'Intel Iris OpenGL Engine',
            };
            return vendorMap[parameter] || getParameter.call(this, parameter);
        };
        
        // Override canvas fingerprint
        const toDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function() {
            return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQ42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
        };
        
        // Override WebGL debug renderer info
        const getExtension = WebGLRenderingContext.prototype.getExtension;
        WebGLRenderingContext.prototype.getExtension = function(name) {
            if (name === 'WEBGL_debug_renderer_info') {
                return {
                    UNMASKED_VENDOR_WEBGL: 37445,
                    UNMASKED_RENDERER_WEBGL: 37446,
                };
            }
            return getExtension.call(this, name);
        };
        
        // Spoof touch capabilities
        window.navigator.__defineGetter__('maxTouchPoints', () => 5);
        
        // Override audio context fingerprint
        const originalDecode = AudioContext.prototype.decodeAudioData;
        AudioContext.prototype.decodeAudioData = function() {
            return Promise.resolve();
        };
        
        // Spoof media devices
        navigator.mediaDevices = {
            enumerateDevices: () => Promise.resolve([]),
            getDisplayMedia: () => Promise.reject(new Error('Not allowed')),
            getUserMedia: () => Promise.reject(new Error('Not allowed')),
        };
    })()
    """

    USER_AGENTS: Final[List[str]] = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]

    @staticmethod
    def get_stealth_args(config: SolverConfig) -> Dict[str, Any]:
        """Generate stealth launch arguments for Playwright."""
        viewport = [config.viewport_width, config.viewport_height]
        if config.viewport_jitter:
            viewport[0] += random.randint(-30, 30)
            viewport[1] += random.randint(-20, 20)

        return {
            'headless': config.headless,
            'args': [
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                f'--window-size={viewport[0]},{viewport[1]}',
                '--disable-webgl',
                '--disable-features=IsolateOrigins,site-per-process',
            ],
            'ignore_default_args': ['--enable-automation'],
        }

    @staticmethod
    def get_selenium_options(config: SolverConfig) -> Any:
        """Generate stealth Chrome options for Selenium."""
        if not SELENIUM_AVAILABLE:
            raise BrowserNotAvailableError("Selenium not installed. Run: pip install selenium")
        if _Options is None:
            raise BrowserNotAvailableError("Selenium Chrome options not available")
        
        options = _Options()
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-setuid-sandbox')
        options.add_argument(f'--window-size={config.viewport_width},{config.viewport_height}')
        options.add_argument(f'--user-agent={random.choice(StealthPatcher.USER_AGENTS)}')
        options.add_argument('--disable-webgl')
        options.add_argument('--disable-features=IsolateOrigins,site-per-process')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        return options

# =============================================================================
# GOD SOLVER (PUBLIC API)
# =============================================================================

class GodSolver:
    """
    Unified CAPTCHA solver interface.
    """

    def __init__(self, config: Optional[SolverConfig] = None):
        self.config = config if config else SolverConfig()
        self._playwright_solver: Optional[PlaywrightSolver] = None
        self._selenium_solver: Optional[SeleniumSolver] = None

    async def solve(self, url: str, backend: BackendType = BackendType.AUTO) -> bool:
        """Solve CAPTCHA at the given URL using the specified backend."""
        if backend == BackendType.AUTO:
            if PLAYWRIGHT_AVAILABLE:
                backend = BackendType.PLAYWRIGHT
            elif SELENIUM_AVAILABLE:
                backend = BackendType.SELENIUM
            else:
                raise BrowserNotAvailableError("No browser automation backend available. Install Playwright or Selenium.")

        if backend == BackendType.PLAYWRIGHT:
            if not self._playwright_solver:
                self._playwright_solver = PlaywrightSolver(self.config)
                await self._playwright_solver.initialize()
            return await self._playwright_solver.solve(url)
        elif backend == BackendType.SELENIUM:
            if not self._selenium_solver:
                self._selenium_solver = SeleniumSolver(self.config)
            return self._selenium_solver.solve(url)
        else:
            raise ValueError(f"Unsupported backend type: {backend}")

    async def close(self) -> None:
        """Close all active solver sessions."""
        if self._playwright_solver:
            await self._playwright_solver.close()
        if self._selenium_solver:
            self._selenium_solver.close()

# Removed SliderSolver, ShapeMatcher, ObjectAlignmentSolver, Trainer as per design

# =============================================================================
# CLI ENTRY POINT
# =============================================================================

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="hCaptcha Solver CLI")
    parser.add_argument("--url", type=str, required=True, help="URL to solve hCaptcha on")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--browser", type=str, default="chromium", choices=["chromium", "firefox", "webkit"], help="Browser type")
    parser.add_argument("--backend", type=str, default="auto", choices=["auto", "playwright", "selenium"], help="Automation backend")
    parser.add_argument("--max-rounds", type=int, default=4, help="Maximum challenge rounds to attempt")
    parser.add_argument("--min-solve-time", type=float, default=8.0, help="Minimum solve time per round in seconds")
    parser.add_argument("--clip-threshold", type=float, default=0.75, help="CLIP confidence threshold")
    parser.add_argument("--yolo-threshold", type=float, default=0.25, help="YOLO confidence threshold (if YOLO is enabled)")
    parser.add_argument("--model-dir", type=str, help="Directory to store models")

    args = parser.parse_args()

    config = SolverConfig(
        headless=args.headless,
        browser_type=args.browser,
        max_challenge_rounds=args.max_rounds,
        min_solve_time_per_round=args.min_solve_time,
        clip_confidence_threshold=args.clip_threshold,
        yolo_confidence_threshold=args.yolo_threshold,
        model_dir=Path(args.model_dir) if args.model_dir else None
    )

    solver = GodSolver(config)
    try:
        logger.info(f"Attempting to solve hCaptcha on {args.url} using {args.backend} backend...")
        success = await solver.solve(args.url, BackendType[args.backend.upper()])
        if success:
            logger.info("hCaptcha solved successfully!")
        else:
            logger.error("Failed to solve hCaptcha.")
        return 0 if success else 1
    except CaptchaHubError as e:
        logger.critical(f"Solver error: {e}")
        return 1
    except Exception as e:
        logger.critical(f"An unexpected error occurred: {e}\n{traceback.format_exc()}")
        return 1
    finally:
        await solver.close()

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
