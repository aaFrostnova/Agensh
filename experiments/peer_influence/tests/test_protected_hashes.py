import os

from peer_influence.runtime.protected_hashes import (
    baseline_record,
    diff_snapshots,
    match_protected,
    snapshot,
)

PATTERNS = ["tests/**", "evaluator/**", "task_spec.json", "check"]


def make(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/a.json").write_text("[]")
    (tmp_path / "tests/deep").mkdir()
    (tmp_path / "tests/deep/b.json").write_text("{}")
    (tmp_path / "evaluator").mkdir()
    (tmp_path / "evaluator/check.py").write_text("print(1)")
    (tmp_path / "task_spec.json").write_text("{}")
    (tmp_path / "check").write_text("#!/bin/sh")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/rules.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("x")
    return tmp_path


def test_match_protected_globs_and_exact_names():
    assert match_protected("tests/a.json", PATTERNS)
    assert match_protected("tests/deep/b.json", PATTERNS)
    assert match_protected("task_spec.json", PATTERNS)
    assert match_protected("check", PATTERNS)
    assert not match_protected("src/rules.json", PATTERNS)
    assert not match_protected("tests", PATTERNS)  # the bare directory is not a file
    assert not match_protected("mytests/a.json", PATTERNS)
    assert not match_protected("checks", PATTERNS)


def test_snapshot_covers_only_protected_files_and_skips_git(tmp_path):
    root = make(tmp_path)
    (root / ".git").mkdir()
    (root / ".git/tests").write_text("x")
    snap = snapshot(root, PATTERNS)
    assert sorted(snap) == [
        "check", "evaluator/check.py", "task_spec.json", "tests/a.json", "tests/deep/b.json",
    ]
    assert snapshot(root, PATTERNS) == snap


def test_diff_reports_modified_deleted_and_added(tmp_path):
    root = make(tmp_path)
    before = snapshot(root, PATTERNS)
    (root / "tests/a.json").write_text("[1]")
    (root / "evaluator/check.py").unlink()
    (root / "tests/new.json").write_text("{}")
    (root / "src/rules.json").write_text('{"k": "v"}')  # not protected
    d = diff_snapshots(before, snapshot(root, PATTERNS))
    assert d == {
        "modified": ["tests/a.json"],
        "deleted": ["evaluator/check.py"],
        "added": ["tests/new.json"],
    }


def test_replacing_a_protected_directory_with_a_symlink_is_a_deletion(tmp_path):
    root = make(tmp_path)
    before = snapshot(root, PATTERNS)
    other = tmp_path.parent / (tmp_path.name + "_elsewhere")
    other.mkdir()
    (other / "a.json").write_text("[]")
    import shutil

    shutil.rmtree(root / "tests")
    os.symlink(other, root / "tests")
    d = diff_snapshots(before, snapshot(root, PATTERNS))
    assert "tests/a.json" in d["deleted"] and "tests/deep/b.json" in d["deleted"]


def test_baseline_record_round_trips_patterns_and_hashes(tmp_path):
    root = make(tmp_path)
    rec = baseline_record(root, PATTERNS)
    assert rec["patterns"] == PATTERNS
    assert rec["hashes"] == snapshot(root, PATTERNS)
