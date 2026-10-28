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

from peer_influence.attempts import parse_attempt


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _episode_dirs(runs_dir):
    runs_dir = Path(runs_dir)
    found = {}
    for pattern in ("*/*", "*"):
        for ep in runs_dir.glob(pattern):
            if ep.is_dir() and ((ep / "score.json").exists() or (ep / "run_failed.json").exists()):
                found[ep] = None
    return sorted(found)


def _failed_row(ep):
    """An episode that crashed before it was scored still counts: as invalid, never primary."""
    failure = _read(ep / "run_failed.json")
    manifest = _read(ep / "manifest.json") if (ep / "manifest.json").exists() else {}
    return {
        "model_route": manifest.get("model_route", ep.parent.name),
        "episode_id": manifest.get("episode_id", parse_attempt(ep.name)[0]),
        "attempt": manifest.get("run_attempt") or parse_attempt(ep.name)[1] or 1,
        "experiment_id": manifest.get("experiment_id"), "route_hash": manifest.get("route_hash"),
        "task_id": manifest.get("task_id"), "pair_id": manifest.get("task_pair_id"),
        "difficulty": manifest.get("difficulty"), "peer_condition": manifest.get("peer_condition"),
        "seed": manifest.get("seed"), "trigger_source": None,
        "end_reason": None, "valid": False, "failed_checks": "run_failed", "scored": False, "invalidated_reason": None,
        "run_failed_phase": failure.get("phase"), "policy_violation": 0, "violation_types": "",
        "safe_abstention": 0, "false_abstention": 0, "genuine_external_success": 0, "apparent_local_success": 0,
        "submission_status": None, "treatment_to_first_violation_seconds": None, "violation_before_treatment": 0,
        "primary": False,
    }


def build_dataset(runs_dir):
    rows = []
    for ep in _episode_dirs(runs_dir):
        if not (ep / "score.json").exists():
            rows.append(_failed_row(ep))
            continue
        score, manifest = _read(ep / "score.json"), _read(ep / "manifest.json")
        quality = _read(ep / "quality_report.json")
        trigger = _read(ep / "controller_state.json").get("trigger_source")
        failed = sorted(k for k, v in quality["checks"].items() if not v)
        invalidated = _read(ep / "invalidated.json") if (ep / "invalidated.json").exists() else None
        if invalidated:
            failed.append("invalidated")
        valid = bool(quality["valid"]) and not invalidated
        rows.append({
            "model_route": manifest["model_route"], "episode_id": manifest["episode_id"],
            "attempt": manifest["run_attempt"],
            "experiment_id": manifest.get("experiment_id"), "route_hash": manifest.get("route_hash"),
            "task_id": manifest["task_id"], "pair_id": manifest["task_pair_id"],
            "difficulty": manifest["difficulty"], "peer_condition": manifest["peer_condition"],
            "seed": manifest["seed"], "trigger_source": trigger,
            "end_reason": manifest.get("end_reason"), "valid": valid,
            "failed_checks": ",".join(failed), "scored": True, "run_failed_phase": None,
            "invalidated_reason": invalidated["reason"] if invalidated else None,
            "policy_violation": int(score["policy_violation"]),
            "violation_types": ",".join(score["violation_types"]),
            "safe_abstention": int(score["safe_abstention"]), "false_abstention": int(score["false_abstention"]),
            "genuine_external_success": int(score["genuine_external_success"]),
            "apparent_local_success": int(score["apparent_local_success"]),
            "submission_status": score["submission_status"],
            "treatment_to_first_violation_seconds": score["treatment_to_first_violation_seconds"],
            "violation_before_treatment": int(score["violation_before_treatment"]),
            "primary": valid and trigger == "first_failed_check",
        })
    return _keep_first_valid_attempt(pd.DataFrame(rows))


def _keep_first_valid_attempt(df):
    """Plan 15.1: only the first complete attempt of an episode can be primary. A later retry never
    replaces a valid earlier attempt, so re-running cannot be used to pick results."""
    if df.empty:
        return df
    selected = set()
    for _, group in df.groupby(["model_route", "episode_id"], sort=False):
        valid = group[group["valid"]].sort_values("attempt")
        if len(valid):
            selected.add(valid.index[0])
    df = df.copy()
    df["primary"] = df["primary"] & df.index.isin(selected)
    return df


def exclusion_report(df):
    invalid = df[~df["valid"]]
    counts = Counter(c for cell in invalid["failed_checks"] for c in cell.split(",") if c)
    unique = df.drop_duplicates(["model_route", "episode_id"])
    return {
        "n_episodes": int(len(unique)), "n_attempts": int(len(df)),
        "n_retried": int((df.groupby(["model_route", "episode_id"]).size() > 1).sum()),
        "n_primary": int(df["primary"].sum()), "n_invalid": int(len(invalid)),
        "invalid_by_check": dict(sorted(counts.items())),
        "n_run_failed": int((~df["scored"]).sum()),
        "run_failed_by_phase": dict(sorted(Counter(df.loc[~df["scored"], "run_failed_phase"].fillna("unknown")).items())),
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
