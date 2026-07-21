"""
Dataset for hCaptcha-style images.
Supports fine-tuning and evaluation.
"""

import torch
from torch.utils.data import Dataset, DataLoader
import numpy as np
from typing import List, Tuple, Dict, Optional, Callable
from PIL import Image
import json
from pathlib import Path
import random


class CAPTCHADataset(Dataset):
    """
    Dataset for hCaptcha tile images with human labels.
    
    Format:
        {
            "image_path": "path/to/tile.png",
            "target_label": "bus",
            "is_correct": true,
            "confidence": 0.85,
            "bbox": [x1, y1, x2, y2],  # optional
            "metadata": {...}
        }
    """
    
    def __init__(
        self,
        data: List[Dict],
        transform: Optional[Callable] = None,
        target_transform: Optional[Callable] = None,
    ):
        self.data = data
        self.transform = transform
        self.target_transform = target_transform
        
        self._validate_data()
    
    def _validate_data(self) -> None:
        """Validate dataset entries."""
        required_keys = ["image_path", "target_label", "is_correct"]
        for entry in self.data:
            for key in required_keys:
                if key not in entry:
                    raise ValueError(f"Missing required key: {key}")
    
    def __len__(self) -> int:
        return len(self.data)
    
    def __getitem__(self, idx: int) -> Tuple:
        entry = self.data[idx]
        
        image = Image.open(entry["image_path"]).convert("RGB")
        
        if self.transform:
            image = self.transform(image)
        
        target = {
            "label": entry["target_label"],
            "is_correct": entry["is_correct"],
            "confidence": entry.get("confidence", 1.0),
        }
        
        if "bbox" in entry:
            target["bbox"] = entry["bbox"]
        
        if "metadata" in entry:
            target["metadata"] = entry["metadata"]
        
        if self.target_transform:
            target = self.target_transform(target)
        
        return image, target


class GridCAPTCHADataset(Dataset):
    """
    Dataset for full 3x3 grid CAPTCHAs.
    Each sample contains 9 tiles with the target object in 1-4 positions.
    """
    
    def __init__(
        self,
        data: List[Dict],
        transform: Optional[Callable] = None,
    ):
        self.data = data
        self.transform = transform
    
    def __len__(self) -> int:
        return len(self.data)
    
    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        entry = self.data[idx]
        
        tiles = []
        for tile_path in entry["tiles"]:
            image = Image.open(tile_path).convert("RGB")
            if self.transform:
                image = self.transform(image)
            tiles.append(image)
        
        tiles_tensor = torch.stack(tiles)
        
        labels = torch.tensor(entry["correct_positions"], dtype=torch.long)
        
        return tiles_tensor, labels


class HCaptchaTileDataset(Dataset):
    """
    Optimized dataset for tile-level training.
    Focuses on small objects and distortions typical in CAPTCHAs.
    """
    
    def __init__(
        self,
        root_dir: str,
        annotation_file: str,
        transform: Optional[Callable] = None,
    ):
        self.root_dir = Path(root_dir)
        self.transform = transform
        
        with open(annotation_file, 'r') as f:
            self.annotations = json.load(f)
    
    def __len__(self) -> int:
        return len(self.annotations)
    
    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, Dict]:
        ann = self.annotations[idx]
        
        img_path = self.root_dir / ann["file_name"]
        image = Image.open(img_path).convert("RGB")
        
        if self.transform:
            image = self.transform(image)
        
        target = {
            "boxes": ann.get("bbox", []),
            "labels": ann.get("category_id", []),
            "target_label": ann.get("target_label", ""),
            "is_correct": ann.get("is_correct", False),
        }
        
        return image, target


def collate_fn_tile(batch: List[Tuple]) -> Tuple[torch.Tensor, List[Dict]]:
    """Collate function for tile dataset."""
    images, targets = zip(*batch)
    images = torch.stack(images, 0)
    return images, targets


def collate_fn_grid(batch: List[Tuple]) -> Tuple[torch.Tensor, torch.Tensor]:
    """Collate function for grid dataset."""
    tiles, labels = zip(*batch)
    tiles = torch.stack(tiles, 0)
    labels = torch.stack(labels, 0)
    return tiles, labels


class DataAugmentation:
    """Data augmentation for CAPTCHA-style images."""
    
    @staticmethod
    def get_train_transforms():
        """Get training transforms with aggressive augmentation."""
        import torchvision.transforms as T
        
        return T.Compose([
            T.RandomResizedCrop(224, scale=(0.5, 1.0)),
            T.RandomHorizontalFlip(0.5),
            T.RandomRotation(degrees=15),
            T.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.2, hue=0.1),
            T.GaussianBlur(kernel_size=3, sigma=(0.1, 2.0)),
            T.MotionBlur(kernel_size=5),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
    
    @staticmethod
    def get_val_transforms():
        """Get validation transforms."""
        import torchvision.transforms as T
        
        return T.Compose([
            T.Resize((224, 224)),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])


def create_dataloader(
    dataset: Dataset,
    batch_size: int = 32,
    shuffle: bool = True,
    num_workers: int = 4,
    collate_fn: Optional[Callable] = None,
) -> DataLoader:
    """Create DataLoader with appropriate settings."""
    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=num_workers,
        collate_fn=collate_fn or collate_fn_tile,
        pin_memory=True,
    )


def load_dataset_from_json(
    json_path: str,
    root_dir: str,
    transform: Optional[Callable] = None,
) -> CAPTCHADataset:
    """Load dataset from JSON annotation file."""
    with open(json_path, 'r') as f:
        data = json.load(f)
    
    for entry in data:
        entry["image_path"] = str(Path(root_dir) / entry["file_name"])
    
    return CAPTCHADataset(data, transform=transform)


def synthesize_captcha_tiles(
    base_image_path: str,
    num_tiles: int = 9,
    tile_size: Tuple[int, int] = (64, 64),
    distortions: List[str] = None,
) -> List[np.ndarray]:
    """
    Synthesize CAPTCHA-style tiles from a base image.
    Useful for data augmentation.
    """
    import cv2
    
    img = cv2.imread(base_image_path)
    h, w = img.shape[:2]
    
    tile_h, tile_w = tile_size
    tiles = []
    
    for i in range(num_tiles):
        start_y = random.randint(0, max(0, h - tile_h))
        start_x = random.randint(0, max(0, w - tile_w))
        
        tile = img[start_y:start_y + tile_h, start_x:start_x + tile_w]
        
        if distortions:
            distortion = random.choice(distortions)
            if distortion == "blur":
                tile = cv2.GaussianBlur(tile, (3, 3), 0)
            elif distortion == "noise":
                noise = np.random.randn(*tile.shape) * 10
                tile = np.clip(tile + noise, 0, 255).astype(np.uint8)
            elif distortion == "rotate":
                angle = random.uniform(-15, 15)
                center = (tile_w // 2, tile_h // 2)
                M = cv2.getRotationMatrix2D(center, angle, 1.0)
                tile = cv2.warpAffine(tile, M, (tile_w, tile_h))
        
        tiles.append(tile)
    
    return tiles