"""Central configuration for the Ollama fine-tuning pipeline.

Supports both SFT (Supervised Fine-Tuning) and DPO (Direct Preference Optimization).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Known base models — Unsloth-optimised HuggingFace IDs
# ---------------------------------------------------------------------------
BASE_MODELS: dict[str, dict[str, str]] = {
    # Qwen 2.5 family — best balance of speed + accuracy
    "qwen2.5-3b": {
        "hf_id": "unsloth/Qwen2.5-3B-Instruct-bnb-4bit",
        "chat_template": "chatml",
        "description": "Qwen 2.5 3B — ultra-fast, good for simple tasks",
    },
    "qwen2.5-7b": {
        "hf_id": "unsloth/Qwen2.5-7B-Instruct-bnb-4bit",
        "chat_template": "chatml",
        "description": "Qwen 2.5 7B — best speed/accuracy sweet spot",
    },
    "qwen2.5-14b": {
        "hf_id": "unsloth/Qwen2.5-14B-Instruct-bnb-4bit",
        "chat_template": "chatml",
        "description": "Qwen 2.5 14B — higher accuracy, needs more VRAM",
    },
    "qwen2.5-coder-7b": {
        "hf_id": "unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit",
        "chat_template": "chatml",
        "description": "Qwen 2.5 Coder 7B — top-tier code generation",
    },
    # Llama 3.2 family — great general-purpose fallback
    "llama3.2-3b": {
        "hf_id": "unsloth/Llama-3.2-3B-Instruct",
        "chat_template": "llama3",
        "description": "Llama 3.2 3B — tiny, fast, good for agent tasks",
    },
}

DEFAULT_MODEL = "qwen2.5-7b"


# ===================================================================
# Training Config (SFT)
# ===================================================================
@dataclass
class TrainingConfig:
    """All hyper-parameters and paths for an SFT fine-tuning run."""

    # --- Paths --------------------------------------------------------------
    data_path: str = "data/train.jsonl"
    output_dir: str = "output"
    eval_data_path: str | None = None

    # --- Model --------------------------------------------------------------
    model_key: str = DEFAULT_MODEL
    max_seq_length: int = 2048
    load_in_4bit: bool = True

    # --- LoRA (upgraded defaults for max accuracy) --------------------------
    lora_r: int = 32
    lora_alpha: int = 32
    lora_dropout: float = 0.0
    lora_target_modules: list[str] = field(default_factory=lambda: [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ])

    # --- Training -----------------------------------------------------------
    per_device_train_batch_size: int = 2
    gradient_accumulation_steps: int = 8  # effective batch size = 16
    learning_rate: float = 2e-4
    warmup_ratio: float = 0.1  # 10% warmup — better than fixed steps
    weight_decay: float = 0.01
    num_train_epochs: int = 2  # multi-epoch for small datasets
    max_steps: int = -1  # -1 = use epochs instead
    logging_steps: int = 10
    save_steps: int = 100
    eval_steps: int | None = None
    seed: int = 42

    # --- Data packing -------------------------------------------------------
    packing: bool = False  # pack multiple short samples into one sequence

    # --- Precision ----------------------------------------------------------
    bf16: bool = True

    # --- Ollama export ------------------------------------------------------
    ollama_model_name: str = "custom-model"
    quantization: Literal["q4_k_m", "q5_k_m", "f16"] = "q5_k_m"

    @property
    def hf_model_id(self) -> str:
        entry = BASE_MODELS.get(self.model_key)
        if entry is None:
            raise KeyError(
                f"Unknown model key '{self.model_key}'. "
                f"Available: {list(BASE_MODELS)}"
            )
        return entry["hf_id"]

    @property
    def chat_template(self) -> str:
        entry = BASE_MODELS.get(self.model_key)
        assert entry is not None
        return entry["chat_template"]


# ===================================================================
# DPO Config — Direct Preference Optimization
# ===================================================================
@dataclass
class DPOConfig:
    """Configuration for Direct Preference Optimization (DPO) training.

    DPO trains on (prompt, chosen, rejected) triples to align the model
    with human preferences — no separate reward model needed.
    """

    # --- Paths --------------------------------------------------------------
    data_path: str = "data/dpo_train.jsonl"
    output_dir: str = "output-dpo"
    sft_adapter_path: str | None = None  # optional: start from SFT checkpoint

    # --- Model --------------------------------------------------------------
    model_key: str = DEFAULT_MODEL
    max_seq_length: int = 2048
    max_prompt_length: int = 1024
    load_in_4bit: bool = True

    # --- LoRA ---------------------------------------------------------------
    lora_r: int = 32
    lora_alpha: int = 32
    lora_dropout: float = 0.0
    lora_target_modules: list[str] = field(default_factory=lambda: [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ])

    # --- DPO Training -------------------------------------------------------
    per_device_train_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    learning_rate: float = 5e-7
    warmup_ratio: float = 0.1
    weight_decay: float = 0.01
    num_train_epochs: int = 1  # DPO: keep epochs low to avoid alignment tax
    max_steps: int = -1
    logging_steps: int = 10
    save_steps: int = 100
    eval_steps: int | None = None
    seed: int = 42

    # --- DPO-specific -------------------------------------------------------
    beta: float = 0.1  # deviation from reference model (0.05-0.3)
    max_length: int = 2048

    # --- Precision ----------------------------------------------------------
    bf16: bool = True

    # --- Ollama export ------------------------------------------------------
    ollama_model_name: str = "custom-model-dpo"
    quantization: Literal["q4_k_m", "q5_k_m", "f16"] = "q5_k_m"

    @property
    def hf_model_id(self) -> str:
        entry = BASE_MODELS.get(self.model_key)
        if entry is None:
            raise KeyError(
                f"Unknown model key '{self.model_key}'. "
                f"Available: {list(BASE_MODELS)}"
            )
        return entry["hf_id"]

    @property
    def chat_template(self) -> str:
        entry = BASE_MODELS.get(self.model_key)
        assert entry is not None
        return entry["chat_template"]
