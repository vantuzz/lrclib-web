import errno
import os

import pytest

from lrclib_web.writer import (
    UnsafePathError,
    WriteSkipped,
    normalize_text,
    remove_sidecar,
    sha256_text,
    sidecar_path,
    write_sidecar,
)


def test_normalize_text():
    assert normalize_text("﻿a\r\nb\rc\n\n\n") == "a\nb\nc\n"
    assert normalize_text("x") == "x\n"


def test_sidecar_path():
    assert sidecar_path("/m/a/b.mp3", "lrc") == "/m/a/b.lrc"
    assert sidecar_path("/m/a.b/song.tar.flac", "txt") == "/m/a.b/song.tar.txt"


def test_write_atomic_and_content(tmp_path):
    t = str(tmp_path / "a.lrc")
    sha = write_sidecar(t, "﻿[00:01.00]hi\r\n[00:02.00]yo\r\n\r\n", str(tmp_path))
    with open(t, "rb") as f:
        data = f.read()
    assert data == b"[00:01.00]hi\n[00:02.00]yo\n"
    assert not data.startswith(b"\xef\xbb\xbf")
    assert sha == sha256_text("[00:01.00]hi\n[00:02.00]yo")
    assert os.listdir(tmp_path) == ["a.lrc"]  # no temp left behind


def test_no_clobber(tmp_path):
    t = tmp_path / "a.lrc"
    t.write_bytes(b"mine")
    with pytest.raises(WriteSkipped):
        write_sidecar(str(t), "new", str(tmp_path))
    assert t.read_bytes() == b"mine"
    assert os.listdir(tmp_path) == ["a.lrc"]


def test_publish_uses_hardlink_and_cleans_tmp(tmp_path, monkeypatch):
    calls = []
    real = os.link
    monkeypatch.setattr(os, "link", lambda a, b: (calls.append((a, b)), real(a, b))[1])
    write_sidecar(str(tmp_path / "a.lrc"), "x", str(tmp_path))
    assert len(calls) == 1 and calls[0][1].endswith("a.lrc")
    assert os.listdir(tmp_path) == ["a.lrc"]


def test_link_race_file_exists_is_skipped_not_clobbered(tmp_path, monkeypatch):
    t = tmp_path / "a.lrc"

    def racing_link(src, dst):
        t.write_bytes(b"theirs")  # somebody else wins between our check and publish
        raise FileExistsError(errno.EEXIST, "exists", dst)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(WriteSkipped):
        write_sidecar(str(t), "mine", str(tmp_path))
    assert t.read_bytes() == b"theirs"
    assert os.listdir(tmp_path) == ["a.lrc"]


@pytest.mark.parametrize("code", [errno.EPERM, errno.EXDEV, errno.ENOSYS])
def test_link_unsupported_falls_back_to_exclusive_create(tmp_path, monkeypatch, code):
    def nolink(src, dst):
        raise OSError(code, "nope")

    monkeypatch.setattr(os, "link", nolink)
    t = tmp_path / "a.lrc"
    write_sidecar(str(t), "x", str(tmp_path))
    assert t.read_bytes() == b"x" + bytes([10]) and os.listdir(tmp_path) == ["a.lrc"]
    t.write_bytes(b"mine")  # fallback keeps the exists-check
    with pytest.raises(WriteSkipped):
        write_sidecar(str(t), "new", str(tmp_path))
    assert t.read_bytes() == b"mine"


def test_link_attribute_error_falls_back(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "link")
    write_sidecar(str(tmp_path / "a.lrc"), "x", str(tmp_path))
    assert os.listdir(tmp_path) == ["a.lrc"]


def test_link_other_oserror_propagates_and_cleans_tmp(tmp_path, monkeypatch):
    def broken(src, dst):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(os, "link", broken)
    with pytest.raises(OSError):
        write_sidecar(str(tmp_path / "a.lrc"), "x", str(tmp_path))
    assert os.listdir(tmp_path) == []



def test_data_dir_problem(tmp_path, monkeypatch):
    from lrclib_web import db as dbmod

    assert dbmod.data_dir_problem(str(tmp_path / "new")) is None  # created and writable
    blocker = tmp_path / "file"
    blocker.write_text("x")
    msg = dbmod.data_dir_problem(str(blocker))
    assert msg and "not writable" in msg and "chown -R 99:100" in msg

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("builtins.open", denied)
    msg = dbmod.data_dir_problem(str(tmp_path))
    assert "Permission denied" in msg


# --- D21: file-operation guard ---------------------------------------------------------------------------------


def _symlink_or_skip(src, dst, **kw):
    try:
        os.symlink(src, dst, **kw)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")


@pytest.mark.parametrize("name", ["a.flac", "a.mp3", "a.lrc.bak", "a", "a.db"])
def test_write_refuses_non_sidecar_extension(tmp_path, name):
    with pytest.raises(UnsafePathError):
        write_sidecar(str(tmp_path / name), "x", str(tmp_path))
    assert os.listdir(tmp_path) == []


def test_extension_check_is_case_insensitive(tmp_path):
    write_sidecar(str(tmp_path / "A.LRC"), "x", str(tmp_path))
    assert os.listdir(tmp_path) == ["A.LRC"]


def test_write_refuses_dotdot_escape(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    with pytest.raises(UnsafePathError):
        write_sidecar(str(root / ".." / "evil.lrc"), "x", str(root))
    assert not (tmp_path / "evil.lrc").exists()


def test_write_refuses_symlinked_dir_outside_root(tmp_path):
    root, outside = tmp_path / "music", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _symlink_or_skip(outside, root / "album", target_is_directory=True)
    with pytest.raises(UnsafePathError):
        write_sidecar(str(root / "album" / "a.lrc"), "x", str(root))
    assert os.listdir(outside) == []


def test_symlinked_dir_inside_root_is_allowed(tmp_path):
    root = tmp_path / "music"
    (root / "real").mkdir(parents=True)
    _symlink_or_skip(root / "real", root / "alias", target_is_directory=True)
    write_sidecar(str(root / "alias" / "a.lrc"), "x", str(root))
    assert os.listdir(root / "real") == ["a.lrc"]


def test_symlinked_root_itself_is_allowed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    _symlink_or_skip(real, tmp_path / "music", target_is_directory=True)
    write_sidecar(str(tmp_path / "music" / "a.lrc"), "x", str(tmp_path / "music"))
    assert os.listdir(real) == ["a.lrc"]


def test_dangling_symlink_target_is_skipped_not_followed(tmp_path):
    victim = tmp_path / "victim.lrc"
    root = tmp_path / "music"
    root.mkdir()
    _symlink_or_skip(victim, root / "a.lrc")
    with pytest.raises(WriteSkipped):
        write_sidecar(str(root / "a.lrc"), "x", str(root))
    assert not victim.exists()


def test_stale_tmp_from_same_pid_is_replaced(tmp_path):
    stale = tmp_path / f".a.lrc.tmp-{os.getpid()}"
    stale.write_bytes(b"old crash leftover")
    write_sidecar(str(tmp_path / "a.lrc"), "x", str(tmp_path))
    assert os.listdir(tmp_path) == ["a.lrc"]
    assert (tmp_path / "a.lrc").read_bytes() == b"x\n"


def test_planted_tmp_symlink_is_not_followed(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    _symlink_or_skip(victim, root / f".a.lrc.tmp-{os.getpid()}")
    write_sidecar(str(root / "a.lrc"), "x", str(root))
    assert victim.read_bytes() == b"precious"
    assert os.listdir(root) == ["a.lrc"]


def test_fallback_never_clobbers_target_created_in_race(tmp_path, monkeypatch):
    t = tmp_path / "a.lrc"

    def nolink_and_race(src, dst):
        t.write_bytes(b"someone else")
        raise OSError(errno.EXDEV, "nope")

    monkeypatch.setattr(os, "link", nolink_and_race)
    with pytest.raises(WriteSkipped):
        write_sidecar(str(t), "x", str(tmp_path))
    assert t.read_bytes() == b"someone else"
    assert os.listdir(tmp_path) == ["a.lrc"]


def test_fallback_write_failure_removes_partial_target(tmp_path, monkeypatch):
    import lrclib_web.writer as w

    monkeypatch.setattr(os, "link", lambda s, d: (_ for _ in ()).throw(OSError(errno.EXDEV, "nope")))
    real = w._write_all
    calls = []

    def flaky(fd, data):
        calls.append(fd)
        if len(calls) == 2:  # second call writes the target in the fallback
            os.close(fd)
            raise OSError(errno.ENOSPC, "full")
        real(fd, data)

    monkeypatch.setattr(w, "_write_all", flaky)
    with pytest.raises(OSError):
        write_sidecar(str(tmp_path / "a.lrc"), "x", str(tmp_path))
    assert os.listdir(tmp_path) == []


def test_remove_sidecar_deletes_regular_file(tmp_path):
    t = tmp_path / "a.txt"
    t.write_bytes(b"x")
    remove_sidecar(str(t), str(tmp_path))
    assert os.listdir(tmp_path) == []


def test_remove_refuses_non_sidecar_and_outside(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    (root / "a.flac").write_bytes(b"audio")
    (tmp_path / "b.txt").write_bytes(b"x")
    with pytest.raises(UnsafePathError):
        remove_sidecar(str(root / "a.flac"), str(root))
    with pytest.raises(UnsafePathError):
        remove_sidecar(str(root / ".." / "b.txt"), str(root))
    assert (root / "a.flac").exists() and (tmp_path / "b.txt").exists()


def test_remove_refuses_symlink(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    victim = root / "real.txt"
    victim.write_bytes(b"x")
    _symlink_or_skip(victim, root / "a.txt")
    with pytest.raises(UnsafePathError):
        remove_sidecar(str(root / "a.txt"), str(root))
    assert victim.exists() and os.path.lexists(root / "a.txt")
