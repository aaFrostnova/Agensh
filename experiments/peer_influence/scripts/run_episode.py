#!/usr/bin/env python3
"""Run (or preflight) one episode: the plan-18 command interface."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from peer_influence.freeze import cohort_gate  # noqa: E402
from peer_influence.preflight import run_preflight  # noqa: E402
from peer_influence.routes import load_routes, load_secret_env  # noqa: E402
from peer_influence.runner import RunContext, run_episode  # noqa: E402
from peer_influence.serving import serving_info  # noqa: E402
from peer_influence.stack import ProjectPaths  # noqa: E402


def project_root():
    root = os.environ.get("PROJECT_ROOT")
    if not root:
        sys.exit("FATAL: set PROJECT_ROOT to the /scratch project directory (plan 0.1)")
    root = Path(root)
    if not (root.is_dir() and os.access(root, os.W_OK)):
        sys.exit(f"FATAL: storage preflight failed: {root} is missing or not writable")
    subprocess.run(["df", "-h", str(root)], check=False)
    return root


def build_context(args, cfg, root):
    paths = ProjectPaths.from_root(root)
    routes_file = paths.repo_root / cfg["model"]["routes_file"]
    routes = load_routes(routes_file)
    route = routes[args.model_route]
    secret_file = root / "secrets" / ("qwen_local.env" if route.deployment == "local_vllm" else "bedrock.env")
    api_key = load_secret_env(secret_file)[route.api_key_env]
    experiment_id = f"{cfg['experiment']['name']}_v{cfg['experiment']['version']}" + (f"-{args.tag}" if getattr(args, "tag", None) else "")
    results_root = root / "runs" / experiment_id / args.model_route
    results_root.mkdir(parents=True, exist_ok=True)
    live_base = Path(os.environ.get("PI_LIVE_ROOT", f"/tmp/agensh-{os.environ.get('USER', 'user')}")) / experiment_id / args.model_route
    code_commit = subprocess.run(["git", "-C", str(paths.repo_root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    task_root = Path(args.task_root) if args.task_root else root / cfg["tasks"]["root"]
    freeze_sha = None
    if not args.preflight_only:
        problems, freeze_sha = cohort_gate(getattr(args, "tag", None), results_root.parent / "freeze.json",
                                           paths.repo_root, task_root, routes, args.model_route)
        if problems:
            sys.exit("FATAL: not a valid formal cohort (plan 15.3):\n  " + "\n  ".join(problems))
    r = cfg["runner"]
    # Always fresh: the quality gate's preflight evidence must describe this endpoint now.
    preflight = run_preflight(route, route.upstream_url, api_key)
    (results_root / "preflight.json").write_text(json.dumps(preflight, indent=2, sort_keys=True))
    return RunContext(
        paths=paths, route=route, routes_file=routes_file, api_key=api_key, experiment_id=experiment_id,
        task_root=task_root, prompts_dir=paths.repo_root / "experiments/peer_influence/prompts",
        results_root=results_root, live_base=live_base, preflight=preflight, serving=serving_info(route, root),
        code_commit=code_commit, agensh_commit=cfg["experiment"]["agensh_commit"],
        max_episode_seconds=cfg["model"]["max_episode_seconds"], fallback_after_seconds=cfg["trigger"]["fallback_after_seconds"],
        idle_seconds=r["idle_seconds"], quiesce_seconds=r["quiesce_seconds"], shutdown_seconds=r["shutdown_seconds"],
        keep_live=args.keep_live, freeze_sha256=freeze_sha, log=lambda m: print(m, flush=True))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--model-route", required=True)
    ap.add_argument("--task", help="task id, e.g. mapping_001_impossible")
    ap.add_argument("--peer", choices=["solo", "benign", "malicious"])
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--episode-id", default="ep_900001")
    ap.add_argument("--task-root")
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--keep-live", action="store_true")
    ap.add_argument("--tag", help="suffix for the experiment id, e.g. smoke1: keeps trial cohorts apart from the pilot")
    args = ap.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text())
    root = project_root()
    ctx = build_context(args, cfg, root)
    if args.preflight_only:
        print(json.dumps(ctx.preflight, indent=2, sort_keys=True))
        return 0 if ctx.preflight["ok"] else 1
    if not ctx.preflight["ok"]:
        sys.exit("FATAL: route preflight failed; refusing to run (fail closed)")
    if not (args.task and args.peer):
        sys.exit("FATAL: --task and --peer are required")
    pair_id, difficulty = args.task.rsplit("_", 1)
    spec = {"episode_id": args.episode_id, "experiment_id": ctx.experiment_id, "model_route": args.model_route,
            "pair_id": pair_id, "difficulty": difficulty, "peer_condition": args.peer, "seed": args.seed,
            "task_id": args.task, "run_attempt": 1}
    score = run_episode(spec, ctx)
    print(json.dumps(score, indent=2, sort_keys=True))
    return 0 if score["data_quality"] == "valid" else 2


if __name__ == "__main__":
    sys.exit(main())
