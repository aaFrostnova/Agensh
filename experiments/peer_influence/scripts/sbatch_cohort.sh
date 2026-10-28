#!/bin/bash
# Serve Qwen3.8-27B (vLLM, Responses API) and run a peer-influence cohort against it in one job.
#
# Resources (justified): Qwen3.8-27B in bf16 is 50.2 GiB of weights plus KV cache, so it needs an
# 80 GB-class GPU (one A100-80GB); 8 CPUs and 64 GB host memory carry vLLM plus two episodes at a
# time (MySQL + Mattermost + Gitea + router per episode). The default 6 h covers the 16-episode
# smoke cohort (~7 min per episode, 2 in parallel); change it with `sbatch --time=...`.
#
# NOT submitted by anything in this repository. Submit it yourself from $PROJECT_ROOT (the logs/
# directory must exist there):
#   cd "$PROJECT_ROOT" && sbatch repo/Agensh/experiments/peer_influence/scripts/sbatch_cohort.sh
# Pin the node with `sbatch --nodelist=gpu019 ...` (may queue longer than any A100 node).
# Cohort selection via environment: TASK_PAIRS, REPEATS, PEERS, PARALLEL, TAG (see below).
#SBATCH --job-name=pi_qwen_cohort
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
#SBATCH --partition=superpod-a100
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=06:00:00
set -euo pipefail

export PROJECT_ROOT="${PROJECT_ROOT:-/scratch3/workspace/mingzhel_umass_edu-agensh-mas-alignment}"
PI="$PROJECT_ROOT/repo/Agensh/experiments/peer_influence"
PY="$PROJECT_ROOT/tools/venv-exp/bin/python"
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}" MKL_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"

hostname
date -u +%FT%TZ
module list 2>&1 || true
which python && python --version
"$PY" --version
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader
echo "job=${SLURM_JOB_ID:-none} gpus=${SLURM_JOB_GPUS:-unset} CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"

# Fail before loading 50 GiB of weights if the card is too small.
total_mib="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)"
if [ "${total_mib:-0}" -lt 70000 ]; then
    echo "FATAL: GPU has ${total_mib} MiB; Qwen3.8-27B needs an 80 GB-class card" >&2
    exit 3
fi

mkdir -p "$PROJECT_ROOT/logs"
bash "$PI/scripts/serve_qwen_local.sh" > "$PROJECT_ROOT/logs/vllm-${SLURM_JOB_ID:-manual}.log" 2>&1 &
VLLM_PID=$!
trap 'kill "$VLLM_PID" 2>/dev/null || true; wait "$VLLM_PID" 2>/dev/null || true' EXIT

echo "waiting for vLLM (pid $VLLM_PID) ..."
for _ in $(seq 1 180); do
    if curl -fsS -m 3 -o /dev/null http://127.0.0.1:18801/health; then break; fi
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        echo "FATAL: vLLM exited during startup; see logs/vllm-${SLURM_JOB_ID:-manual}.log" >&2
        exit 4
    fi
    sleep 5
done
curl -fsS -m 3 -o /dev/null http://127.0.0.1:18801/health

# --resume makes a re-submitted job continue this cohort: scored episodes are skipped and cut-off
# ones are archived as <episode>.attempt<k> and retried.
"$PY" "$PI/scripts/run_matrix.py" \
    --config "$PI/configs/pilot.yaml" \
    --model-route qwen3_8_27b_local \
    --task-pairs "${TASK_PAIRS:-2}" --repeats "${REPEATS:-2}" \
    --peers "${PEERS:-solo,benign}" \
    --parallel "${PARALLEL:-2}" \
    ${TAG:+--tag "$TAG"} \
    --resume
