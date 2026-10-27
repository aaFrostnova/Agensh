"""Small shared helpers: hashing, canonical JSON and atomic writes."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


MAX_HASH_BYTES = 256 << 20


def safe_digest(path):
    """Digest of a path inside an untrusted workspace: regular files are hashed, everything
    else (links, FIFOs, devices, huge files) is described without being opened."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return "link:" + os.readlink(path)
    if not stat.S_ISREG(st.st_mode):
        return "special:%o" % stat.S_IFMT(st.st_mode)
    if st.st_size > MAX_HASH_BYTES:
        return "large:%d" % st.st_size
    return sha256_file(path)


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def tree_sha256(root: Path) -> str:
    """Digest of every file path and content under root (symlinks hashed by target)."""
    root = Path(root)
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            h.update(rel.encode() + b"\0")
            h.update(safe_digest(path).encode())
            h.update(b"\n")
    return h.hexdigest()


def write_json_atomic(path: Path, obj) -> None:
    """Write JSON via temp file + fsync + rename so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
