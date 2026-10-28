"""Small shared helpers: hashing, canonical JSON and atomic writes."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
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
    try:
        return sha256_file(path)
    except OSError as exc:  # unreadable is a state of the file, not a harness crash
        return "error:" + type(exc).__name__


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def tree_sha256(root: Path, skip_unreadable: bool = False) -> str:
    """Digest of every file path and content under root (symlinks hashed by target).

    With skip_unreadable, entries that cannot be copied faithfully (FIFOs, devices, unreadable
    files) are left out, so a workspace and its safe copy can be compared."""
    root = Path(root)
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            digest = safe_digest(path)
            if skip_unreadable and digest.startswith(("special:", "error:")):
                continue
            h.update(rel.encode() + b"\0")
            h.update(digest.encode())
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


SAFE_GIT = [
    "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
    "-c", "core.quotepath=off", "-c", "protocol.ext.allow=never",
]
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
}


def git_safe(workspace, *args, timeout=60):
    """Run git in a repository the agent controls without letting its config launch programs."""
    proc = subprocess.run(SAFE_GIT + ["-C", str(workspace), *args], capture_output=True, env=GIT_ENV, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {proc.stderr.decode(errors='replace').strip()[:200]}")
    return proc.stdout.decode("utf-8", errors="surrogateescape")


MAX_COPY_BYTES = 256 << 20


def safe_copyfile(src, dst, max_bytes=MAX_COPY_BYTES):
    """Copy a regular file from an untrusted tree; False (nothing copied) for anything else."""
    try:
        st = os.lstat(src)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return False
        shutil.copyfile(src, dst, follow_symlinks=False)
        # git treats 100755 -> 100644 as a modification, so a copy that dropped the exec bit
        # would turn the untouched ./check into a "protected file write".
        os.chmod(dst, stat.S_IMODE(st.st_mode))
        return True
    except OSError:
        return False


def safe_copytree(src, dst, max_file_bytes=MAX_COPY_BYTES):
    """Copy an untrusted tree: links stay links (never followed), FIFOs/devices/unreadable/huge
    files are skipped and reported instead of blocking or aborting the copy."""
    src, dst = Path(src), Path(dst)
    report = {"special": [], "unreadable": [], "oversize": []}
    dst.mkdir(parents=True, exist_ok=True)

    def unreadable_dir(err):
        report["unreadable"].append(Path(err.filename).relative_to(src).as_posix())

    for dirpath, dirnames, filenames in os.walk(src, followlinks=False, onerror=unreadable_dir):
        rel_dir = Path(dirpath).relative_to(src)
        (dst / rel_dir).mkdir(parents=True, exist_ok=True)
        for name in list(dirnames):
            path = Path(dirpath) / name
            if path.is_symlink():
                os.symlink(os.readlink(path), dst / rel_dir / name)
                dirnames.remove(name)
        for name in filenames:
            path, rel = Path(dirpath) / name, (rel_dir / name).as_posix()
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(path), dst / rel_dir / name)
            elif not stat.S_ISREG(st.st_mode):
                report["special"].append(rel)
            elif st.st_size > max_file_bytes:
                report["oversize"].append(rel)
            elif not safe_copyfile(path, dst / rel_dir / name, max_file_bytes):
                report["unreadable"].append(rel)
                (dst / rel_dir / name).unlink(missing_ok=True)
    return {k: sorted(v) for k, v in report.items()}
