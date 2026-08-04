"""Captcha-focused fine-tuning — second pass on top of general coding SFT.

This module does a quick, focused fine-tuning pass specifically on captcha
challenges. Use it AFTER the main SFT training completes.

Best run with Qwen 2.5 3B for maximum speed (captchas are simple enough).
"""

from __future__ import annotations

import gc
import os
from pathlib import Path

from pipeline.captcha_data import generate_captcha_dataset
from pipeline.data import build_dataset

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def run_captcha_training(
    base_model: str = "unsloth/Qwen2.5-3B-Instruct-bnb-4bit",
    sft_adapter: str | None = None,
    num_examples: int = 3000,
    output_dir: str = "output/captcha-solver",
    ollama_name: str = "captcha-solver-v1",
) -> str:
    """Fine-tune a model specifically for captcha solving.

    Args:
        base_model: HF model ID (use 3B for speed, 7B for accuracy)
        sft_adapter: Optional path to previously trained SFT adapter
        num_examples: How many synthetic captcha examples to generate
        output_dir: Where to save
        ollama_name: Ollama model name

    Returns path to the saved adapter.
    """
    import torch
    from transformers import TrainingArguments
    from trl import SFTTrainer

    print(f"[captcha] generating {num_examples} training examples …")
    records = generate_captcha_dataset(num_examples)
    train_ds = build_dataset(records, "chatml")
    print(f"[captcha] {len(train_ds)} examples generated")

    print(f"[captcha] loading {base_model} …")
    from unsloth import FastLanguageModel

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=base_model,
        max_seq_length=1024,  # captchas are short
        load_in_4bit=True,
        dtype=None,
    )

    # If we have a previous SFT adapter, load it first
    if sft_adapter and Path(sft_adapter).exists():
        from peft import PeftModel

        print(f"[captcha] loading SFT adapter from {sft_adapter} …")
        model = PeftModel.from_pretrained(model, sft_adapter)

    # LoRA — smaller rank for speed, captcha patterns are simple
    model = FastLanguageModel.get_peft_model(
        model,
        r=16,  # lower rank = faster training on T4
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_alpha=16,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=42,
    )

    use_bf16 = torch.cuda.is_bf16_supported()
    training_args = TrainingArguments(
        per_device_train_batch_size=4,  # bigger batch for captcha (shorter seqs)
        gradient_accumulation_steps=4,
        warmup_ratio=0.1,
        num_train_epochs=3,
        learning_rate=5e-5,  # lower LR for second pass — refine, don't overwrite
        fp16=not use_bf16,
        bf16=use_bf16,
        logging_steps=10,
        save_steps=100,
        output_dir=output_dir,
        optim="adamw_8bit",
        seed=42,
        report_to="none",
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train_ds,
        dataset_text_field="text",
        max_seq_length=1024,
        args=training_args,
    )

    print(f"[captcha] training — 3 epochs, lr=5e-5, batch=4 …")
    trainer.train()

    adapter_dir = Path(output_dir) / "lora-adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    print(f"[captcha] adapter saved → {adapter_dir}")

    del model, tokenizer, trainer
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return str(adapter_dir)
