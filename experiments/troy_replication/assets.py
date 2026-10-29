"""Pinned official seven-task inputs; no network and no fabricated provenance.

Author checkpoints contain a reconstruction recipe, not a published container
snapshot. Original OCI digests and several author environment-constructor inputs
were not released. A fresh reconstruction must therefore be reported as such.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .prompts import prompt_manifest, render_static_stimuli, sha256_text

AUTHOR_COMMIT = "3c6f15b65e3b3b1e47a611acdc23e2c25c63f63a"
IMPOSSIBLEBENCH_COMMIT = "061dc3dce6a96ab6cf02a855157263033dcfa3ba"
DATASET_REVISION = "9c2d34f364b7229e8c0ff807c646100bdc18bbb5"
DATASET_SHA256 = "25d96f864431f9279375c94df05ffdaeeafbcacde0be8b2b0a0333daefa4186d"
FROZEN_INPUTS = Path(__file__).resolve().parent / "frozen_inputs"
TASK_IDS = (
    "astropy-12907", "django-10554", "matplotlib-20859", "xarray-2905",
    "pytest-10051", "scikit-learn-10297", "sphinx-10323",
)
INSTANCE_IDS = (
    "astropy__astropy-12907", "django__django-10554",
    "matplotlib__matplotlib-20859", "pydata__xarray-2905",
    "pytest-dev__pytest-10051", "scikit-learn__scikit-learn-10297",
    "sphinx-doc__sphinx-10323",
)
INPUT_SHA256 = {
    "checkpoints_v3.json": "ea14626016ce12f0e4e92e8120bd29cbac472439e40461e41a3faca5b5d2722d",
    "stimuli_v3.json": "50d00c924782d2857e7aa619791fa7f59756d7d6b5b2c82e61c5282efcb31fb6",
    "impossible_seven.json": "07d047e11d6e109119e9d5c1e783660a2463718dbcbb2548d78607b395b32afe",
    "TROY_LICENSE": "f2243866babc4a47cb75c538cf96fa664ad20d107821a574b7ca684005cfc0a6",
}
MISSING_AUTHOR_PATHS = (
    "data/frozen/task_environments.json", "data/candidates/impossible_raw",
    "scripts/build_task_environments.py", "scripts/run_scripted_construct.py",
)


class AssetError(ValueError):
    """A pinned input is missing, altered, ambiguous, or not in the main seven."""


def _read_pinned(path: Path, digest: str) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AssetError(f"Missing pinned input: {path}") from exc
    observed = hashlib.sha256(raw).hexdigest()
    if observed != digest:
        raise AssetError(f"SHA-256 mismatch for {path}: {observed} != {digest}")
    return raw


def _author_root(upstream_root: str | Path | None) -> Path | None:
    if upstream_root is None:
        return None
    root = Path(upstream_root).resolve()
    if (root / "troy-moment").is_dir():
        root = root / "troy-moment"
    if not (root / "data/checkpoints/checkpoints_v3.json").is_file():
        raise AssetError(f"Not the released Troy checkout: {root}")
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0 or result.stdout.strip() != AUTHOR_COMMIT:
        raise AssetError(f"Troy checkout must be exactly {AUTHOR_COMMIT}")
    return root


def _inputs(upstream_root: str | Path | None) -> tuple[dict, dict, dict, Path | None]:
    # Always verify vendored inputs, including lossless seven-row dataset extract.
    raw = {name: _read_pinned(FROZEN_INPUTS / name, digest)
           for name, digest in INPUT_SHA256.items()}
    root = _author_root(upstream_root)
    if root is not None:
        for name in ("checkpoints_v3.json", "stimuli_v3.json"):
            raw[name] = _read_pinned(root / "data/checkpoints" / name,
                                     INPUT_SHA256[name])
    checkpoints = json.loads(raw["checkpoints_v3.json"])
    stimuli = json.loads(raw["stimuli_v3.json"])
    dataset = json.loads(raw["impossible_seven.json"])
    if (dataset["revision"] != DATASET_REVISION or dataset["source_sha256"] != DATASET_SHA256
            or dataset["split"] != "conflicting" or dataset["selection"] != list(INSTANCE_IDS)
            or [r["instance_id"] for r in dataset["rows"]] != list(INSTANCE_IDS)):
        raise AssetError("Dataset identity or seven-task selection is not the frozen selection")
    return checkpoints, stimuli, dataset, root


def patch_paths(patch: str) -> list[str]:
    """Extract safe relative destination paths; fail closed on traversal."""
    paths = re.findall(r"^\+\+\+ b/(.+)$", patch, re.MULTILINE)
    if not paths:
        raise AssetError("Patch has no destination file paths")
    for name in paths:
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or "\x00" in name:
            raise AssetError(f"Unsafe patch path: {name!r}")
    return list(dict.fromkeys(paths))


def _unique(items: list[dict], key: str, value: str) -> dict:
    found = [item for item in items if item.get(key) == value]
    if len(found) != 1:
        raise AssetError(f"Expected exactly one {key}={value!r}; found {len(found)}")
    return found[0]


def load_task(task_id: str, upstream_root: str | Path | None = None) -> dict[str, Any]:
    """Return verified source, conflicting tests, official stimuli and provenance.

    Accept a paper short ID, full SWE-bench ID, or ``impossible::<full ID>``.
    No model-visible prompt contains gold repair patches, scorer configuration,
    treatment labels, author metadata, or any private reference material.
    """
    normalized = task_id[len("impossible::"):] if task_id.startswith("impossible::") else task_id
    if normalized in TASK_IDS:
        index = TASK_IDS.index(normalized)
    elif normalized in INSTANCE_IDS:
        index = INSTANCE_IDS.index(normalized)
    else:
        raise AssetError(f"Not one of the paper's seven task IDs: {task_id!r}")
    short, instance = TASK_IDS[index], INSTANCE_IDS[index]
    checkpoints, stimuli, dataset, root = _inputs(upstream_root)
    cp = _unique(checkpoints["checkpoints"], "task_id", "impossible::" + instance)
    spec = _unique(stimuli["stimuli"], "stimulus_id", cp["stimulus_id"])
    row = _unique(dataset["rows"], "instance_id", instance)
    snapshot = cp["snapshot"]
    meta = snapshot["metadata"]
    bundle = meta["state_bundle"]
    if cp["variant"] != "conflicting" or row["impossible_type"] != "conflicting":
        raise AssetError("Only the official conflicting variant is eligible")
    if row["base_commit"] != meta["base_commit"] or row["test_patch"] != bundle["test_patch"]:
        raise AssetError("Author checkpoint does not byte-match the official dataset")
    if bundle.get("diff_b64"):
        raise AssetError("Unexpected extra checkpoint diff: needs explicit review")
    env = checkpoints["environment_identity"][instance]
    allowed = patch_paths(row["patch"])
    protected = list(dict.fromkeys(patch_paths(row["test_patch"]) + patch_paths(row["original_test_patch"])))
    if set(allowed) & set(protected):
        raise AssetError("Gold source repair overlaps protected tests")
    blockers = [
        {"code": "original_image_digest_unpublished", "scope": "exact_environment_replication",
         "status": "blocked", "detail": "Released checkpoint names a mutable :latest image; original OCI digest is absent."},
        {"code": "original_snapshot_blob_unpublished", "scope": "byte_identical_original_snapshot",
         "status": "blocked", "detail": "git_recipe_v2 plus a recorded fingerprint is available, but no exported original Modal/container snapshot."},
    ]
    missing = list(MISSING_AUTHOR_PATHS)
    if root is not None:
        missing = [path for path in MISSING_AUTHOR_PATHS if not (root / path).exists()]
    if missing:
        blockers.append({"code": "author_environment_inputs_unpublished", "scope": "original_author_pipeline",
                         "status": "blocked", "paths": missing})
    task = {
        "schema_version": "0.6.0", "task_id": short, "instance_id": instance,
        "repo": row["repo"], "version": row["version"], "variant": "conflicting",
        "base_commit": row["base_commit"], "environment_setup_commit": row["environment_setup_commit"],
        "problem_statement": row["problem_statement"], "source_patch": row["patch"],
        "original_test_patch": row["original_test_patch"], "conflicting_test_patch": row["test_patch"],
        "allowed_paths": allowed, "protected_paths": protected,
        "protected_path_rules": ["**/tests/**", "tests/**", "testing/**", "evaluation/**", "**/AGENTS.md", "AGENTS.md", "policy.yaml"],
        "test_command": env["test_command"], "environment": env,
        "image_ref": env["image"], "original_image_digest": None,
        "fail_to_pass": row["FAIL_TO_PASS"], "pass_to_pass": row["PASS_TO_PASS"],
        "boundary_target": cp["boundary_target"], "checkpoint_id": cp["checkpoint_id"],
        "checkpoint_recipe": snapshot, "author_checkpoint": cp,
        "official_stimulus_spec": spec, "stimuli": render_static_stimuli(spec),
        "original_asset_blockers": blockers, "blockers": blockers,
        "reconstruction_status": "recipe_available_requires_fresh_fixture_validation",
        "provenance": {
            "paper": "https://arxiv.org/html/2609.15494v3", "author_commit": AUTHOR_COMMIT,
            "impossiblebench_commit": IMPOSSIBLEBENCH_COMMIT, "dataset": dataset["dataset"],
            "dataset_revision": DATASET_REVISION, "dataset_parquet_sha256": DATASET_SHA256,
            "input_sha256": dict(INPUT_SHA256), "official_patch_matches_author": True,
            "source_patch_sha256": sha256_text(row["patch"]),
            "conflicting_test_patch_sha256": sha256_text(row["test_patch"]),
            "original_test_patch_sha256": sha256_text(row["original_test_patch"]),
            "upstream_checkout": str(root) if root else None,
            "static_stimulus_fidelity": "author_strict_open_unpadded_bytes",
            "original_snapshot_fingerprint_record_only": snapshot["fingerprint"],
        },
    }
    task["prompts"] = prompt_manifest(task)
    return task


def _json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise AssetError(f"Refusing to overwrite different frozen content: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def freeze_assets(output_dir: str | Path, upstream_root: str | Path | None = None) -> dict[str, Any]:
    """Materialize all seven official tasks and a SHA-256 manifest, offline.

    Repeating with identical inputs is idempotent. This freezes inputs only; it
    does NOT assert fixture A-F passed or that any experiment has been run.
    """
    root = Path(output_dir).resolve()
    tasks = [load_task(task_id, upstream_root) for task_id in TASK_IDS]
    records = []
    files = {}
    for task in tasks:
        task_dir = root / task["task_id"]
        for key, name in (("source_patch", "source.patch"), ("original_test_patch", "original_tests.patch"),
                          ("conflicting_test_patch", "conflicting_tests.patch")):
            path = task_dir / name
            _write_once(path, task[key].encode("utf-8"))
            task[key + "_path"] = str(path)
        for condition, message in task["stimuli"].items():
            _write_once(task_dir / "stimuli" / (condition + ".txt"), message.encode("utf-8"))
        for name, prompt in task["prompts"]["texts"].items():
            _write_once(task_dir / "prompts" / (name + ".txt"), prompt.encode("utf-8"))
        _write_once(task_dir / "task.json", _json_bytes(task))
        records.append({"task_id": task["task_id"], "instance_id": task["instance_id"],
                        "task_path": str(task_dir / "task.json"), "directory": str(task_dir),
                        "original_asset_blockers": task["original_asset_blockers"]})
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"schema_version": "0.6.0", "kind": "official_task_input_freeze",
                "author_commit": AUTHOR_COMMIT, "impossiblebench_commit": IMPOSSIBLEBENCH_COMMIT,
                "dataset_revision": DATASET_REVISION, "dataset_parquet_sha256": DATASET_SHA256,
                "task_ids": list(TASK_IDS), "tasks": records, "files_sha256": files,
                "fixture_validation": "not_run", "exact_original_environment_replication": "blocked",
                "manifest_path": str(root / "manifest.json")}
    _write_once(root / "manifest.json", _json_bytes(manifest))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--upstream-root", type=Path)
    args = parser.parse_args()
    manifest = freeze_assets(args.output_dir, args.upstream_root)
    print(json.dumps({"manifest": manifest["manifest_path"], "tasks": len(manifest["tasks"]),
                      "exact_original_environment_replication": "blocked"}, indent=2))


if __name__ == "__main__":
    main()
