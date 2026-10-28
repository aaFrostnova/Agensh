import subprocess

from peer_influence.runner import seed_workspace
from peer_influence.tasks.history import seed_history


def test_runner_seeds_the_replayed_history_and_returns_its_head(impossible_dir, tmp_path):
    head = seed_workspace(impossible_dir, tmp_path / "work")
    assert head == seed_history(impossible_dir, tmp_path / "again")
    count = subprocess.run(["git", "-C", str(tmp_path / "work"), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert count == "2"
