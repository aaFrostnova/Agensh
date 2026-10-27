"""Timestamp protected-file changes as they happen (the scorer only sees the final state)."""
from __future__ import annotations

import time
from pathlib import Path

from peer_influence.runtime.emit_event import append_event
from peer_influence.runtime.protected_hashes import diff_snapshots, snapshot

_ABSENT = object()


class WorkspaceMonitor:
    def __init__(self, workspace, patterns, baseline_hashes, log_path, clock=time.time):
        self.workspace = Path(workspace)
        self.patterns = list(patterns)
        self.baseline = dict(baseline_hashes)
        self.log_path = Path(log_path)
        self.clock = clock
        self._deviation = {}  # path -> digest of the deviating file (None when deleted)

    def _current_deviation(self):
        after = snapshot(self.workspace, self.patterns)
        d = diff_snapshots(self.baseline, after)
        dev = {p: after[p] for p in d["modified"] + d["added"]}
        dev.update({p: None for p in d["deleted"]})
        return dev, d

    def poll(self):
        dev, d = self._current_deviation()
        now = self.clock()
        changed = sorted(p for p, digest in dev.items() if self._deviation.get(p, _ABSENT) != digest)
        restored = sorted(p for p in self._deviation if p not in dev)
        self._deviation = dev
        events = []
        if changed:
            events.append({
                "kind": "protected_change", "t": now, "paths": changed,
                "modified": [p for p in d["modified"] if p in changed],
                "deleted": [p for p in d["deleted"] if p in changed],
                "added": [p for p in d["added"] if p in changed],
            })
        if restored:
            events.append({"kind": "protected_restored", "t": now, "paths": restored})
        for ev in events:
            append_event(self.log_path, ev)
        return events
