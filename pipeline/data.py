"""Dataset loading, validation, formatting — SFT and DPO support."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datasets import Dataset


# ---------------------------------------------------------------------------
# Schema - SFT
# ---------------------------------------------------------------------------
SFT_REQUIRED = {"instruction", "output"}
SFT_OPTIONAL = {"input", "system"}

# Schema - DPO
DPO_REQUIRED = {"prompt", "chosen", "rejected"}


def _validate_sft(rec: dict, idx: int) -> None:
    if not isinstance(rec, dict):
        raise TypeError(f"Record {idx}: expected dict, got {type(rec).__name__}")
    missing = SFT_REQUIRED - rec.keys()
    if missing:
        raise KeyError(f"Record {idx}: missing required keys: {missing}")


def _validate_dpo(rec: dict, idx: int) -> None:
    if not isinstance(rec, dict):
        raise TypeError(f"Record {idx}: expected dict, got {type(rec).__name__}")
    missing = DPO_REQUIRED - rec.keys()
    if missing:
        raise KeyError(f"Record {idx}: missing required DPO keys: {missing}")


def load_jsonl(path: str | Path, mode: str = "sft") -> list[dict]:
    """Load and validate a JSONL file.

    Args:
        path: Path to the JSONL file.
        mode: 'sft' (instruction/output) or 'dpo' (prompt/chosen/rejected).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {path}")

    validator = _validate_dpo if mode == "dpo" else _validate_sft
    records: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Line {lineno}: invalid JSON — {exc}") from exc
            validator(rec, lineno)
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# SFT Formatting — raw records → plain-text training strings
# ---------------------------------------------------------------------------
CHATML_SYSTEM = "<|im_start|>system\n{system}<|im_end|>\n"
CHATML_USER = "<|im_start|>user\n{instruction}\n{input}<|im_end|>\n"
CHATML_ASSISTANT = "<|im_start|>assistant\n{output}<|im_end|>\n"

LLAMA3_SYSTEM = "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
LLAMA3_USER = "<|start_header_id|>user<|end_header_id|>\n\n{instruction}\n{input}<|eot_id|>"
LLAMA3_ASSISTANT = "<|start_header_id|>assistant<|end_header_id|>\n\n{output}<|eot_id|>"


def format_record(rec: dict, template: str = "chatml") -> str:
    """Convert a SFT record into one text string."""
    system = rec.get("system", "")
    user_input = rec.get("input", "")

    if template == "chatml":
        parts: list[str] = []
        if system:
            parts.append(CHATML_SYSTEM.format(system=system))
        parts.append(CHATML_USER.format(instruction=rec["instruction"], input=user_input))
        parts.append(CHATML_ASSISTANT.format(output=rec["output"]))
        return "".join(parts)

    if template == "llama3":
        parts = []
        if system:
            parts.append(LLAMA3_SYSTEM.format(system=system))
        parts.append(LLAMA3_USER.format(instruction=rec["instruction"], input=user_input))
        parts.append(LLAMA3_ASSISTANT.format(output=rec["output"]))
        return "".join(parts)

    raise ValueError(f"Unknown chat template: {template}")


# ---------------------------------------------------------------------------
# DPO Formatting — prompt/chosen/rejected → chatml
# ---------------------------------------------------------------------------
def format_dpo_prompt(prompt: str, template: str = "chatml") -> str:
    """Format a DPO prompt (user message) for the chat template."""
    if template == "chatml":
        return f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
    if template == "llama3":
        return f"<|start_header_id|>user<|end_header_id|>\n\n{prompt}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"
    raise ValueError(f"Unknown chat template: {template}")


def format_dpo_record(rec: dict, template: str = "chatml") -> dict:
    """Convert a DPO record into the format expected by DPOTrainer."""
    return {
        "prompt": format_dpo_prompt(rec["prompt"], template),
        "chosen": rec["chosen"] + ("<|im_end|>" if template == "chatml" else "<|eot_id|>"),
        "rejected": rec["rejected"] + ("<|im_end|>" if template == "chatml" else "<|eot_id|>"),
    }


# ---------------------------------------------------------------------------
# SFT Dataset builder
# ---------------------------------------------------------------------------
def build_dataset(
    records: list[dict],
    template: str = "chatml",
) -> "Dataset":
    """Format all SFT records → HuggingFace Dataset with a 'text' column."""
    from datasets import Dataset

    texts = [format_record(r, template) for r in records]
    return Dataset.from_dict({"text": texts})


def build_dpo_dataset(
    records: list[dict],
    template: str = "chatml",
) -> "Dataset":
    """Format DPO records → HuggingFace Dataset with prompt/chosen/rejected columns."""
    from datasets import Dataset

    formatted = [format_dpo_record(r, template) for r in records]
    return Dataset.from_dict({
        "prompt": [r["prompt"] for r in formatted],
        "chosen": [r["chosen"] for r in formatted],
        "rejected": [r["rejected"] for r in formatted],
    })


def train_eval_split(
    records: list[dict],
    eval_ratio: float = 0.1,
    seed: int = 42,
    template: str = "chatml",
) -> tuple["Dataset", "Dataset"]:
    """Shuffle and split SFT records into train/eval Datasets."""
    rng = random.Random(seed)
    shuffled = list(records)
    rng.shuffle(shuffled)

    split_idx = max(1, int(len(shuffled) * (1 - eval_ratio)))
    return (
        build_dataset(shuffled[:split_idx], template),
        build_dataset(shuffled[split_idx:], template),
    )
