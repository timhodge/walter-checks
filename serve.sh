#!/bin/bash
# serve.sh — Start the vLLM inference server
# Works on any GPU with 24GB+ VRAM
# Tested: RTX 4090, RTX PRO 4500, A40, L40S, RTX 6000 Ada
# NOT supported: Blackwell GPUs (RTX PRO 6000, B200) — vLLM not yet compatible
set -e

# Source tool paths if available
[ -f /workspace/.waltercheck-env ] && source /workspace/.waltercheck-env

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_PATH="$SCRIPT_DIR/models/qwen2.5-coder-7b-instruct"

if [ ! -d "$MODEL_PATH" ]; then
    echo "Model not found at $MODEL_PATH"
    echo "Run ./setup.sh first to download the model."
    exit 1
fi

echo "Starting vLLM server..."
echo "Model: Qwen2.5-Coder-7B-Instruct (fp16)"
echo ""
echo "Once you see 'Uvicorn running', open a second terminal and run:"
echo "  python qa-bot/review.py repo repos/<your-repo> --profile wordpress"
echo ""

# Auto-detect VRAM and pick optimal settings
GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)

echo "GPU: $GPU_NAME (${GPU_MEM}MB VRAM)"

if [ "$GPU_MEM" -ge 45000 ]; then
    # 48GB+ cards (A40, L40S, RTX 6000 Ada)
    MAX_MODEL_LEN=32768
    GPU_UTIL=0.90
    echo "→ 48GB+ mode: 32K context window"
elif [ "$GPU_MEM" -ge 22000 ]; then
    # 24GB cards (RTX 4090) — 7B fp16 is ~14GB, leaves ~10GB for KV cache
    MAX_MODEL_LEN=32768
    GPU_UTIL=0.92
    echo "→ 24GB mode: 32K context window"
else
    echo "GPU has <24GB VRAM. Need at least 24GB for this model."
    exit 1
fi

echo ""

python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL_PATH" \
    --host 0.0.0.0 \
    --port 8000 \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization "$GPU_UTIL" \
    --dtype auto \
    --trust-remote-code \
    --disable-log-requests
