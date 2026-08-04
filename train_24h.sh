#!/usr/bin/env bash
# ==========================================================================
# Quick 24hr training launcher — run this on any Linux GPU machine.
#
# Usage:
#   chmod +x train_24h.sh
#   ./train_24h.sh
#
# Requirements: CUDA GPU (16GB+ VRAM), Python 3.10+, git
# ==========================================================================
set -euo pipefail

echo "========================================"
echo "  Qwen 2.5 Solver — 24hr Training Run"
echo "========================================"

# --- detect GPU ---
VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 0)

if [ "$VRAM" -ge 35000 ]; then
    MODEL="qwen2.5-14b"
    LORA_R=64
    BATCH=4
    SEQ=4096
elif [ "$VRAM" -ge 20000 ]; then
    MODEL="qwen2.5-7b"
    LORA_R=64
    BATCH=4
    SEQ=4096
else
    MODEL="qwen2.5-7b"
    LORA_R=32
    BATCH=2
    SEQ=2048
fi

echo "GPU VRAM: ${VRAM}MB → Model: $MODEL, LoRA r=$LORA_R, Batch=$BATCH, Seq=$SEQ"

# --- install deps ---
echo ""
echo "[1/4] Installing dependencies..."
pip install -q unsloth transformers datasets trl peft accelerate bitsandbytes xformers sentencepiece protobuf requests beautifulsoup4 2>&1 | tail -1
pip install -q flash-attn --no-build-isolation 2>/dev/null || true
echo "✓ Dependencies ready"

# --- download data ---
echo ""
echo "[2/4] Downloading training datasets..."
python3 -m pipeline.datasets download --dataset all --max 3000 2>&1 | tail -5
echo "✓ Data downloaded"

# --- train ---
echo ""
echo "[3/4] Starting 24-hour training — this will run until complete..."
echo "       Checkpoints saved every 100 steps to output/solver/"
echo ""
python3 -m pipeline.cli train \
    --data data/train_merged.jsonl \
    --model "$MODEL" \
    --output output/solver \
    --ollama-name solver-v1 \
    --epochs 3 \
    --lr 1e-4 \
    --lora-r "$LORA_R" \
    --lora-alpha $((LORA_R * 2)) \
    --batch-size "$BATCH" \
    --grad-accum 4 \
    --seq-length "$SEQ" \
    --warmup-ratio 0.1 \
    --weight-decay 0.01 \
    --packing \
    --quant q5_k_m

echo ""
echo "✓ Training complete!"

# --- export ---
echo ""
echo "[4/4] Exporting to GGUF + Ollama Modelfile..."
python3 -m pipeline.cli export \
    --data data/train_merged.jsonl \
    --model "$MODEL" \
    --output output/solver \
    --ollama-name solver-v1 \
    --quant q5_k_m

echo ""
echo "========================================"
echo "  DONE! Model exported to:"
echo "  output/solver/ollama-model/"
echo ""
echo "  Register with Ollama:"
echo "  cd output/solver/ollama-model"
echo "  ollama create solver-v1 -f Modelfile"
echo "  ollama run solver-v1"
echo "========================================"
