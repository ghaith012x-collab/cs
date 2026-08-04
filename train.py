#!/usr/bin/env python3
"""Fine-tune an LLM with Unsloth and export to Ollama.

Usage:
    # Full pipeline (train + eval + export)
    python train.py all --data data/train.jsonl --model qwen2.5-7b

    # Just train
    python train.py train --data data/train.jsonl --model qwen2.5-7b

    # Just evaluate
    python train.py eval --data data/train.jsonl --model qwen2.5-7b

    # Just export
    python train.py export --data data/train.jsonl --model qwen2.5-7b

See all options:
    python train.py --help
    python train.py train --help
"""

from pipeline.cli import main

if __name__ == "__main__":
    main()
