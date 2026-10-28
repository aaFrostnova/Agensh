"""Host-side provider usage ledger and aggregation (never estimates missing tokens).

Responses output_tokens/completion_tokens already include reasoning tokens. Reasoning
and cached input are reported as subsets, never added to the total a second time.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import fcntl
import os
from pathlib import Path
import threading

FIELDS = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "cached_input_tokens")
_LOCK = threading.Lock()


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(usage):
    usage = usage if isinstance(usage, dict) else {}
    inp = _count(usage.get("input_tokens", usage.get("prompt_tokens")))
    out = _count(usage.get("output_tokens", usage.get("completion_tokens")))
    total = _count(usage.get("total_tokens"))
    if total is None and inp is not None and out is not None:
        total = inp + out
    od = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    ind = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    od = od if isinstance(od, dict) else {}
    ind = ind if isinstance(ind, dict) else {}
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": total,
            "reasoning_tokens": _count(od.get("reasoning_tokens")),
            "cached_input_tokens": _count(ind.get("cached_tokens"))}


def extract_usage(body, content_type="application/json", transport_complete=True):
    """Read JSON or SSE snapshots; final cumulative usage is counted exactly once."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    snapshots, terminal, malformed = [], False, False
    responses_stream, terminal_usage_observed = False, False
    is_sse = "text/event-stream" in content_type or text.lstrip().startswith(("data:", "event:"))
    if is_sse:
        data = []
        # A trailing unclosed SSE frame is not a completed event.
        for line in text.replace("\r\n", "\n").split("\n"):
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload = "\n".join(data)
                data = []
                if payload == "[DONE]":
                    terminal = True
                    continue
                try:
                    obj = json.loads(payload)
                except ValueError:
                    malformed = True
                    continue
                if not isinstance(obj, dict):
                    malformed = True
                    continue
                typ = obj.get("type", "")
                responses_stream = responses_stream or (isinstance(typ, str) and typ.startswith("response."))
                response = obj.get("response") if isinstance(obj.get("response"), dict) else obj
                if typ in ("response.completed", "response.incomplete", "response.failed"):
                    terminal = True
                    terminal_usage_observed = isinstance(response.get("usage"), dict)
                if isinstance(response.get("usage"), dict):
                    snapshots.append((response["usage"], response.get("id"), response.get("model"), response.get("status") or typ))
        if data:
            malformed = True
    else:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                terminal = True
                if isinstance(obj.get("usage"), dict):
                    snapshots.append((obj["usage"], obj.get("id"), obj.get("model"), obj.get("status")))
        except (ValueError, TypeError):
            malformed = True
    usage, response_id, model, status = snapshots[-1] if snapshots else ({}, None, None, None)
    normalized = normalize_usage(usage)
    authoritative = not responses_stream or terminal_usage_observed
    complete = bool(transport_complete and terminal and authoritative and not malformed
                    and all(normalized[k] is not None for k in FIELDS[:3]))
    return {**normalized, "usage_present": bool(snapshots), "accounting_complete": complete,
            "response_id": response_id, "response_model": model, "response_status": status,
            "terminal_observed": terminal, "transport_complete": transport_complete,
            "usage_status": "complete" if complete else "partial" if snapshots else "missing"}


def append_event(path, event):
    """Append and flush before network work; host-only path must not be worker-mounted."""
    if not path:
        return
    record = {"schema_version": 1, "timestamp": datetime.now(timezone.utc).isoformat(), **event}
    raw = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with _LOCK:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)


def read_events(path):
    path = Path(path)
    if not path.exists():
        return [], ["ledger_missing"]
    events, errors = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("not an object")
            events.append(event)
        except ValueError:
            errors.append(f"invalid_ledger_line:{n}")
    return events, errors


def _summary(events, ledger_errors=()):
    starts, finishes, attempts, done = {}, {}, {}, {}
    for e in events:
        key = (e.get("episode_id"), e.get("request_id"))
        if e.get("event") == "request_started": starts[key] = e
        elif e.get("event") == "request_finished": finishes[key] = e
        elif e.get("event") == "attempt_started": attempts[(*key, e.get("attempt"))] = e
        elif e.get("event") == "attempt_finished": done[(*key, e.get("attempt"))] = e
    unfinished = len(set(attempts) - set(done))
    unresolved_requests = len(set(starts) - set(finishes))
    usage_rows = [e.get("usage", {}) for e in done.values()]
    incomplete = sum(not u.get("accounting_complete", False) for u in usage_rows)
    complete = bool(not ledger_errors and not unfinished and not unresolved_requests and not incomplete)
    totals, known, missing = {}, {}, {}
    for field in FIELDS:
        values = [_count(u.get(field)) for u in usage_rows]
        known[field] = sum(v for v in values if v is not None)
        missing[field] = sum(v is None for v in values) + unfinished
        # The zero-call case is exact only with an existing, valid ledger.
        totals[field] = known[field] if complete and not missing[field] else None
    return {"requests": len(starts), "attempts": len(attempts), "finished_attempts": len(done),
            "unfinished_requests": unresolved_requests, "unfinished_attempts": unfinished,
            "incomplete_usage_attempts": incomplete, "accounting_complete": complete,
            "totals": totals, "known_tokens": known, "missing_counts": missing,
            "ledger_errors": list(ledger_errors)}


def aggregate_usage(events, expected_agents=(), ledger_errors=()):
    by_agent, by_episode, by_cohort = defaultdict(list), defaultdict(list), defaultdict(list)
    for e in events:
        by_agent[e.get("agent_id", "unknown")].append(e)
        by_episode[e.get("episode_id", "unknown")].append(e)
        by_cohort[e.get("cohort_id", "unknown")].append(e)
    for agent in expected_agents:
        by_agent.setdefault(agent, [])
    def grouped_summary(rows):
        agents = defaultdict(list)
        for row in rows:
            agents[row.get("agent_id", "unknown")].append(row)
        return {**_summary(rows, ledger_errors),
                "agents": {agent: _summary(items, ledger_errors) for agent, items in sorted(agents.items())}}
    return {"schema_version": 1, "total": _summary(events, ledger_errors),
            "agents": {k: _summary(v, ledger_errors) for k, v in sorted(by_agent.items())},
            "episodes": {k: grouped_summary(v) for k, v in sorted(by_episode.items())},
            "cohorts": {k: grouped_summary(v) for k, v in sorted(by_cohort.items())},
            "semantics": "Provider-reported tokens; reasoning is a subset of output, cached input a subset of input. Null means unknown; known_tokens is a lower bound when accounting is incomplete."}


def summarize_episode_usage(path, expected_agents=()):
    events, errors = read_events(path)
    return aggregate_usage(events, expected_agents, errors)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ledgers", nargs="+", type=Path, help="usage_events.jsonl files or run directories searched recursively")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    paths = set()
    for path in args.ledgers:
        if path.is_dir():
            paths.update(p.resolve() for name in ("usage_events.jsonl", "usage.jsonl") for p in path.rglob(name))
        else:
            paths.add(path.resolve())
    events, errors = [], []
    if not paths:
        errors.append("no_ledgers_found")
    for path in sorted(paths):
        batch, errs = read_events(path)
        events.extend(batch)
        errors.extend(f"{path}:{e}" for e in errs)
    result = aggregate_usage(events, ledger_errors=errors)
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if result["total"]["accounting_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
