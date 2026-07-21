"""
Main hCaptcha solver with all improvements.
"""

import asyncio
import cv2
import numpy as np
from typing import List, Dict, Optional, Tuple, Union
from PIL import Image
import torch
from pathlib import Path
import json
import time
import random
import logging

from .yolo import YOLO11Detector
from .vision_language import VisionLanguageMatcher, ObjectPresenceMatcher
from .stealth import StealthBrowser, HCaptchaPage

logger = logging.getLogger(__name__)


class HCaptchaSolver:
    """
    Advanced hCaptcha solver with:
    - YOLO11 for object detection
    - Vision-language models for precise matching
    - Calibrated confidence thresholds
    - Grid-aware solving
    - Training loop support
    """
    
    def __init__(
        self,
        yolo_model_path: Optional[str] = None,
        device: str = "cuda" if torch.cuda.is_available() else "cpu",
        confidence_threshold: float = 0.3,
        use_vlm: bool = True,
        vlm_model: str = "ViT-B/32",
    ):
        self.device = device
        self.confidence_threshold = confidence_threshold
        
        self.yolo = YOLO11Detector(
            model_path=yolo_model_path,
            device=device,
            conf_threshold=confidence_threshold,
        )
        
        if use_vlm:
            self.vlm = VisionLanguageMatcher(
                model_name=vlm_model,
                device=device,
                threshold=confidence_threshold,
            )
            self.object_matcher = ObjectPresenceMatcher(
                model_name=vlm_model,
                device=device,
            )
        else:
            self.vlm = None
            self.object_matcher = None
        
        self._calibration_data = []
        self._confidence_map = {}
    
    def set_confidence_threshold(self, threshold: float) -> None:
        """Set confidence threshold for predictions."""
        self.confidence_threshold = threshold
        if self.yolo:
            self.yolo.conf_threshold = threshold
        if self.vlm:
            self.vlm.threshold = threshold
    
    def calibrate_threshold(
        self,
        predictions: List[float],
        labels: List[bool],
    ) -> float:
        """
        Calibrate confidence threshold using ROC analysis.
        
        Args:
            predictions: Model confidence scores
            labels: Ground truth labels
            
        Returns:
            Optimal threshold
        """
        from sklearn.metrics import roc_curve, auc
        
        fpr, tpr, thresholds = roc_curve(labels, predictions)
        
        youden_j = tpr - fpr
        optimal_idx = np.argmax(youden_j)
        self.confidence_threshold = thresholds[optimal_idx]
        
        self._confidence_map = {
            "threshold": self.confidence_threshold,
            "auc": auc(fpr, tpr),
        }
        
        return self.confidence_threshold
    
    def solve_tile(
        self,
        image: Union[np.ndarray, bytes, str],
        target_label: str,
    ) -> Tuple[bool, float]:
        """
        Solve a single tile for target object.
        
        Args:
            image: Tile image (numpy array, bytes, or URL)
            target_label: Target object to find
            
        Returns:
            Tuple of (is_present, confidence)
        """
        if isinstance(image, bytes):
            nparr = np.frombuffer(image, np.uint8)
            image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        elif isinstance(image, str):
            response = requests.get(image)
            nparr = np.frombuffer(response.content, np.uint8)
            image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        
        if self.yolo.model is not None:
            detections = self.yolo.detect(image, [target_label])
            if detections:
                best = max(detections, key=lambda x: x["confidence"])
                if best["confidence"] >= self.confidence_threshold:
                    return True, best["confidence"]
        
        if self.object_matcher:
            result = self.object_matcher.match_object_presence(image, target_label)
            if result >= self.confidence_threshold:
                return True, result
        
        if self.vlm:
            result = self.vlm.calibrated_match(
                image,
                [target_label],
                self.confidence_threshold,
            )
            return result["is_confident"], result["confidence"]
        
        return False, 0.0
    
    def solve_grid(
        self,
        tiles: List[Union[np.ndarray, bytes, str]],
        target_label: str,
        max_selections: int = 4,
        min_selections: int = 1,
    ) -> List[int]:
        """
        Solve a 3x3 grid CAPTCHA.
        
        Uses tile relationships and consistency checking.
        
        Args:
            tiles: List of 9 tile images
            target_label: Target object to find
            max_selections: Maximum number of tiles to select
            min_selections: Minimum number of tiles to select
            
        Returns:
            List of indices of correct tiles
        """
        scores = []
        confidences = []
        
        for tile in tiles:
            is_present, confidence = self.solve_tile(tile, target_label)
            scores.append(is_present)
            confidences.append(confidence)
        
        selected = []
        for i, (score, conf) in enumerate(zip(scores, confidences)):
            if score and conf >= self.confidence_threshold:
                selected.append(i)
        
        if len(selected) < min_selections:
            sorted_indices = np.argsort(confidences)[::-1]
            needed = min_selections - len(selected)
            selected.extend(sorted_indices[:needed].tolist())
        
        if len(selected) > max_selections:
            sorted_indices = np.argsort(confidences)[::-1]
            selected = sorted_indices[:max_selections].tolist()
        
        return sorted(selected)
    
    def solve_with_browser(
        self,
        browser: StealthBrowser,
        target_label: str,
        url: Optional[str] = None,
    ) -> bool:
        """
        Solve hCaptcha using browser automation.
        
        Args:
            browser: Stealth browser instance
            target_label: Target object
            url: Page URL (optional if browser already on page)
            
        Returns:
            True if solved
        """
        async def _solve():
            if url:
                await browser.goto(url)
            
            hcaptcha = HCaptchaPage(browser)
            
            success = await hcaptcha.solve_image_challenge(
                target_label,
                self._solver_wrapper,
            )
            
            return success
        
        return asyncio.run(_solve())
    
    def _solver_wrapper(self, image_bytes: bytes, target_label: str) -> bool:
        """Wrapper for solver function."""
        is_present, _ = self.solve_tile(image_bytes, target_label)
        return is_present
    
    def batch_solve(
        self,
        images: List[Union[np.ndarray, bytes, str]],
        target_label: str,
    ) -> List[Tuple[bool, float]]:
        """
        Solve multiple tiles in batch.
        """
        results = []
        for image in images:
            result = self.solve_tile(image, target_label)
            results.append(result)
        return results
    
    def get_metrics(self) -> Dict:
        """Get current solver metrics."""
        return {
            "confidence_threshold": self.confidence_threshold,
            "device": self.device,
            "model_loaded": self.yolo.model is not None,
            "vlm_enabled": self.vlm is not None,
        }
    
    def save_state(self, path: str) -> None:
        """Save solver state for later use."""
        state = {
            "confidence_threshold": self.confidence_threshold,
            "calibration_data": self._calibration_data,
            "confidence_map": self._confidence_map,
        }
        with open(path, 'w') as f:
            json.dump(state, f)
    
    def load_state(self, path: str) -> None:
        """Load solver state."""
        with open(path, 'r') as f:
            state = json.load(f)
        
        self.confidence_threshold = state.get("confidence_threshold", 0.3)
        self._calibration_data = state.get("calibration_data", [])
        self._confidence_map = state.get("confidence_map", {})


class SliderSolver:
    """
    Solver for slider CAPTCHAs (Geetest, etc.).
    Uses image processing and feature matching.
    """
    
    def __init__(self):
        self._last_offset = 0
    
    def solve(
        self,
        puzzle_image: Union[np.ndarray, bytes, str],
        background_image: Union[np.ndarray, bytes, str],
    ) -> int:
        """
        Calculate slider offset.
        
        Args:
            puzzle_image: Image with the gap
            background_image: Full background image
            
        Returns:
            Offset distance in pixels
        """
        if isinstance(puzzle_image, bytes):
            nparr = np.frombuffer(puzzle_image, np.uint8)
            puzzle = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(puzzle_image, str):
            puzzle = cv2.imread(puzzle_image)
        else:
            puzzle = puzzle_image
        
        if isinstance(background_image, bytes):
            nparr = np.frombuffer(background_image, np.uint8)
            bg = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(background_image, str):
            bg = cv2.imread(background_image)
        else:
            bg = background_image
        
        puzzle_gray = cv2.cvtColor(puzzle, cv2.COLOR_BGR2GRAY)
        bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        
        puzzle_blur = cv2.GaussianBlur(puzzle_gray, (5, 5), 0)
        bg_blur = cv2.GaussianBlur(bg_gray, (5, 5), 0)
        
        result = cv2.matchTemplate(bg_blur, puzzle_blur, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(result)
        
        offset = max_loc[0]
        self._last_offset = offset
        
        return offset
    
    def solve_with_loftr(
        self,
        puzzle_image: np.ndarray,
        background_image: np.ndarray,
    ) -> int:
        """
        Use LoFTR for precise matching (requires loftr package).
        """
        try:
            import torch
            from loftr import LoFTR
            
            device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            config = {
                'coarse': {'top_k': 100},
                'match': {'cross_attention': True},
            }
            matcher = LoFTR(config).to(device)
            
            img0 = cv2.cvtColor(background_image, cv2.COLOR_BGR2RGB)
            img1 = cv2.cvtColor(puzzle_image, cv2.COLOR_BGR2RGB)
            
            img0 = torch.from_numpy(img0).permute(2, 0, 1).float() / 255.
            img1 = torch.from_numpy(img1).permute(2, 0, 1).float() / 255.
            
            img0 = img0.unsqueeze(0).to(device)
            img1 = img1.unsqueeze(0).to(device)
            
            with torch.no_grad():
                outputs = matcher({'image0': img0, 'image1': img1})
            
            mkpts0 = outputs['mkpts0_c'].cpu().numpy()
            mkpts1 = outputs['mkpts1_c'].cpu().numpy()
            
            dx = mkpts0[:, 0] - mkpts1[:, 0]
            dy = mkpts0[:, 1] - mkpts1[:, 1]
            
            offset = int(np.median(dx))
            return max(0, offset)
            
        except ImportError:
            return self.solve(puzzle_image, background_image)


class ShapeMatcher:
    """
    Solve same-shape CAPTCHAs using contour analysis.
    """
    
    def match_shapes(
        self,
        target_shape: np.ndarray,
        candidates: List[np.ndarray],
        threshold: float = 0.8,
    ) -> List[int]:
        """
        Find shapes that match the target.
        
        Args:
            target_shape: Target shape image
            candidates: List of candidate shape images
            threshold: Similarity threshold
            
        Returns:
            Indices of matching shapes
        """
        target_gray = cv2.cvtColor(target_shape, cv2.COLOR_BGR2GRAY)
        _, target_thresh = cv2.threshold(target_gray, 127, 255, cv2.THRESH_BINARY)
        target_contours, _ = cv2.findContours(target_thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        if not target_contours:
            return []
        
        target_hu = cv2.HuMoments(cv2.moments(target_contours[0])).flatten()
        
        matches = []
        for i, candidate in enumerate(candidates):
            cand_gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
            _, cand_thresh = cv2.threshold(cand_gray, 127, 255, cv2.THRESH_BINARY)
            cand_contours, _ = cv2.findContours(cand_thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            if not cand_contours:
                continue
            
            cand_hu = cv2.HuMoments(cv2.moments(cand_contours[0])).flatten()
            
            similarity = cv2.matchShapes(target_contours[0], cand_contours[0], cv2.CONTOURS_MATCH_I1)
            
            if similarity < (1 - threshold):
                matches.append(i)
        
        return matches


class ObjectAlignmentSolver:
    """
    Solve image alignment puzzles using feature matching.
    """
    
    def __init__(self):
        self.sift = cv2.SIFT_create()
        self.bf = cv2.BFMatcher()
    
    def align_objects(
        self,
        object_image: np.ndarray,
        background_image: np.ndarray,
    ) -> Tuple[int, int]:
        """
        Find position to place object in background.
        
        Returns (x, y) coordinates.
        """
        obj_gray = cv2.cvtColor(object_image, cv2.COLOR_BGR2GRAY)
        bg_gray = cv2.cvtColor(background_image, cv2.COLOR_BGR2GRAY)
        
        kp1, des1 = self.sift.detectAndCompute(obj_gray, None)
        kp2, des2 = self.sift.detectAndCompute(bg_gray, None)
        
        if des1 is None or des2 is None:
            return 0, 0
        
        matches = self.bf.knnMatch(des1, des2, k=2)
        
        good_matches = []
        for m, n in matches:
            if m.distance < 0.7 * n.distance:
                good_matches.append(m)
        
        if len(good_matches) < 4:
            return 0, 0
        
        src_pts = np.float32([kp1[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)
        
        M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
        
        h, w = obj_gray.shape
        corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
        transformed_corners = cv2.perspectiveTransform(corners, M)
        
        center_x = int(np.mean(transformed_corners[:, 0, 0]))
        center_y = int(np.mean(transformed_corners[:, 0, 1]))
        
        return center_x, center_y