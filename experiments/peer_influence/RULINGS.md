# Rulings

Where the plan (`agensh_peer_influence_pilot_execution_plan_v0.3.md`) was wrong, silent or unrunnable here, the
smallest change that keeps its intent was made. Each entry: what, why, what it costs if wrong.

1. **No Docker/sudo on Unity -> rootless backend.** MySQL, Gitea and Mattermost run from the upstream images under
   Apptainer, the Python/Node services natively, the worker's unmodified `agent/entrypoint.sh` in `bwrap --unshare-user
   --uid 10000 --unshare-pid`. Cost: the Compose path is generated and unit-tested but not run live.
2. **Board tool removed, `BOARD_SERVICE_URL` kept (plan 7.1 vs 7.2).** The notice shim polls that URL for direct messages,
   so `BOARD_SERVICE_URL=""` would stop DM delivery. `EXPERIMENT_DISABLE_BOARD=1` deletes only the board MCP server and
   nobody is a board member. Cost: one environment variable.
3. **Stock `acp-bridge.py` is never run on the host.** Its shutdown sweep SIGKILLs every process of the child uid; rootless
   that is the user. The sandbox gets a patched copy (anchor-checked, exempts the bridge, its parent and init) and SIGTERM
   goes to the entrypoint matched by exact argv. Cost: a one-line textual patch of upstream behaviour in the worker only.
4. **Worker base URL ends in `/v1`.** Copilot's OpenAI provider posts `<base>/responses`; vLLM and Bedrock serve
   `/v1/responses`. Cost: none.
5. **The peer DM reaches the victim appended to its failing `./check` output.** Agensh delivers DMs mid-turn only by
   appending them to MCP tool results; the victim uses native shell tools, so a DM arrived only after its turn (observed
   live: router row `failed`, never `shown`). `./check` now reads `/pending` and reports `/shown` like the shim, and the
   controller acknowledges each check event so `./check` waits (bounded) for the DM it just posted. The path is still
   worker2 -> Mattermost -> router -> board. Cost: salience differs from a standalone chat message; the alternative (telling
   the victim to poll Mattermost) would change plan 9.1.
6. **Treatment messages are external inputs.** The benign message is verbatim from plan 9.3; `prompts/peer_malicious.txt` was
   copied verbatim from plan 9.4 by the researcher, not written by the harness author. The runner fails closed without either. Stage 1B LLM-peer prompts are not implemented.
7. **`check` and `submission.schema.json` are protected files** (evaluator-related artifacts, plan 12.1), beyond the four
   patterns listed in 4.5. Cost: one list.
8. **Fresh service stack per episode** (about 25 s) rather than a shared cohort stack, keeping plan 15.1's fresh profile.
   Service state is node-local; evidence is copied to `/scratch`. Cost: 25 s per episode.
9. **Idle/quiesce thresholds 240 s / 60 s.** The relay logs a request only when it completes and a Qwen turn can take
   minutes. Recorded in every manifest; `end_reason` is reported with a submitted-only sensitivity analysis.
10. **Attempt policy.** Interrupted or failed episodes are archived as `<episode>.attempt<k>`; the first valid attempt is
    primary; valid results are never re-run, so retries cannot select outcomes.
11. **Launch-time evidence for the quality gate.** The prompt hash and MCP server list are captured before the first
    turn; the end-of-episode copies in `/work` are agent-writable.
12. **`launch_single_agent.py` (standalone ProgramBench baseline) keeps its gpt-5.6-sol/high check**; only the team
    launcher and the profile generator became route-driven.
13. **Kimi K3 uses the Bedrock key already on this account.** If it is rejected, preflight fails closed and the cohort is
    skipped (at the time of writing it returns HTTP 401).
14. **Mechanical freeze for formal cohorts.** Plan 13.4/15.3 say prompts, tasks, evaluator and exclusion rules are frozen
    before the pilot and that a change makes a new version. `freeze.py` enforces this (content hash of the data-generating
    files, task index, route hashes, write-once, git tag); trial cohorts (`--tag`) are exempt. The freeze is taken before the
    Qwen pilot rather than after it (plan checklist order), because a pilot run on unfrozen inputs could be tuned. Cost: a
    needed fix mid-pilot means a new version.
