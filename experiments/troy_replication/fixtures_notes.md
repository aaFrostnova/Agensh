# Real task fixture reconstruction

This implementation reconstructs the authors' released `git_recipe_v2` with the official SWE-bench instance image, an immutable current Docker Hub `linux/amd64` manifest, and the byte-verified official ImpossibleBench conflicting patch. It does not reproduce an exported original Modal filesystem. The authors published neither the historical image digest nor a downloadable snapshot blob. Those exact-replication blockers remain in every fixture's `original_asset_blockers`.

All image extraction, repository copies, logs and test runs belong in an allocated scratch workspace. `prepare()` requires a Unity Slurm allocation. It preserves failures and refuses to overwrite a partial tree automatically. No model creates or repairs these fixtures.

## Verified execution scope

Two official images were downloaded and exercised. The remaining five have current registry digests frozen in `images/manifest.json`, but were not downloaded or executed.

| Task | Original source + original tests | Original source + conflicting tests | Official source fix + conflicting tests | Official source fix + original tests | Admission |
|---|---|---|---|---|---|
| astropy-12907 | 2 failed, 13 passed | 10 failed, 5 passed | 10 failed, 5 passed | 15 passed | A/B/C validated |
| django-10554 | Target database error followed by unittest result-formatting crash | Same reporter crash | 27 tests, 1 failure, 2 skips | 27 tests, OK, 2 skips | blocked |

The Django failure is preserved as `unittest_subtest_result_formatting_crashed`. Python 3.6 unittest evaluates a QuerySet when formatting subTest parameters; the original source's ORDER BY bug then raises again while the runner prints errors. We do not modify the source defect, test framework or protected assertions to manufacture a valid initial verdict. The successful official source fix establishes the cause, but the initial incomplete report remains outside admitted episodes.

Astropy's Apptainer and bwrap executions produce identical statuses for all 15 named tests and identical exit codes. The bwrap worker uses host `/usr` with the image's `/opt/miniconda3` mounted read-only. This is an explicitly recorded runtime migration, not full OS identity.

## Preparation and use

Run from this repository inside a Slurm allocation, setting `ROOT` to an existing allocated scratch directory:

```sh
python experiments/troy_replication/scripts/prepare_fixtures.py \
  --root "$ROOT" --tasks astropy-12907 django-10554
```

The same command supports all seven IDs from `assets.TASK_IDS`. To prepare the five not yet executed:

```sh
python experiments/troy_replication/scripts/prepare_fixtures.py \
  --root "$ROOT" --tasks matplotlib-20859 xarray-2905 pytest-10051 scikit-learn-10297 sphinx-10323
```

Image pulls use a resolved immutable manifest. Do not change a manifest, patch or image in an existing frozen cohort. If an attempt is blocked, retain its directory/logs and use a fresh explicitly labelled reconstruction root for any revised environment. Passing A/B/C alone does not establish D/F monitor isolation or live message exposure; the host engineering-validation layer must complete those checks before an episode is admitted.

`FixtureStore(root).prepare(load_task(id))` returns `engineering_abc.json`, including `snapshot_path`, `runtime_binds`, `runtime_env`, `test_command`, each test's original log and SHA-256, patch hashes and upstream recipe. `copy_snapshot(id, destination)` requires validated status and checks protected-file hashes before copying. Only the `observed_failure` snapshot is eligible as an episode's starting state. The official gold source patch stays in host-only fixture validation inputs.

`ApptainerRunner(image_root).run(repo, command, log, backend="apptainer" | "bwrap")` provides the isolated local test entry point. The worker must bind its fresh workspace to `/testbed` as well as its normal `/work/repo`; image editable-install links expect `/testbed`. The only environment bind exposed to the worker is `/opt/miniconda3`. Do not mount the parent fixture store, original patches, treatment metadata, monitors or output logs.
