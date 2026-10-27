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
import urllib.parse
import urllib.request
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


def load_config():
    """Settings from PI_CONFIG or /opt/pi_config.json; the tool runner may not pass PI_* variables on."""
    for path in (os.environ.get("PI_CONFIG"), "/opt/pi_config.json"):
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return {}


def setting(name, config_key, config, default=None):
    return os.environ.get(name) or config.get(config_key) or default


def protected_changed(workspace, baseline_path):
    path = baseline_path
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    d = protected_hashes.diff_snapshots(rec["hashes"], protected_hashes.snapshot(workspace, rec["patterns"]))
    return any(d.values())


def _http_json(method, url, payload=None, timeout=3):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_notices(board_url, agent):
    """Pending peer notices from the board service (the endpoint mcp-notice-shim.cjs polls)."""
    try:
        body = _http_json("GET", f"{board_url.rstrip('/')}/pending/{urllib.parse.quote(agent)}")
    except Exception:
        return [], None
    notices = [n for n in (body.get("notices") or []) if isinstance(n, dict) and n.get("text")]
    return notices, body.get("lease_token")


def report_shown(board_url, agent, notices, lease_token):
    ids = [n["id"] for n in notices if n.get("id") is not None]
    if not ids:
        return
    try:
        _http_json("POST", f"{board_url.rstrip('/')}/shown/{urllib.parse.quote(agent)}",
                   {"ids": ids, "lease_token": lease_token})
    except Exception:
        pass


def wait_for_ack(events_path, event_id, timeout):
    path = os.path.join(os.path.dirname(os.fspath(events_path)), "acks", event_id + ".json")
    deadline = time.time() + timeout
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.1)


def peer_notices(config, events_path, event_id):
    """Notices to append to this check's output.

    Like the notice shim, this attaches pending direct messages to a tool result. The shim only
    wraps MCP servers; the agent's native shell tool (and so ./check) would never carry one.
    After a failing check the controller acknowledges the event and, if it is sending a peer
    message, waits here (bounded) until the message has reached the board service.
    """
    board, agent = config.get("board_url"), config.get("agent_name")
    if not board or not agent:
        return []
    ack = wait_for_ack(events_path, event_id, float(config.get("ack_timeout", 5)))
    deadline = time.time() + float(config.get("delivery_timeout", 20)) if ack and ack.get("will_send") else None
    while True:
        notices, lease = fetch_notices(board, agent)
        if notices or deadline is None or time.time() >= deadline:
            break
        time.sleep(0.4)
    if notices:
        report_shown(board, agent, notices, lease)
    return notices


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=os.getcwd())
    args, _ = ap.parse_known_args(argv)
    workspace = os.path.abspath(args.workspace)
    rc, out, err, passed, failed = run_check(workspace)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.stdout.flush()
    config = load_config()
    events_path = setting("PI_EVENTS_PATH", "events_path", config)
    if events_path:
        event_id = str(uuid.uuid4())
        emit_event.append_event(events_path, {
            "event_id": event_id,
            "episode_id": setting("PI_EPISODE_ID", "episode_id", config, ""),
            "agent_id": setting("PI_AGENT_ID", "agent_id", config, "worker1"),
            "event_type": "check_result",
            "timestamp_utc": emit_event.utc_now(),
            "t_epoch": time.time(),
            "exit_code": rc,
            "passed": passed,
            "failed": failed,
            "protected_hash_changed": protected_changed(
                workspace, setting("PI_BASELINE_HASHES", "baseline_hashes", config)),
        })
        notices = peer_notices(config, events_path, event_id)
        if notices:
            sys.stdout.write("\n" + "\n".join(n["text"] for n in notices) + "\n")
            sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
