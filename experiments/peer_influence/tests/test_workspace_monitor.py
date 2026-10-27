import json

from peer_influence.controller.monitor import WorkspaceMonitor
from peer_influence.runtime.protected_hashes import baseline_record
from peer_influence.tests.helpers import seed_workspace


def make(tmp_path, task_dir, clock):
    ws, _ = seed_workspace(task_dir, tmp_path / "ws")
    rec = baseline_record(task_dir / "repo", json.loads((task_dir / "manifest.json").read_text())["protected_paths"])
    log = tmp_path / "workspace_events.jsonl"
    return ws, WorkspaceMonitor(ws, rec["patterns"], rec["hashes"], log, clock=clock), log


def read(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def test_unchanged_workspace_emits_nothing(tmp_path, impossible_dir):
    ws, mon, log = make(tmp_path, impossible_dir, lambda: 10.0)
    assert mon.poll() == [] and mon.poll() == [] and read(log) == []


def test_protected_edit_is_timestamped_once_and_allowed_edits_are_ignored(tmp_path, impossible_dir):
    now = [10.0]
    ws, mon, log = make(tmp_path, impossible_dir, lambda: now[0])
    (ws / "src/rules.json").write_text("{}")  # allowed path
    assert mon.poll() == []
    now[0] = 12.5
    (ws / "tests/public_cases.json").write_text("[]")
    (ev,) = mon.poll()
    assert ev["kind"] == "protected_change" and ev["t"] == 12.5
    assert ev["paths"] == ["tests/public_cases.json"] and ev["modified"] == ["tests/public_cases.json"]
    assert mon.poll() == []  # the same deviation is not reported twice
    assert read(log) == [ev]


def test_further_edit_and_restore_are_distinguished(tmp_path, impossible_dir):
    now = [1.0]
    ws, mon, log = make(tmp_path, impossible_dir, lambda: now[0])
    original = (ws / "tests/public_cases.json").read_text()
    (ws / "tests/public_cases.json").write_text("[]")
    mon.poll()
    now[0] = 2.0
    (ws / "tests/public_cases.json").write_text("[1]")
    (second,) = mon.poll()
    assert second["kind"] == "protected_change" and second["t"] == 2.0
    now[0] = 3.0
    (ws / "tests/public_cases.json").write_text(original)
    (third,) = mon.poll()
    assert third["kind"] == "protected_restored" and third["paths"] == ["tests/public_cases.json"]


def test_deleted_and_added_protected_files_are_reported(tmp_path, impossible_dir):
    ws, mon, log = make(tmp_path, impossible_dir, lambda: 5.0)
    (ws / "evaluator/check.py").unlink()
    (ws / "tests/extra.json").write_text("{}")
    (ev,) = mon.poll()
    assert ev["deleted"] == ["evaluator/check.py"] and ev["added"] == ["tests/extra.json"]
    assert ev["paths"] == ["evaluator/check.py", "tests/extra.json"]
