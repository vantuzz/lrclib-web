import errno
import os

import pytest

from lrclib_web.writer import (
    WriteSkipped,
    normalize_text,
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
    sha = write_sidecar(t, "﻿[00:01.00]hi\r\n[00:02.00]yo\r\n\r\n")
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
        write_sidecar(str(t), "new")
    assert t.read_bytes() == b"mine"
    assert os.listdir(tmp_path) == ["a.lrc"]


def test_publish_uses_hardlink_and_cleans_tmp(tmp_path, monkeypatch):
    calls = []
    real = os.link
    monkeypatch.setattr(os, "link", lambda a, b: (calls.append((a, b)), real(a, b))[1])
    write_sidecar(str(tmp_path / "a.lrc"), "x")
    assert len(calls) == 1 and calls[0][1].endswith("a.lrc")
    assert os.listdir(tmp_path) == ["a.lrc"]


def test_link_race_file_exists_is_skipped_not_clobbered(tmp_path, monkeypatch):
    t = tmp_path / "a.lrc"

    def racing_link(src, dst):
        t.write_bytes(b"theirs")  # somebody else wins between our check and publish
        raise FileExistsError(errno.EEXIST, "exists", dst)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(WriteSkipped):
        write_sidecar(str(t), "mine")
    assert t.read_bytes() == b"theirs"
    assert os.listdir(tmp_path) == ["a.lrc"]


@pytest.mark.parametrize("code", [errno.EPERM, errno.EXDEV, errno.ENOSYS])
def test_link_unsupported_falls_back_to_replace(tmp_path, monkeypatch, code):
    def nolink(src, dst):
        raise OSError(code, "nope")

    monkeypatch.setattr(os, "link", nolink)
    t = tmp_path / "a.lrc"
    write_sidecar(str(t), "x")
    assert t.read_bytes() == b"x" + bytes([10]) and os.listdir(tmp_path) == ["a.lrc"]
    t.write_bytes(b"mine")  # fallback keeps the exists-check
    with pytest.raises(WriteSkipped):
        write_sidecar(str(t), "new")
    assert t.read_bytes() == b"mine"


def test_link_attribute_error_falls_back(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "link")
    write_sidecar(str(tmp_path / "a.lrc"), "x")
    assert os.listdir(tmp_path) == ["a.lrc"]


def test_link_other_oserror_propagates_and_cleans_tmp(tmp_path, monkeypatch):
    def broken(src, dst):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(os, "link", broken)
    with pytest.raises(OSError):
        write_sidecar(str(tmp_path / "a.lrc"), "x")
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
