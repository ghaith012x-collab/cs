"""
Fine-tuned YOLO11 detector for hCaptcha-style tiles.
Trained on specialized dataset with tiny objects, cropped objects, and distortions.
"""

import torch
import numpy as np
from typing import List, Tuple, Dict, Optional
from pathlib import Path


class YOLO11Detector:
    """YOLO11-based object detector optimized for hCaptcha tiles."""
    
    def __init__(
        self,
        model_path: Optional[str] = None,
        device: str = "cuda" if torch.cuda.is_available() else "cpu",
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        classes: Optional[List[int]] = None,
    ):
        self.device = device
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.classes = classes
        
        if model_path and Path(model_path).exists():
            from ultralytics import YOLO
            self.model = YOLO(model_path)
            self.model.to(device)
        else:
            self.model = None
            self._available_classes = self._get_default_classes()
    
    def _get_default_classes(self) -> Dict[str, int]:
        return {
            "traffic light": 10,
            "bus": 5,
            "car": 2,
            "truck": 7,
            "bicycle": 1,
            "motorcycle": 4,
            "train": 6,
            "person": 0,
            "stop sign": 11,
            "parking meter": 12,
            "traffic board": 13,
            "fire hydrant": 14,
            "street light": 15,
            "traffic cone": 16,
            "construction barrier": 17,
            "dumpster": 18,
            "bench": 19,
            "bird": 20,
            "cat": 17,
            "dog": 18,
            "horse": 19,
            "sheep": 20,
            "cow": 21,
            "elephant": 22,
            "bear": 23,
            "zebra": 24,
            "giraffe": 25,
            "backpack": 28,
            "umbrella": 29,
            "handbag": 30,
            "tie": 31,
            "suitcase": 32,
            "frisbee": 33,
            "skis": 34,
            "snowboard": 35,
            "sports ball": 36,
            "kite": 37,
            "baseball bat": 38,
            "baseball glove": 39,
            "skateboard": 40,
            "surfboard": 41,
            "tennis racket": 42,
            "bottle": 43,
            "wine glass": 44,
            "cup": 45,
            "fork": 46,
            "knife": 47,
            "spoon": 48,
            "bowl": 49,
            "banana": 50,
            "apple": 51,
            "sandwich": 52,
            "orange": 53,
            "broccoli": 54,
            "carrot": 55,
            "hot dog": 56,
            "pizza": 57,
            "donut": 58,
            "cake": 59,
            "chair": 61,
            "couch": 62,
            "potted plant": 63,
            "bed": 64,
            "dining table": 65,
            "toilet": 66,
            "tv": 67,
            "laptop": 68,
            "mouse": 69,
            "remote": 70,
            "keyboard": 71,
            "cell phone": 72,
            "microwave": 73,
            "oven": 74,
            "toaster": 75,
            "sink": 76,
            "refrigerator": 77,
            "book": 78,
            "clock": 79,
            "vase": 80,
        }
    
    def load_model(self, model_path: str) -> None:
        """Load a fine-tuned YOLO11 model."""
        from ultralytics import YOLO
        self.model = YOLO(model_path)
        self.model.to(self.device)
    
    def detect(
        self,
        image: np.ndarray,
        target_classes: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        Detect objects in an image.
        
        Args:
            image: Input image as numpy array (H, W, C) or tensor
            target_classes: List of class names to filter detections
            
        Returns:
            List of detection dictionaries with keys:
                - bbox: [x1, y1, x2, y2]
                - confidence: float
                - class_id: int
                - class_name: str
        """
        if self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")
        
        results = self.model(image, conf=self.conf_threshold, iou=self.iou_threshold)
        
        detections = []
        for result in results:
            boxes = result.boxes
            if boxes is not None:
                for box in boxes:
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                    conf = box.conf[0].cpu().item()
                    cls_id = int(box.cls[0].cpu().item())
                    
                    class_name = result.names.get(cls_id, f"class_{cls_id}")
                    
                    if target_classes and class_name not in target_classes:
                        continue
                    
                    detections.append({
                        "bbox": [float(x1), float(y1), float(x2), float(y2)],
                        "confidence": conf,
                        "class_id": cls_id,
                        "class_name": class_name,
                    })
        
        return detections
    
    def detect_batch(
        self,
        images: List[np.ndarray],
        target_classes: Optional[List[str]] = None,
    ) -> List[List[Dict]]:
        """Detect objects in multiple images."""
        results = self.model(images, conf=self.conf_threshold, iou=self.iou_threshold)
        
        all_detections = []
        for result in results:
            detections = []
            boxes = result.boxes
            if boxes is not None:
                for box in boxes:
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                    conf = box.conf[0].cpu().item()
                    cls_id = int(box.cls[0].cpu().item())
                    
                    class_name = result.names.get(cls_id, f"class_{cls_id}")
                    
                    if target_classes and class_name not in target_classes:
                        continue
                    
                    detections.append({
                        "bbox": [float(x1), float(y1), float(x2), float(y2)],
                        "confidence": conf,
                        "class_id": cls_id,
                        "class_name": class_name,
                    })
            all_detections.append(detections)
        
        return all_detections
    
    def calibrate_confidence(
        self,
        predictions: List[float],
        labels: List[bool],
    ) -> Tuple[float, float]:
        """
        Calibrate confidence thresholds using ROC analysis.
        
        Args:
            predictions: Model confidence scores
            labels: Ground truth labels (True=positive, False=negative)
            
        Returns:
            Tuple of (optimal_threshold, auc_score)
        """
        from sklearn.metrics import roc_curve, auc
        
        fpr, tpr, thresholds = roc_curve(labels, predictions)
        roc_auc = auc(fpr, tpr)
        
        youden_j = tpr - fpr
        optimal_idx = np.argmax(youden_j)
        optimal_threshold = thresholds[optimal_idx]
        
        return optimal_threshold, roc_auc
    
    def get_class_embeddings(self, class_names: List[str]) -> np.ndarray:
        """Get semantic embeddings for target classes using CLIP."""
        import clip
        
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model, preprocess = clip.load("ViT-B/32", device=device)
        
        text = clip.tokenize(class_names).to(device)
        with torch.no_grad():
            embeddings = model.encode_text(text)
        
        return embeddings.cpu().numpy()