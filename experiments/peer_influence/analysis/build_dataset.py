#!/usr/bin/env python3
"""Freeze episode artifacts into one table (plan 6, 14). Primary inclusion is mechanical:

an episode is primary iff its data-quality gate passed and its treatment was triggered by
the first failed check. Model behaviour is never a reason to exclude an episode (plan 14.3).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_dataset(runs_dir):
    runs_dir = Path(runs_dir)
    score_paths = sorted(runs_dir.glob("*/*/score.json")) + sorted(runs_dir.glob("*/score.json"))
    rows = []
    for sp in score_paths:
        ep = sp.parent
        score, manifest = _read(sp), _read(ep / "manifest.json")
        quality = _read(ep / "quality_report.json")
        trigger = _read(ep / "controller_state.json").get("trigger_source")
        failed = sorted(k for k, v in quality["checks"].items() if not v)
        rows.append({
            "model_route": manifest["model_route"], "episode_id": manifest["episode_id"],
            "task_id": manifest["task_id"], "pair_id": manifest["task_pair_id"],
            "difficulty": manifest["difficulty"], "peer_condition": manifest["peer_condition"],
            "seed": manifest["seed"], "run_attempt": manifest["run_attempt"], "trigger_source": trigger,
            "end_reason": manifest.get("end_reason"), "valid": bool(quality["valid"]),
            "failed_checks": ",".join(failed),
            "policy_violation": int(score["policy_violation"]),
            "violation_types": ",".join(score["violation_types"]),
            "safe_abstention": int(score["safe_abstention"]), "false_abstention": int(score["false_abstention"]),
            "genuine_external_success": int(score["genuine_external_success"]),
            "apparent_local_success": int(score["apparent_local_success"]),
            "submission_status": score["submission_status"],
            "treatment_to_first_violation_seconds": score["treatment_to_first_violation_seconds"],
            "violation_before_treatment": int(score["violation_before_treatment"]),
            "primary": bool(quality["valid"]) and trigger == "first_failed_check",
        })
    return pd.DataFrame(rows)


def exclusion_report(df):
    invalid = df[~df["valid"]]
    counts = Counter(c for cell in invalid["failed_checks"] for c in cell.split(",") if c)
    return {
        "n_episodes": int(len(df)), "n_primary": int(df["primary"].sum()), "n_invalid": int(len(invalid)),
        "invalid_by_check": dict(sorted(counts.items())),
        "fallback_trigger": int((df["trigger_source"] == "fallback_timer").sum()),
        "fallback_share": float((df["trigger_source"] == "fallback_timer").mean()) if len(df) else 0.0,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="path ending in .parquet or .csv")
    args = ap.parse_args(argv)
    df = build_dataset(args.runs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    (df.to_parquet if args.out.suffix == ".parquet" else df.to_csv)(args.out, **({} if args.out.suffix == ".parquet" else {"index": False}))
    print(json.dumps(exclusion_report(df), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
