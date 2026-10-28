import json

import pytest

from peer_influence.attempts import parse_attempt, plan_resume, prepare_attempt


def episode(root, name, *, scored=False, valid=True, failed=None, manifest=True):
    d = root / name
    d.mkdir(parents=True)
    (d / "marker.txt").write_text(name)
    if manifest:
        (d / "manifest.json").write_text(json.dumps({"episode_id": name.split(".")[0], "run_attempt": 1}))
    if scored:
        (d / "score.json").write_text(json.dumps({"policy_violation": False}))
        (d / "quality_report.json").write_text(json.dumps({"valid": valid, "checks": {}, "invalid_reasons": []}))
    if failed:
        (d / "run_failed.json").write_text(json.dumps(failed))
    return d


def test_attempt_directory_names_round_trip():
    assert parse_attempt("ep_000024") == ("ep_000024", None)
    assert parse_attempt("ep_000024.attempt3") == ("ep_000024", 3)
    assert parse_attempt("weird.attemptx") == ("weird.attemptx", None)


def test_a_fresh_episode_is_attempt_1_and_nothing_is_touched(tmp_path):
    assert prepare_attempt(tmp_path, "ep_000001") == 1
    assert list(tmp_path.iterdir()) == []


def test_a_cut_off_episode_is_archived_with_a_failure_record_and_retried_as_attempt_2(tmp_path):
    episode(tmp_path, "ep_000024")  # no score.json, no run_failed.json: the node went away
    assert prepare_attempt(tmp_path, "ep_000024") == 2
    assert not (tmp_path / "ep_000024").exists()
    kept = tmp_path / "ep_000024.attempt1"
    assert (kept / "marker.txt").read_text() == "ep_000024"  # evidence preserved, not deleted
    record = json.loads((kept / "run_failed.json").read_text())
    assert record["phase"] == "interrupted" and "cut off" in record["error"]


def test_an_existing_failure_record_is_kept_as_it_was(tmp_path):
    episode(tmp_path, "ep_000005", failed={"phase": "stack", "error": "RuntimeError: boom"})
    assert prepare_attempt(tmp_path, "ep_000005") == 2
    assert json.loads((tmp_path / "ep_000005.attempt1" / "run_failed.json").read_text())["phase"] == "stack"


def test_attempt_numbers_keep_counting_across_repeated_failures(tmp_path):
    episode(tmp_path, "ep_000007")
    assert prepare_attempt(tmp_path, "ep_000007") == 2
    episode(tmp_path, "ep_000007")  # the retry is cut off as well
    assert prepare_attempt(tmp_path, "ep_000007") == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ep_000007.attempt1", "ep_000007.attempt2"]


def test_a_scored_episode_is_never_rerun_unless_it_is_quality_invalid_and_retry_is_requested(tmp_path):
    episode(tmp_path, "ep_000001", scored=True, valid=True)
    episode(tmp_path, "ep_000002", scored=True, valid=False)
    assert prepare_attempt(tmp_path, "ep_000001", retry_invalid=True) is None  # valid results stay, whatever the flag
    assert prepare_attempt(tmp_path, "ep_000002") is None
    assert (tmp_path / "ep_000002").exists()
    assert prepare_attempt(tmp_path, "ep_000002", retry_invalid=True) == 2
    assert (tmp_path / "ep_000002.attempt1" / "score.json").exists() and not (tmp_path / "ep_000002").exists()


def test_plan_resume_returns_only_pending_specs_in_order_with_their_attempt_numbers(tmp_path):
    episode(tmp_path, "ep_000001", scored=True, valid=True)
    episode(tmp_path, "ep_000002")  # interrupted
    plan = [{"episode_id": f"ep_{i:06d}", "run_attempt": 1} for i in (1, 2, 3, 4)]
    todo = plan_resume(plan, tmp_path)
    assert [(s["episode_id"], s["run_attempt"]) for s in todo] == [("ep_000002", 2), ("ep_000003", 1), ("ep_000004", 1)]
    assert plan[1]["run_attempt"] == 1  # the input plan is not modified


def test_plan_resume_limit_counts_runs_and_does_not_archive_what_it_will_not_run(tmp_path):
    episode(tmp_path, "ep_000002")
    episode(tmp_path, "ep_000003")
    plan = [{"episode_id": f"ep_{i:06d}", "run_attempt": 1} for i in (1, 2, 3)]
    todo = plan_resume(plan, tmp_path, limit=2)
    assert [s["episode_id"] for s in todo] == ["ep_000001", "ep_000002"]
    assert (tmp_path / "ep_000003").exists() and not (tmp_path / "ep_000003.attempt1").exists()


def test_plan_resume_clears_the_node_local_live_root_of_episodes_it_will_rerun(tmp_path):
    runs, live = tmp_path / "runs", tmp_path / "live"
    episode(runs, "ep_000002")
    episode(runs, "ep_000001", scored=True, valid=True)
    (live / "ep_000002" / "mysql").mkdir(parents=True)
    (live / "ep_000001").mkdir(parents=True)
    plan = [{"episode_id": "ep_000001", "run_attempt": 1}, {"episode_id": "ep_000002", "run_attempt": 1}]
    todo = plan_resume(plan, runs, live_base=live)
    assert [s["episode_id"] for s in todo] == ["ep_000002"]
    assert not (live / "ep_000002").exists() and (live / "ep_000001").exists()


def test_an_operator_invalidated_episode_is_retried_by_default_and_the_evidence_stays(tmp_path):
    from peer_influence.attempts import invalidate

    d = episode(tmp_path, "ep_000009", scored=True, valid=True)
    record = invalidate(d, "scorer bug: snapshot lost the exec bit")
    assert record["reason"].startswith("scorer bug") and (d / "invalidated.json").exists()
    assert invalidate(d, "second reason")["reason"] == "scorer bug: snapshot lost the exec bit"  # first reason is kept
    assert prepare_attempt(tmp_path, "ep_000009") == 2  # no flag needed: an operator decision, not a guess
    assert (tmp_path / "ep_000009.attempt1" / "score.json").exists()
    assert (tmp_path / "ep_000009.attempt1" / "invalidated.json").exists()
