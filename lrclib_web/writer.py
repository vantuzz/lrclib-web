"""Atomic, no-clobber sidecar writing (ARCHITECTURE.md §7)."""

from __future__ import annotations

import errno
import hashlib
import os


class WriteSkipped(Exception):
    """Target already exists (D1: never overwrite)."""


class PermanentWriteError(Exception):
    pass


# os.link() failures that mean "this filesystem cannot hard-link": fall back to exists-check + replace
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


def _publish(tmp: str, target: str) -> None:
    """Atomic no-clobber publish: hard-link tmp to target (fails if target exists), then drop tmp."""
    try:
        os.link(tmp, target)
    except FileExistsError as e:
        raise WriteSkipped(target) from e
    except AttributeError:
        pass
    except OSError as e:
        if e.errno not in _LINK_UNSUPPORTED:
            raise
    else:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return
    # fallback (no hard links): single writer, TOCTOU accepted
    if os.path.lexists(target):
        raise WriteSkipped(target)
    os.replace(tmp, target)


def write_sidecar(target: str, text: str) -> str:
    """Write text atomically to target. Returns sha256. Raises WriteSkipped if target exists."""
    if os.path.lexists(target):
        raise WriteSkipped(target)
    data = normalize_text(text).encode("utf-8")
    d, name = os.path.split(target)
    tmp = os.path.join(d, f".{name}.tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    except OSError as e:
        if e.errno == errno.ENAMETOOLONG:
            raise PermanentWriteError("file name too long") from e
        raise
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _publish(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return hashlib.sha256(data).hexdigest()
