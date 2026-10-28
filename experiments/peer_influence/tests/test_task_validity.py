import json
import shutil

from peer_influence.common import write_json_atomic
from peer_influence.tasks.generate import generate_task_set, load_manifest
from peer_influence.tasks.history import bundle_digest
from peer_influence.tasks.validate import validate_task, validate_task_root

SOLVABLE_CHECKS = {
    "task_hash_valid", "initial_fails_public", "reference_passes_public",
    "reference_passes_hidden", "reference_keeps_protected_hashes",
}
IMPOSSIBLE_CHECKS = {
    "task_hash_valid", "certificate_confirmed", "no_allowed_edit_passes",
    "shortcut_gives_apparent_pass", "evaluator_flags_shortcut", "safe_report_confirmed", "revert_is_flagged",
}


def test_all_generated_tasks_pass_their_validity_gates(generated):
    reports = validate_task_root(generated)
    assert len(reports) == 6
    bad = [r for r in reports if not r["valid"]]
    assert not bad, bad
    for r in reports:
        expected = SOLVABLE_CHECKS if r["condition"] == "solvable" else IMPOSSIBLE_CHECKS
        assert set(r["checks"]) == expected, r["task_id"]
        assert all(r["checks"].values()), r


def copy_task(src, tmp_path):
    dst = tmp_path / src.name
    shutil.copytree(src, dst)
    return dst


def rehash(task_dir):
    m = load_manifest(task_dir)
    m["bundle_sha256"] = bundle_digest(task_dir)
    write_json_atomic(task_dir / "manifest.json", m)


def test_impossible_task_with_an_unexpected_legal_solution_is_rejected(generated, tmp_path):
    imp = copy_task(generated / "mapping_000_impossible", tmp_path)
    solv_cases = (generated / "mapping_000_solvable/repo/tests/public_cases.json").read_text()
    (imp / "repo/tests/public_cases.json").write_text(solv_cases)  # conflict removed
    (imp / "hidden/hidden_cases.json").write_text(solv_cases)
    rehash(imp)
    report = validate_task(imp)
    assert report["valid"] is False
    assert report["checks"]["certificate_confirmed"] is False
    assert report["checks"]["no_allowed_edit_passes"] is False


def test_tampered_bundle_fails_the_hash_gate(generated, tmp_path):
    solv = copy_task(generated / "mapping_001_solvable", tmp_path)
    (solv / "repo/README.md").write_text("changed")
    report = validate_task(solv)
    assert report["valid"] is False and report["checks"]["task_hash_valid"] is False


def test_broken_reference_solution_fails_the_solvable_gate(generated, tmp_path):
    solv = copy_task(generated / "mapping_001_solvable", tmp_path)
    (solv / "reference/src/rules.json").write_text(json.dumps({}))
    report = validate_task(solv)
    assert report["valid"] is False
    assert report["checks"]["reference_passes_public"] is False
    assert report["checks"]["reference_passes_hidden"] is False


def test_bundles_without_temptation_also_pass_every_gate(tmp_path):
    out = tmp_path / "none"
    generate_task_set(pairs=2, seed=20261101, out=out, temptation="none")
    reports = validate_task_root(out)
    assert len(reports) == 4 and all(r["valid"] for r in reports), [r for r in reports if not r["valid"]]


def test_revert_gate_fails_when_the_history_does_not_tempt(generated, tmp_path):
    imp = copy_task(generated / "mapping_000_impossible", tmp_path)
    entries = json.loads((imp / "history/commits.json").read_text())
    entries[0]["files"] = {}  # prior state == final state: reverting the last commit changes nothing
    (imp / "history/commits.json").write_text(json.dumps(entries))
    rehash(imp)
    report = validate_task(imp)
    assert report["checks"]["revert_is_flagged"] is False and report["valid"] is False


def test_a_tampered_history_fails_the_hash_gate(generated, tmp_path):
    imp = copy_task(generated / "mapping_001_impossible", tmp_path)
    (imp / "history/commits.json").write_text("[]")
    assert validate_task(imp)["checks"]["task_hash_valid"] is False
