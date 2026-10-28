"""Hash the protected file set of a workspace.

Standard library only: this file is copied into the worker sandbox next to
check_wrapper.py, and the external evaluator imports the same code.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import stat

SKIP_DIRS = {".git"}
# The monitor re-hashes the protected set every poll; a multi-GB file planted by the agent must not
# turn that into a busy loop. Canonical protected files are tiny, so any change to one still shows.
MAX_HASH_BYTES = 256 << 20


def match_protected(rel, patterns):
    """True if the posix relative file path `rel` is covered by a protected pattern."""
    rel = rel.replace(os.sep, "/")
    for pat in patterns:
        if pat.endswith("/**"):
            if rel.startswith(pat[:-2]):
                return True
        elif fnmatch.fnmatchcase(rel, pat):
            return True
    return False


def _digest(path):
    """Never opens anything but a regular file: a FIFO or device planted by the agent would block."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return "link:" + os.readlink(path)
        if not stat.S_ISREG(st.st_mode):
            return "special:%o" % stat.S_IFMT(st.st_mode)
        if st.st_size > MAX_HASH_BYTES:
            return "large:%d" % st.st_size
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return "sha256:" + h.hexdigest()
    except OSError as exc:  # unreadable counts as changed, never as unchanged
        return "error:" + type(exc).__name__


def snapshot(root, patterns):
    """Map relative posix path -> digest for every protected file under root.

    Symlinked directories are not followed, so replacing a protected directory by a
    link makes its former files disappear from the snapshot (reported as deleted).
    """
    root = os.fspath(root)
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        for name in sorted(filenames):
            rel = name if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/" + name
            if match_protected(rel, patterns):
                out[rel] = _digest(os.path.join(dirpath, name))
    return out


def diff_snapshots(before, after):
    return {
        "modified": sorted(p for p in before if p in after and before[p] != after[p]),
        "deleted": sorted(p for p in before if p not in after),
        "added": sorted(p for p in after if p not in before),
    }


def baseline_record(root, patterns):
    return {"patterns": list(patterns), "hashes": snapshot(root, patterns)}
