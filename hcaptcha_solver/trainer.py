"""
Training loop for hCaptcha solver.
Fine-tunes models on CAPTCHA-specific data.
"""

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import numpy as np
from typing import Dict, List, Optional, Tuple, Callable
from pathlib import Path
import json
import copy
from tqdm import tqdm
import logging

from .dataset import CAPTCHADataset, GridCAPTCHADataset, create_dataloader


logger = logging.getLogger(__name__)


class HCAPTCHATrainer:
    """
    Trainer for hCaptcha-specific models.
    Supports YOLO11, CLIP fine-tuning, and custom vision-language models.
    """
    
    def __init__(
        self,
        model,
        train_dataset: Dataset,
        val_dataset: Optional[Dataset] = None,
        output_dir: str = "./checkpoints",
        device: str = "cuda" if torch.cuda.is_available() else "cpu",
        learning_rate: float = 1e-4,
        weight_decay: float = 1e-4,
        batch_size: int = 32,
        num_epochs: int = 50,
        warmup_epochs: int = 5,
        grad_accum_steps: int = 1,
        mixed_precision: bool = True,
    ):
        self.model = model
        self.train_dataset = train_dataset
        self.val_dataset = val_dataset
        self.output_dir = Path(output_dir)
        self.device = device
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        self.warmup_epochs = warmup_epochs
        self.grad_accum_steps = grad_accum_steps
        self.mixed_precision = mixed_precision
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.train_loader = create_dataloader(
            train_dataset, batch_size=batch_size, shuffle=True
        )
        
        if val_dataset:
            self.val_loader = create_dataloader(
                val_dataset, batch_size=batch_size, shuffle=False
            )
        else:
            self.val_loader = None
        
        self.optimizer = optim.AdamW(
            model.parameters(),
            lr=learning_rate,
            weight_decay=weight_decay,
        )
        
        self.scheduler = optim.lr_scheduler.CosineAnnealingLR(
            self.optimizer,
            T_max=num_epochs,
            eta_min=learning_rate * 0.01,
        )
        
        self.scaler = torch.cuda.amp.GradScaler() if mixed_precision else None
        self.best_model_state = None
        self.best_val_loss = float('inf')
        
        self.metrics = {
            "train_loss": [],
            "val_loss": [],
            "train_accuracy": [],
            "val_accuracy": [],
        }
    
    def train_epoch(self, epoch: int) -> Dict:
        """Train for one epoch."""
        self.model.train()
        
        total_loss = 0
        total_samples = 0
        correct = 0
        
        warmup_lr = (epoch < self.warmup_epochs)
        if warmup_lr:
            lr_scale = (epoch + 1) / self.warmup_epochs
            for pg in self.optimizer.param_groups:
                pg['lr'] = self.learning_rate * lr_scale
        
        pbar = tqdm(self.train_loader, desc=f"Epoch {epoch + 1}/{self.num_epochs}")
        
        for batch_idx, (images, targets) in enumerate(pbar):
            images = images.to(self.device)
            
            if self.mixed_precision:
                with torch.cuda.amp.autocast():
                    outputs = self._forward(images, targets)
                    loss = self._compute_loss(outputs, targets)
                    loss = loss / self.grad_accum_steps
                
                self.scaler.scale(loss).backward()
                
                if (batch_idx + 1) % self.grad_accum_steps == 0:
                    self.scaler.step(self.optimizer)
                    self.scaler.update()
                    self.optimizer.zero_grad()
            else:
                outputs = self._forward(images, targets)
                loss = self._compute_loss(outputs, targets)
                loss = loss / self.grad_accum_steps
                
                loss.backward()
                
                if (batch_idx + 1) % self.grad_accum_steps == 0:
                    self.optimizer.step()
                    self.optimizer.zero_grad()
            
            total_loss += loss.item() * self.grad_accum_steps * images.size(0)
            total_samples += images.size(0)
            
            if isinstance(outputs, dict):
                if "pred_labels" in outputs:
                    correct += (outputs["pred_labels"] == targets.get("labels")).sum().item()
            elif isinstance(outputs, torch.Tensor):
                pred_labels = outputs.argmax(dim=1)
                if "labels" in targets:
                    correct += (pred_labels == targets["labels"]).sum().item()
            
            pbar.set_postfix({
                "loss": f"{total_loss / total_samples:.4f}",
                "acc": f"{correct / total_samples:.4f}",
            })
        
        return {
            "loss": total_loss / total_samples,
            "accuracy": correct / total_samples,
        }
    
    def _forward(self, images, targets):
        """Forward pass - to be overridden by subclasses."""
        return self.model(images)
    
    def _compute_loss(self, outputs, targets):
        """Compute loss - to be overridden by subclasses."""
        if isinstance(outputs, dict):
            return outputs.get("loss", torch.tensor(0.0))
        return torch.tensor(0.0)
    
    def validate(self) -> Dict:
        """Validate the model."""
        if self.val_loader is None:
            return {"loss": 0, "accuracy": 1.0}
        
        self.model.eval()
        
        total_loss = 0
        total_samples = 0
        correct = 0
        
        with torch.no_grad():
            for images, targets in self.val_loader:
                images = images.to(self.device)
                
                if self.mixed_precision:
                    with torch.cuda.amp.autocast():
                        outputs = self._forward(images, targets)
                        loss = self._compute_loss(outputs, targets)
                else:
                    outputs = self._forward(images, targets)
                    loss = self._compute_loss(outputs, targets)
                
                total_loss += loss.item() * images.size(0)
                total_samples += images.size(0)
                
                if isinstance(outputs, dict):
                    if "pred_labels" in outputs:
                        correct += (outputs["pred_labels"] == targets.get("labels")).sum().item()
                elif isinstance(outputs, torch.Tensor):
                    pred_labels = outputs.argmax(dim=1)
                    if "labels" in targets:
                        correct += (pred_labels == targets["labels"]).sum().item()
        
        return {
            "loss": total_loss / total_samples,
            "accuracy": correct / total_samples,
        }
    
    def save_checkpoint(self, epoch: int, is_best: bool = False):
        """Save model checkpoint."""
        checkpoint = {
            "epoch": epoch,
            "model_state_dict": self.model.state_dict(),
            "optimizer_state_dict": self.optimizer.state_dict(),
            "scheduler_state_dict": self.scheduler.state_dict(),
            "best_val_loss": self.best_val_loss,
            "metrics": self.metrics,
        }
        
        path = self.output_dir / f"checkpoint-epoch-{epoch}.pt"
        torch.save(checkpoint, path)
        logger.info(f"Saved checkpoint to {path}")
        
        if is_best:
            best_path = self.output_dir / "best-model.pt"
            torch.save(self.best_model_state, best_path)
            logger.info(f"Saved best model to {best_path}")
    
    def load_checkpoint(self, path: str):
        """Load model from checkpoint."""
        checkpoint = torch.load(path, map_location=self.device)
        
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        self.scheduler.load_state_dict(checkpoint["scheduler_state_dict"])
        
        if "metrics" in checkpoint:
            self.metrics = checkpoint["metrics"]
        
        logger.info(f"Loaded checkpoint from {path}")
        return checkpoint.get("epoch", 0)
    
    def train(self) -> Dict:
        """Full training loop."""
        logger.info(f"Starting training on device: {self.device}")
        
        for epoch in range(self.num_epochs):
            train_metrics = self.train_epoch(epoch)
            
            val_metrics = self.validate()
            
            self.metrics["train_loss"].append(train_metrics["loss"])
            self.metrics["val_loss"].append(val_metrics["loss"])
            self.metrics["train_accuracy"].append(train_metrics["accuracy"])
            self.metrics["val_accuracy"].append(val_metrics["accuracy"])
            
            self.scheduler.step()
            
            is_best = val_metrics["loss"] < self.best_val_loss
            if is_best:
                self.best_val_loss = val_metrics["loss"]
                self.best_model_state = copy.deepcopy(self.model.state_dict())
            
            self.save_checkpoint(epoch, is_best)
            
            logger.info(
                f"Epoch {epoch + 1}/{self.num_epochs} - "
                f"Train Loss: {train_metrics['loss']:.4f}, "
                f"Val Loss: {val_metrics['loss']:.4f}, "
                f"Train Acc: {train_metrics['accuracy']:.4f}, "
                f"Val Acc: {val_metrics['accuracy']:.4f}"
            )
        
        return {
            "best_val_loss": self.best_val_loss,
            "final_train_loss": self.metrics["train_loss"][-1],
            "final_val_loss": self.metrics["val_loss"][-1],
        }


class YOLO11Trainer(HCAPTCHATrainer):
    """Trainer for YOLO11 object detection on CAPTCHA tiles."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    def _forward(self, images, targets):
        """YOLO forward pass."""
        return self.model(images)
    
    def _compute_loss(self, outputs, targets):
        """YOLO loss computation."""
        if hasattr(outputs, 'loss'):
            return outputs.loss
        return torch.tensor(0.0, device=self.device)


class VisionLanguageTrainer(HCAPTCHATrainer):
    """Trainer for vision-language models on CAPTCHA data."""
    
    def __init__(self, *args, temperature: float = 0.07, **kwargs):
        super().__init__(*args, **kwargs)
        self.temperature = temperature
        self.criterion = nn.CrossEntropyLoss()
    
    def _forward(self, images, targets):
        """Vision-language forward pass."""
        return self.model(images)
    
    def _compute_loss(self, outputs, targets):
        """Compute contrastive loss."""
        if isinstance(outputs, dict):
            return outputs.get("loss", torch.tensor(0.0))
        
        if "target_labels" in targets:
            labels = targets["target_labels"].to(self.device)
            return self.criterion(outputs / self.temperature, labels)
        
        return torch.tensor(0.0, device=self.device)


class GridTransformerTrainer(HCAPTCHATrainer):
    """Trainer for transformer-based grid CAPTCHA solver."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    def _forward(self, tiles, targets):
        """Forward pass for 3x3 grid."""
        return self.model(tiles)
    
    def _compute_loss(self, outputs, targets):
        """Compute loss for grid positions."""
        if isinstance(outputs, torch.Tensor):
            labels = targets["correct_positions"]
            return nn.CrossEntropyLoss()(outputs, labels)
        return torch.tensor(0.0, device=self.device)


def train_on_synthetic_data(
    model,
    base_images_dir: str,
    output_dir: str,
    num_epochs: int = 50,
    batch_size: int = 32,
):
    """Train model on synthetic CAPTCHA tiles."""
    from .dataset import synthesize_captcha_tiles, DataAugmentation
    
    base_images = list(Path(base_images_dir).glob("*.png"))
    
    data = []
    for img_path in base_images[:100]:
        tiles = synthesize_captcha_tiles(
            str(img_path),
            num_tiles=9,
            distortions=["blur", "noise", "rotate"],
        )
        
        for i, tile in enumerate(tiles):
            data.append({
                "image_path": f"/tmp/synth_tile_{i}_{img_path.stem}.png",
                "target_label": "object",
                "is_correct": True,
                "confidence": 0.9,
            })
    
    train_dataset = CAPTCHADataset(
        data,
        transform=DataAugmentation.get_train_transforms(),
    )
    
    val_dataset = CAPTCHADataset(
        data[:10],
        transform=DataAugmentation.get_val_transforms(),
    )
    
    trainer = HCAPTCHATrainer(
        model=model,
        train_dataset=train_dataset,
        val_dataset=val_dataset,
        output_dir=output_dir,
        num_epochs=num_epochs,
        batch_size=batch_size,
    )
    
    return trainer.train()