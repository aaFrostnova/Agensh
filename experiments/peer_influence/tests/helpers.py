"""Test-only helpers shared by several test modules."""
import subprocess

from peer_influence.tasks.history import seed_history

GIT_ENV = ["-c", "user.name=seed", "-c", "user.email=seed@example.invalid", "-c", "commit.gpgsign=false"]


def git(ws, *args):
    return subprocess.run(
        ["git", "-C", str(ws), *GIT_ENV, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def seed_workspace(task_dir, dest):
    """The workspace as the runner seeds it (history replayed); return (dest, base_commit)."""
    return dest, seed_history(task_dir, dest)
