# Launch scripts

Host-side setup, launch, evaluation and recovery for the released single-node
team and standalone baseline. Run commands from the repository root; full
prerequisites and configuration examples are in the
[reproduction guide](../REPRODUCE.md).

| File(s) | Responsibility |
|---|---|
| `pbench_gen_env.sh` | Writes private per-task profiles, preserving passwords and allocating ports/subnets |
| `launch_single_node.sh` | Checks all team profiles, images, host guards and the live model route before launch |
| `lib_launch.sh` | Profile/run naming, environment precedence and inference/evaluation sequencing |
| `pbench_run.sh` | Seeds one task, starts workers, sends reminders/idle nudges, exports and stops workers |
| `pbench_chain.sh` | Separates run/eval phases, salvages interrupted output and verifies trajectory copies before cleanup |
| `pbench_build_pertask_images.sh`, `lib_pbench_images.sh` | Builds v6-based worker images and enforces the reference cleanroom tag |
| `lib_prompts.sh` | Renders launch messages with an explicit substitution allowlist |
| `launch_single_agent.py` | Runs the standalone baseline from JSON config with provenance, snapshots and export |
| `acp_driver.py`, `single_agent_stdio.py` | Drives immediate ACP continuation through the worker's Unix socket |
| `strip_metadata_proxy.py` | Relays model HTTP traffic, removes unsupported fields, injects auth and retries selected failures |
| `pbench-docker-sudo.sh` | Evaluator Docker shim for memory/network settings and appended pytest options |
| `pbench_reboot_guard.sh` | Stops labeled orphan stacks after reboot while retaining their data |

Team inference runs one task at a time; completed-task evaluation can overlap
the next task. `RUN_ID` names trajectories, while profile suffixes, ports,
subnets and data roots isolate concurrent stacks. The normal team deadline
exports committed Git HEAD before graceful worker shutdown; final state archives
are recovery artifacts and can contain later work.

The standalone driver immediately resumes completed turns while more than
60 seconds remain, and tries to reload native sessions after transport failures.
Its periodic and final snapshots stage and commit non-ignored workspace changes
before source export. The team exports Gitea HEAD before shutdown, waits for
queued events between turns and uses a separate idle watchdog. Both paths use
[`agent/archive.py`](../agent/archive.py) to verify shutdown archives.
Canonical comparison scores come from
[`tools/pbench_score_report.py`](../tools/pbench_score_report.py); the chain's
`SCORE` line counts raw attempt rows.

`pbench_build_pertask_images.sh` uses `TAG` (default `pbench`) for output images
and the fixed `omcgitea-agent-base:pbench` artifact. Profile generation instead
uses `AGENT_IMAGE_TAG`; exporting that variable alone does not retag builds.
For private cohort/base-image tags and uncached builds, use the explicit Docker
commands in the reproduction guide.
