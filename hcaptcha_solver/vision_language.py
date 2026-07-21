"""
Vision-Language model for hCaptcha tile matching.
Uses CLIP-style embeddings for precise object matching.
"""

import torch
import numpy as np
from typing import List, Tuple, Dict, Optional, Union
from PIL import Image
import clip


class VisionLanguageMatcher:
    """Vision-Language model for matching images to text labels."""
    
    def __init__(
        self,
        model_name: str = "ViT-B/32",
        device: Optional[str] = None,
        threshold: float = 0.28,
    ):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.threshold = threshold
        self.model, self.preprocess = clip.load(model_name, device=self.device)
        self.model.eval()
        
        self._text_embeddings_cache: Dict[str, torch.Tensor] = {}
    
    def preprocess_image(self, image: Union[np.ndarray, Image.Image]) -> torch.Tensor:
        """Preprocess image for the model."""
        if isinstance(image, np.ndarray):
            image = Image.fromarray(image)
        return self.preprocess(image).unsqueeze(0).to(self.device)
    
    def encode_text(self, text: str) -> torch.Tensor:
        """Encode text to embedding."""
        if text in self._text_embeddings_cache:
            return self._text_embeddings_cache[text]
        
        tokens = clip.tokenize([text]).to(self.device)
        with torch.no_grad():
            embedding = self.model.encode_text(tokens)
        
        embedding = embedding / embedding.norm(dim=-1, keepdim=True)
        self._text_embeddings_cache[text] = embedding
        return embedding
    
    def encode_image(self, image: Union[np.ndarray, Image.Image]) -> torch.Tensor:
        """Encode image to embedding."""
        preprocessed = self.preprocess_image(image)
        with torch.no_grad():
            embedding = self.model.encode_image(preprocessed)
        
        return embedding / embedding.norm(dim=-1, keepdim=True)
    
    def match(
        self,
        image: Union[np.ndarray, Image.Image],
        labels: List[str],
        return_scores: bool = True,
    ) -> Union[str, Tuple[str, Dict[str, float]]]:
        """
        Match an image to the most similar label.
        
        Args:
            image: Input image
            labels: List of candidate labels
            return_scores: Whether to return similarity scores
            
        Returns:
            Best matching label, optionally with scores
        """
        image_embed = self.encode_image(image)
        
        text_embeds = []
        for label in labels:
            text_embeds.append(self.encode_text(label))
        
        text_embed_stack = torch.stack(text_embeds, dim=0).squeeze(1)
        
        similarities = (image_embed @ text_embed_stack.T).squeeze(0)
        scores = torch.softmax(similarities, dim=0)
        
        best_idx = scores.argmax().item()
        best_label = labels[best_idx]
        best_score = scores[best_idx].item()
        
        if return_scores:
            all_scores = {labels[i]: scores[i].item() for i in range(len(labels))}
            return best_label, all_scores
        return best_label
    
    def match_batch(
        self,
        images: List[Union[np.ndarray, Image.Image]],
        labels: List[str],
    ) -> Tuple[List[str], List[Dict[str, float]]]:
        """Match multiple images to labels."""
        best_labels = []
        all_scores_list = []
        
        for image in images:
            label, scores = self.match(image, labels, return_scores=True)
            best_labels.append(label)
            all_scores_list.append(scores)
        
        return best_labels, all_scores_list
    
    def calibrated_match(
        self,
        image: Union[np.ndarray, Image.Image],
        labels: List[str],
        confidence_threshold: Optional[float] = None,
    ) -> Dict:
        """
        Match with calibrated confidence thresholds.
        
        Returns dict with:
            - label: best match
            - confidence: similarity score
            - is_confident: whether above threshold
            - alternatives: top 3 alternatives
        """
        label, scores = self.match(image, labels, return_scores=True)
        confidence = scores[label]
        
        threshold = confidence_threshold or self.threshold
        is_confident = confidence >= threshold
        
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        alternatives = {k: v for k, v in sorted_scores[1:4]}
        
        return {
            "label": label,
            "confidence": confidence,
            "is_confident": is_confident,
            "alternatives": alternatives,
        }
    
    def compute_similarity(
        self,
        image1: Union[np.ndarray, Image.Image],
        image2: Union[np.ndarray, Image.Image],
    ) -> float:
        """Compute similarity between two images."""
        embed1 = self.encode_image(image1)
        embed2 = self.encode_image(image2)
        
        similarity = (embed1 @ embed2.T).item()
        return similarity
    
    def get_embeddings(self, images: List[Union[np.ndarray, Image.Image]]) -> np.ndarray:
        """Get embeddings for multiple images."""
        embeddings = []
        for image in images:
            embed = self.encode_image(image)
            embeddings.append(embed.cpu().numpy())
        
        return np.array(embeddings)


class ObjectPresenceMatcher(VisionLanguageMatcher):
    """
    Specialized matcher for object presence detection.
    Better for CAPTCHA tasks where we need to detect if an object is present.
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.object_descriptions = {
            "bus": "a bus vehicle, a large wooden vehicle with wheels",
            "car": "a car vehicle, an automobile with four wheels",
            "truck": "a truck vehicle, a large cargo vehicle",
            "bicycle": "a bicycle, a two-wheeled vehicle",
            "traffic light": "a traffic light, a red yellow green signal",
            "stop sign": "a stop sign, a red octagon sign",
            "train": "a train, a railway vehicle",
            "motorcycle": "a motorcycle, a two-wheeled motor vehicle",
        }
    
    def match_object_presence(
        self,
        image: Union[np.ndarray, Image.Image],
        object_name: str,
    ) -> float:
        """
        Check if an object is present in the image.
        
        Returns confidence score (0-1).
        """
        if object_name not in self.object_descriptions:
            raise ValueError(f"Unknown object: {object_name}")
        
        description = self.object_descriptions[object_name]
        _, scores = self.match(image, [description], return_scores=True)
        
        return scores[description]
    
    def match_multiple_objects(
        self,
        image: Union[np.ndarray, Image.Image],
        object_names: List[str],
        threshold: float = 0.3,
    ) -> Dict[str, float]:
        """
        Check presence of multiple objects.
        
        Returns dict mapping object names to confidence scores.
        """
        descriptions = [self.object_descriptions[name] for name in object_names]
        
        image_embed = self.encode_image(image)
        text_embeds = [self.encode_text(d) for d in descriptions]
        text_embed_stack = torch.stack(text_embeds, dim=0).squeeze(1).to(self.device)
        
        similarities = (image_embed @ text_embed_stack.T).squeeze(0)
        scores = torch.softmax(similarities, dim=0)
        
        return {object_names[i]: scores[i].item() for i in range(len(object_names))}