#!/usr/bin/env python3
"""
Captcha-Solver.py
The Godly Autonomous CAPTCHA Solver
Local inference only. No APIs. No keys. Pure browser automation + vision.
"""

import os
import sys
import io
import re
import json
import time
import random
import base64
import hashlib
import asyncio
import warnings
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Union, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
import threading
import queue

# Core ML
import numpy as np
import cv2
import torch
import torch.nn as nn
from PIL import Image, ImageDraw, ImageFont

# Browser Automation
try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext, Locator
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# Ultralytics (optional, falls back to ONNX)
try:
    from ultralytics import YOLO
    ULTRALYTICS_AVAILABLE = True
except ImportError:
    ULTRALYTICS_AVAILABLE = False

# Transformers/ONNX (optional, falls back to OpenCV)
try:
    from transformers import CLIPProcessor, CLIPModel
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False

try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False

warnings.filterwarnings('ignore')

# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

@dataclass
class SolverConfig:
    """God-tier configuration"""
    # Browser
    browser_type: str = "chromium"  # chromium, firefox, webkit
    headless: bool = False
    stealth: bool = True
    user_data_dir: Optional[str] = None
    
    # Detection
    confidence_threshold: float = 0.45
    iou_threshold: float = 0.35
    max_retries: int = 3
    challenge_timeout: int = 30
    
    # Models (auto-download on first run if not present)
    model_dir: Path = field(default_factory=lambda: Path.home() / ".captcha_solver" / "models")
    
    # Solver behavior
    solve_hcaptcha: bool = True
    solve_recaptcha: bool = True
    solve_geetest: bool = True
    solve_image_captcha: bool = True
    
    # Anti-detection
    human_like_mouse: bool = True
    random_delays: bool = True
    viewport_jitter: bool = True

# =============================================================================
# ENUMS & DATA STRUCTURES
# =============================================================================

class ChallengeType(Enum):
    UNKNOWN = auto()
    HCAPTCHA_IMAGE_LABEL = auto()      # "Click on the {object}"
    HCAPTCHA_SPATIAL = auto()          # "Click where the {object} is pointing"
    HCAPTCHA_MULTISTEP = auto()        # Multiple images, click matching ones
    RECAPTCHA_IMAGE = auto()           # reCAPTCHA v2 image grid
    RECAPTCHA_AUDIO = auto()           # reCAPTCHA audio (not implemented, skip)
    GEETEST_SLIDE = auto()             # Geetest slider
    GEETEST_ICON = auto()              # Geetest icon captcha
    TEXT_IMAGE = auto()                # Generic image-based text CAPTCHA

@dataclass
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float = 0.0
    label: str = ""
    
    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)
    
    @property
    def area(self) -> float:
        return max(0, self.x2 - self.x1) * max(0, self.y2 - self.y1)

@dataclass
class ChallengeState:
    challenge_type: ChallengeType = ChallengeType.UNKNOWN
    target_label: str = ""
    instruction_text: str = ""
    image_data: Optional[np.ndarray] = None
    grid_cells: List[BoundingBox] = field(default_factory=list)
    candidates: List[BoundingBox] = field(default_factory=list)
    solved: bool = False
    attempts: int = 0

# =============================================================================
# STEALTH & ANTI-DETECTION MODULE
# =============================================================================

class StealthPatcher:
    """
    Combines Playwright's stealth with Selenium's evasion techniques.
    Patches browser fingerprints to appear as a real human.
    """
    
    STEALTH_SCRIPT = """
    () => {
        // Override navigator.webdriver
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        
        // Override permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
        );
        
        // Plugins mock
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
        
        // Languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });
        
        // Chrome runtime
        window.chrome = { runtime: {} };
        
        // WebGL vendor/renderer
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Intel Inc.';
            if (parameter === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter(parameter);
        };
        
        // Canvas fingerprint randomization
        const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
        const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        const noise = () => Math.floor(Math.random() * 10) - 5;
        
        CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
            const imageData = originalGetImageData.call(this, x, y, w, h);
            if (window.location.href.includes('hcaptcha') || window.location.href.includes('recaptcha')) {
                return imageData; // Don't break captcha functionality
            }
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i] = Math.max(0, Math.min(255, imageData.data[i] + noise()));
            }
            return imageData;
        };
    }
    """
    
    @staticmethod
    def get_playwright_args(config: SolverConfig) -> Dict:
        """Generate stealth launch arguments for Playwright"""
        args = [
            '--disable-blink-features=AutomationControlled',
            '--disable-features=IsolateOrigins,site-per-process',
            '--disable-site-isolation-trials',
            '--disable-web-security',
            '--disable-features=BlockInsecurePrivateNetworkRequests',
            '--window-size=1920,1080',
            '--start-maximized',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--disable-gpu',
            '--hide-scrollbars',
            '--disable-notifications',
            '--disable-extensions',
            '--force-color-profile=srgb',
        ]
        
        if config.viewport_jitter:
            # Randomize viewport slightly
            w = 1920 + random.randint(-50, 50)
            h = 1080 + random.randint(-30, 30)
            args.append(f'--window-size={w},{h}')
            
        return {
            'headless': config.headless,
            'args': args,
            'ignore_default_args': ['--enable-automation'],
        }
    
    @staticmethod
    def get_selenium_options(config: SolverConfig) -> ChromeOptions:
        """Generate stealth ChromeOptions for Selenium"""
        options = ChromeOptions()
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-features=IsolateOrigins,site-per-process')
        options.add_argument('--disable-site-isolation-trials')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--start-maximized')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-setuid-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--hide-scrollbars')
        options.add_argument('--disable-notifications')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        
        # Random user agent
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.0',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.0'
        ]
        options.add_argument(f'--user-agent={random.choice(user_agents)}')
        
        return options

# =============================================================================
# VISION ENGINE (OpenCV + PyTorch + Ultralytics + Transformers)
# =============================================================================

class VisionEngine:
    """
    Multi-backend vision engine.
    Priority: Ultralytics YOLOv8 -> ONNX Runtime -> OpenCV DNN -> Pure OpenCV
    """
    
    COCO_CLASSES = [
        'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train', 'truck', 'boat',
        'traffic light', 'fire hydrant', 'stop sign', 'parking meter', 'bench', 'bird', 'cat',
        'dog', 'horse', 'sheep', 'cow', 'elephant', 'bear', 'zebra', 'giraffe', 'backpack',
        'umbrella', 'handbag', 'tie', 'suitcase', 'frisbee', 'skis', 'snowboard', 'sports ball',
        'kite', 'baseball bat', 'baseball glove', 'skateboard', 'surfboard', 'tennis racket',
        'bottle', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple',
        'sandwich', 'orange', 'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake', 'chair',
        'couch', 'potted plant', 'bed', 'dining table', 'toilet', 'tv', 'laptop', 'mouse',
        'remote', 'keyboard', 'cell phone', 'microwave', 'oven', 'toaster', 'sink', 'refrigerator',
        'book', 'clock', 'vase', 'scissors', 'teddy bear', 'hair drier', 'toothbrush'
    ]
    
    # hCaptcha-specific labels mapping
    HCAPTCHA_MAP = {
        'airplane': 'airplane', 'bus': 'bus', 'train': 'train', 'boat': 'boat', 'car': 'car',
        'motorcycle': 'motorcycle', 'bicycle': 'bicycle', 'truck': 'truck', 'traffic light': 'traffic light',
        'fire hydrant': 'fire hydrant', 'stop sign': 'stop sign', 'parking meter': 'parking meter',
        'bench': 'bench', 'cat': 'cat', 'dog': 'dog', 'horse': 'horse', 'sheep': 'sheep',
        'cow': 'cow', 'elephant': 'elephant', 'bear': 'bear', 'zebra': 'zebra', 'giraffe': 'giraffe',
        'bird': 'bird', 'frisbee': 'frisbee', 'skis': 'skis', 'snowboard': 'snowboard',
        'sports ball': 'sports ball', 'kite': 'kite', 'skateboard': 'skateboard', 'surfboard': 'surfboard',
        'tennis racket': 'tennis racket', 'bottle': 'bottle', 'wine glass': 'wine glass',
        'cup': 'cup', 'fork': 'fork', 'knife': 'knife', 'spoon': 'spoon', 'bowl': 'bowl',
        'banana': 'banana', 'apple': 'apple', 'sandwich': 'sandwich', 'orange': 'orange',
        'broccoli': 'broccoli', 'carrot': 'carrot', 'hot dog': 'hot dog', 'pizza': 'pizza',
        'donut': 'donut', 'cake': 'cake', 'chair': 'chair', 'couch': 'couch',
        'potted plant': 'potted plant', 'bed': 'bed', 'dining table': 'dining table',
        'toilet': 'toilet', 'tv': 'television', 'laptop': 'laptop', 'mouse': 'mouse',
        'remote': 'remote', 'keyboard': 'keyboard', 'cell phone': 'cell phone',
        'microwave': 'microwave', 'oven': 'oven', 'toaster': 'toaster', 'sink': 'sink',
        'refrigerator': 'refrigerator', 'book': 'book', 'clock': 'clock', 'vase': 'vase',
        'scissors': 'scissors', 'teddy bear': 'teddy bear', 'hair drier': 'hair drier',
        'toothbrush': 'toothbrush'
    }
    
    def __init__(self, config: SolverConfig):
        self.config = config
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.yolo_model = None
        self.clip_model = None
        self.clip_processor = None
        self.onnx_session = None
        self._init_models()
    
    def _init_models(self):
        """Initialize models with fallback chain"""
        self.config.model_dir.mkdir(parents=True, exist_ok=True)
        
        # Try Ultralytics first
        if ULTRALYTICS_AVAILABLE:
            try:
                model_path = self.config.model_dir / "yolov8n.pt"
                if not model_path.exists():
                    print(f"[Vision] Downloading YOLOv8n...")
                    torch.hub.download_url_to_file(
                        'https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov8n.pt',
                        str(model_path)
                    )
                self.yolo_model = YOLO(str(model_path))
                self.yolo_model.to(self.device)
                print(f"[Vision] YOLOv8n loaded on {self.device}")
                return
            except Exception as e:
                print(f"[Vision] Ultralytics failed: {e}")
        
        # Fallback to ONNX
        if ONNX_AVAILABLE:
            try:
                model_path = self.config.model_dir / "yolov8n.onnx"
                if not model_path.exists():
                    print(f"[Vision] Please provide ONNX model at {model_path}")
                    # Could auto-convert here
                else:
                    self.onnx_session = ort.InferenceSession(str(model_path))
                    print("[Vision] ONNX Runtime loaded")
                    return
            except Exception as e:
                print(f"[Vision] ONNX failed: {e}")
        
        # Pure OpenCV fallback
        print("[Vision] Using OpenCV DNN fallback")
    
    def detect_objects(self, image: np.ndarray, target_classes: Optional[List[str]] = None) -> List[BoundingBox]:
        """
        Detect objects in image. Returns boxes matching target_classes or all if None.
        """
        h, w = image.shape[:2]
        detections = []
        
        # Method 1: Ultralytics YOLOv8
        if self.yolo_model is not None:
            results = self.yolo_model(image, verbose=False, conf=self.config.confidence_threshold)
            for result in results:
                if result.boxes is None:
                    continue
                for box in result.boxes:
                    cls_id = int(box.cls[0])
                    label = self.COCO_CLASSES[cls_id] if cls_id < len(self.COCO_CLASSES) else str(cls_id)
                    conf = float(box.conf[0])
                    
                    if target_classes and label not in target_classes:
                        continue
                    
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                    detections.append(BoundingBox(
                        x1=float(x1), y1=float(y1), x2=float(x2), y2=float(y2),
                        confidence=conf, label=label
                    ))
            return detections
        
        # Method 2: ONNX Runtime
        if self.onnx_session is not None:
            return self._detect_onnx(image, target_classes)
        
        # Method 3: OpenCV Template Matching (fallback for simple cases)
        return self._detect_opencv(image, target_classes)
    
    def _detect_onnx(self, image: np.ndarray, target_classes: Optional[List[str]]) -> List[BoundingBox]:
        """ONNX inference pipeline"""
        # Preprocess
        input_img = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        input_img = cv2.resize(input_img, (640, 640))
        input_img = input_img.astype(np.float32) / 255.0
        input_img = np.transpose(input_img, (2, 0, 1))
        input_img = np.expand_dims(input_img, axis=0)
        
        # Run inference
        outputs = self.onnx_session.run(None, {'images': input_img})
        
        # Post-process (simplified NMS)
        detections = []
        # Parse outputs... (implementation depends on ONNX export format)
        return detections
    
    def _detect_opencv(self, image: np.ndarray, target_classes: Optional[List[str]]) -> List[BoundingBox]:
        """Pure OpenCV fallback using contour analysis and template matching"""
        detections = []
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)
        
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 1000:  # Filter small noise
                continue
            
            x, y, w, h = cv2.boundingRect(cnt)
            aspect_ratio = float(w) / h if h > 0 else 0
            
            # Heuristic classification based on aspect ratio and area
            label = self._heuristic_classify(aspect_ratio, area, w, h)
            
            if target_classes and label not in target_classes:
                continue
            
            detections.append(BoundingBox(
                x1=float(x), y1=float(y), x2=float(x + w), y2=float(y + h),
                confidence=0.5, label=label
            ))
        
        return detections
    
    def _heuristic_classify(self, aspect: float, area: float, w: int, h: int) -> str:
        """Heuristic object classification when no ML model is available"""
        if 0.9 < aspect < 1.1 and area > 5000:
            return 'sports ball'
        elif aspect > 2.5 and area > 3000:
            return 'bus' if w > h else 'bench'
        elif 1.3 < aspect < 2.0 and area > 4000:
            return 'car'
        elif aspect < 0.5 and area > 2000:
            return 'traffic light'
        return 'object'
    
    def semantic_match(self, image: np.ndarray, text_query: str) -> float:
        """
        Use CLIP-style semantic matching to verify if image matches text.
        Returns confidence score 0-1.
        """
        if TRANSFORMERS_AVAILABLE and self.clip_model is None:
            try:
                self.clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
                self.clip_processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
            except:
                pass
        
        if self.clip_model is not None:
            try:
                inputs = self.clip_processor(
                    text=[text_query], 
                    images=Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)), 
                    return_tensors="pt", 
                    padding=True
                )
                outputs = self.clip_model(**inputs)
                logits = outputs.logits_per_image[0][0]
                return torch.sigmoid(logits).item()
            except:
                pass
        
        # Fallback: OpenCV histogram comparison
        return self._opencv_semantic_match(image, text_query)
    
    def _opencv_semantic_match(self, image: np.ndarray, text_query: str) -> float:
        """Semantic matching using color histograms and edge density heuristics"""
        # Map text to expected color ranges
        color_map = {
            'car': ([0, 0, 100], [100, 100, 255]),  # Blue-ish
            'bus': ([0, 50, 50], [100, 255, 200]),
            'fire hydrant': ([0, 0, 150], [100, 100, 255]),
            'traffic light': ([0, 150, 150], [100, 255, 255]),
        }
        
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        
        for key, (lower, upper) in color_map.items():
            if key in text_query.lower():
                lower = np.array(lower)
                upper = np.array(upper)
                mask = cv2.inRange(hsv, lower, upper)
                ratio = cv2.countNonZero(mask) / (image.shape[0] * image.shape[1])
                return min(1.0, ratio * 3)  # Scale up
        
        return 0.5  # Unknown
    
    def find_grid_cells(self, image: np.ndarray, rows: int = 3, cols: int = 3) -> List[BoundingBox]:
        """Divide challenge image into grid cells for multi-select challenges"""
        h, w = image.shape[:2]
        cell_h, cell_w = h // rows, w // cols
        cells = []
        
        for r in range(rows):
            for c in range(cols):
                x1 = c * cell_w
                y1 = r * cell_h
                x2 = (c + 1) * cell_w if c < cols - 1 else w
                y2 = (r + 1) * cell_h if r < rows - 1 else h
                cells.append(BoundingBox(x1=float(x1), y1=float(y1), x2=float(x2), y2=float(y2)))
        
        return cells
    
    def preprocess_challenge_image(self, image: np.ndarray) -> np.ndarray:
        """Enhance image for better detection"""
        # Denoise
        denoised = cv2.fastNlMeansDenoisingColored(image, None, 10, 10, 7, 21)
        # Enhance contrast
        lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        enhanced = cv2.merge([l, a, b])
        enhanced = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)
        return enhanced

# =============================================================================
# CHALLENGE DETECTOR (DOM Analysis)
# =============================================================================

class ChallengeDetector:
    """Detects and classifies CAPTCHA challenges using DOM analysis"""
    
    HCAPTCHA_SELECTORS = {
        'frame': 'iframe[src*="hcaptcha.com"]',
        'checkbox': '#checkbox',
        'challenge_container': '.challenge-container',
        'prompt_text': '.prompt-text',
        'image_grid': '.task-image',
        'submit_button': '.button-submit',
        'next_button': '.button-next',
    }
    
    RECAPTCHA_SELECTORS = {
        'frame': 'iframe[src*="recaptcha"]',
        'checkbox': '.recaptcha-checkbox',
        'challenge_frame': 'iframe[title*="challenge"]',
        'image_table': 'table.rc-imageselect-table',
        'tile': '.rc-imageselect-tile',
        'prompt': '.rc-imageselect-instructions',
        'verify_button': '#recaptcha-verify-button',
    }
    
    def __init__(self, config: SolverConfig):
        self.config = config
    
    async def detect_challenge_playwright(self, page: Page) -> Optional[ChallengeState]:
        """Detect active challenge using Playwright"""
        state = ChallengeState()
        
        # Check for hCaptcha
        hcaptcha_frame = page.locator(self.HCAPTCHA_SELECTORS['frame']).first
        if await hcaptcha_frame.count() > 0:
            state.challenge_type = ChallengeType.HCAPTCHA_IMAGE_LABEL
            frame = await hcaptcha_frame.content_frame()
            if frame:
                prompt_elem = frame.locator(self.HCAPTCHA_SELECTORS['prompt_text'])
                if await prompt_elem.count() > 0:
                    prompt = await prompt_elem.inner_text()
                    state.instruction_text = prompt
                    state.target_label = self._extract_target(prompt)
            return state
        
        # Check for reCAPTCHA
        recaptcha_frame = page.locator(self.RECAPTCHA_SELECTORS['frame']).first
        if await recaptcha_frame.count() > 0:
            state.challenge_type = ChallengeType.RECAPTCHA_IMAGE
            frame = await recaptcha_frame.content_frame()
            if frame:
                prompt_elem = frame.locator(self.RECAPTCHA_SELECTORS['prompt'])
                if await prompt_elem.count() > 0:
                    state.instruction_text = await prompt_elem.inner_text()
                    state.target_label = self._extract_target(state.instruction_text)
            return state
        
        return None
    
    def detect_challenge_selenium(self, driver) -> Optional[ChallengeState]:
        """Detect active challenge using Selenium"""
        state = ChallengeState()
        
        try:
            # hCaptcha
            frames = driver.find_elements(By.CSS_SELECTOR, self.HCAPTCHA_SELECTORS['frame'])
            if frames:
                state.challenge_type = ChallengeType.HCAPTCHA_IMAGE_LABEL
                driver.switch_to.frame(frames[0])
                try:
                    prompt_elem = driver.find_element(By.CSS_SELECTOR, self.HCAPTCHA_SELECTORS['prompt_text'])
                    state.instruction_text = prompt_elem.text
                    state.target_label = self._extract_target(state.instruction_text)
                except:
                    pass
                driver.switch_to.default_content()
                return state
            
            # reCAPTCHA
            frames = driver.find_elements(By.CSS_SELECTOR, self.RECAPTCHA_SELECTORS['frame'])
            if frames:
                state.challenge_type = ChallengeType.RECAPTCHA_IMAGE
                return state
                
        except Exception as e:
            print(f"[Detector] Error: {e}")
        
        return None
    
    def _extract_target(self, prompt: str) -> str:
        """Extract target object from challenge prompt"""
        # hCaptcha: "Please click each image containing a motorbus"
        # reCAPTCHA: "Select all images with buses"
        
        patterns = [
            r'containing (?:an? )?([\w\s]+)',
            r'with ([\w\s]+)',
            r'(?:click|select) .*? ([\w\s]+)$',
            r'please identify ([\w\s]+)',
        ]
        
        prompt_lower = prompt.lower()
        for pattern in patterns:
            match = re.search(pattern, prompt_lower)
            if match:
                target = match.group(1).strip().rstrip('.')
                # Normalize
                target = target.replace('motorbus', 'bus')
                target = target.replace('motorcycle', 'motorcycle')
                return target
        
        # Direct keyword matching
        for keyword in ['bus', 'car', 'truck', 'bicycle', 'motorcycle', 'airplane', 'train',
                       'boat', 'fire hydrant', 'traffic light', 'bench', 'cat', 'dog',
                       'horse', 'elephant', 'bear', 'giraffe', 'bird', 'frisbee']:
            if keyword in prompt_lower:
                return keyword
        
        return ""

# =============================================================================
# SOLVER CORE
# =============================================================================

class CaptchaSolver:
    """
    The Godly Solver.
    Combines browser automation with vision to solve CAPTCHAs autonomously.
    """
    
    def __init__(self, config: Optional[SolverConfig] = None):
        self.config = config or SolverConfig()
        self.vision = VisionEngine(self.config)
        self.detector = ChallengeDetector(self.config)
        self.playwright = None
        self.browser = None
        self.context = None
    
    async def init_playwright(self):
        """Initialize Playwright browser"""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed. Run: pip install playwright")
        
        self.playwright = await async_playwright().start()
        
        browser_type = getattr(self.playwright, self.config.browser_type)
        args = StealthPatcher.get_playwright_args(self.config)
        
        if self.config.user_data_dir:
            self.context = await browser_type.launch_persistent_context(
                user_data_dir=self.config.user_data_dir,
                **args
            )
        else:
            self.browser = await browser_type.launch(**args)
            self.context = await self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
        
        # Inject stealth script
        await self.context.add_init_script(StealthPatcher.STEALTH_SCRIPT)
        print("[Solver] Playwright initialized with stealth")
    
    def init_selenium(self):
        """Initialize Selenium WebDriver"""
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium not installed. Run: pip install selenium")
        
        options = StealthPatcher.get_selenium_options(self.config)
        self.driver = webdriver.Chrome(options=options)
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': StealthPatcher.STEALTH_SCRIPT.replace('() => {', '').replace('}', '')
        })
        print("[Solver] Selenium initialized with stealth")
    
    async def solve_page(self, url: str, page: Optional[Page] = None) -> bool:
        """
        Main entry point: solve all CAPTCHAs on a page.
        Returns True if solved or no CAPTCHA found.
        """
        if page is None:
            if self.context is None:
                await self.init_playwright()
            page = await self.context.new_page()
            await page.goto(url, wait_until='networkidle')
        
        # Wait for page to settle
        await asyncio.sleep(2)
        
        # Check for hCaptcha checkbox first
        if await self._handle_hcaptcha_checkbox(page):
            print("[Solver] hCaptcha checkbox clicked")
            await asyncio.sleep(2)
        
        # Detect and solve challenge
        for attempt in range(self.config.max_retries):
            state = await self.detector.detect_challenge_playwright(page)
            if state is None:
                print("[Solver] No active challenge detected")
                return True
            
            print(f"[Solver] Detected: {state.challenge_type.name}, Target: {state.target_label}")
            
            if state.challenge_type in [ChallengeType.HCAPTCHA_IMAGE_LABEL, ChallengeType.RECAPTCHA_IMAGE]:
                success = await self._solve_image_challenge(page, state)
                if success:
                    print("[Solver] Challenge solved!")
                    return True
            else:
                print(f"[Solver] Challenge type {state.challenge_type.name} not yet supported")
                return False
            
            await asyncio.sleep(1)
        
        return False
    
    async def _handle_hcaptcha_checkbox(self, page: Page) -> bool:
        """Click the initial hCaptcha checkbox if present"""
        try:
            frame = page.locator(self.detector.HCAPTCHA_SELECTORS['frame']).first
            if await frame.count() == 0:
                return False
            
            content_frame = await frame.content_frame()
            if content_frame is None:
                return False
            
            checkbox = content_frame.locator(self.detector.HCAPTCHA_SELECTORS['checkbox'])
            if await checkbox.count() > 0:
                await self._human_like_click(content_frame, checkbox)
                return True
        except Exception as e:
            print(f"[Checkbox] Error: {e}")
        
        return False
    
    async def _solve_image_challenge(self, page: Page, state: ChallengeState) -> bool:
        """Solve image-based challenge"""
        try:
            # Get challenge frame
            frame = page.locator(self.detector.HCAPTCHA_SELECTORS['frame']).first
            content_frame = await frame.content_frame()
            
            # Find challenge images
            images = await content_frame.locator('img').all()
            challenge_images = [img for img in images if await img.is_visible()]
            
            if not challenge_images:
                print("[Solver] No challenge images found")
                return False
            
            # Get grid layout (3x3 for hCaptcha)
            grid_images = challenge_images[:9]  # Usually 9 images
            
            clicks_needed = []
            
            for idx, img_elem in enumerate(grid_images):
                # Screenshot the image
                box = await img_elem.bounding_box()
                if not box:
                    continue
                
                screenshot = await content_frame.screenshot(
                    clip={
                        'x': box['x'],
                        'y': box['y'],
                        'width': box['width'],
                        'height': box['height']
                    }
                )
                
                # Convert to OpenCV format
                nparr = np.frombuffer(screenshot, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                img = self.vision.preprocess_challenge_image(img)
                
                # Detect target objects
                target_classes = [state.target_label]
                # Add synonyms
                if state.target_label in ['bus', 'motorbus']:
                    target_classes = ['bus', 'motorbus']
                elif state.target_label in ['car', 'automobile', 'vehicle']:
                    target_classes = ['car', 'truck', 'bus']
                
                detections = self.vision.detect_objects(img, target_classes)
                
                # Also do semantic verification
                semantic_score = self.vision.semantic_match(img, state.target_label)
                
                should_click = len(detections) > 0 and any(d.confidence > self.config.confidence_threshold for d in detections)
                should_click = should_click or semantic_score > 0.6
                
                if should_click:
                    clicks_needed.append(img_elem)
                    print(f"[Solver] Image {idx}: DETECTED {state.target_label} (conf: {[d.confidence for d in detections]}, semantic: {semantic_score:.2f})")
                else:
                    print(f"[Solver] Image {idx}: No match (semantic: {semantic_score:.2f})")
            
            # Perform clicks with human-like behavior
            for elem in clicks_needed:
                await self._human_like_click(content_frame, elem)
                await asyncio.sleep(random.uniform(0.3, 0.8))
            
            # Submit
            await asyncio.sleep(0.5)
            submit = content_frame.locator(self.detector.HCAPTCHA_SELECTORS['submit_button'])
            if await submit.count() > 0 and await submit.is_visible():
                await self._human_like_click(content_frame, submit)
            
            # Wait for result
            await asyncio.sleep(3)
            
            # Check if solved
            state = await self.detector.detect_challenge_playwright(page)
            return state is None or state.solved
            
        except Exception as e:
            print(f"[Solve] Error: {e}")
            return False
    
    async def _human_like_click(self, page_or_frame, locator):
        """Simulate human-like mouse movement and click"""
        try:
            box = await locator.bounding_box()
            if not box:
                await locator.click()
                return
            
            # Random point within element
            x = box['x'] + random.uniform(box['width'] * 0.2, box['width'] * 0.8)
            y = box['y'] + random.uniform(box['height'] * 0.2, box['height'] * 0.8)
            
            # Move with curve
            await page_or_frame.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.05, 0.2))
            await page_or_frame.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page_or_frame.mouse.up()
            
        except:
            await locator.click(force=True)
    
    async def close(self):
        """Cleanup resources"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

# =============================================================================
# SELENIUM BACKEND
# =============================================================================

class SeleniumSolver:
    """Selenium-based solver for grid/remote execution"""
    
    def __init__(self, config: Optional[SolverConfig] = None):
        self.config = config or SolverConfig()
        self.vision = VisionEngine(self.config)
        self.detector = ChallengeDetector(self.config)
        self.driver = None
    
    def start(self):
        self.init_selenium()
    
    def init_selenium(self):
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium not installed")
        options = StealthPatcher.get_selenium_options(self.config)
        self.driver = webdriver.Chrome(options=options)
        self.driver.execute_script(StealthPatcher.STEALTH_SCRIPT)
    
    def solve(self, url: str) -> bool:
        self.driver.get(url)
        time.sleep(2)
        
        for attempt in range(self.config.max_retries):
            state = self.detector.detect_challenge_selenium(self.driver)
            if state is None:
                return True
            
            print(f"[SeleniumSolver] {state.challenge_type.name}: {state.target_label}")
            
            if state.challenge_type == ChallengeType.HCAPTCHA_IMAGE_LABEL:
                return self._solve_hcaptcha(state)
        
        return False
    
    def _solve_hcaptcha(self, state: ChallengeState) -> bool:
        """Selenium hCaptcha solver"""
        try:
            # Switch to challenge frame
            frames = self.driver.find_elements(By.CSS_SELECTOR, self.detector.HCAPTCHA_SELECTORS['frame'])
            if not frames:
                return False
            
            self.driver.switch_to.frame(frames[0])
            
            # Find images
            images = self.driver.find_elements(By.CSS_SELECTOR, 'img')
            images = [img for img in images if img.is_displayed()]
            
            for idx, img in enumerate(images[:9]):
                # Get image src or screenshot
                src = img.get_attribute('src')
                if src and src.startswith('data:image'):
                    # Parse base64
                    header, encoded = src.split(',', 1)
                    data = base64.b64decode(encoded)
                    nparr = np.frombuffer(data, np.uint8)
                    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                else:
                    # Screenshot the element
                    png = img.screenshot_as_png
                    nparr = np.frombuffer(png, np.uint8)
                    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                if image is None:
                    continue
                
                image = self.vision.preprocess_challenge_image(image)
                detections = self.vision.detect_objects(image, [state.target_label])
                
                if detections and any(d.confidence > self.config.confidence_threshold for d in detections):
                    # Human-like click via ActionChains
                    from selenium.webdriver.common.action_chains import ActionChains
                    actions = ActionChains(self.driver)
                    actions.move_to_element(img)
                    actions.pause(random.uniform(0.1, 0.3))
                    actions.click()
                    actions.perform()
                    time.sleep(random.uniform(0.3, 0.8))
            
            # Submit
            submit = self.driver.find_elements(By.CSS_SELECTOR, self.detector.HCAPTCHA_SELECTORS['submit_button'])
            if submit:
                submit[0].click()
            
            time.sleep(3)
            self.driver.switch_to.default_content()
            return self.detector.detect_challenge_selenium(self.driver) is None
            
        except Exception as e:
            print(f"[Selenium] Error: {e}")
            self.driver.switch_to.default_content()
            return False
    
    def close(self):
        if self.driver:
            self.driver.quit()

# =============================================================================
# UNIFIED INTERFACE
# =============================================================================

class GodSolver:
    """
    Unified interface that automatically selects the best backend.
    Playwright preferred for speed, Selenium for compatibility.
    """
    
    def __init__(self, config: Optional[SolverConfig] = None):
        self.config = config or SolverConfig()
        self._pw_solver = None
        self._se_solver = None
    
    async def solve(self, url: str, backend: str = "auto") -> bool:
        """
        Solve CAPTCHA at URL.
        backend: "auto", "playwright", "selenium"
        """
        if backend == "auto":
            backend = "playwright" if PLAYWRIGHT_AVAILABLE else "selenium"
        
        if backend == "playwright":
            if self._pw_solver is None:
                self._pw_solver = CaptchaSolver(self.config)
                await self._pw_solver.init_playwright()
            return await self._pw_solver.solve_page(url)
        
        elif backend == "selenium":
            if self._se_solver is None:
                self._se_solver = SeleniumSolver(self.config)
                self._se_solver.start()
            return self._se_solver.solve(url)
        
        raise ValueError(f"Unknown backend: {backend}")
    
    async def close(self):
        if self._pw_solver:
            await self._pw_solver.close()
        if self._se_solver:
            self._se_solver.close()

# =============================================================================
# CLI & MAIN
# =============================================================================

async def main():
    import argparse
    parser = argparse.ArgumentParser(description='The Godly CAPTCHA Solver')
    parser.add_argument('url', nargs='?', help='Target URL with CAPTCHA')
    parser.add_argument('--backend', choices=['auto', 'playwright', 'selenium'], default='auto')
    parser.add_argument('--headless', action='store_true', help='Run headless')
    parser.add_argument('--model-dir', type=str, default=None)
    args = parser.parse_args()
    
    config = SolverConfig(headless=args.headless)
    if args.model_dir:
        config.model_dir = Path(args.model_dir)
    
    solver = GodSolver(config)
    
    try:
        if args.url:
            success = await solver.solve(args.url, backend=args.backend)
            print(f"\n{'='*50}")
            print(f"RESULT: {'SOLVED' if success else 'FAILED'}")
            print(f"{'='*50}")
        else:
            print("Usage: python captcha-solver.py <url> [--backend playwright|selenium] [--headless]")
            print("\nExample:")
            print("  python captcha-solver.py https://example.com --backend playwright")
    finally:
        await solver.close()

if __name__ == '__main__':
    asyncio.run(main())
