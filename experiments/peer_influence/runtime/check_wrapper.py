#!/usr/bin/env python3
"""Run the workspace's public evaluator and record the result as an event.

The wrapper never decides the scientific label: it reports what the agent's own
checker said and whether protected files differ from the canonical hashes.
Standard library only; deployed as /opt/pi/check_wrapper.py inside the sandbox.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid

try:
    from peer_influence.runtime import emit_event, protected_hashes
except ImportError:  # flat deployment next to the sibling modules
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import emit_event
    import protected_hashes

SUMMARY = re.compile(r"SUMMARY passed=(\d+) failed=(\d+)")


def run_check(workspace):
    proc = subprocess.run(
        [sys.executable, os.path.join(workspace, "evaluator", "check.py")],
        cwd=workspace, capture_output=True, text=True,
    )
    m = SUMMARY.search(proc.stdout)
    passed, failed = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    return proc.returncode, proc.stdout, proc.stderr, passed, failed


def protected_changed(workspace):
    path = os.environ.get("PI_BASELINE_HASHES")
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    d = protected_hashes.diff_snapshots(rec["hashes"], protected_hashes.snapshot(workspace, rec["patterns"]))
    return any(d.values())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=os.getcwd())
    args, _ = ap.parse_known_args(argv)
    workspace = os.path.abspath(args.workspace)
    rc, out, err, passed, failed = run_check(workspace)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.stdout.flush()
    events_path = os.environ.get("PI_EVENTS_PATH")
    if events_path:
        emit_event.append_event(events_path, {
            "event_id": str(uuid.uuid4()),
            "episode_id": os.environ.get("PI_EPISODE_ID", ""),
            "agent_id": os.environ.get("PI_AGENT_ID", "worker1"),
            "event_type": "check_result",
            "timestamp_utc": emit_event.utc_now(),
            "t_epoch": time.time(),
            "exit_code": rc,
            "passed": passed,
            "failed": failed,
            "protected_hash_changed": protected_changed(workspace),
        })
    return rc


if __name__ == "__main__":
    sys.exit(main())
