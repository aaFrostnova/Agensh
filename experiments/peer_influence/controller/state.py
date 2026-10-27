"""Durable controller state and a restart-safe tail of the check-event log."""
from __future__ import annotations

import json
from pathlib import Path

from peer_influence.common import write_json_atomic


class StateStore:
    """JSON state file; every save is an atomic replace so a crash never leaves half a file."""

    def __init__(self, path, default):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = dict(default)
            self.save()

    def save(self):
        write_json_atomic(self.path, self.data)


class EventTail:
    """Read complete JSONL lines appended since `offset`; a partial last line waits for its newline."""

    def __init__(self, path, offset=0):
        self.path = Path(path)
        self.offset = offset

    def read_new(self):
        if not self.path.exists():
            return [], 0
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return [], 0
        events, malformed = [], 0
        for line in chunk[: end + 1].splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                malformed += 1
        self.offset += end + 1
        return events, malformed
