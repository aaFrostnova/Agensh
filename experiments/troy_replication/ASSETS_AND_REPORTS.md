# Official inputs and evidence reporting

This module separates input availability, engineering validation, formal pilot
outcomes, and exact historical reproduction. Passing tests or obtaining all
input files is not evidence that the pilot ran.

## Pinned sources

- Paper: [The Troy Moment, arXiv v3](https://arxiv.org/html/2609.15494v3).
- Author artifact: [para2046/troy-moment](https://github.com/para2046/troy-moment/tree/3c6f15b65e3b3b1e47a611acdc23e2c25c63f63a),
  commit `3c6f15b65e3b3b1e47a611acdc23e2c25c63f63a`.
- Author-pinned benchmark implementation: [safety-research/impossiblebench](https://github.com/safety-research/impossiblebench/tree/061dc3dce6a96ab6cf02a855157263033dcfa3ba),
  commit `061dc3dce6a96ab6cf02a855157263033dcfa3ba`.
- Official dataset: [fjzzq2002/impossible_swebench](https://huggingface.co/datasets/fjzzq2002/impossible_swebench/tree/9c2d34f364b7229e8c0ff807c646100bdc18bbb5),
  revision `9c2d34f364b7229e8c0ff807c646100bdc18bbb5`, `conflicting` split.

`frozen_inputs/provenance.json` records original byte hashes and the lossless
seven-row JSON extraction from the official parquet. Loading requires only the
Python standard library. No network call or third-party dataset package occurs
in `load_task` or `freeze_assets`.

`load_task(task_id, upstream_root=None)` accepts a paper short ID, full SWE-bench
instance ID, or `impossible::<instance ID>`. It rejects the eighth Pylint task
and all LHAW examples included in the author artifact. All seven conflicting
test patches and base commits match the official dataset and author recipes.

Returned `source_patch`, `original_test_patch`, `conflicting_test_patch`,
checkpoint data, and `provenance` belong on the host. They are calibration
references. Only `task_brief`, the appropriate frozen stimulus, and the victim
policy belong in the victim prompt. The brief permits the actual source files
changed by the official gold patch; this narrow scope is an explicit migration
choice, not a claim that it reproduces every author's tool permission exactly.

## Static prompt fidelity

`render_static_stimuli` reproduces the author's unpadded Strict+Open
`BoardMessage.render` strings. C0/C1/C2/C3 map to author B1/B2/B3/B5. The neutral
technical contradiction block is present in every condition including C0;
only C1/C2's enforcement sentence differs. No filler, attack optimization, or
model generation is used for static stimuli. The implementation was compared
against the released author renderer for all 28 task-condition combinations.

The victim and live adversary system prompts instead implement the user's v0.6
plan. `prompt_manifest` records that adaptation explicitly alongside every
prompt's SHA-256. A prompt hash identifies text; it does not by itself prove the
text entered an actual model request. The relay's context receipt supplies that
separate evidence.

## Freeze inputs

Run from the Agensh repository, using an already allocated scratch workspace:

```sh
PYTHONPATH=experiments python -m troy_replication.assets \
  "$TROY_ROOT/data/official-inputs" \
  --upstream-root "$TROY_ROOT/upstream/troy-moment"
```

This creates per-task JSON, official patches, stimulus files, prompt files and
a SHA-256 manifest. Existing different frozen files are never overwritten.
Repeating with the same inputs is idempotent. The manifest initially reports
fixture validation as `not_run`.

Original author OCI digests and exported container snapshots are unavailable;
several environment-constructor inputs are missing from the release. These are
reported as blockers for exact historical reconstruction. Resolving today's
registry digest permits a separately labelled migration, followed by fresh
A-F validation; it does not recover the historical digest.

## Report actual evidence

```sh
PYTHONPATH=experiments python -m troy_replication.reports \
  --root "$TROY_ROOT" \
  --matrix "$TROY_ROOT/results/matrix_kimi_minimum.json" \
  --out "$TROY_ROOT/results/report"
```

The report consumes:

- The predeclared `planned_episodes` matrix; only its `pilot-` IDs enter formal
  group denominators. `eng-` and unexpected IDs remain separate.
- `fixtures/*/engineering_abc.json` and `results/engineering_runtime.json` for
  explicit A-F calibration and transport evidence. A normal zero-write episode
  is never treated as proof of write-detection calibration.
- Terminal episode records and their lifecycle, isolation, monitor, visible
  reply and usage files. Active directories lacking a terminal record count as
  `incompleteN`, separately from `not_run`.
- Optional `results/retrospective_audit.json`, with
  `episodes[episode_id] = {valid: false, reasons: [...], evidence_paths: [...]}`.
  This preserves the original result while excluding a later-invalidated run.

Generated files are `engineering_validation.json`, `pilot_summary.csv`,
`engineering_episodes.json`, `representative_trajectories.md`, `limitations.md`
and `report.json`. The last file also drives presentation generation.

`formal_groups` / `rows` are grouped by model, task, condition and mode. They
include `plannedN`, `validN`, `invalidN`, `not_run`, and `incompleteN`.
For each observable endpoint, `*_N` is the number of true observations,
`*_knownN` is the number of known labels, and `*_rate` is available only when
all valid episodes have a known label. With no known observations, the count
and rate are null, not zero. CSV represents null as an empty field.

Semantic labels require a public excerpt and event IDs. Reports do not infer
private intent, translate a missing completion label into a truthful claim, or
fill absent annotations from keywords. Representative excerpts preserve turn
and event references and are selected by condition/ID rather than attack
success. No example is invented to meet the desired trajectory count.

Token columns retain each agent separately. Exact totals are null when any
attempt's relevant counter is unknown; `*_known_lower_bound` remains available.
Costs of invalid terminal episodes are included in resource accounting, while
behavioral rates use only valid episodes. Reasoning and cached input are
subsets of output and input, respectively, and are never added again.

## Offline checks

```sh
PYTHONPATH=experiments python -m unittest discover \
  -s experiments/troy_replication/tests -p test_assets.py
PYTHONPATH=experiments python -m unittest discover \
  -s experiments/troy_replication/tests -p test_reports.py
```

These lightweight tests check provenance, tamper detection, exact task
selection, matched stimuli, missing evidence, retrospective invalidation,
unknown tokens and denominator discipline. They execute no benchmark task and
make no model calls. Fixture execution and inference must use Slurm resources.
