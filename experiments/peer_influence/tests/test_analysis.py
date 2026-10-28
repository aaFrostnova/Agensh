import json

import numpy as np
import pandas as pd
import pytest

from peer_influence.analysis.analyze import (
    analyze,
    condition_table,
    interaction_contrast,
    risk_difference,
    wilson_interval,
)
from peer_influence.analysis.build_dataset import build_dataset, exclusion_report


def write_episode(root, n, *, route="qwen", difficulty="impossible", peer="malicious", pair=0, seed=1,
                  violation=False, valid=True, trigger="first_failed_check", failing=(), end_reason="submitted",
                  experiment_id="x", route_hash="h1"):
    d = root / route / f"ep_{n:06d}"
    d.mkdir(parents=True)
    task = f"mapping_{pair:03d}_{difficulty}"
    (d / "manifest.json").write_text(json.dumps({
        "experiment_id": experiment_id, "route_hash": route_hash, "episode_id": f"ep_{n:06d}", "model_route": route, "task_id": task,
        "task_pair_id": f"mapping_{pair:03d}", "difficulty": difficulty, "peer_condition": peer, "seed": seed,
        "run_attempt": 1, "trigger_source": trigger, "end_reason": end_reason}))
    (d / "score.json").write_text(json.dumps({
        "episode_id": f"ep_{n:06d}", "policy_violation": violation, "violation_types": ["protected_file_write"] if violation else [],
        "safe_abstention": (not violation and difficulty == "impossible"), "false_abstention": False,
        "genuine_external_success": (not violation and difficulty == "solvable"), "apparent_local_success": violation,
        "submission_status": "completed", "treatment_to_first_violation_seconds": 5.0 if violation else None,
        "violation_before_treatment": False, "trigger_source": trigger,
        "data_quality": "valid" if valid else "invalid"}))
    checks = {"treatment_delivered": True, "fresh_profile": True}
    for name in failing:
        checks[name] = False
    (d / "quality_report.json").write_text(json.dumps({
        "valid": valid and not failing, "checks": checks,
        "invalid_reasons": [f"{c}: x" for c in failing]}))
    (d / "controller_state.json").write_text(json.dumps({"state": "EPISODE_DONE", "trigger_source": trigger}))
    return d


def toy_runs(tmp_path):
    n = 0
    # impossible: solo 0/6, benign 0/6, malicious 5/6 ; solvable: solo 0/6, benign 0/6, malicious 1/6
    plan = {("impossible", "solo"): 0, ("impossible", "benign"): 0, ("impossible", "malicious"): 5,
            ("solvable", "solo"): 0, ("solvable", "benign"): 0, ("solvable", "malicious"): 1}
    for (difficulty, peer), k in plan.items():
        for i in range(6):
            n += 1
            write_episode(tmp_path, n, difficulty=difficulty, peer=peer, pair=i % 3, seed=i // 3 + 1, violation=i < k)
    return tmp_path


def test_dataset_has_one_row_per_episode_with_flat_columns(tmp_path):
    df = build_dataset(toy_runs(tmp_path))
    assert len(df) == 36
    for col in ("model_route", "episode_id", "task_id", "pair_id", "difficulty", "peer_condition", "seed",
                "policy_violation", "valid", "trigger_source", "primary"):
        assert col in df.columns, col
    assert df["policy_violation"].dtype == int and df["primary"].all()


def test_only_engineering_failures_exclude_an_episode_from_the_primary_analysis(tmp_path):
    write_episode(tmp_path, 1, violation=True)
    write_episode(tmp_path, 2, violation=True, valid=False, failing=("treatment_delivered",))
    write_episode(tmp_path, 3, violation=False, trigger="fallback_timer")
    df = build_dataset(tmp_path)
    by_id = df.set_index("episode_id")
    assert by_id.loc["ep_000001", "primary"] and not by_id.loc["ep_000002", "primary"]
    assert not by_id.loc["ep_000003", "primary"]  # fallback trigger: kept, flagged, analysed separately
    rep = exclusion_report(df)
    assert rep["n_episodes"] == 3 and rep["n_primary"] == 1
    assert rep["invalid_by_check"] == {"treatment_delivered": 1} and rep["fallback_trigger"] == 1


def test_a_violating_but_valid_episode_is_never_excluded_for_strange_behaviour(tmp_path):
    write_episode(tmp_path, 1, violation=True)
    df = build_dataset(tmp_path)
    assert df.loc[0, "primary"] == True and df.loc[0, "policy_violation"] == 1  # noqa: E712


def test_wilson_interval_matches_known_values():
    lo, hi = wilson_interval(5, 6)
    assert lo == pytest.approx(0.4365, abs=1e-3) and hi == pytest.approx(0.9699, abs=1e-3)
    assert wilson_interval(0, 0) == (None, None)


def test_condition_table_reports_rate_and_n_per_cell(tmp_path):
    t = condition_table(build_dataset(toy_runs(tmp_path)))
    row = t[(t.difficulty == "impossible") & (t.peer_condition == "malicious")].iloc[0]
    assert (row.n, row.violations) == (6, 5) and row.rate == pytest.approx(5 / 6)
    assert len(t) == 6


def test_risk_difference_and_interaction_use_the_plan_definitions(tmp_path):
    df = build_dataset(toy_runs(tmp_path))
    rd = risk_difference(df, {"peer_condition": "malicious", "difficulty": "impossible"},
                         {"peer_condition": "benign", "difficulty": "impossible"})
    assert rd["estimate"] == pytest.approx(5 / 6)
    # Delta_pressure = (5/6 - 0) - (1/6 - 0)
    ic = interaction_contrast(df, reference="benign")
    assert ic["estimate"] == pytest.approx(4 / 6)
    assert ic["ci_low"] <= ic["estimate"] <= ic["ci_high"]


def test_bootstrap_intervals_are_reproducible(tmp_path):
    df = build_dataset(toy_runs(tmp_path))
    assert interaction_contrast(df, "benign", seed=3) == interaction_contrast(df, "benign", seed=3)


def test_analyze_writes_tables_and_a_reproducible_summary(tmp_path):
    runs = toy_runs(tmp_path / "runs")
    df = build_dataset(runs)
    out = tmp_path / "results"
    first = analyze(df, out)
    second = analyze(df, tmp_path / "results2")
    assert (out / "summary.json").exists() and (out / "condition_table_qwen.csv").exists()
    assert first == second
    q = first["models"]["qwen"]
    assert q["h2_malicious_minus_solo"]["estimate"] == pytest.approx((5 + 1) / 12)
    assert q["interaction"]["estimate"] == pytest.approx(4 / 6)
    assert q["mixed_model"]["status"] in ("ok", "failed")
    assert np.isfinite(q["mixed_model"].get("interaction_log_odds", 0.0))
    assert first["exclusions"]["qwen"]["n_primary"] == 36


def write_failed(root, n, route="qwen", phase="stack", with_manifest=True):
    d = root / route / f"ep_{n:06d}"
    d.mkdir(parents=True)
    (d / "run_failed.json").write_text(json.dumps({"phase": phase, "error": "RuntimeError: boom"}))
    if with_manifest:
        (d / "manifest.json").write_text(json.dumps({
            "experiment_id": "x", "route_hash": "h1", "episode_id": f"ep_{n:06d}", "model_route": route,
            "task_id": "mapping_000_impossible", "task_pair_id": "mapping_000", "difficulty": "impossible",
            "peer_condition": "benign", "seed": 1, "run_attempt": 1}))


def test_episodes_that_crashed_before_scoring_are_counted_not_dropped(tmp_path):
    write_episode(tmp_path, 1, violation=True)
    write_failed(tmp_path, 2)
    write_failed(tmp_path, 3, phase="inputs", with_manifest=False)  # failed before a manifest existed
    df = build_dataset(tmp_path)
    assert len(df) == 3
    failed = df[df["episode_id"].isin(["ep_000002", "ep_000003"])]
    assert not failed["valid"].any() and not failed["primary"].any() and (failed["failed_checks"] == "run_failed").all()
    assert set(failed["model_route"]) == {"qwen"}
    rep = exclusion_report(df)
    assert rep["n_episodes"] == 3 and rep["n_run_failed"] == 2 and rep["n_primary"] == 1
    assert rep["run_failed_by_phase"] == {"inputs": 1, "stack": 1}


def test_dataset_carries_experiment_and_route_identity_and_analysis_refuses_to_mix_cohorts(tmp_path):
    write_episode(tmp_path, 1, experiment_id="exp_v1")
    write_episode(tmp_path, 2, experiment_id="exp_v2")
    df = build_dataset(tmp_path)
    assert {"experiment_id", "route_hash"} <= set(df.columns)
    with pytest.raises(ValueError, match="mixed"):
        analyze(df, tmp_path / "out")
    other_hash = build_dataset(_runs_with_two_route_hashes(tmp_path / "second"))
    with pytest.raises(ValueError, match="mixed"):
        analyze(other_hash, tmp_path / "out2")


def _runs_with_two_route_hashes(root):
    write_episode(root, 1, route_hash="h1")
    write_episode(root, 2, route_hash="h2")
    return root


def test_end_reasons_are_reported_and_a_submitted_only_sensitivity_is_given(tmp_path):
    n = 0
    for peer, k in (("solo", 0), ("malicious", 3)):
        for i in range(4):
            n += 1
            write_episode(tmp_path, n, difficulty="impossible", peer=peer, pair=i % 2, seed=i // 2 + 1,
                          violation=i < k, end_reason="submitted" if i < 3 else "idle")
    summary = analyze(build_dataset(tmp_path), tmp_path / "out")
    model = summary["models"]["qwen"]
    assert model["end_reasons"] == {"idle": 2, "submitted": 6}
    sens = summary["sensitivity_submitted_only"]["qwen"]
    assert sens["h2_malicious_minus_solo"]["n_a"] == 3 and sens["h2_malicious_minus_solo"]["n_b"] == 3


def write_attempt(root, name, **kw):
    """An attempt directory named like the runner archives it (<episode>.attempt<k>)."""
    n = kw.pop("n")
    d = write_episode(root, n, **kw)
    d.rename(d.parent / name)
    return d.parent / name


def test_first_valid_attempt_is_the_one_analysed_and_failed_attempts_are_counted(tmp_path):
    failed = tmp_path / "qwen" / "ep_000001.attempt1"
    failed.mkdir(parents=True)
    (failed / "run_failed.json").write_text(json.dumps({"phase": "interrupted", "error": "cut off"}))
    (failed / "manifest.json").write_text(json.dumps({
        "experiment_id": "x", "route_hash": "h1", "episode_id": "ep_000001", "model_route": "qwen",
        "task_id": "mapping_000_impossible", "task_pair_id": "mapping_000", "difficulty": "impossible",
        "peer_condition": "benign", "seed": 1, "run_attempt": 1}))
    ok = write_episode(tmp_path, 1, peer="benign", violation=True)
    manifest = json.loads((ok / "manifest.json").read_text())
    manifest["run_attempt"] = 2
    (ok / "manifest.json").write_text(json.dumps(manifest))
    df = build_dataset(tmp_path)
    assert sorted(df["attempt"]) == [1, 2] and set(df["episode_id"]) == {"ep_000001"}
    assert df[df["attempt"] == 2]["primary"].item() and not df[df["attempt"] == 1]["primary"].item()
    rep = exclusion_report(df)
    assert (rep["n_episodes"], rep["n_attempts"], rep["n_retried"], rep["n_primary"]) == (1, 2, 1, 1)


def test_a_later_retry_never_replaces_a_valid_first_attempt(tmp_path):
    write_episode(tmp_path, 1, peer="benign", violation=False)
    write_attempt(tmp_path, "ep_000001.attempt2", n=2, peer="benign", violation=True)
    # the second directory claims to be a retry of episode 1
    d = tmp_path / "qwen" / "ep_000001.attempt2"
    manifest = json.loads((d / "manifest.json").read_text())
    manifest.update(episode_id="ep_000001", run_attempt=2)
    (d / "manifest.json").write_text(json.dumps(manifest))
    df = build_dataset(tmp_path)
    chosen = df[df["primary"]]
    assert len(chosen) == 1 and chosen["attempt"].item() == 1 and chosen["policy_violation"].item() == 0


def test_a_scored_but_quality_invalid_attempt_gives_way_to_a_valid_retry(tmp_path):
    write_attempt(tmp_path, "ep_000001.attempt1", n=1, peer="benign", violation=True, valid=False,
                  failing=("treatment_delivered",))
    d = tmp_path / "qwen" / "ep_000001.attempt1"
    manifest = json.loads((d / "manifest.json").read_text())
    manifest.update(episode_id="ep_000001", run_attempt=1)
    (d / "manifest.json").write_text(json.dumps(manifest))
    ok = write_episode(tmp_path, 1, peer="benign", violation=False)
    manifest = json.loads((ok / "manifest.json").read_text())
    manifest["run_attempt"] = 2
    (ok / "manifest.json").write_text(json.dumps(manifest))
    df = build_dataset(tmp_path)
    chosen = df[df["primary"]]
    assert len(chosen) == 1 and chosen["attempt"].item() == 2


def test_an_invalidated_scored_episode_is_never_primary_and_the_retry_takes_over(tmp_path):
    bad = write_episode(tmp_path, 1, peer="benign", violation=True)
    (bad / "invalidated.json").write_text(json.dumps({"reason": "scorer bug", "at": "2026-10-28T03:40:00Z"}))
    bad.rename(bad.parent / "ep_000001.attempt1")
    ok = write_episode(tmp_path, 1, peer="benign", violation=False)
    manifest = json.loads((ok / "manifest.json").read_text())
    manifest["run_attempt"] = 2
    (ok / "manifest.json").write_text(json.dumps(manifest))
    df = build_dataset(tmp_path)
    first = df[df["attempt"] == 1].iloc[0]
    assert not first["valid"] and "invalidated" in first["failed_checks"] and not first["primary"]
    chosen = df[df["primary"]]
    assert len(chosen) == 1 and chosen["attempt"].item() == 2 and chosen["policy_violation"].item() == 0
