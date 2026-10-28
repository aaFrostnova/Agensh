"""Repository history of a task bundle (v0.3.0 design, L2).

A task directory holds `repo/` (the final workspace) and `history/commits.json`: two entries, the
first describing the prior state of some files, the second the final bundle. `seed_history` replays
them into a fresh git workspace so the agent sees a plausible two-commit history; the runner, the
validity gates and the tests all seed through it, so the scorer's base commit is always commit B.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from peer_influence.common import sha256_bytes, tree_sha256

COMMIT_A = {"author_name": "Release Bot", "author_email": "release-bot@example.invalid",
            "date": "2026-10-20T09:00:00Z"}
COMMIT_B = {"author_name": "Dana Okafor", "author_email": "dana.okafor@example.invalid",
            "date": "2026-10-27T17:42:00Z"}


def history_entries(task_dir):
    return json.loads((Path(task_dir) / "history" / "commits.json").read_text(encoding="utf-8"))


def bundle_digest(task_dir):
    """One digest over the final workspace and the history that leads to it."""
    task_dir = Path(task_dir)
    return sha256_bytes((tree_sha256(task_dir / "repo") + "\n" + tree_sha256(task_dir / "history")).encode())


def _commit(work, entry):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": entry["author_name"], "GIT_AUTHOR_EMAIL": entry["author_email"],
           "GIT_AUTHOR_DATE": entry["date"], "GIT_COMMITTER_NAME": entry["author_name"],
           "GIT_COMMITTER_EMAIL": entry["author_email"], "GIT_COMMITTER_DATE": entry["date"]}
    for args in (["add", "-A"], ["commit", "-q", "-m", entry["message"]]):
        subprocess.run(["git", "-C", str(work), "-c", "commit.gpgsign=false", *args], check=True,
                       capture_output=True, text=True, env=env)


def seed_history(task_dir, work):
    """Copy `repo/` into `work`, replay the two commits, return HEAD (the final bundle)."""
    task_dir, work = Path(task_dir), Path(work)
    entries = history_entries(task_dir)
    if len(entries) != 2:
        raise ValueError(f"history/commits.json must hold exactly two commits, found {len(entries)}")
    if entries[1].get("files"):
        raise ValueError("the last history entry is the final bundle and must not override files")
    if work.exists() and any(work.iterdir()):
        raise FileExistsError(f"{work} is not empty: not a fresh profile")
    shutil.copytree(task_dir / "repo", work, dirs_exist_ok=True)  # the stack pre-creates the empty bind-mount target
    subprocess.run(["git", "-C", str(work), "init", "-q", "-b", "main"], check=True, capture_output=True)
    for rel, text in entries[0].get("files", {}).items():
        (work / rel).write_text(text, encoding="utf-8")
    _commit(work, entries[0])
    for rel in entries[0].get("files", {}):
        shutil.copy2(task_dir / "repo" / rel, work / rel)
    _commit(work, entries[1])
    return subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
