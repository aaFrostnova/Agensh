#!/usr/bin/env bash
#SBATCH --job-name=troy-v060-engineering
#SBATCH --partition=superpod-a100,gpu-highvram
#SBATCH --account=pi_shiqingma_umass_edu
#SBATCH --constraint=a100-80g
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=01:00:00
set -euo pipefail
: "${SLURM_JOB_ID:?This script requires a Slurm GPU allocation}"
root=/scratch3/workspace/mingzhel_umass_edu-agensh-mas-alignment/v060
repo="$root/repo/Agensh"
py=/scratch3/workspace/mingzhel_umass_edu-agensh-mas-alignment/tools/venv-exp/bin/python
cd "$repo"
export PYTHONPATH="$repo:$repo/experiments" PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$root/tmp" OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
mkdir -p "$root/logs/slurm" "$root/tmp"
bash experiments/troy_replication/scripts/serve_qwen.sh 1 28801 "$root" > "$root/logs/slurm/qwen-$SLURM_JOB_ID.log" 2>&1 &
model_pid=$!
cleanup() { kill "$model_pid" 2>/dev/null || true; wait "$model_pid" 2>/dev/null || true; }
trap cleanup EXIT TERM INT
ready=0
for attempt in $(seq 1 90); do
  if curl --silent --fail http://127.0.0.1:28801/health >/dev/null; then ready=1; break; fi
  if ! kill -0 "$model_pid" 2>/dev/null; then break; fi
  sleep 5
done
[ "$ready" = 1 ] || exit 3
"$py" - <<'PY'
import json,os,socket,time
from pathlib import Path
root=Path('/scratch3/workspace/mingzhel_umass_edu-agensh-mas-alignment/v060')
record={'slurm_job_id':os.environ['SLURM_JOB_ID'],'host':socket.gethostname(),'time':time.time(),'weight_revision':'1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0','model_id':'Qwen/Qwen3.8-27B','tensor_parallel_size':1,'max_model_len':65536,'max_num_seqs':4,'gpu_memory_utilization':.85,'dtype':'bfloat16','port':28801,'allocated_cuda_devices':os.environ.get('CUDA_VISIBLE_DEVICES')}
(root/'results'/('serving-'+os.environ['SLURM_JOB_ID']+'.json')).write_text(json.dumps(record,indent=2))
PY
"$py" -m troy_replication.runner --root "$root" --route qwen3_8_27b_local --task astropy-12907 --condition C0 --seed 1 --episode-id "eng-astropy-qwen-c0-j$SLURM_JOB_ID" --keep-live
