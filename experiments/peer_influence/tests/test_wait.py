"""Review fix I3: with the checker validating submission.json, agents write it early; the episode must not end
as "submitted" while the agent is still working on it."""
import json
import os
import time

from peer_influence import runner
from peer_influence.runner import _wait


class Stack:
    procs = []

    def __init__(self):
        self.n = 1  # the victim already made a request

    def relay_stats(self):
        return {"responses_ok": self.n}


class Ctx:
    max_episode_seconds = 30
    quiesce_seconds = 0.3
    idle_seconds = 1.2


def live_dirs(tmp_path):
    work, events = tmp_path / "work", tmp_path / "events"
    work.mkdir(), events.mkdir()
    (work / "README.md").write_text("x")
    return work, events / "events.jsonl"


def check_event(path, t):
    with open(path, "a") as f:
        f.write(json.dumps({"event_type": "check_result", "exit_code": 0, "t_epoch": t}) + "\n")


def run_wait(stack, work):
    manifest = {}
    started = time.time()
    reason = _wait(stack, Ctx(), work, manifest, lambda m: None)
    return reason, time.time() - started, manifest


def test_an_unvalidated_submission_waits_for_the_idle_threshold_not_the_quiesce(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.05)
    work, events = live_dirs(tmp_path)
    (work / "submission.json").write_text("{}")  # written right after the first check, no check since
    reason, took, manifest = run_wait(Stack(), work)
    assert reason == "submitted" and took >= Ctx.idle_seconds  # not after 0.3 s
    assert manifest["submission_validated_by_check"] is False


def test_a_submission_followed_by_a_check_ends_after_the_quiesce(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.05)
    work, events = live_dirs(tmp_path)
    (work / "submission.json").write_text("{}")
    os.utime(work / "submission.json", (time.time() - 5, time.time() - 5))
    check_event(events, time.time())  # the agent ran ./check after writing the submission
    reason, took, manifest = run_wait(Stack(), work)
    assert reason == "submitted" and took < Ctx.idle_seconds
    assert manifest["submission_validated_by_check"] is True


def test_a_check_older_than_the_submission_does_not_count_as_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.05)
    work, events = live_dirs(tmp_path)
    check_event(events, time.time() - 10)
    (work / "submission.json").write_text("{}")
    reason, took, manifest = run_wait(Stack(), work)
    assert reason == "submitted" and took >= Ctx.idle_seconds and manifest["submission_validated_by_check"] is False


def test_without_a_submission_the_episode_ends_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.05)
    work, _ = live_dirs(tmp_path)
    reason, _, manifest = run_wait(Stack(), work)
    assert reason == "idle" and "submission_validated_by_check" not in manifest
