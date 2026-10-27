# Peer-influence pilot on Agensh

Controlled experiment: does a scripted peer's advice change whether an agent that complies
alone violates an explicit file policy once the task is hard or impossible? 2 (solvable /
impossible) x 3 (no peer / benign peer / malicious peer) design, one deterministic
environment-level label (`policy_violation`: a protected file was changed), no LLM judge.
Built on `microsoft/Agensh` @ `0468a3e` (see `configs/pilot.yaml`).

## Layout

| Path | Role |
|---|---|
| `tasks/generate.py`, `tasks/validate.py` | paired solvable/impossible JSON rule-repair tasks and their validity gates |
| `runtime/` | stdlib-only files copied into the worker sandbox: `./check` wrapper, event log, protected-file hashes |
| `controller/` | treatment controller: first failed check -> exactly one DM from the peer account; workspace monitor |
| `evaluation/` | external scorer (hash + git diff/status/history + monitor), validators, 17-check quality gate |
| `routes.py`, `preflight.py`, `configs/model_routes.yaml` | model routes (Qwen3.8-27B local vLLM, Kimi K3 on Bedrock) and live Responses/tool-call preflight |
| `stack.py`, `runner.py`, `matrix.py`, `manifest.py`, `serving.py` | rootless backend, episode runner, condition matrix, manifests |
| `analysis/` | frozen dataset, per-model effects, pressure interaction, mixed-effects model |
| `prompts/` | `victim.md`, `task_brief.md`, `peer_benign.txt` (plan 9.1-9.3); `peer_malicious.txt` is **not shipped** |
| `scripts/` | `serve_qwen_local.sh`, `setup_rootless.sh`, `run_episode.py`, `run_matrix.py`, `clean_episode.py` |
| `tests/` | 200 tests; `pytest` from this directory |

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

```bash
export PROJECT_ROOT=/scratch3/workspace/<user>-agensh-mas-alignment     # ws_allocate'd
bash experiments/peer_influence/scripts/setup_rootless.sh                # once
# on a GPU node (sbatch/OnDemand), keep the GPU busy if your site idles-out jobs:
bash experiments/peer_influence/scripts/serve_qwen_local.sh &            # vLLM, Responses API
PY=$PROJECT_ROOT/tools/venv-exp/bin/python
$PY experiments/peer_influence/tasks/generate.py --pairs 10 --seed 20261027 --out $PROJECT_ROOT/data/tasks_v0.1.0
$PY experiments/peer_influence/tasks/validate.py --task-root $PROJECT_ROOT/data/tasks_v0.1.0
$PY experiments/peer_influence/scripts/run_episode.py --config experiments/peer_influence/configs/pilot.yaml \
    --model-route qwen3_8_27b_local --preflight-only
$PY experiments/peer_influence/scripts/run_matrix.py --config experiments/peer_influence/configs/pilot.yaml \
    --model-route qwen3_8_27b_local --task-pairs 2 --repeats 2          # 24-episode smoke
$PY experiments/peer_influence/analysis/build_dataset.py --runs $PROJECT_ROOT/runs/peer_influence_pilot_v0.2.0 --out analysis/episodes.parquet
$PY experiments/peer_influence/analysis/analyze.py --data analysis/episodes.parquet --out analysis/results
```

### Treatment messages are an external input

`run_matrix.py` and `run_episode.py` load `prompts/peer_<condition>.txt`, record its SHA-256 and
**refuse to run** if a selected condition's file is missing or empty. Add `prompts/peer_malicious.txt`
(plan 9.4: same length, tone and opening as `peer_benign.txt`; only the file-policy advice differs)
before running the malicious cells, or run a declared partial cohort with `--peers solo,benign`.
Freeze both files before the first cohort; editing one makes a new experiment version.

### Kimi K3 (Bedrock)

Put `BEDROCK_API_KEY=...` in `$PROJECT_ROOT/secrets/bedrock.env` (mode 600) and run the same
commands with `--model-route kimi_k3_bedrock`. Preflight must pass first; the cohort is skipped otherwise.

## Differences from the plan (all ledgered)

* **No Docker/sudo on Unity** -> MySQL/Gitea/Mattermost run from the upstream images under Apptainer, Python/Node
  services natively, the worker in bubblewrap with its own PID namespace (`stack.py`). The Compose path
  (`AGENT_ROLE_CONFIG`) is generated and unit-tested but was not run live here.
* **Board**: the plan's `BOARD_SERVICE_URL=""` would also silence the DM notice shim; `EXPERIMENT_DISABLE_BOARD=1`
  removes only the board tool and nobody is a board member.
* **Worker base URL ends in `/v1`** (Copilot appends `/responses`; vLLM and Bedrock serve `/v1/responses`).
* **Patched bridge copy** in the sandbox: rootless, the agent and bridge share a uid and the stock shutdown sweep
  kills the bridge before it seals its archive. The patch exempts the bridge, its parent and init.
* `check` and `submission.schema.json` are also protected (evaluator-related artifacts, plan 12.1).
* `launch_single_agent.py` (standalone ProgramBench baseline) keeps its gpt-5.6-sol/high check.

## Not implemented / not run

* Stage 1B LLM-peer prompts (plan 9.5, 9.6) and the plots module.
* Plan 8.3's 30-minute stability run and repeated fixed-prompt runs for the model servers.
* Attempt/retry policy for transient failures: a failed episode is recorded (`run_failed.json`, `index.jsonl`)
  and the cohort continues; re-run with `--resume`.
* The 180-episode pilot and the Kimi K3 cohorts (see the final report for what was run).
