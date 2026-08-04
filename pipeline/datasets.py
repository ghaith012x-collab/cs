"""Download & merge open-source instruction datasets from HuggingFace.

Fetches the best reasoning + code datasets for fine-tuning:
- LeetCodeDataset (competitive programming)
- NuminaMath-CoT (math reasoning)
- OpenOrca (general reasoning traces)
- CodeAlpaca / Magicoder (code instruction)

Usage:
    python -m pipeline.datasets download --dataset leetcode
    python -m pipeline.datasets download --dataset all --output data/hf_merged.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Known high-quality datasets on HuggingFace
# ---------------------------------------------------------------------------
AVAILABLE_DATASETS: dict[str, dict[str, Any]] = {
    "leetcode": {
        "hf_id": "newfacade/LeetCodeDataset",
        "split": "train",
        "description": "LeetCode problems with test cases — great for code reasoning",
        "field_map": {
            "instruction": "content",
            "output": "python_solution",
        },
        "max_rows": 3000,
    },
    "numina-math": {
        "hf_id": "AI-MO/NuminaMath-CoT",
        "split": "train",
        "description": "Math problems with chain-of-thought solutions",
        "field_map": {
            "instruction": "problem",
            "output": "solution",
        },
        "max_rows": 5000,
    },
    "open-orca": {
        "hf_id": "Open-Orca/OpenOrca",
        "split": "train",
        "description": "GPT-augmented FLAN reasoning traces — broad general reasoning",
        "field_map": {
            "instruction": "question",
            "output": "response",
        },
        "max_rows": 5000,
    },
    "code-alpaca": {
        "hf_id": "sahil2801/CodeAlpaca-20k",
        "split": "train",
        "description": "20k code generation instruction pairs",
        "field_map": {
            "instruction": "instruction",
            "output": "output",
        },
        "max_rows": 5000,
    },
    "magicoder": {
        "hf_id": "ise-uiuc/Magicoder-OSS-Instruct-75K",
        "split": "train",
        "description": "75k open-source code instruction pairs via Evol-Instruct",
        "field_map": {
            "instruction": "problem",
            "output": "solution",
        },
        "max_rows": 5000,
    },
    "dpo-math": {
        "hf_id": "argilla/distilabel-math-preference-dpo",
        "split": "train",
        "description": "Math preference pairs — chosen vs rejected solutions (DPO-ready)",
        "field_map": {
            "instruction": "prompt",
            "chosen": "chosen",
            "rejected": "rejected",
        },
        "max_rows": 3000,
    },
}


def download_dataset(
    name: str,
    output: str | None = None,
    max_rows: int | None = None,
) -> list[dict]:
    """Download a dataset from HuggingFace and convert to our JSONL format."""
    from datasets import load_dataset

    cfg = AVAILABLE_DATASETS[name]
    hf_id: str = cfg["hf_id"]
    split: str = cfg["split"]
    field_map: dict[str, str] = cfg["field_map"]
    limit = max_rows or cfg.get("max_rows", 5000)

    print(f"[{name}] downloading {hf_id} ({split}) …")
    ds = load_dataset(hf_id, split=split, streaming=True)

    records: list[dict] = []
    has_chosen = "chosen" in field_map

    for row in ds:
        if len(records) >= limit:
            break

        try:
            instruction = str(row.get(field_map["instruction"], ""))
            output_key = "chosen" if has_chosen else "output"

            rec: dict[str, str] = {
                "instruction": instruction.strip(),
                "output": str(row.get(field_map[output_key], "")).strip(),
                "source": name,
            }

            if has_chosen:
                rec["chosen"] = str(row.get(field_map["chosen"], "")).strip()
                rec["rejected"] = str(row.get(field_map["rejected"], "")).strip()

            # Skip empty instructions
            if len(rec["instruction"]) < 10:
                continue

            records.append(rec)
        except Exception:
            continue

    print(f"[{name}] {len(records)} records downloaded")

    if output:
        _save_jsonl(output, records)

    return records


def download_all(output_dir: str = "data/hf", max_per_dataset: int | None = None) -> dict[str, int]:
    """Download all available datasets."""
    counts: dict[str, int] = {}
    for name in AVAILABLE_DATASETS:
        out = f"{output_dir}/{name}.jsonl"
        records = download_dataset(name, output=out, max_rows=max_per_dataset)
        counts[name] = len(records)
    return counts


def merge_datasets(
    paths: list[str],
    output: str = "data/hf_merged.jsonl",
    deduplicate: bool = True,
) -> int:
    """Merge multiple JSONL files, optionally deduplicating by instruction."""
    seen: set[str] = set()
    merged: list[dict] = []

    for path in paths:
        if not os.path.exists(path):
            print(f"  [!] skipping missing file: {path}")
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                key = rec.get("instruction", "")
                if deduplicate:
                    key_norm = key.strip().lower()[:200]
                    if key_norm in seen:
                        continue
                    seen.add(key_norm)
                merged.append(rec)

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        for rec in merged:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[merge] {len(merged)} records → {output}")
    return len(merged)


def _save_jsonl(path: str, records: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ===================================================================
# CLI
# ===================================================================
def main() -> None:
    p = argparse.ArgumentParser(
        description="Download open-source datasets from HuggingFace for fine-tuning"
    )
    sub = p.add_subparsers(dest="cmd", help="Action")

    dl = sub.add_parser("download", help="Download a dataset")
    dl.add_argument(
        "--dataset",
        choices=list(AVAILABLE_DATASETS) + ["all"],
        default="all",
        help="Dataset to download",
    )
    dl.add_argument("--output-dir", default="data/hf", help="Output directory")
    dl.add_argument("--max", type=int, default=None, help="Max rows per dataset")

    mg = sub.add_parser("merge", help="Merge downloaded JSONL files")
    mg.add_argument("--input-dir", default="data/hf", help="Directory with JSONL files")
    mg.add_argument("--output", default="data/train_merged.jsonl", help="Merged output file")

    args = p.parse_args()

    if args.cmd == "download":
        if args.dataset == "all":
            counts = download_all(args.output_dir, max_per_dataset=args.max)
            print(f"\n✓ Downloaded {sum(counts.values())} total records from {len(counts)} datasets")

            # Auto-merge
            import glob
            files = sorted(glob.glob(f"{args.output_dir}/*.jsonl"))
            if files:
                merge_datasets(files, output="data/train_merged.jsonl")
        else:
            out = f"{args.output_dir}/{args.dataset}.jsonl"
            download_dataset(args.dataset, output=out, max_rows=args.max)

    elif args.cmd == "merge":
        import glob
        files = sorted(glob.glob(f"{args.input_dir}/*.jsonl"))
        if not files:
            print("[!] No JSONL files found. Run 'download' first.")
            return
        merge_datasets(files, output=args.output)

    else:
        p.print_help()


if __name__ == "__main__":
    main()
