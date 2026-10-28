#!/usr/bin/env python3
"""Freeze (or verify) the inputs of a formal cohort: prompts, task set, configs, harness and runtime code.

`create` refuses unless everything in the frozen paths is committed, every task passes its validity gates and the
two treatment messages match (plan 9.4); it writes runs/<experiment_id>/freeze.json (write-once) and a git tag
freeze/<experiment_id>. Changing a frozen input afterwards needs a new experiment version in pilot.yaml.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from peer_influence.freeze import FreezeError, build_freeze, check_cohort, create_freeze_tag, write_freeze  # noqa: E402
from peer_influence.manifest import load_treatment_message, treatment_pair_problems  # noqa: E402
from peer_influence.routes import load_routes  # noqa: E402
from peer_influence.stack import ProjectPaths  # noqa: E402
from peer_influence.tasks.validate import validate_task_root  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("create", "verify"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", type=Path, required=True)
        sp.add_argument("--task-root", type=Path)
    sub.choices["create"].add_argument("--no-tag", action="store_true", help="do not create the git tag")
    sub.choices["verify"].add_argument("--model-route", required=True)
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    root = Path(os.environ["PROJECT_ROOT"])
    paths = ProjectPaths.from_root(root)
    paths.repo_root = Path(__file__).resolve().parents[3]
    routes = load_routes(paths.repo_root / cfg["model"]["routes_file"])
    task_root = Path(args.task_root) if args.task_root else root / cfg["tasks"]["root"]
    experiment_id = f"{cfg['experiment']['name']}_v{cfg['experiment']['version']}"
    freeze_path = root / "runs" / experiment_id / "freeze.json"

    if args.cmd == "verify":
        problems = check_cohort(freeze_path, paths.repo_root, task_root, routes, args.model_route)
        print("\n".join(problems) if problems else f"freeze intact for {experiment_id} / {args.model_route}")
        return 1 if problems else 0

    try:
        bad = [r["task_id"] for r in validate_task_root(task_root) if not r["valid"]]
        if bad:
            raise FreezeError(f"tasks fail their validity gates: {bad}")
        prompts = paths.repo_root / cfg.get("prompts_dir", "experiments/peer_influence/prompts")
        if cfg.get("agents", {}).get("peer_mode") == "autonomous":
            prompt = prompts / "peer_adversary.md"
            if not prompt.is_file() or not prompt.read_text().strip():
                raise FreezeError("autonomous adversary role prompt is missing or empty")
        else:
            pair = treatment_pair_problems(load_treatment_message(prompts, "benign")[0], load_treatment_message(prompts, "malicious")[0])
            if pair:
                raise FreezeError("treatment messages do not satisfy plan 9.4: " + "; ".join(pair))
        freeze = build_freeze(paths.repo_root, task_root, routes, experiment_id)
        write_freeze(freeze_path, freeze)
        tag = None if args.no_tag else create_freeze_tag(paths.repo_root, freeze)
    except FreezeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print(f"froze {experiment_id}: commit {freeze['code_commit'][:10]}, {len(freeze['files'])} files, "
          f"content {freeze['content_sha256'][:16]}.., routes {sorted(freeze['routes'])}, tag {tag}\n-> {freeze_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
