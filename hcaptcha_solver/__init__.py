"""
Advanced hCaptcha solver with YOLO11 and vision-language models.
"""

from .solver import HCaptchaSolver
from .yolo import YOLO11Detector
from .vision_language import VisionLanguageMatcher
from .dataset import CAPTCHADataset
from .trainer import HCAPTCHATrainer
from .stealth import StealthBrowser

__all__ = [
    "HCaptchaSolver",
    "YOLO11Detector",
    "VisionLanguageMatcher",
    "CAPTCHADataset",
    "HCAPTCHATrainer",
    "StealthBrowser",
]