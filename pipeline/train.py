"""Fine-tuning with Unsloth — SFT and DPO training."""

from __future__ import annotations

import gc
import os
from pathlib import Path

from pipeline.config import DPOConfig as DPOConfig_, TrainingConfig
from pipeline.data import (
    build_dataset,
    build_dpo_dataset,
    load_jsonl,
    train_eval_split,
)

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


# ===================================================================
# SFT Training
# ===================================================================
def run_training(config: TrainingConfig) -> str:
    """Fine-tune a model via Unsloth SFT. Returns adapter path."""
    import torch
    from transformers import TrainingArguments
    from trl import SFTTrainer

    # ---------- data ----------
    print(f"[data] loading {config.data_path} …")
    records = load_jsonl(config.data_path, mode="sft")
    print(f"[data] {len(records)} records")

    if config.eval_data_path:
        train_ds = build_dataset(records, config.chat_template)
        eval_records = load_jsonl(config.eval_data_path, mode="sft")
        eval_ds = build_dataset(eval_records, config.chat_template)
    else:
        train_ds, eval_ds = train_eval_split(
            records, eval_ratio=0.1, seed=config.seed, template=config.chat_template
        )
    print(f"[data] train={len(train_ds)}  eval={len(eval_ds)}")

    # ---------- model ----------
    print(f"[model] loading {config.hf_model_id} …")
    from unsloth import FastLanguageModel

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=config.hf_model_id,
        max_seq_length=config.max_seq_length,
        load_in_4bit=config.load_in_4bit,
        dtype=None,
    )

    # ---------- LoRA (upgraded: r=32, all linear layers) ----------
    model = FastLanguageModel.get_peft_model(
        model,
        r=config.lora_r,
        target_modules=config.lora_target_modules,
        lora_alpha=config.lora_alpha,
        lora_dropout=config.lora_dropout,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=config.seed,
    )

    # ---------- training args ----------
    use_bf16 = config.bf16 and torch.cuda.is_bf16_supported()
    training_args = TrainingArguments(
        per_device_train_batch_size=config.per_device_train_batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        warmup_ratio=config.warmup_ratio,
        weight_decay=config.weight_decay,
        num_train_epochs=config.num_train_epochs,
        max_steps=config.max_steps if config.max_steps > 0 else -1,
        learning_rate=config.learning_rate,
        fp16=not use_bf16,
        bf16=use_bf16,
        logging_steps=config.logging_steps,
        save_steps=config.save_steps,
        eval_strategy="steps",
        eval_steps=config.eval_steps or config.save_steps,
        output_dir=config.output_dir,
        optim="adamw_8bit",
        seed=config.seed,
        run_name=config.ollama_model_name,
        report_to="none",
        dataloader_num_workers=2,
        save_total_limit=3,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
    )

    # ---------- train ----------
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        dataset_text_field="text",
        max_seq_length=config.max_seq_length,
        packing=config.packing,
        args=training_args,
    )

    print(f"[train] starting SFT — {config.num_train_epochs} epochs, lr={config.learning_rate}, r={config.lora_r} …")
    trainer.train()

    # ---------- save ----------
    adapter_dir = Path(config.output_dir) / "lora-adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    print(f"[train] adapter saved → {adapter_dir}")

    del model, tokenizer, trainer
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return str(adapter_dir)


# ===================================================================
# DPO Training
# ===================================================================
def run_dpo_training(config: DPOConfig_) -> str:
    """Direct Preference Optimization — align the model with chosen/rejected pairs."""
    import torch
    from trl import DPOTrainer
    from transformers import TrainingArguments

    print(f"[dpo] loading {config.data_path} …")
    records = load_jsonl(config.data_path, mode="dpo")
    print(f"[dpo] {len(records)} preference pairs")

    # Split
    rng = __import__("random").Random(config.seed)
    shuffled = list(records)
    rng.shuffle(shuffled)
    split = max(1, int(len(shuffled) * 0.9))
    train_records, eval_records = shuffled[:split], shuffled[split:]

    train_ds = build_dpo_dataset(train_records, config.chat_template)
    eval_ds = build_dpo_dataset(eval_records, config.chat_template)
    print(f"[dpo] train={len(train_ds)}  eval={len(eval_ds)}")

    # ---------- model ----------
    print(f"[dpo] loading {config.hf_model_id} …")
    from unsloth import FastLanguageModel, PatchDPOTrainer

    PatchDPOTrainer()

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=config.hf_model_id,
        max_seq_length=config.max_seq_length,
        load_in_4bit=config.load_in_4bit,
        dtype=None,
    )

    # Load SFT adapter if provided
    if config.sft_adapter_path:
        from peft import PeftModel
        print(f"[dpo] loading SFT adapter from {config.sft_adapter_path} …")
        model = PeftModel.from_pretrained(model, config.sft_adapter_path)

    model = FastLanguageModel.get_peft_model(
        model,
        r=config.lora_r,
        target_modules=config.lora_target_modules,
        lora_alpha=config.lora_alpha,
        lora_dropout=config.lora_dropout,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=config.seed,
    )

    # ---------- training ----------
    use_bf16 = config.bf16 and torch.cuda.is_bf16_supported()
    training_args = TrainingArguments(
        per_device_train_batch_size=config.per_device_train_batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        warmup_ratio=config.warmup_ratio,
        weight_decay=config.weight_decay,
        num_train_epochs=config.num_train_epochs,
        max_steps=config.max_steps if config.max_steps > 0 else -1,
        learning_rate=config.learning_rate,
        fp16=not use_bf16,
        bf16=use_bf16,
        logging_steps=config.logging_steps,
        save_steps=config.save_steps,
        eval_strategy="steps",
        eval_steps=config.eval_steps or config.save_steps,
        output_dir=config.output_dir,
        optim="adamw_8bit",
        seed=config.seed,
        run_name=config.ollama_model_name,
        report_to="none",
        dataloader_num_workers=2,
        save_total_limit=3,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
    )

    dpo_trainer = DPOTrainer(
        model=model,
        ref_model=None,
        args=training_args,
        beta=config.beta,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        tokenizer=tokenizer,
        max_length=config.max_length,
        max_prompt_length=config.max_prompt_length,
    )

    print(f"[dpo] starting DPO — {config.num_train_epochs} epochs, lr={config.learning_rate}, beta={config.beta} …")
    dpo_trainer.train()

    # ---------- save ----------
    adapter_dir = Path(config.output_dir) / "lora-adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    print(f"[dpo] adapter saved → {adapter_dir}")

    del model, tokenizer, dpo_trainer
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return str(adapter_dir)
