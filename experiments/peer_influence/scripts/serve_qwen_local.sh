#!/usr/bin/env bash
# Serve Qwen/Qwen3.8-27B through vLLM's OpenAI-compatible Responses API on the
# allocated GPU. Every flag here is part of the frozen serving configuration
# recorded in each episode manifest; change it only with a new experiment version.
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/scratch3/workspace/mingzhel_umass_edu-agensh-mas-alignment}"
QWEN_WEIGHTS="${QWEN_WEIGHTS:-/scratch/workspace/mingzhel_umass_edu-ablator/models/Qwen3.8-27B/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0}"
QWEN_SERVE_PY="${QWEN_SERVE_PY:-/scratch/workspace/mingzhel_umass_edu-ablator/envs/cnr-qwen38/bin/python}"
QWEN_PORT="${QWEN_PORT:-18801}"
QWEN_MAX_MODEL_LEN="${QWEN_MAX_MODEL_LEN:-65536}"
QWEN_MAX_NUM_SEQS="${QWEN_MAX_NUM_SEQS:-8}"
QWEN_GPU_UTIL="${QWEN_GPU_UTIL:-0.85}"

# shellcheck disable=SC1091
source "$PROJECT_ROOT/secrets/qwen_local.env"
: "${QWEN_LOCAL_API_KEY:?QWEN_LOCAL_API_KEY missing from secrets/qwen_local.env}"

test -d "$PROJECT_ROOT" && test -w "$PROJECT_ROOT"
test -f "$QWEN_WEIGHTS/config.json"
mkdir -p "$PROJECT_ROOT"/{logs,cache}

prefix="$(dirname "$(dirname "$QWEN_SERVE_PY")")"
cuda_home="$prefix/lib/python3.11/site-packages/nvidia/cu13"
test -x "$cuda_home/bin/nvcc"
# NVIDIA wheels ship lib/ and a versioned libcudart; FlashInfer expects toolkit links.
[ -e "$cuda_home/lib64" ] || ln -s lib "$cuda_home/lib64"
[ -e "$cuda_home/lib/libcudart.so" ] || ln -s libcudart.so.13 "$cuda_home/lib/libcudart.so"

cache="$PROJECT_ROOT/cache"
export CUDA_HOME="$cuda_home" PATH="$cuda_home/bin:$prefix/bin:$PATH" MAX_JOBS=4 \
       PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 \
       HF_HOME="$cache/huggingface" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
       VLLM_CACHE_ROOT="$cache/vllm" FLASHINFER_WORKSPACE_BASE="$cache/flashinfer" \
       XDG_CACHE_HOME="$cache/xdg" TORCH_EXTENSIONS_DIR="$cache/torch_extensions" \
       TORCHINDUCTOR_CACHE_DIR="$cache/torchinductor" TRITON_CACHE_DIR="$cache/triton" \
       CUDA_CACHE_PATH="$cache/cuda" TMPDIR="${TMPDIR:-$PROJECT_ROOT/tmp}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
mkdir -p "$TMPDIR"

hostname; date -u +%FT%TZ
nvidia-smi --query-gpu=name,uuid,memory.used,memory.total --format=csv,noheader
"$QWEN_SERVE_PY" -c 'import vllm, torch; print("vllm", vllm.__version__, "torch", torch.__version__)'

echo $$ > "$PROJECT_ROOT/logs/vllm-qwen38.pid"
exec "$QWEN_SERVE_PY" -m vllm.entrypoints.openai.api_server \
    --model "$QWEN_WEIGHTS" --served-model-name Qwen/Qwen3.8-27B \
    --dtype bfloat16 --safetensors-load-strategy lazy \
    --max-model-len "$QWEN_MAX_MODEL_LEN" --max-num-seqs "$QWEN_MAX_NUM_SEQS" \
    --max-num-batched-tokens 8192 --gpu-memory-utilization "$QWEN_GPU_UTIL" \
    --tensor-parallel-size 1 --enforce-eager --language-model-only \
    --enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3 \
    --host 127.0.0.1 --port "$QWEN_PORT" --api-key "$QWEN_LOCAL_API_KEY"
