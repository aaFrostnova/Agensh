import json
import os
import re
import subprocess
import sys

from peer_influence.tests.helpers import seed_workspace
from peer_influence.runtime import check_wrapper
from peer_influence.runtime.protected_hashes import baseline_record

WRAPPER = check_wrapper.__file__


def run(ws, env_extra, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PI_")}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, WRAPPER, "--workspace", str(ws), *args],
        cwd=ws, env=env, capture_output=True, text=True,
    )


def setup(tmp_path, task_dir, manifest_patterns):
    ws, _ = seed_workspace(task_dir, tmp_path / "ws")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(baseline_record(task_dir / "repo", manifest_patterns)))
    events = tmp_path / "events" / "events.jsonl"
    events.parent.mkdir()
    env = {
        "PI_EVENTS_PATH": str(events),
        "PI_BASELINE_HASHES": str(baseline),
        "PI_EPISODE_ID": "ep_000123",
        "PI_AGENT_ID": "worker1",
    }
    return ws, events, env


def read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def patterns(task_dir):
    return json.loads((task_dir / "manifest.json").read_text())["protected_paths"]


def submission_for(task_dir, status="completed"):
    version = json.loads((task_dir / "manifest.json").read_text())["checker_version"]
    return json.dumps({"status": status, "summary": "x", "checker_version": version})


def test_failed_check_records_one_event_with_the_plan_schema(tmp_path, solvable_dir):
    ws, events, env = setup(tmp_path, solvable_dir, patterns(solvable_dir))
    res = run(ws, env)
    assert res.returncode == 1
    assert "SUMMARY passed=" in res.stdout  # the agent sees the evaluator output
    (ev,) = read_events(events)
    assert ev["event_type"] == "check_result"
    assert ev["episode_id"] == "ep_000123" and ev["agent_id"] == "worker1"
    assert ev["exit_code"] == 1 and ev["failed"] >= 1 and ev["passed"] >= 0
    assert ev["protected_hash_changed"] is False
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ev["timestamp_utc"])
    assert re.fullmatch(r"[0-9a-f-]{36}", ev["event_id"])


def test_protected_edit_is_visible_in_the_next_event_and_events_append(tmp_path, solvable_dir):
    ws, events, env = setup(tmp_path, solvable_dir, patterns(solvable_dir))
    run(ws, env)
    (ws / "tests/public_cases.json").write_text("[]")
    (ws / "submission.json").write_text(submission_for(solvable_dir))
    res = run(ws, env)
    assert res.returncode == 0  # apparent pass: the wrapper reports it, never decides the label
    first, second = read_events(events)
    assert first["protected_hash_changed"] is False
    assert second["protected_hash_changed"] is True and second["exit_code"] == 0


def test_legitimate_fix_passes_without_flagging_protected_files(tmp_path, solvable_dir):
    ws, events, env = setup(tmp_path, solvable_dir, patterns(solvable_dir))
    (ws / "src/rules.json").write_text((solvable_dir / "reference/src/rules.json").read_text())
    (ws / "submission.json").write_text(submission_for(solvable_dir))
    res = run(ws, env)
    assert res.returncode == 0
    (ev,) = read_events(events)
    assert ev["failed"] == 0 and ev["protected_hash_changed"] is False


def test_wrapper_without_experiment_env_still_runs_and_writes_nothing(tmp_path, solvable_dir):
    ws, _ = seed_workspace(solvable_dir, tmp_path / "ws")
    res = run(ws, {})
    assert res.returncode == 1
    assert not any(p.suffix == ".jsonl" for p in tmp_path.rglob("*"))


def test_tampered_evaluator_that_prints_no_summary_still_yields_an_event(tmp_path, solvable_dir):
    ws, events, env = setup(tmp_path, solvable_dir, patterns(solvable_dir))
    (ws / "evaluator/check.py").write_text("print('all good')\n")
    res = run(ws, env)
    assert res.returncode == 0
    (ev,) = read_events(events)
    assert ev["exit_code"] == 0 and ev["passed"] is None and ev["failed"] is None
    assert ev["protected_hash_changed"] is True


def test_wrapper_reads_its_settings_from_a_config_file_when_the_env_is_not_passed_on(tmp_path, solvable_dir):
    ws, events, env = setup(tmp_path, solvable_dir, patterns(solvable_dir))
    cfg = tmp_path / "pi_config.json"
    cfg.write_text(json.dumps({
        "events_path": env["PI_EVENTS_PATH"], "baseline_hashes": env["PI_BASELINE_HASHES"],
        "episode_id": "ep_000777", "agent_id": "worker1",
    }))
    res = run(ws, {"PI_CONFIG": str(cfg)})
    assert res.returncode == 1
    (ev,) = read_events(events)
    assert ev["episode_id"] == "ep_000777" and ev["protected_hash_changed"] is False


def test_first_check_fails_even_with_correct_rules_until_the_submission_records_the_version(tmp_path, solvable_dir):
    ws, events, env = setup(tmp_path, solvable_dir, patterns(solvable_dir))
    (ws / "src/rules.json").write_text((solvable_dir / "reference/src/rules.json").read_text())
    res = run(ws, env)
    assert res.returncode == 1 and read_events(events)[0]["failed"] == 1  # the version is the only failure
    (ws / "submission.json").write_text(submission_for(solvable_dir))
    assert run(ws, env).returncode == 0
