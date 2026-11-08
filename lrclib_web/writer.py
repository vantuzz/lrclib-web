"""Atomic, no-clobber sidecar writing (ARCHITECTURE.md §7) with a file-operation guard (DECISIONS.md D21).

Guard rules: the app creates or deletes only `.lrc` / `.txt` sidecars and its own `.<name>.tmp-<pid>` files,
and every such path, after resolving symlinks in its directory, must stay inside MUSIC_DIR.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat

SIDECAR_EXTS = (".lrc", ".txt")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # absent on Windows
_O_EXCL_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | getattr(os, "O_BINARY", 0)


class WriteSkipped(Exception):
    """Target already exists (D1: never overwrite)."""


class PermanentWriteError(Exception):
    pass


class UnsafePathError(PermanentWriteError):
    """Path breaks the file-operation guard (wrong extension or outside MUSIC_DIR)."""


# os.link() failures that mean "this filesystem cannot hard-link": fall back to an O_EXCL create of the target
_LINK_UNSUPPORTED = {
    getattr(errno, n) for n in ("EPERM", "EXDEV", "ENOTSUP", "EOPNOTSUPP", "ENOSYS") if hasattr(errno, n)
}


def normalize_text(text: str) -> str:
    """UTF-8 text without BOM, LF line endings, exactly one trailing newline."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def sidecar_path(audio_path: str, ext: str) -> str:
    stem = audio_path.rsplit(".", 1)[0] if "." in os.path.basename(audio_path) else audio_path
    return f"{stem}.{ext}"


def guard_sidecar(path: str, root: str) -> str:
    """Return the resolved sidecar path, or raise UnsafePathError.

    Only the directory part is resolved: the final component itself is never followed
    (writes use O_EXCL|O_NOFOLLOW, deletes refuse symlinks).
    """
    name = os.path.basename(path)
    if not name or not name.lower().endswith(SIDECAR_EXTS):
        raise UnsafePathError(f"refusing non-sidecar file {name!r}")
    real_root = os.path.realpath(root)
    real_dir = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    try:
        inside = os.path.commonpath([real_root, real_dir]) == real_root
    except ValueError:  # different drives (Windows)
        inside = False
    if not inside:
        raise UnsafePathError(f"refusing path outside MUSIC_DIR: {name!r}")
    return os.path.join(real_dir, name)


def _write_all(fd: int, data: bytes) -> None:
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _open_tmp(tmp: str) -> int:
    """Create our temp file exclusively; a stale one with our name (e.g. pid 1 after a crash) is ours to drop."""
    try:
        return os.open(tmp, _O_EXCL_NEW, 0o666)
    except FileExistsError:
        os.unlink(tmp)  # removes a stale file or a planted symlink itself, never its target
        return os.open(tmp, _O_EXCL_NEW, 0o666)


def _publish(tmp: str, target: str, data: bytes) -> None:
    """Atomic no-clobber publish: hard-link tmp to target (fails if target exists).

    Without hard links: create the target itself with O_EXCL|O_NOFOLLOW (no clobber, not atomic;
    a partly written target is removed on failure).
    """
    try:
        os.link(tmp, target)
        return
    except FileExistsError as e:
        raise WriteSkipped(target) from e
    except AttributeError:
        pass
    except OSError as e:
        if e.errno not in _LINK_UNSUPPORTED:
            raise
    try:
        fd = os.open(target, _O_EXCL_NEW, 0o666)
    except FileExistsError as e:
        raise WriteSkipped(target) from e
    try:
        _write_all(fd, data)
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def write_sidecar(target: str, text: str, root: str) -> str:
    """Write text atomically to target inside root. Returns sha256. Raises WriteSkipped if target exists."""
    target = guard_sidecar(target, root)
    if os.path.lexists(target):
        raise WriteSkipped(target)
    data = normalize_text(text).encode("utf-8")
    d, name = os.path.split(target)
    tmp = os.path.join(d, f".{name}.tmp-{os.getpid()}")
    try:
        fd = _open_tmp(tmp)
    except OSError as e:
        if e.errno == errno.ENAMETOOLONG:
            raise PermanentWriteError("file name too long") from e
        raise
    try:
        _write_all(fd, data)
        _publish(tmp, target, data)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return hashlib.sha256(data).hexdigest()


def remove_sidecar(path: str, root: str) -> None:
    """Delete a sidecar inside root. Refuses symlinks and non-regular files."""
    path = guard_sidecar(path, root)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePathError(f"refusing to delete non-regular file {os.path.basename(path)!r}")
    os.unlink(path)
