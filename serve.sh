#!/bin/bash
# serve.sh — Start the vLLM inference server
# Auto-detects GPU, recommends the best model, lets you confirm before downloading.
# Works on any GPU with 24GB+ VRAM (Ada/Ampere/Hopper)
# NOT supported: Blackwell GPUs — vLLM not yet compatible
set -e

# Source tool paths if available
[ -f /workspace/.waltercheck-env ] && source /workspace/.waltercheck-env

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="$SCRIPT_DIR/models"
mkdir -p "$MODELS_DIR"

# ============================================================
# Model catalog
# ============================================================

ALL_MODELS="7b 32b-awq 32b"

# 7B — fp16, fits any 24GB+ card
MODEL_7b_REPO="Qwen/Qwen2.5-Coder-7B-Instruct"
MODEL_7b_DIR="qwen2.5-coder-7b-instruct"
MODEL_7b_LABEL="Qwen2.5-Coder-7B"
MODEL_7b_SIZE="~14GB"
MODEL_7b_MIN_VRAM=18
MODEL_7b_QUANT=""
MODEL_7b_DESC="fast, lower quality"

# 32B AWQ — 4-bit quantized, fits 24GB+ (reduced context on 24GB)
MODEL_32b_awq_REPO="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ"
MODEL_32b_awq_DIR="qwen2.5-coder-32b-instruct-awq"
MODEL_32b_awq_LABEL="Qwen2.5-Coder-32B-AWQ"
MODEL_32b_awq_SIZE="~20GB"
MODEL_32b_awq_MIN_VRAM=22
MODEL_32b_awq_QUANT="awq"
MODEL_32b_awq_DESC="best quality"

# 32B — fp16, needs 80GB+
MODEL_32b_REPO="Qwen/Qwen2.5-Coder-32B-Instruct"
MODEL_32b_DIR="qwen2.5-coder-32b-instruct"
MODEL_32b_LABEL="Qwen2.5-Coder-32B"
MODEL_32b_SIZE="~64GB"
MODEL_32b_MIN_VRAM=70
MODEL_32b_QUANT=""
MODEL_32b_DESC="best quality, full precision"

# ============================================================
# Helper functions
# ============================================================

get_model_field() {
    local model="$1" field="$2"
    # Normalize key: 32b-awq -> 32b_awq for variable names
    local key="${model//-/_}"
    local var="MODEL_${key}_${field}"
    echo "${!var}"
}

is_model_installed() {
    local dir
    dir=$(get_model_field "$1" DIR)
    [ -d "$MODELS_DIR/$dir" ] && [ -f "$MODELS_DIR/$dir/config.json" ]
}

download_model() {
    local model="$1"
    local repo dir
    repo=$(get_model_field "$model" REPO)
    dir=$(get_model_field "$model" DIR)
    local dest="$MODELS_DIR/$dir"

    echo ""
    echo "  Downloading $repo ($(get_model_field "$model" SIZE))..."
    echo ""

    export HF_HUB_ENABLE_HF_TRANSFER=1

    if command -v hf &> /dev/null; then
        hf download "$repo" --local-dir "$dest"
    else
        python -c "
from huggingface_hub import snapshot_download
snapshot_download('$repo', local_dir='$dest')
"
    fi
    echo ""
    echo "  Model downloaded."
}

get_vllm_params() {
    local model="$1" vram="$2"
    local quant
    quant=$(get_model_field "$model" QUANT)

    # Set defaults
    VLLM_GPU_UTIL=""
    VLLM_MAX_MODEL_LEN=""
    VLLM_EXTRA_ARGS=""

    if [ -n "$quant" ]; then
        VLLM_EXTRA_ARGS="--quantization $quant"
    fi

    case "$model" in
        7b)
            if [ "$vram" -ge 70000 ]; then
                VLLM_GPU_UTIL=0.80
                VLLM_MAX_MODEL_LEN=32768
            elif [ "$vram" -ge 35000 ]; then
                VLLM_GPU_UTIL=0.85
                VLLM_MAX_MODEL_LEN=32768
            else
                VLLM_GPU_UTIL=0.92
                VLLM_MAX_MODEL_LEN=32768
            fi
            ;;
        32b-awq)
            if [ "$vram" -ge 70000 ]; then
                VLLM_GPU_UTIL=0.80
                VLLM_MAX_MODEL_LEN=32768
            elif [ "$vram" -ge 35000 ]; then
                VLLM_GPU_UTIL=0.85
                VLLM_MAX_MODEL_LEN=32768
            else
                VLLM_GPU_UTIL=0.95
                VLLM_MAX_MODEL_LEN=8192
            fi
            ;;
        32b)
            if [ "$vram" -ge 85000 ]; then
                # >80GB card — comfortable headroom
                VLLM_GPU_UTIL=0.85
                VLLM_MAX_MODEL_LEN=32768
            else
                # 80GB class (A100/H100) — model is ~61GB, need higher util for KV cache
                VLLM_GPU_UTIL=0.92
                VLLM_MAX_MODEL_LEN=32768
            fi
            ;;
    esac
}

# ============================================================
# Parse --model flag
# ============================================================

SELECTED_MODEL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            SELECTED_MODEL="$2"
            shift 2
            ;;
        --model=*)
            SELECTED_MODEL="${1#*=}"
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: ./serve.sh [--model 7b|32b-awq|32b]"
            exit 1
            ;;
    esac
done

# Validate --model value if provided
if [ -n "$SELECTED_MODEL" ]; then
    valid=false
    for m in $ALL_MODELS; do
        if [ "$m" = "$SELECTED_MODEL" ]; then
            valid=true
            break
        fi
    done
    if ! $valid; then
        echo "Unknown model: $SELECTED_MODEL"
        echo "Available models: $ALL_MODELS"
        exit 1
    fi
fi

# ============================================================
# Detect GPU
# ============================================================

# Block AMD GPUs early (before nvidia-smi fails)
if command -v rocm-smi &> /dev/null && ! command -v nvidia-smi &> /dev/null; then
    echo ""
    echo "  AMD GPU detected (ROCm)"
    echo "  WalterChecks currently supports NVIDIA GPUs only."
    echo "  AMD MI300X support requires ROCm-specific vLLM install — not yet implemented."
    exit 1
fi

GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)

echo ""
echo "  GPU: $GPU_NAME (${GPU_MEM} MB)"

# ============================================================
# Block Blackwell GPUs
# ============================================================

if echo "$GPU_NAME" | grep -qiE 'RTX 50|RTX PRO|B200|B100'; then
    echo ""
    echo "  Blackwell GPU detected ($GPU_NAME)"
    echo "  vLLM does not yet support Blackwell architecture."
    echo "  Use an Ada or Ampere GPU (RTX 4090, A100, L40S, etc.)"
    exit 1
fi

# ============================================================
# Check minimum VRAM
# ============================================================

if [ "$GPU_MEM" -lt 22000 ]; then
    echo ""
    echo "  GPU has <24GB VRAM. Need at least 24GB."
    exit 1
fi

# ============================================================
# Build compatible model list and pick recommendation
# ============================================================

COMPATIBLE_MODELS=""
RECOMMENDED=""

for m in $ALL_MODELS; do
    min_vram=$(get_model_field "$m" MIN_VRAM)
    # MIN_VRAM is in GB (thousands), GPU_MEM is in MB
    min_vram_mb=$((min_vram * 1000))
    if [ "$GPU_MEM" -ge "$min_vram_mb" ]; then
        COMPATIBLE_MODELS="$COMPATIBLE_MODELS $m"
    fi
done
COMPATIBLE_MODELS="${COMPATIBLE_MODELS# }"  # trim leading space

# Recommendation logic: pick the highest quality compatible model
if echo " $COMPATIBLE_MODELS " | grep -q ' 32b '; then
    RECOMMENDED="32b"
elif echo " $COMPATIBLE_MODELS " | grep -q ' 32b-awq '; then
    RECOMMENDED="32b-awq"
else
    RECOMMENDED="7b"
fi

# ============================================================
# Model selection (interactive or via --model)
# ============================================================

if [ -n "$SELECTED_MODEL" ]; then
    # Validate that --model choice is compatible with this GPU
    if ! echo " $COMPATIBLE_MODELS " | grep -q " $SELECTED_MODEL "; then
        min_vram=$(get_model_field "$SELECTED_MODEL" MIN_VRAM)
        echo ""
        echo "  Model '$SELECTED_MODEL' needs ${min_vram}GB+ VRAM but this GPU has $((GPU_MEM / 1000))GB."
        exit 1
    fi
else
    # Interactive selection
    echo ""
    echo "  Models for your GPU:"
    echo ""

    i=1
    declare -A MENU_MAP
    for m in $COMPATIBLE_MODELS; do
        label=$(get_model_field "$m" LABEL)
        size=$(get_model_field "$m" SIZE)
        desc=$(get_model_field "$m" DESC)

        line="  $i) $label"
        # Pad to align columns
        printf -v line "  %d) %-30s %-8s %-28s" "$i" "$label" "$size" "$desc"

        if [ "$m" = "$RECOMMENDED" ]; then
            line="$line (recommended)"
        fi

        if is_model_installed "$m"; then
            line="$line [installed]"
        fi

        echo "$line"
        MENU_MAP[$i]="$m"
        i=$((i + 1))
    done

    echo ""

    # Find the menu number for the recommended model
    default_num=1
    for num in "${!MENU_MAP[@]}"; do
        if [ "${MENU_MAP[$num]}" = "$RECOMMENDED" ]; then
            default_num=$num
            break
        fi
    done

    read -r -p "  Select model [$default_num]: " choice
    choice="${choice:-$default_num}"

    if [ -z "${MENU_MAP[$choice]}" ]; then
        echo "  Invalid selection."
        exit 1
    fi

    SELECTED_MODEL="${MENU_MAP[$choice]}"
fi

# ============================================================
# Download if needed
# ============================================================

if ! is_model_installed "$SELECTED_MODEL"; then
    download_model "$SELECTED_MODEL"
fi

echo ""
echo "  Model ready — starting vLLM..."

# ============================================================
# Calculate vLLM params and launch
# ============================================================

MODEL_DIR_NAME=$(get_model_field "$SELECTED_MODEL" DIR)
MODEL_PATH="$MODELS_DIR/$MODEL_DIR_NAME"
MODEL_LABEL=$(get_model_field "$SELECTED_MODEL" LABEL)

get_vllm_params "$SELECTED_MODEL" "$GPU_MEM"

echo ""
echo "  Model:   $MODEL_LABEL"
echo "  Context: $VLLM_MAX_MODEL_LEN tokens"
echo "  GPU util: $VLLM_GPU_UTIL"
if [ -n "$VLLM_EXTRA_ARGS" ]; then
    echo "  Quant:   $(get_model_field "$SELECTED_MODEL" QUANT)"
fi
echo ""
echo "  Once you see 'Uvicorn running', open a second terminal and run:"
echo "    python qa-bot/review.py repo repos/<your-repo> --profile wordpress"
echo ""

# Build the command
CMD=(
    python -m vllm.entrypoints.openai.api_server
    --model "$MODEL_PATH"
    --host 0.0.0.0
    --port 8000
    --max-model-len "$VLLM_MAX_MODEL_LEN"
    --gpu-memory-utilization "$VLLM_GPU_UTIL"
    --dtype auto
    --trust-remote-code
    --disable-log-requests
)

if [ -n "$VLLM_EXTRA_ARGS" ]; then
    # Word-split intentionally here
    CMD+=($VLLM_EXTRA_ARGS)
fi

exec "${CMD[@]}"
