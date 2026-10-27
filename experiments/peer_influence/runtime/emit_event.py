"""Append-only JSONL event log. Standard library only (runs inside the worker sandbox)."""
from __future__ import annotations

import datetime
import json
import os


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_event(path, event):
    """Append one event as a single O_APPEND write so concurrent writers never interleave."""
    line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
    directory = os.path.dirname(os.fspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
