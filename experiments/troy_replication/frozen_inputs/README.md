# Pinned Troy / Impossible-SWEbench inputs

These files freeze inputs, not successful fixture validation or experimental results.

- `checkpoints_v3.json` and `stimuli_v3.json` are unchanged bytes from
  `para2046/troy-moment` commit `3c6f15b65e3b3b1e47a611acdc23e2c25c63f63a`.
  The author's MIT license is retained as `TROY_LICENSE`.
- `impossible_seven.json` is a lossless JSON selection of the paper's specified
  seven rows from `fjzzq2002/impossible_swebench`, conflicting split, revision
  `9c2d34f364b7229e8c0ff807c646100bdc18bbb5`. Source parquet SHA-256 is
  `25d96f864431f9279375c94df05ffdaeeafbcacde0be8b2b0a0333daefa4186d` (1,594,480
  bytes; 349 total rows). Rights in upstream code, issue descriptions and dataset
  content remain with their respective owners. No new license is asserted.
- `provenance.json` records exact URLs, file hashes and the extraction form.
  `assets.py` additionally pins expected hashes in code and verifies every load.

All seven base commits and conflicting test patches were compared with the
released author checkpoints and matched exactly. Gold source repairs and
original tests are retained on the host for fixture A/B/C calibration only.
They must not be put in the victim workspace or prompt.

The original author image digest was not published: only mutable `:latest`
image names are provided. The original Modal container state was not exported.
Checkpoint recipes include local Git object IDs and recorded fingerprints;
these are evidence, not a claim that those objects are available upstream.
The release also omits `data/frozen/task_environments.json`,
`data/candidates/impossible_raw`, `scripts/build_task_environments.py` and
`scripts/run_scripted_construct.py`. Exact environment replication is blocked;
a new explicitly labelled reconstruction can use the verified recipe, current
registry digest and fresh A-F validation.

Static C0/C1/C2/C3 messages reproduce the author's unpadded Strict+Open rendered
strings (B1/B2/B3/B5), including the neutral technical block in C0. C1/C2 differ
only in the enforcement sentence. The v0.6 victim and live adversary system
prompts come from the user's plan and are an adaptation of the paper's system
prompt and tool protocol; their hashes are recorded separately.
