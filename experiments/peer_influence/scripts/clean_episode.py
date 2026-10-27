#!/usr/bin/env python3
"""Stop whatever still runs from an episode's live root and delete it (plan 15.1: no leftover state)."""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import time
from pathlib import Path


def processes_using(live_root):
    """Pids (other than ours) whose command line or working directory is under live_root."""
    live = str(Path(live_root).resolve())
    mine = {os.getpid(), os.getppid()}
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in mine:
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            continue
        if live in cmdline or cwd == live or cwd.startswith(live + "/"):
            found.append(int(entry))
    return sorted(found)


def clean_episode(live_root):
    live = Path(live_root)
    pids = processes_using(live)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(0.5)
    shutil.rmtree(live, ignore_errors=True)
    return {"killed": pids, "removed": not live.exists()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("live_root", type=Path)
    args = ap.parse_args(argv)
    print(clean_episode(args.live_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
