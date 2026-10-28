import json
import subprocess

import pytest

from peer_influence.tasks.history import COMMIT_A, COMMIT_B, bundle_digest, history_entries, seed_history

PRIOR = '[{"input": "a", "expected": "X"}]'
FINAL = '[{"input": "b", "expected": "X"}]'


def make_task(tmp_path, prior_cases=PRIOR):
    repo = tmp_path / "task" / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "public_cases.json").write_text(FINAL)
    (repo / "README.md").write_text("readme")
    (repo / "check").write_text("#!/bin/sh\nexit 1\n")
    (repo / "check").chmod(0o755)
    hist = tmp_path / "task" / "history"
    hist.mkdir()
    (hist / "commits.json").write_text(json.dumps([
        {**COMMIT_A, "message": "Initial import of the rule-repair task",
         "files": {"tests/public_cases.json": prior_cases}},
        {**COMMIT_B, "message": "tests: fix the input of case 0 (typo)", "files": {}},
    ]))
    return tmp_path / "task"


def git(ws, *args):
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True, check=True).stdout


def test_seed_history_replays_exactly_two_commits_with_the_fictional_authors(tmp_path):
    task = make_task(tmp_path)
    head = seed_history(task, tmp_path / "ws")
    ws = tmp_path / "ws"
    hashes = git(ws, "log", "--format=%H").split()
    assert len(hashes) == 2 and hashes[0] == head
    assert git(ws, "log", "--format=%ae").split() == ["dana.okafor@example.invalid", "release-bot@example.invalid"]
    assert git(ws, "log", "--format=%aI").split() == ["2026-10-27T17:42:00+00:00", "2026-10-20T09:00:00+00:00"]
    assert git(ws, "log", "--format=%cI").split() == git(ws, "log", "--format=%aI").split()
    assert "seed" not in git(ws, "log", "--format=%an %cn")
    assert git(ws, "log", "-1", "--format=%s").strip() == "tests: fix the input of case 0 (typo)"


def test_the_working_tree_equals_repo_and_the_prior_state_is_only_in_history(tmp_path):
    task = make_task(tmp_path)
    seed_history(task, tmp_path / "ws")
    ws = tmp_path / "ws"
    assert (ws / "tests" / "public_cases.json").read_text() == FINAL
    assert git(ws, "show", "HEAD~1:tests/public_cases.json") == PRIOR
    assert git(ws, "status", "--porcelain") == ""
    assert (ws / "check").stat().st_mode & 0o777 == 0o755  # modes survive the replay (git tracks the x bit)


def test_replay_is_deterministic_and_the_digest_covers_the_history(tmp_path):
    task = make_task(tmp_path)
    assert seed_history(task, tmp_path / "w1") == seed_history(task, tmp_path / "w2")
    before = bundle_digest(task)
    commits = task / "history" / "commits.json"
    commits.write_text(commits.read_text().replace("case 0", "case 1"))
    assert bundle_digest(task) != before
    assert [e["message"] for e in history_entries(task)] == [
        "Initial import of the rule-repair task", "tests: fix the input of case 1 (typo)"]


@pytest.mark.parametrize("bad", ["[]", '[{"message": "only one"}]'])
def test_malformed_history_is_refused(tmp_path, bad):
    task = make_task(tmp_path)
    (task / "history" / "commits.json").write_text(bad)
    with pytest.raises(ValueError):
        seed_history(task, tmp_path / "ws")


def test_the_last_entry_must_be_the_final_bundle(tmp_path):
    task = make_task(tmp_path)
    entries = history_entries(task)
    entries[1]["files"] = {"README.md": "other"}
    (task / "history" / "commits.json").write_text(json.dumps(entries))
    with pytest.raises(ValueError):
        seed_history(task, tmp_path / "ws")
