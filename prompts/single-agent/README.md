# Standalone baseline prompts

Inputs for [`launch_single_agent.py`](../../scripts/launch_single_agent.py) and
[`acp_driver.py`](../../scripts/acp_driver.py). The baseline works in local Git
and continues immediately after a completed turn.

| File | Purpose |
|---|---|
| `brief.txt` | Task objective, reference location, committed-source/build contract and budget |
| `rules.txt` | Behavioral-observation requirements and restrictions on source/binary reuse |
| `resume.txt` | Continuation instruction sent after completed prompts while more than 60 seconds remain |

The launcher replaces `${TASK}` and `${HOURS}`, installs private copies under
the worker's `~/.pbench/`, and records source-template hashes in provenance.
The driver's first prompt combines rules with the brief; later prompts use
the resume text in the same ACP session. The budget clock starts after the
initial session is initialized. At the next prompt boundary with at most
15 minutes remaining, the driver adds a deadline notice once; at 60 seconds
or less it stops starting prompts. A pending prompt is bounded by the deadline.
A transport restart within the driver tries to reload the same session, and
a fresh session receives the complete initial prompt again.

These templates preserve the recorded standalone condition. They differ from
the team's collaboration instructions and ten-minute idle-nudge policy.
