"""Freeze the data-generating inputs of a formal cohort (plan 13.4, 15.3).

A formal cohort (an experiment id without --tag) may only run while the prompts, task set, configs,
harness code that produces or labels episodes, and upstream runtime files are exactly what was frozen.
Any change needs a new experiment version, not a quiet edit. Analysis statistics are deliberately not
frozen (they can be fixed after the data exist); the primary-inclusion rule in build_dataset.py is.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from peer_influence.common import sha256_bytes, sha256_file, write_json_atomic
from peer_influence.evaluation.score_episode import EVALUATOR_VERSION
from peer_influence.routes import route_hash
from peer_influence.runtime.emit_event import utc_now

PI = "experiments/peer_influence"
FROZEN_PATHS = (
    f"{PI}/prompts/victim.md", f"{PI}/prompts/task_brief.md", f"{PI}/prompts/peer_benign.txt",
    f"{PI}/prompts/peer_malicious.txt", f"{PI}/configs", f"{PI}/tasks", f"{PI}/runtime", f"{PI}/controller",
    f"{PI}/evaluation", f"{PI}/analysis/build_dataset.py",
    *(f"{PI}/{name}.py" for name in ("common", "routes", "preflight", "stack", "runner", "matrix", "attempts",
                                    "manifest", "serving", "freeze")),
    f"{PI}/scripts/run_episode.py", f"{PI}/scripts/run_matrix.py", f"{PI}/scripts/clean_episode.py",
    f"{PI}/scripts/serve_qwen_local.sh",
    "agent", "board-service", "mini-router/src", "mini-router/package-lock.json", "secret-service",
    "docker/bootstrap", "scripts/strip_metadata_proxy.py", "prompts/pbench-brief.txt",
)


class FreezeError(RuntimeError):
    pass


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise FreezeError(f"git {args[0]}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _tracked_files(repo, paths):
    return sorted(p for p in _git(repo, "ls-files", "-z", "--", *paths).split("\0") if p)


def _file_hashes(repo, paths):
    out = {}
    for rel in _tracked_files(repo, paths):
        f = Path(repo) / rel
        if f.is_file():
            out[rel] = sha256_file(f)
    return out


def _combined(files):
    return sha256_bytes("".join(f"{p}\0{h}\n" for p, h in sorted(files.items())).encode())


def _untracked(repo, paths):
    return sorted(p for p in _git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths).split("\0") if p)


def _modified(repo, paths):
    return sorted(p for p in _git(repo, "diff", "--name-only", "-z", "HEAD", "--", *paths).split("\0") if p)


def build_freeze(repo_root, task_root, routes, experiment_id, frozen_paths=FROZEN_PATHS):
    repo = Path(repo_root)
    dirty = _modified(repo, frozen_paths) + _untracked(repo, frozen_paths)
    if dirty:
        raise FreezeError("commit these before freezing (uncommitted or untracked in the frozen paths): " + ", ".join(dirty))
    files = _file_hashes(repo, frozen_paths)
    return {
        "experiment_id": experiment_id, "created_at": utc_now(), "code_commit": _git(repo, "rev-parse", "HEAD").strip(),
        "files": files, "content_sha256": _combined(files),
        "task_index_sha256": sha256_file(Path(task_root) / "index.json"),
        "routes": {name: route_hash(r) for name, r in routes.items()}, "evaluator_version": EVALUATOR_VERSION,
    }


def verify_freeze(freeze, repo_root, task_root, routes, route_name, frozen_paths=FROZEN_PATHS):
    repo, problems = Path(repo_root), []
    now = _file_hashes(repo, frozen_paths)
    for rel in _untracked(repo, frozen_paths):
        now.setdefault(rel, "untracked")
    old = freeze["files"]
    for rel in sorted(set(old) | set(now)):
        if rel not in now:
            problems.append(f"frozen file removed: {rel}")
        elif rel not in old:
            problems.append(f"file added to the frozen paths: {rel}")
        elif old[rel] != now[rel]:
            problems.append(f"frozen file changed: {rel}")
    if sha256_file(Path(task_root) / "index.json") != freeze["task_index_sha256"]:
        problems.append("the task set (tasks index) changed")
    if route_name not in freeze["routes"] or route_name not in routes:
        problems.append(f"route {route_name!r} is not part of this freeze")
    elif route_hash(routes[route_name]) != freeze["routes"][route_name]:
        problems.append(f"route {route_name!r} configuration changed")
    if freeze.get("evaluator_version") != EVALUATOR_VERSION:
        problems.append("evaluator version changed")
    return problems


def write_freeze(path, freeze):
    path = Path(path)
    if path.exists():
        raise FreezeError(f"{path} already exists: a freeze is write-once, bump the experiment version instead")
    write_json_atomic(path, freeze)


def create_freeze_tag(repo_root, freeze):
    tag = f"freeze/{freeze['experiment_id']}"
    if subprocess.run(["git", "-C", str(repo_root), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                      capture_output=True).returncode == 0:
        raise FreezeError(f"tag {tag} already exists")
    _git(repo_root, "-c", "user.name=freeze", "-c", "user.email=freeze@example.invalid", "tag", "-a", tag,
         freeze["code_commit"], "-m", f"freeze of {freeze['experiment_id']} (content {freeze['content_sha256'][:16]})")
    return tag


def check_cohort(freeze_path, repo_root, task_root, routes, route_name, frozen_paths=FROZEN_PATHS):
    freeze_path = Path(freeze_path)
    if not freeze_path.exists():
        return [f"no freeze at {freeze_path}: run scripts/freeze.py create first (or use --tag for a trial cohort)"]
    return verify_freeze(json.loads(freeze_path.read_text(encoding="utf-8")), repo_root, task_root, routes,
                         route_name, frozen_paths)


def cohort_gate(tag, freeze_path, repo_root, task_root, routes, route_name, frozen_paths=FROZEN_PATHS):
    """(problems, freeze_sha256). A trial cohort (tagged experiment id) needs no freeze; a formal one
    needs an intact one, and the hash of the freeze file is recorded in every manifest."""
    if tag:
        return [], None
    problems = check_cohort(freeze_path, repo_root, task_root, routes, route_name, frozen_paths)
    return problems, (None if problems else sha256_file(Path(freeze_path)))
