#!/usr/bin/env bash
set -euo pipefail
: "${SLURM_JOB_ID:?Run only inside a Slurm allocation}"
tp="$1"
port="$2"
bench_root="$3"
prefix=/scratch/workspace/mingzhel_umass_edu-ablator/envs/cnr-qwen38
model=/scratch/workspace/mingzhel_umass_edu-ablator/models/Qwen3.8-27B/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0
cache="$bench_root/cache"
export CUDA_HOME="$prefix/lib/python3.11/site-packages/nvidia/cu13"
export PATH="$CUDA_HOME/bin:$prefix/bin:$PATH"
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MAX_JOBS=2
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
export VLLM_CACHE_ROOT="$cache/vllm" FLASHINFER_WORKSPACE_BASE="$cache/flashinfer"
export XDG_CACHE_HOME="$cache/xdg" TORCH_EXTENSIONS_DIR="$cache/torch_extensions"
export TORCHINDUCTOR_CACHE_DIR="$cache/torchinductor" TRITON_CACHE_DIR="$cache/triton"
export CUDA_CACHE_PATH="$cache/cuda" TMPDIR="$bench_root/tmp"
mkdir -p "$TMPDIR" "$cache"
exec "$prefix/bin/python" -m vllm.entrypoints.openai.api_server \
  --model "$model" --served-model-name Qwen/Qwen3.8-27B \
  --dtype bfloat16 --safetensors-load-strategy lazy \
  --max-model-len 65536 --max-num-seqs 4 --max-num-batched-tokens 8192 \
  --gpu-memory-utilization 0.85 --tensor-parallel-size "$tp" \
  --enforce-eager --language-model-only \
  --enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3 \
  --host 127.0.0.1 --port "$port"
