"""Attempt bookkeeping (plan 15.1): never overwrite an episode, keep the first complete attempt.

An episode directory without a score (a failure, or a run cut off by a lost node) is archived as
`<episode>.attempt<k>` together with a failure record, and the retry runs under the original
episode id as attempt k+1. A scored episode whose quality gate failed can be retried the same way
(`retry_invalid`): that gate looks only at infrastructure evidence, never at the outcome, so
retrying on it cannot select for a result. Valid results are never re-run.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from peer_influence.common import write_json_atomic
from peer_influence.runtime.emit_event import utc_now

_ATTEMPT = re.compile(r"^(?P<episode>.+)\.attempt(?P<n>\d+)$")


def parse_attempt(dirname):
    """('ep_000024', 2) for 'ep_000024.attempt2'; (name, None) for an un-suffixed directory."""
    m = _ATTEMPT.match(dirname)
    return (m.group("episode"), int(m.group("n"))) if m else (dirname, None)


def _archived(root, episode_id):
    return sorted(n for p in Path(root).glob(f"{episode_id}.attempt*")
                  for _, n in [parse_attempt(p.name)] if n is not None)


def _quality_valid(directory):
    try:
        return bool(json.loads((directory / "quality_report.json").read_text(encoding="utf-8"))["valid"])
    except (OSError, ValueError, KeyError):
        return False  # a score without a readable quality report cannot be trusted


def invalidate(directory, reason):
    """Operator decision that an attempt's result must not be used (for example a harness bug found
    afterwards). Recorded next to the evidence, never by deleting it; the first reason is kept."""
    path = Path(directory) / "invalidated.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    record = {"reason": reason, "at": utc_now()}
    write_json_atomic(path, record)
    return record


def prepare_attempt(results_root, episode_id, retry_invalid=False):
    """Make room for the next attempt of `episode_id`; its run_attempt number, or None if a result stays."""
    root = Path(results_root)
    current = root / episode_id
    done = _archived(root, episode_id)
    if current.exists():
        scored = (current / "score.json").exists()
        invalidated = (current / "invalidated.json").exists()
        if scored and not invalidated and not (retry_invalid and not _quality_valid(current)):
            return None
        if not (current / "run_failed.json").exists():
            if invalidated:
                phase, reason = "invalidated", json.loads((current / "invalidated.json").read_text())["reason"]
            elif scored:
                phase, reason = "invalid", "quality gate failed"
            else:
                phase, reason = "interrupted", "no score.json and no failure record: the run was cut off (node loss, kill, ...)"
            write_json_atomic(current / "run_failed.json", {"phase": phase, "error": reason})
        number = max(done, default=0) + 1
        current.rename(root / f"{episode_id}.attempt{number}")
        done.append(number)
    return max(done, default=0) + 1


def plan_resume(plan, results_root, retry_invalid=False, limit=None, live_base=None):
    """Specs that still need a run, in plan order, each with its run_attempt.

    Leftovers of the episodes it will run are archived here; `live_base` (node-local service
    state of a killed run) is cleared for them. The input plan is not modified."""
    todo = []
    for spec in plan:
        if limit is not None and len(todo) >= limit:
            break
        attempt = prepare_attempt(results_root, spec["episode_id"], retry_invalid)
        if attempt is None:
            continue
        if live_base is not None and (Path(live_base) / spec["episode_id"]).exists():
            from peer_influence.scripts.clean_episode import clean_episode

            clean_episode(Path(live_base) / spec["episode_id"])
        todo.append({**spec, "run_attempt": attempt})
    return todo
