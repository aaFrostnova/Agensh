"""The workspace belongs to the agent: odd file types must never hang or crash the harness."""
import os
import threading

import pytest

from peer_influence.common import tree_sha256
from peer_influence.evaluation.score_episode import score_workspace
from peer_influence.runtime.protected_hashes import diff_snapshots, snapshot
from peer_influence.tests.helpers import seed_workspace


def within(seconds, fn, fifo=None):
    """Run fn in a daemon thread; fail (not hang) if it does not finish. Unblocks a stuck FIFO read."""
    box = {}
    t = threading.Thread(target=lambda: box.update(value=fn()), daemon=True)
    t.start()
    t.join(seconds)
    alive = t.is_alive()
    if alive and fifo is not None:
        os.close(os.open(fifo, os.O_RDWR))  # releases a reader blocked in open()
        t.join(2)
    assert not alive, "harness call blocked on a special file"
    return box["value"]


@pytest.fixture()
def fifo_workspace(tmp_path, impossible_dir):
    ws, base = seed_workspace(impossible_dir, tmp_path / "ws")
    return ws, base


def test_snapshot_does_not_block_on_a_fifo_inside_a_protected_directory(fifo_workspace):
    ws, _ = fifo_workspace
    os.mkfifo(ws / "tests" / "pipe")
    snap = within(5, lambda: snapshot(ws, ["tests/**"]), ws / "tests" / "pipe")
    assert snap["tests/pipe"].startswith("special:")
    before = {"tests/public_cases.json": snap["tests/public_cases.json"]}
    assert diff_snapshots(before, snap)["added"] == ["tests/pipe"]


def test_tree_hash_does_not_block_on_a_fifo_anywhere(fifo_workspace):
    ws, _ = fifo_workspace
    before = tree_sha256(ws)
    os.mkfifo(ws / "scratch.pipe")
    after = within(5, lambda: tree_sha256(ws), ws / "scratch.pipe")
    assert after != before


def test_scoring_survives_a_fifo_and_a_planted_fifo_in_tests_is_a_violation(fifo_workspace, impossible_dir):
    ws, base = fifo_workspace
    os.mkfifo(ws / "scratch.pipe")
    os.mkfifo(ws / "tests" / "pipe")
    score = within(30, lambda: score_workspace(workspace=ws, task_dir=impossible_dir, base_commit=base), ws / "scratch.pipe")
    assert "scratch.pipe" in score["modified_other_paths"]
    assert score["policy_violation"] is True and "tests/pipe" in score["modified_protected_paths"]


def test_protected_hashing_does_not_read_a_huge_file_in_full(tmp_path, monkeypatch):
    from peer_influence.runtime import protected_hashes

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "big.bin").write_bytes(b"x" * 2048)
    (tmp_path / "tests" / "small.json").write_text("{}")
    monkeypatch.setattr(protected_hashes, "MAX_HASH_BYTES", 1024)
    snap = protected_hashes.snapshot(tmp_path, ["tests/**"])
    assert snap["tests/big.bin"] == "large:2048" and snap["tests/small.json"].startswith("sha256:")
