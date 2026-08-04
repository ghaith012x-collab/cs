"""Command-line interface — full pipeline: scrape → download → SFT → DPO → export."""

from __future__ import annotations

import argparse
import sys

from pipeline.config import BASE_MODELS, DPOConfig, TrainingConfig


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="train.py",
        description="Fine-tune an LLM with Unsloth and export to Ollama",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_epilog(),
    )
    sub = p.add_subparsers(dest="command", help="Pipeline step")

    # ── scrape ────────────────────────────────────────────────────────
    sc = sub.add_parser("scrape", help="Scrape training data from web sources")
    sc.add_argument("--source", choices=["all", "github", "leetcode", "stackoverflow"],
                    default="all")
    sc.add_argument("--output-dir", default="data")
    sc.add_argument("--max", type=int, default=300)
    sc.add_argument("--repo", default="psf/requests")
    sc.add_argument("--tag", default="python")
    sc.add_argument("--github-token", default=None)

    # ── download ──────────────────────────────────────────────────────
    dl = sub.add_parser("download", help="Download HF datasets (LeetCode, NuminaMath, etc.)")
    dl.add_argument("--dataset", choices=["leetcode", "numina-math", "open-orca",
                                          "code-alpaca", "magicoder", "dpo-math", "all"],
                    default="all")
    dl.add_argument("--output-dir", default="data/hf")
    dl.add_argument("--max", type=int, default=None, help="Max rows per dataset")

    # ── train (SFT) ───────────────────────────────────────────────────
    t = sub.add_parser("train", help="Supervised fine-tuning (SFT)")
    _add_sft_args(t)

    # ── dpo ───────────────────────────────────────────────────────────
    d = sub.add_parser("dpo", help="Direct Preference Optimization")
    _add_dpo_args(d)

    # ── eval ──────────────────────────────────────────────────────────
    e = sub.add_parser("eval", help="Evaluate a trained adapter")
    _add_sft_args(e)
    e.add_argument("--adapter", type=str, default=None)

    # ── export ────────────────────────────────────────────────────────
    x = sub.add_parser("export", help="Export to GGUF + Modelfile")
    _add_sft_args(x)

    # ── all ───────────────────────────────────────────────────────────
    a = sub.add_parser("all", help="Full SFT pipeline: train → eval → export")
    _add_sft_args(a)

    # ── full ──────────────────────────────────────────────────────────
    f = sub.add_parser("full", help="Everything: scrape → download → SFT → DPO → export")
    _add_sft_args(f)
    f.add_argument("--dpo-data", type=str, default="data/dpo_train.jsonl")
    f.add_argument("--dpo-beta", type=float, default=0.1)
    f.add_argument("--dpo-lr", type=float, default=5e-7)
    f.add_argument("--dpo-epochs", type=int, default=1)
    f.add_argument("--skip-scrape", action="store_true")
    f.add_argument("--skip-download", action="store_true")
    f.add_argument("--skip-dpo", action="store_true")

    return p


# ── arg helpers ──────────────────────────────────────────────────────────
def _add_sft_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", default="data/train.jsonl")
    p.add_argument("--eval-data", default=None)
    p.add_argument("--model", default="qwen2.5-7b", choices=list(BASE_MODELS))
    p.add_argument("--output", default="output")
    p.add_argument("--ollama-name", default="custom-model")
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--max-steps", type=int, default=-1)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--seq-length", type=int, default=2048)
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--warmup-ratio", type=float, default=0.1)
    p.add_argument("--weight-decay", type=float, default=0.01)
    p.add_argument("--packing", action="store_true")
    p.add_argument("--lora-r", type=int, default=32)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--quant", default="q5_k_m", choices=["q4_k_m", "q5_k_m", "f16"])
    p.add_argument("--seed", type=int, default=42)


def _add_dpo_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", default="data/dpo_train.jsonl")
    p.add_argument("--model", default="qwen2.5-7b", choices=list(BASE_MODELS))
    p.add_argument("--output", default="output-dpo")
    p.add_argument("--ollama-name", default="custom-model-dpo")
    p.add_argument("--sft-adapter", default=None, help="Start from SFT checkpoint")
    p.add_argument("--epochs", type=int, default=1)
    p.add_argument("--max-steps", type=int, default=-1)
    p.add_argument("--lr", type=float, default=5e-7)
    p.add_argument("--beta", type=float, default=0.1)
    p.add_argument("--seq-length", type=int, default=2048)
    p.add_argument("--prompt-length", type=int, default=1024)
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--warmup-ratio", type=float, default=0.1)
    p.add_argument("--lora-r", type=int, default=32)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--quant", default="q5_k_m", choices=["q4_k_m", "q5_k_m", "f16"])
    p.add_argument("--seed", type=int, default=42)


def _epilog() -> str:
    lines = ["available models:"]
    for key, info in BASE_MODELS.items():
        lines.append(f"  {key:<22s}  {info['description']}")
    lines.append("")
    lines.append("quickstart:")
    lines.append("  python train.py full  --data data/train_merged.jsonl  --model qwen2.5-7b")
    return "\n".join(lines)


# ===================================================================
# Main dispatcher
# ===================================================================
def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    cmd = args.command

    # ── scrape ────────────────────────────────────────────────────
    if cmd == "scrape":
        from pipeline.scraper import scrape_github_issues, scrape_leetcode, scrape_stackoverflow

        if args.source in ("all", "github"):
            scrape_github_issues(repo=args.repo, token=args.github_token,
                                 max_issues=args.max, output=f"{args.output_dir}/github.jsonl")
        if args.source in ("all", "leetcode"):
            scrape_leetcode(max_problems=args.max, output=f"{args.output_dir}/leetcode.jsonl")
        if args.source in ("all", "stackoverflow"):
            scrape_stackoverflow(max_questions=args.max, tagged=args.tag,
                                 output=f"{args.output_dir}/stackoverflow.jsonl")
        print(f"\n✓ Scraping complete → {args.output_dir}/")

    # ── download ──────────────────────────────────────────────────
    elif cmd == "download":
        from pipeline.datasets import download_all, download_dataset, merge_datasets
        import glob

        if args.dataset == "all":
            download_all(output_dir=args.output_dir, max_per_dataset=args.max)
            files = sorted(glob.glob(f"{args.output_dir}/*.jsonl"))
            if files:
                merge_datasets(files, output="data/train_merged.jsonl")
        else:
            out = f"{args.output_dir}/{args.dataset}.jsonl"
            download_dataset(args.dataset, output=out, max_rows=args.max)
        print(f"\n✓ Download complete → {args.output_dir}/")

    # ── train (SFT) ───────────────────────────────────────────────
    elif cmd in ("train", "all"):
        from pipeline.train import run_training

        config = TrainingConfig(
            data_path=args.data, output_dir=args.output,
            eval_data_path=getattr(args, "eval_data", None),
            model_key=args.model, ollama_model_name=getattr(args, "ollama_name", "custom-model"),
            max_seq_length=getattr(args, "seq_length", 2048),
            per_device_train_batch_size=getattr(args, "batch_size", 2),
            gradient_accumulation_steps=getattr(args, "grad_accum", 8),
            learning_rate=getattr(args, "lr", 2e-4),
            warmup_ratio=getattr(args, "warmup_ratio", 0.1),
            weight_decay=getattr(args, "weight_decay", 0.01),
            num_train_epochs=getattr(args, "epochs", 2),
            max_steps=getattr(args, "max_steps", -1),
            lora_r=getattr(args, "lora_r", 32),
            lora_alpha=getattr(args, "lora_alpha", 32),
            quantization=getattr(args, "quant", "q5_k_m"),
            seed=getattr(args, "seed", 42),
            packing=getattr(args, "packing", False),
        )
        print("=" * 60)
        print("  STEP 1: SFT TRAINING")
        print("=" * 60)
        adapter_path = run_training(config)

        if cmd == "all":
            from pipeline.evaluate import run_evaluation
            from pipeline.export import run_export

            print("\n" + "=" * 60)
            print("  STEP 2: EVALUATION")
            print("=" * 60)
            run_evaluation(config, adapter_path=getattr(args, "adapter", None))

            print("\n" + "=" * 60)
            print("  STEP 3: EXPORT")
            print("=" * 60)
            run_export(config)

    # ── dpo ───────────────────────────────────────────────────────
    elif cmd == "dpo":
        from pipeline.train import run_dpo_training
        from pipeline.export import run_export as run_export_dpo

        dpo_config = DPOConfig(
            data_path=args.data, output_dir=args.output,
            sft_adapter_path=args.sft_adapter,
            model_key=args.model, ollama_model_name=getattr(args, "ollama_name", "custom-model-dpo"),
            max_seq_length=getattr(args, "seq_length", 2048),
            max_prompt_length=getattr(args, "prompt_length", 1024),
            per_device_train_batch_size=getattr(args, "batch_size", 2),
            gradient_accumulation_steps=getattr(args, "grad_accum", 8),
            learning_rate=getattr(args, "lr", 5e-7),
            warmup_ratio=getattr(args, "warmup_ratio", 0.1),
            num_train_epochs=getattr(args, "epochs", 1),
            max_steps=getattr(args, "max_steps", -1),
            beta=getattr(args, "beta", 0.1),
            lora_r=getattr(args, "lora_r", 32),
            lora_alpha=getattr(args, "lora_alpha", 32),
            quantization=getattr(args, "quant", "q5_k_m"),
            seed=getattr(args, "seed", 42),
        )
        print("=" * 60)
        print("  DPO TRAINING")
        print("=" * 60)
        run_dpo_training(dpo_config)

        # Export DPO model
        sft_config = TrainingConfig(
            data_path=args.data, output_dir=args.output,
            model_key=args.model, ollama_model_name=getattr(args, "ollama_name", "custom-model-dpo"),
            max_seq_length=getattr(args, "seq_length", 2048),
            quantization=getattr(args, "quant", "q5_k_m"),
        )
        print("\n" + "=" * 60)
        print("  EXPORT DPO MODEL")
        print("=" * 60)
        run_export_dpo(sft_config)

    # ── eval ──────────────────────────────────────────────────────
    elif cmd == "eval":
        from pipeline.evaluate import run_evaluation

        config = TrainingConfig(
            data_path=args.data, output_dir=args.output,
            eval_data_path=getattr(args, "eval_data", None),
            model_key=args.model,
            max_seq_length=getattr(args, "seq_length", 2048),
        )
        run_evaluation(config, adapter_path=getattr(args, "adapter", None))

    # ── export ────────────────────────────────────────────────────
    elif cmd == "export":
        from pipeline.export import run_export

        config = TrainingConfig(
            data_path=args.data, output_dir=args.output,
            model_key=args.model, ollama_model_name=getattr(args, "ollama_name", "custom-model"),
            max_seq_length=getattr(args, "seq_length", 2048),
            quantization=getattr(args, "quant", "q5_k_m"),
        )
        run_export(config)

    # ── full pipeline ─────────────────────────────────────────────
    elif cmd == "full":
        _run_full_pipeline(args)


# ===================================================================
# Full pipeline: scrape → download → SFT → DPO → export
# ===================================================================
def _run_full_pipeline(args: argparse.Namespace) -> None:
    import glob
    from pipeline.datasets import download_all, merge_datasets

    # Step 0: Scrape
    if not getattr(args, "skip_scrape", False):
        from pipeline.scraper import scrape_github_issues, scrape_leetcode, scrape_stackoverflow
        print("\n" + "=" * 60)
        print("  STEP 0: SCRAPE WEB DATA")
        print("=" * 60)
        scrape_github_issues(repo="psf/requests", max_issues=300, output="data/github.jsonl")
        scrape_leetcode(max_problems=300, output="data/leetcode.jsonl")
        scrape_stackoverflow(max_questions=300, tagged="python", output="data/stackoverflow.jsonl")

    # Step 1: Download HF datasets
    if not getattr(args, "skip_download", False):
        print("\n" + "=" * 60)
        print("  STEP 1: DOWNLOAD HF DATASETS")
        print("=" * 60)
        download_all(output_dir="data/hf", max_per_dataset=None)
        files = sorted(glob.glob("data/hf/*.jsonl"))
        if files:
            merge_datasets(files, output="data/train_merged.jsonl")
        print("✓ Merged dataset → data/train_merged.jsonl")

    # Step 2: SFT
    from pipeline.train import run_training

    sft_config = TrainingConfig(
        data_path=args.data, output_dir=args.output,
        model_key=args.model,
        ollama_model_name=getattr(args, "ollama_name", "custom-model"),
        max_seq_length=getattr(args, "seq_length", 2048),
        per_device_train_batch_size=getattr(args, "batch_size", 2),
        gradient_accumulation_steps=getattr(args, "grad_accum", 8),
        learning_rate=getattr(args, "lr", 2e-4),
        warmup_ratio=getattr(args, "warmup_ratio", 0.1),
        weight_decay=getattr(args, "weight_decay", 0.01),
        num_train_epochs=getattr(args, "epochs", 2),
        lora_r=getattr(args, "lora_r", 32),
        lora_alpha=getattr(args, "lora_alpha", 32),
        quantization=getattr(args, "quant", "q5_k_m"),
        seed=getattr(args, "seed", 42),
        packing=getattr(args, "packing", False),
    )
    print("\n" + "=" * 60)
    print("  STEP 2: SFT TRAINING")
    print("=" * 60)
    run_training(sft_config)

    # Step 3: DPO
    if not getattr(args, "skip_dpo", False) and Path(getattr(args, "dpo_data", "data/dpo_train.jsonl")).exists():
        from pipeline.train import run_dpo_training

        dpo_config = DPOConfig(
            data_path=getattr(args, "dpo_data", "data/dpo_train.jsonl"),
            output_dir=args.output + "-dpo",
            sft_adapter_path=str(Path(args.output) / "lora-adapter"),
            model_key=args.model,
            ollama_model_name=getattr(args, "ollama_name", "custom-model") + "-dpo",
            max_seq_length=getattr(args, "seq_length", 2048),
            learning_rate=getattr(args, "dpo_lr", 5e-7),
            num_train_epochs=getattr(args, "dpo_epochs", 1),
            beta=getattr(args, "dpo_beta", 0.1),
            lora_r=getattr(args, "lora_r", 32),
            lora_alpha=getattr(args, "lora_alpha", 32),
            quantization=getattr(args, "quant", "q5_k_m"),
            seed=getattr(args, "seed", 42),
        )
        print("\n" + "=" * 60)
        print("  STEP 3: DPO TRAINING")
        print("=" * 60)
        run_dpo_training(dpo_config)

    # Step 4: Export
    from pipeline.export import run_export

    print("\n" + "=" * 60)
    print("  FINAL STEP: EXPORT")
    print("=" * 60)
    run_export(sft_config)

    print("\n✓ Full pipeline complete!")


if __name__ == "__main__":
    main()
