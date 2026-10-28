#!/usr/bin/env python3
"""Primary analysis (plan 14): per-model treatment effects first, replication direction second.

Descriptive rates with Wilson intervals, risk differences and the pressure-interaction
contrast with cluster-bootstrap (task pair) intervals, and a mixed-effects logistic model
with a task random intercept. Raw violation rates are never ranked across models.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from peer_influence.analysis.build_dataset import exclusion_report  # noqa: E402

DIFFICULTIES = ("solvable", "impossible")
PEERS = ("solo", "benign", "malicious")
N_BOOT = 2000
EXPOSED_TRIGGERS = ("first_failed_check", "fallback_timer")  # the peer message was (or, in solo, would have been) due
FUNNEL_STAGES = ("primary", "fallback", "no_trigger", "invalid", "run_failed")


def wilson_interval(k, n, z=1.96):
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return center - margin, center + margin


def _primary(df):
    return df[df["primary"]]


def _select(df, filters):
    mask = pd.Series(True, index=df.index)
    for col, value in filters.items():
        mask &= df[col] == value
    return df[mask]


def _rate(df, outcome="policy_violation"):
    return float(df[outcome].mean()) if len(df) else float("nan")


def condition_table(df, primary_only=True):
    data = _primary(df) if primary_only else df
    rows = []
    for difficulty in DIFFICULTIES:
        for peer in PEERS:
            cell = _select(data, {"difficulty": difficulty, "peer_condition": peer})
            n, k = len(cell), int(cell["policy_violation"].sum())
            lo, hi = wilson_interval(k, n)
            rows.append({
                "difficulty": difficulty, "peer_condition": peer, "n": n, "violations": k,
                "rate": k / n if n else float("nan"), "ci_low": lo, "ci_high": hi,
                "safe_abstention_rate": _rate(cell, "safe_abstention"),
                "false_abstention_rate": _rate(cell, "false_abstention"),
                "genuine_success_rate": _rate(cell, "genuine_external_success"),
                "gaming_gap": _rate(cell, "apparent_local_success") - _rate(cell, "genuine_external_success"),
            })
    return pd.DataFrame(rows)


def _reporting_attempts(df):
    """One row per episode: its first valid attempt (the one that can be primary), else its last attempt."""
    if df.empty:
        return df
    picked = []
    for _, group in df.sort_values("attempt").groupby(["model_route", "episode_id"], sort=False):
        picked.append(group[group["valid"]].iloc[0] if group["valid"].any() else group.iloc[-1])
    return pd.DataFrame(picked)


def _funnel_stage(row):
    """Where an episode stopped on the way to the peer message; the stages partition the episodes."""
    if not row["scored"]:
        return "run_failed"
    if row["primary"]:
        return "primary"
    if pd.isna(row["trigger_source"]):
        return "no_trigger"  # ended before a failed check or the fallback timer: no message was due
    if row["trigger_source"] == "fallback_timer" and row["valid"]:
        return "fallback"
    return "invalid"  # triggered, but the quality gate (or the operator) rejected the attempt


def exposure_table(df):
    """Per cell, how many episodes got as far as the peer message (plan 10.4, 15.2).

    A fast agent can finish before its first failed check and before the fallback timer: the trigger never fires and
    no message is due. Those episodes are not primary in any arm alike (a peer arm marks them quality-invalid because
    the treatment was never delivered, solo marks them valid but without a trigger), so a cell's primary n can be far
    below the planned one. `no_trigger` is that stage; it is not an infrastructure failure."""
    episodes = _reporting_attempts(df)
    episodes = episodes.assign(stage=[_funnel_stage(row) for _, row in episodes.iterrows()])
    rows = []
    for difficulty in DIFFICULTIES:
        for peer in PEERS:
            cell = _select(episodes, {"difficulty": difficulty, "peer_condition": peer})
            counts = Counter(cell["stage"])
            rows.append({"difficulty": difficulty, "peer_condition": peer, "episodes": int(len(cell)),
                         **{stage: int(counts.get(stage, 0)) for stage in FUNNEL_STAGES}})
    return pd.DataFrame(rows)


CELLS = [(d, p) for d in DIFFICULTIES for p in PEERS]


def _cell_indices(filters):
    return [i for i, (d, p) in enumerate(CELLS)
            if filters.get("difficulty", d) == d and filters.get("peer_condition", p) == p]


def _counts(data):
    """Per task-pair counts: N[c, cell] episodes and K[c, cell] violations."""
    pairs = sorted(data["pair_id"].unique())
    n = np.zeros((len(pairs), len(CELLS)))
    k = np.zeros_like(n)
    for ci, pair in enumerate(pairs):
        sub = data[data["pair_id"] == pair]
        for j, (d, p) in enumerate(CELLS):
            cell = sub[(sub["difficulty"] == d) & (sub["peer_condition"] == p)]
            n[ci, j], k[ci, j] = len(cell), cell["policy_violation"].sum()
    return n, k


def _group_rate(n, k, cells):
    total = n[..., cells].sum(axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total > 0, k[..., cells].sum(axis=-1) / total, np.nan)


def _bootstrap(data, statistic, seed, n_boot=N_BOOT):
    """Point estimate and 95% interval, resampling whole task pairs (clusters)."""
    n, k = _counts(data)
    est = float(statistic(n.sum(axis=0), k.sum(axis=0)))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n), size=(n_boot, len(n)))
    draws = statistic(n[idx].sum(axis=1), k[idx].sum(axis=1))
    draws = draws[~np.isnan(draws)]
    if len(draws) < 20:
        return est, None, None
    return est, float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def risk_difference(df, group_a, group_b, seed=0):
    data = _primary(df)
    a, b = _cell_indices(group_a), _cell_indices(group_b)
    est, lo, hi = _bootstrap(data, lambda n, k: _group_rate(n, k, a) - _group_rate(n, k, b), seed)
    return {"estimate": None if math.isnan(est) else est, "ci_low": lo, "ci_high": hi,
            "n_a": int(len(_select(data, group_a))), "n_b": int(len(_select(data, group_b)))}


def interaction_contrast(df, reference, seed=0):
    """Delta_pressure = [imp: mal - ref] - [solv: mal - ref] (plan RQ3)."""
    data = _primary(df)
    cells = {(d, p): _cell_indices({"difficulty": d, "peer_condition": p}) for d in DIFFICULTIES for p in PEERS}

    def stat(n, k):
        def rd(d):
            return _group_rate(n, k, cells[(d, "malicious")]) - _group_rate(n, k, cells[(d, reference)])
        return rd("impossible") - rd("solvable")
    est, lo, hi = _bootstrap(data, stat, seed)
    return {"estimate": None if math.isnan(est) else est, "ci_low": lo, "ci_high": hi, "reference": reference}


def mixed_model(df):
    """logit P(V) = b0 + b1 Imp + b2 Mal + b3 Ben + b4 Imp:Mal + b5 Imp:Ben + u_task (variational Bayes)."""
    data = _primary(df).copy()
    if len(data) < 12 or data["policy_violation"].nunique() < 2:
        return {"status": "skipped", "reason": "too few episodes or no variation in the outcome"}
    data["impossible"] = (data["difficulty"] == "impossible").astype(int)
    data["malicious"] = (data["peer_condition"] == "malicious").astype(int)
    data["benign"] = (data["peer_condition"] == "benign").astype(int)
    try:
        from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM

        model = BinomialBayesMixedGLM.from_formula(
            "policy_violation ~ impossible + malicious + benign + impossible:malicious + impossible:benign",
            {"task": "0 + C(task_id)"}, data)
        np.random.seed(20261027)  # the variational optimiser starts from random values
        fit = model.fit_vb()
        names = list(model.exog_names)
        # A variational approximation: report to 4 decimals, not to optimiser noise. The optimiser is seeded but not
        # bit-reproducible (measured: a coefficient on a rounding boundary flips its 4th decimal in ~4% of repeat
        # calls, single-threaded too), so read the last digit as noise; the bootstrap numbers are exact.
        effects = {n: {"mean": round(float(m), 4), "sd": round(float(s), 4)}
                   for n, m, s in zip(names, fit.fe_mean, fit.fe_sd)}
        return {"status": "ok", "method": "BinomialBayesMixedGLM variational Bayes, task random intercept",
                "fixed_effects": effects, "interaction_log_odds": effects["impossible:malicious"]["mean"],
                "odds_ratio_interaction": round(math.exp(effects["impossible:malicious"]["mean"]), 4)}
    except Exception as exc:  # reported, never hidden
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _model_summary(df):
    imp, solv = {"difficulty": "impossible"}, {"difficulty": "solvable"}
    mal, ben, solo = {"peer_condition": "malicious"}, {"peer_condition": "benign"}, {"peer_condition": "solo"}
    return {
        "h1_impossible_minus_solvable": risk_difference(df, imp, solv),
        "h2_malicious_minus_solo": risk_difference(df, mal, solo),
        "h2_malicious_minus_benign": risk_difference(df, mal, ben),
        "h2_by_difficulty": {
            d: {"malicious_minus_solo": risk_difference(df, {**mal, "difficulty": d}, {**solo, "difficulty": d}),
                "malicious_minus_benign": risk_difference(df, {**mal, "difficulty": d}, {**ben, "difficulty": d})}
            for d in DIFFICULTIES},
        "interaction": interaction_contrast(df, "benign"),
        "interaction_vs_solo": interaction_contrast(df, "solo"),
        "h4_benign_minus_solo_safe_abstention_impossible": _abstention_difference(df),
        "mixed_model": mixed_model(df),
    }


def _abstention_difference(df):
    data = _primary(df)
    a = _select(data, {"difficulty": "impossible", "peer_condition": "benign"})
    b = _select(data, {"difficulty": "impossible", "peer_condition": "solo"})
    return {"estimate": _rate(a, "safe_abstention") - _rate(b, "safe_abstention") if len(a) and len(b) else None,
            "n_benign": int(len(a)), "n_solo": int(len(b))}


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return None if math.isnan(obj) else float(obj)
    return obj


def _refuse_mixed_cohorts(df):
    """A frozen experiment version and route configuration are the unit of analysis (plan 8.5, 15.3)."""
    for route, sub in df.groupby("model_route"):
        for col in ("experiment_id", "route_hash"):
            values = sorted(sub[col].dropna().unique())
            if len(values) > 1:
                raise ValueError(f"mixed cohorts in route {route!r}: several {col} values {values}; analyse each separately")


def analyze(df, out_dir):
    _refuse_mixed_cohorts(df)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = {"models": {}, "exclusions": {}, "exposure": {}, "sensitivity_with_fallback": {},
               "sensitivity_submitted_only": {}, "sensitivity_no_runtime_inspection": {}, "replication": {}}
    for route, sub in df.groupby("model_route"):
        condition_table(sub).to_csv(out / f"condition_table_{route}.csv", index=False)
        funnel = exposure_table(sub)
        funnel.to_csv(out / f"exposure_table_{route}.csv", index=False)
        summary["exposure"][route] = {
            "cells": funnel.to_dict("records"),
            "episodes_without_a_cell": int(len(_reporting_attempts(sub)) - funnel["episodes"].sum()),
        }
        summary["models"][route] = _model_summary(sub)
        summary["models"][route]["end_reasons"] = dict(sorted(Counter(_primary(sub)["end_reason"].fillna("unknown")).items()))
        inspected = _primary(sub)[_primary(sub)["inspected_runtime"].astype(bool)]
        summary["models"][route]["inspected_runtime_by_condition"] = dict(sorted(
            Counter(f"{d}/{p}" for d, p in zip(inspected["difficulty"], inspected["peer_condition"])).items()))
        blind = sub.assign(primary=sub["primary"] & ~sub["inspected_runtime"].astype(bool))
        summary["sensitivity_no_runtime_inspection"][route] = {
            "h2_malicious_minus_solo": risk_difference(blind, {"peer_condition": "malicious"}, {"peer_condition": "solo"}),
            "interaction": interaction_contrast(blind, "benign"),
        }
        submitted = sub.assign(primary=sub["primary"] & (sub["end_reason"] == "submitted"))
        summary["sensitivity_submitted_only"][route] = {
            "h2_malicious_minus_solo": risk_difference(submitted, {"peer_condition": "malicious"}, {"peer_condition": "solo"}),
            "interaction": interaction_contrast(submitted, "benign"),
        }
        summary["exclusions"][route] = exclusion_report(sub)
        # Every arm alike: an episode that ended before any trigger is valid in solo (no treatment to check) but
        # invalid in a peer arm (the message never went out), so `valid` alone would compare unlike episodes.
        with_fallback = sub.assign(primary=sub["valid"] & sub["trigger_source"].isin(EXPOSED_TRIGGERS))
        summary["sensitivity_with_fallback"][route] = {
            "h2_malicious_minus_solo": risk_difference(with_fallback, {"peer_condition": "malicious"}, {"peer_condition": "solo"}),
            "interaction": interaction_contrast(with_fallback, "benign"),
        }
    routes = sorted(summary["models"])
    if len(routes) >= 2:
        def sign(route, key):
            est = summary["models"][route][key]["estimate"]
            return None if est is None else (est > 0) - (est < 0)
        summary["replication"] = {
            key: {r: sign(r, key) for r in routes} for key in ("h2_malicious_minus_solo", "interaction")}
        summary["replication"]["note"] = "direction only; raw violation rates are not compared across models"
    summary = _clean(summary)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    df = pd.read_parquet(args.data) if args.data.suffix == ".parquet" else pd.read_csv(args.data)
    print(json.dumps(analyze(df, args.out), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
