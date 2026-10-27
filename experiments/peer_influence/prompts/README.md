# Experiment prompts

| File | Role |
|---|---|
| `victim.md` | System prompt of the victim worker (plan 9.1), mounted in place of `prompts/agents.txt` |
| `task_brief.md` | Task brief delivered to the victim as the kickoff DM; `${TASK_ID}` is the neutral public task id |
| `peer_benign.txt` | Scripted peer message for the benign condition (plan 9.3) |
| `peer_malicious.txt` | **Supplied by the researcher, not included in this repository.** Scripted peer message for the malicious condition (plan 9.4) |

The runner loads treatment messages from `peer_<condition>.txt`, fails closed when a
file is missing, and records each file's SHA-256 in `prompt_hashes.json`. Keep the benign
and malicious messages matched in length, tone and opening (plan 9.4) and freeze both
before the first cohort; changing either creates a new experiment version.
