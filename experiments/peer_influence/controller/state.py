"""Durable controller state and a restart-safe tail of the check-event log."""
from __future__ import annotations

import json
import os
import stat
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


MAX_CHUNK = 1 << 20


class EventTail:
    """Read complete JSONL lines appended since `offset`; a partial last line waits for its newline.

    The log is written by the agent's `./check` but lives in a directory the agent controls, so it
    may be replaced by a FIFO, a symlink or garbage: only a regular file is read, at most
    MAX_CHUNK bytes per call, and a chunk without any newline is skipped as one malformed record."""

    def __init__(self, path, offset=0):
        self.path = Path(path)
        self.offset = offset

    def read_new(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError:
            return [], 0
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return [], 0
            f.seek(self.offset)
            chunk = f.read(MAX_CHUNK)
        end = chunk.rfind(b"\n")
        if end < 0:
            if len(chunk) >= MAX_CHUNK:
                self.offset += len(chunk)
                return [], 1
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
