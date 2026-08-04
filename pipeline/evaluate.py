"""Evaluate a fine-tuned adapter on a held-out dataset."""

from __future__ import annotations

import time
from pathlib import Path

from pipeline.config import TrainingConfig
from pipeline.data import load_jsonl, build_dataset


def run_evaluation(
    config: TrainingConfig,
    adapter_path: str | None = None,
) -> dict:
    """Load the base model + LoRA adapter, run inference on eval set, and report metrics.

    Returns a dict with keys: avg_loss, perplexity, samples_per_second, num_samples.
    """
    import torch  # lazy — heavy GPU dep

    adapter_path = adapter_path or str(Path(config.output_dir) / "lora-adapter")

    print(f"[eval] loading base model {config.hf_model_id} …")
    from unsloth import FastLanguageModel

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=config.hf_model_id,
        max_seq_length=config.max_seq_length,
        load_in_4bit=config.load_in_4bit,
        dtype=None,
    )

    # Load the fine-tuned LoRA adapter
    print(f"[eval] loading adapter from {adapter_path} …")
    from peft import PeftModel  # noqa: E402

    model = PeftModel.from_pretrained(model, adapter_path)
    model.eval()

    # Prep eval data
    if config.eval_data_path:
        eval_records = load_jsonl(config.eval_data_path)
    else:
        all_records = load_jsonl(config.data_path)
        eval_records = all_records[-max(1, int(len(all_records) * 0.1)):]

    eval_ds = build_dataset(eval_records, config.chat_template)
    print(f"[eval] {len(eval_ds)} evaluation samples")

    # ---------- loss computation ----------
    total_loss = 0.0
    total_tokens = 0
    start_time = time.time()

    for batch in eval_ds.select(range(min(len(eval_ds), 200))):
        text = batch["text"]
        inputs = tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=config.max_seq_length,
        ).to(model.device)

        with torch.no_grad():
            outputs = model(**inputs, labels=inputs["input_ids"])
            total_loss += outputs.loss.item() * inputs["input_ids"].numel()
            total_tokens += inputs["input_ids"].numel()

    elapsed = time.time() - start_time
    avg_loss = total_loss / total_tokens if total_tokens else float("inf")
    perplexity = torch.exp(torch.tensor(avg_loss)).item()

    metrics = {
        "avg_loss": round(avg_loss, 4),
        "perplexity": round(perplexity, 2),
        "num_samples": len(eval_ds),
        "elapsed_seconds": round(elapsed, 1),
    }
    print(f"[eval] {metrics}")
    return metrics
