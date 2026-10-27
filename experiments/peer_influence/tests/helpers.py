"""Test-only helpers shared by several test modules."""
import shutil
import subprocess

GIT_ENV = ["-c", "user.name=seed", "-c", "user.email=seed@example.invalid", "-c", "commit.gpgsign=false"]


def git(ws, *args):
    return subprocess.run(
        ["git", "-C", str(ws), *GIT_ENV, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def seed_workspace(task_dir, dest):
    """Copy the canonical repo into dest as a git repository; return (dest, seed_commit)."""
    shutil.copytree(task_dir / "repo", dest)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "seed")
    return dest, git(dest, "rev-parse", "HEAD")
