import json
import subprocess
from pathlib import Path

import pytest

from peer_influence.freeze import (
    FreezeError,
    build_freeze,
    check_cohort,
    create_freeze_tag,
    verify_freeze,
    write_freeze,
)
from peer_influence.routes import load_routes

ROUTES = load_routes(Path(__file__).resolve().parents[1] / "configs" / "model_routes.yaml")
FROZEN = ("prompts", "code")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    for rel, text in {"prompts/a.txt": "alpha", "code/m.py": "x = 1\n", "code/sub/n.py": "y = 2\n", "docs/readme.md": "docs"}.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture()
def tasks(tmp_path):
    d = tmp_path / "tasks"
    d.mkdir()
    (d / "index.json").write_text(json.dumps([{"task_id": "t1", "bundle_sha256": "aa"}]))
    return d


def build(repo, tasks):
    return build_freeze(repo, tasks, ROUTES, "exp_v1", frozen_paths=FROZEN)


def test_build_records_the_commit_every_frozen_file_the_task_index_and_the_routes(repo, tasks):
    f = build(repo, tasks)
    assert f["experiment_id"] == "exp_v1" and f["code_commit"] == git(repo, "rev-parse", "HEAD")
    assert sorted(f["files"]) == ["code/m.py", "code/sub/n.py", "prompts/a.txt"]  # docs are not frozen
    assert len(f["content_sha256"]) == 64 and len(f["task_index_sha256"]) == 64
    assert set(f["routes"]) == set(ROUTES) and all(len(v) == 64 for v in f["routes"].values())
    assert f["evaluator_version"]


def test_build_refuses_uncommitted_or_untracked_changes_inside_the_frozen_paths(repo, tasks):
    (repo / "code" / "m.py").write_text("x = 2\n")
    with pytest.raises(FreezeError, match="code/m.py"):
        build(repo, tasks)
    git(repo, "checkout", "--", "code/m.py")
    (repo / "code" / "new.py").write_text("z = 3\n")
    with pytest.raises(FreezeError, match="code/new.py"):
        build(repo, tasks)


def test_unfrozen_files_may_change_freely(repo, tasks):
    f = build(repo, tasks)
    (repo / "docs" / "readme.md").write_text("edited")
    git(repo, "commit", "-qam", "docs only")
    assert verify_freeze(f, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN) == []


def test_verify_names_every_changed_added_and_removed_frozen_file(repo, tasks):
    f = build(repo, tasks)
    (repo / "code" / "m.py").write_text("x = 99\n")
    (repo / "code" / "extra.py").write_text("e = 1\n")
    (repo / "prompts" / "a.txt").unlink()
    problems = " | ".join(verify_freeze(f, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN))
    assert "code/m.py" in problems and "code/extra.py" in problems and "prompts/a.txt" in problems


def test_verify_detects_a_changed_task_set_and_a_changed_route(repo, tasks):
    f = build(repo, tasks)
    (tasks / "index.json").write_text(json.dumps([{"task_id": "t1", "bundle_sha256": "bb"}]))
    assert any("task" in p for p in verify_freeze(f, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN))
    (tasks / "index.json").write_text(json.dumps([{"task_id": "t1", "bundle_sha256": "aa"}]))
    f["routes"]["qwen3_8_27b_local"] = "0" * 64
    assert any("route" in p for p in verify_freeze(f, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN))
    assert verify_freeze(f, repo, tasks, ROUTES, "kimi_k3_bedrock", frozen_paths=FROZEN) == []  # other route unaffected
    assert any("route" in p for p in verify_freeze(f, repo, tasks, ROUTES, "no_such_route", frozen_paths=FROZEN))


def test_a_freeze_is_write_once(tmp_path, repo, tasks):
    path = tmp_path / "runs" / "exp_v1" / "freeze.json"
    write_freeze(path, build(repo, tasks))
    assert json.loads(path.read_text())["experiment_id"] == "exp_v1"
    with pytest.raises(FreezeError, match="already"):
        write_freeze(path, build(repo, tasks))


def test_the_freeze_commit_gets_a_tag_once(repo, tasks):
    f = build(repo, tasks)
    create_freeze_tag(repo, f)
    assert git(repo, "rev-parse", "freeze/exp_v1^{commit}") == f["code_commit"]
    with pytest.raises(FreezeError, match="tag"):
        create_freeze_tag(repo, f)


def test_a_formal_cohort_needs_an_intact_freeze_file(tmp_path, repo, tasks):
    path = tmp_path / "freeze.json"
    assert any("no freeze" in p for p in check_cohort(path, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN))
    write_freeze(path, build(repo, tasks))
    assert check_cohort(path, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN) == []
    (repo / "code" / "m.py").write_text("x = 5\n")
    assert check_cohort(path, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN)


def test_trial_cohorts_are_exempt_and_formal_ones_get_the_freeze_hash_recorded(tmp_path, repo, tasks):
    from peer_influence.common import sha256_file
    from peer_influence.freeze import cohort_gate

    path = tmp_path / "runs" / "exp_v1" / "freeze.json"
    assert cohort_gate("smoke1", path, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN) == ([], None)
    problems, sha = cohort_gate(None, path, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN)
    assert problems and sha is None
    write_freeze(path, build(repo, tasks))
    problems, sha = cohort_gate(None, path, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN)
    assert problems == [] and sha == sha256_file(path)
    (repo / "prompts" / "a.txt").write_text("edited after the freeze")
    problems, sha = cohort_gate(None, path, repo, tasks, ROUTES, "qwen3_8_27b_local", frozen_paths=FROZEN)
    assert any("prompts/a.txt" in p for p in problems) and sha is None


def test_the_real_frozen_paths_all_exist_in_this_repository():
    from peer_influence.freeze import FROZEN_PATHS

    root = Path(__file__).resolve().parents[3]
    assert [p for p in FROZEN_PATHS if not (root / p).exists()] == []
