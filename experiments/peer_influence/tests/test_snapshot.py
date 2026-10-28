import os
import threading

from peer_influence.common import git_safe, safe_copytree, tree_sha256


def within(seconds, fn):
    box = {}
    t = threading.Thread(target=lambda: box.update(v=fn()), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "blocked"
    return box["v"]


def build(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")
    os.symlink("/etc/hostname", src / "link_to_file")
    os.symlink("/etc", src / "link_to_dir")
    os.mkfifo(src / "pipe")
    secret = src / "locked.txt"
    secret.write_text("x")
    secret.chmod(0)
    return src


def test_safe_copytree_copies_what_it_can_links_stay_links_and_odd_entries_are_reported(tmp_path):
    src, dst = build(tmp_path), tmp_path / "dst"
    try:
        report = within(10, lambda: safe_copytree(src, dst))
        assert (dst / "a.txt").read_text() == "a" and (dst / "sub" / "b.txt").read_text() == "b"
        assert os.path.islink(dst / "link_to_dir") and os.readlink(dst / "link_to_dir") == "/etc"
        assert not (dst / "pipe").exists() and not (dst / "locked.txt").exists()
        assert report["special"] == ["pipe"] and report["unreadable"] == ["locked.txt"] and report["oversize"] == []
    finally:
        (src / "locked.txt").chmod(0o600)


def test_files_over_the_size_cap_are_reported_not_copied(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    (src / "big.bin").write_bytes(b"x" * 100)
    (src / "ok.txt").write_text("ok")
    report = safe_copytree(src, tmp_path / "d", max_file_bytes=50)
    assert report["oversize"] == ["big.bin"] and (tmp_path / "d" / "ok.txt").exists()


def test_tree_digests_of_a_workspace_and_its_safe_copy_agree_when_odd_entries_are_skipped(tmp_path):
    src, dst = build(tmp_path), tmp_path / "dst"
    try:
        safe_copytree(src, dst)
        assert within(10, lambda: tree_sha256(src, skip_unreadable=True)) == tree_sha256(dst, skip_unreadable=True)
        (dst / "a.txt").write_text("changed")
        assert tree_sha256(src, skip_unreadable=True) != tree_sha256(dst, skip_unreadable=True)
    finally:
        (src / "locked.txt").chmod(0o600)


def test_git_safe_never_runs_a_program_named_in_the_repository_config(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    marker = tmp_path / "PWNED"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    git_safe(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x")
    with open(repo / ".git" / "config", "a") as f:
        f.write(f"[core]\n\tfsmonitor = {hook}\n")
    git_safe(repo, "status", "--porcelain")
    assert not marker.exists()


def test_copies_keep_permission_bits_because_git_sees_a_mode_change_as_a_modification(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "run.sh").write_text("#!/bin/sh\n")
    (src / "run.sh").chmod(0o755)
    (src / "ro.txt").write_text("x")
    (src / "ro.txt").chmod(0o444)
    safe_copytree(src, tmp_path / "dst")
    assert (os.stat(tmp_path / "dst" / "run.sh").st_mode & 0o777) == 0o755
    assert (os.stat(tmp_path / "dst" / "ro.txt").st_mode & 0o777) == 0o444


def test_scoring_the_safe_copy_gives_exactly_the_score_of_the_live_workspace(tmp_path, solvable_dir):
    """The scorer runs on the preserved snapshot, so the snapshot must be faithful for everything it checks."""
    import json
    import shutil

    from peer_influence.evaluation.score_episode import score_workspace
    from peer_influence.tests.helpers import seed_workspace

    ws, base = seed_workspace(solvable_dir, tmp_path / "ws")
    shutil.copy(solvable_dir / "reference/src/rules.json", ws / "src/rules.json")
    (ws / "submission.json").write_text(json.dumps({"status": "completed", "summary": "fixed"}))
    (ws / "scratch.txt").write_text("notes")
    os.symlink("src/rules.json", ws / "rules_link")
    for name in ("AGENTS.md", "CLAUDE.md", ".mcp.json"):
        (ws / name).write_text("runtime")
    assert (ws / "check").stat().st_mode & 0o111  # the wrapper is executable in the live workspace
    copy_report = safe_copytree(ws, tmp_path / "copy")
    assert copy_report == {"special": [], "unreadable": [], "oversize": []}
    live = score_workspace(workspace=ws, task_dir=solvable_dir, base_commit=base, episode_id="e")
    copy = score_workspace(workspace=tmp_path / "copy", task_dir=solvable_dir, base_commit=base, episode_id="e")
    assert copy == live
    assert copy["policy_violation"] is False and copy["modified_protected_paths"] == []
