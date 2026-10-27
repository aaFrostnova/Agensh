#!/usr/bin/env python3
"""Run a cohort (smoke: --task-pairs 2 --repeats 2 = 24 episodes; pilot: 10 x 3 = 180).

Fail closed before any episode: the route must pass preflight, every task must pass its
validity gates, and every selected peer condition must have its treatment message file.
Episodes run in the cohort's randomized order; finished episodes are skipped on --resume.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from peer_influence.manifest import MissingTreatmentMessage, load_treatment_message  # noqa: E402
from peer_influence.matrix import PEERS, filter_plan, plan_matrix  # noqa: E402
from peer_influence.runner import run_episode  # noqa: E402
from peer_influence.scripts.run_episode import build_context, project_root  # noqa: E402
from peer_influence.tasks.validate import validate_task  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--model-route", required=True)
    ap.add_argument("--task-pairs", type=int, required=True)
    ap.add_argument("--repeats", type=int, required=True)
    ap.add_argument("--seed", type=int, default=20261027, help="cohort shuffle seed")
    ap.add_argument("--peers", default=",".join(PEERS), help="comma list; a subset makes the cohort partial and says so")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--parallel", type=int, default=1, help="episodes at once; independent stacks, same global design")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--task-root")
    ap.add_argument("--keep-live", action="store_true")
    args = ap.parse_args(argv)
    args.preflight_only = False
    cfg = yaml.safe_load(args.config.read_text())
    ctx = build_context(args, cfg, project_root())
    if not ctx.preflight["ok"]:
        sys.exit("FATAL: route preflight failed; refusing to run the cohort (fail closed)")
    peers = tuple(p for p in args.peers.split(",") if p)
    plan = filter_plan(plan_matrix(args.task_pairs, args.repeats, args.model_route, ctx.experiment_id, args.seed), peers=peers)
    if set(peers) != set(PEERS):
        print(f"NOTE: partial cohort, peer conditions {peers} only", flush=True)
    for peer in peers:
        try:
            load_treatment_message(ctx.prompts_dir, peer)
        except MissingTreatmentMessage as exc:
            sys.exit(f"FATAL: {exc}")
    for task_id in sorted({s["task_id"] for s in plan}):
        report = validate_task(ctx.task_root / task_id)
        if not report["valid"]:
            sys.exit(f"FATAL: task {task_id} fails its validity gates: {report['checks']}")
    done = {p.parent.name for p in ctx.results_root.glob("*/score.json")} if args.resume else set()
    plan = filter_plan(plan, limit=args.limit, skip=done)
    index = ctx.results_root / "index.jsonl"
    print(f"{len(plan)} episodes to run on {args.model_route} (experiment {ctx.experiment_id})", flush=True)
    lock = threading.Lock()

    def one(item):
        n, spec = item
        started = time.time()
        with lock:
            print(f"--- [{n}/{len(plan)}] {spec['episode_id']} {spec['task_id']} peer={spec['peer_condition']} seed={spec['seed']}", flush=True)
        try:
            score = run_episode(spec, ctx)
            row = {"episode_id": spec["episode_id"], "status": "scored", "data_quality": score["data_quality"],
                   "policy_violation": score["policy_violation"]}
        except Exception as exc:
            row = {"episode_id": spec["episode_id"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        row["seconds"] = round(time.time() - started, 1)
        with lock:
            with open(index, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
            print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        list(pool.map(one, enumerate(plan, 1)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
