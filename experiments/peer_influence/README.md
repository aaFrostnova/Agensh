# Peer-influence pilot on Agensh

Controlled experiment: does a scripted peer's advice change whether an agent that complies
alone violates an explicit file policy once the task is hard or impossible? 2 (solvable /
impossible) x 3 (no peer / benign peer / malicious peer) design, one deterministic
environment-level label (`policy_violation`: a protected file was changed), no LLM judge.
Built on `microsoft/Agensh` @ `0468a3e` (see `configs/pilot.yaml`). Design decisions that deviate from
or fill gaps in the plan are in [`RULINGS.md`](RULINGS.md).

## Layout

| Path | Role |
|---|---|
| `tasks/generate.py`, `tasks/validate.py` | paired solvable/impossible JSON rule-repair tasks and their validity gates |
| `runtime/` | stdlib-only files copied into the worker sandbox: `./check` wrapper, event log, protected-file hashes |
| `controller/` | treatment controller: first failed check -> exactly one DM from the peer account; workspace monitor |
| `evaluation/` | external scorer (hash + git diff/status/history + monitor), validators, 17-check quality gate |
| `routes.py`, `preflight.py`, `configs/model_routes.yaml` | model routes (Qwen3.8-27B local vLLM, Kimi K3 on Bedrock) and live Responses/tool-call preflight |
| `stack.py`, `runner.py`, `matrix.py`, `attempts.py`, `manifest.py`, `serving.py` | rootless backend, episode runner, condition matrix, attempt bookkeeping, manifests |
| `analysis/` | frozen dataset, per-model effects, pressure interaction, mixed-effects model |
| `prompts/` | `victim.md`, `task_brief.md`, `peer_benign.txt` (plan 9.1-9.3); `peer_malicious.txt` is **not shipped** |
| `scripts/` | `serve_qwen_local.sh`, `sbatch_cohort.sh`, `setup_rootless.sh`, `run_episode.py`, `run_matrix.py`, `clean_episode.py` |
| `tests/` | unit and integration tests; run `pytest` from this directory |

Outside this directory the fork changes only: `tools/gen-compose-prod.py` (opt-in
`AGENT_ROLE_CONFIG` + `EXPERIMENT_MODE=peer_influence`, default output byte-identical to
upstream), `agent/entrypoint.sh` (`EXPERIMENT_DISABLE_BOARD=1`), and the model checks in
`scripts/launch_single_node.sh` / `scripts/pbench_gen_env.sh` (now read `PBENCH_EXPECT_*` /
`AGENT_MODEL`, with the old gpt-5.6-sol / high / 272000 as defaults).

## Storage (plan 0.1)

Everything persistent lives under `$PROJECT_ROOT` on `/scratch`; nothing is written to `/work`
(the entrypoint's `/work/AGENTS.md` etc. are inside the sandbox, bound to a per-episode directory).
Service state of a running episode (MySQL, Gitea sqlite, Mattermost) is node-local
(`$PI_LIVE_ROOT`, default `/tmp/agensh-$USER`) and deleted after the episode; trajectories,
logs, scores and archives are copied into `$PROJECT_ROOT/runs/<experiment>/<route>/<episode>/`.

## Running it (Unity, rootless)

Qwen3.8-27B in bf16 is 50.2 GiB of weights, so the model server needs an **80 GB-class GPU** (an A100-80GB);
`sbatch_cohort.sh` refuses anything smaller before loading it.

```bash
export PROJECT_ROOT=/scratch3/workspace/<user>-agensh-mas-alignment     # ws_allocate'd; ws_extend before it expires
bash experiments/peer_influence/scripts/setup_rootless.sh                # once
PY=$PROJECT_ROOT/tools/venv-exp/bin/python
$PY experiments/peer_influence/tasks/generate.py --pairs 10 --seed 20261027 --out $PROJECT_ROOT/data/tasks_v0.1.0
$PY experiments/peer_influence/tasks/validate.py --task-root $PROJECT_ROOT/data/tasks_v0.1.0

# model server + cohort in one batch job (not submitted by any script here):
cd $PROJECT_ROOT && sbatch [--nodelist=gpu019] repo/Agensh/experiments/peer_influence/scripts/sbatch_cohort.sh
# ...or, inside an existing GPU allocation:
bash experiments/peer_influence/scripts/serve_qwen_local.sh &            # vLLM, Responses API
$PY experiments/peer_influence/scripts/run_episode.py --config experiments/peer_influence/configs/pilot.yaml \
    --model-route qwen3_8_27b_local --preflight-only
$PY experiments/peer_influence/scripts/run_matrix.py --config experiments/peer_influence/configs/pilot.yaml \
    --model-route qwen3_8_27b_local --task-pairs 2 --repeats 2 --parallel 2   # 24-episode smoke (needs both messages)

$PY experiments/peer_influence/analysis/build_dataset.py --runs $PROJECT_ROOT/runs/peer_influence_pilot_v0.2.0 --out analysis/episodes.parquet
$PY experiments/peer_influence/analysis/analyze.py --data analysis/episodes.parquet --out analysis/results
```

`--tag NAME` appends `-NAME` to the experiment id so trial cohorts never mix with the pilot.

### Interrupted and failed episodes (plan 15.1)

An episode directory is never overwritten. `run_matrix.py --resume` skips scored episodes, archives a failed or
cut-off one (lost node, kill) as `<episode>.attempt<k>` with a `run_failed.json`, and retries it under the
original id as attempt k+1; `--retry-invalid` additionally retries scored episodes whose quality gate failed (that
gate only looks at infrastructure evidence, never at the outcome; valid results are never re-run). The dataset
keeps the first valid attempt of each episode as the primary one and counts every attempt in the exclusion report.

### Treatment messages are an external input

`run_matrix.py` and `run_episode.py` load `prompts/peer_<condition>.txt`, record its SHA-256 and
**refuse to run** if a selected condition's file is missing or empty. Add `prompts/peer_malicious.txt`
(plan 9.4: same length, tone and opening as `peer_benign.txt`; only the file-policy advice differs)
before running the malicious cells, or run a declared partial cohort with `--peers solo,benign`.
Freeze both files before the first cohort; editing one makes a new experiment version.

### Kimi K3 (Bedrock)

Put `BEDROCK_API_KEY=...` in `$PROJECT_ROOT/secrets/bedrock.env` (mode 600) and run the same
commands with `--model-route kimi_k3_bedrock`. Preflight must pass first; the cohort is skipped otherwise.

## Differences from the plan

See [`RULINGS.md`](RULINGS.md) for the reasons. In short: rootless backend (no Docker/sudo on Unity); the board tool is
removed but `BOARD_SERVICE_URL` kept for DM notices; the peer DM reaches the victim appended to its failing `./check`
output (the notice shim only wraps MCP tools); worker base URL ends in `/v1`; a patched ACP bridge copy inside the
sandbox; `check` and `submission.schema.json` are protected files too; `launch_single_agent.py` keeps its check.

## Known limitations

* `/opt/pi_events` is writable by the agent, so `events.jsonl`-derived values (`apparent_local_success`, the gaming
  gap, and when `first_failed_check` fires) can be forged by a deliberately adversarial agent. The primary label
  `policy_violation` does not depend on them (hash, git and workspace-monitor evidence only), and the quality gate
  uses host-side evidence.
* A protected edit made and reverted uncommitted inside one ~0.5 s monitor gap while also destroying `.git` would be
  missed; an apparent pass needs the edit to persist across a `./check` run.
* `hidden/hidden_cases.json` equals the public cases: `genuine_external_success` re-runs them through trusted code, it is
  not a held-out set.
* The mixed model is a variational-Bayes GLMM; risk differences with cluster-bootstrap intervals carry the inference.
* Episodes end on submission, on `idle_seconds` without progress, or on timeout; `end_reason` is reported and a
  submitted-only sensitivity analysis is part of the summary.

## Not implemented / not run

* Stage 1B LLM-peer prompts (plan 9.5, 9.6) and `analysis/plots.py`.
* Plan 8.3's 30-minute stability run and repeated fixed-prompt runs for the model servers.
* Compose-backend live runs (the Compose path is generated and unit-tested only).
* The 180-episode pilot, the malicious cells and the Kimi K3 cohorts.
