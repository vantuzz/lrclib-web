import os
import time

import pytest

from lrclib_web import scanner as scanner_mod
from lrclib_web.config import Config
from lrclib_web.scanner import Scanner
from lrclib_web.tags import Tags


def fake_tags(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.startswith("untagged"):
        return Tags(error="missing title/artist tag")
    return Tags(title=stem, artist="Art", album="Alb", duration_ms=200000)


@pytest.fixture(autouse=True)
def settled_clock(monkeypatch):
    """Pretend an hour has passed so fresh test dirs are outside the 2 s mtime settle window."""
    monkeypatch.setattr(scanner_mod, "_now_ns", lambda: time.time_ns() + 3600 * 10**9)


@pytest.fixture
def cfg(music_dir, data_dir):
    return Config(music_dir=music_dir, data_dir=data_dir)


def mk(root, rel, data=b"x"):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def scan(db, cfg, mode="quick"):
    return Scanner(db, cfg, tag_reader=fake_tags).run(mode)


def statuses(db):
    return {r["rel_path"]: r["status"] for r in db.execute("SELECT rel_path, status FROM tracks")}


def test_new_files_and_sidecars(db, cfg, music_dir):
    mk(music_dir, "A/pending.mp3")
    mk(music_dir, "A/haslrc.flac")
    mk(music_dir, "A/haslrc.LRC")
    mk(music_dir, "A/untagged1.mp3")
    mk(music_dir, "A/notaudio.jpg")
    res = scan(db, cfg)
    assert res.new == 3 and res.untagged == 1 and res.files_seen == 3
    assert statuses(db) == {"A/pending.mp3": "pending", "A/haslrc.flac": "has_lrc",
                            "A/untagged1.mp3": "untagged"}


def test_hidden_entries_skipped(db, cfg, music_dir):
    mk(music_dir, ".hidden.mp3")
    mk(music_dir, "@eaDir/x.mp3")
    mk(music_dir, ".git/y.mp3")
    mk(music_dir, "ok/z.mp3")
    scan(db, cfg)
    assert list(statuses(db)) == ["ok/z.mp3"]


def test_quick_skips_unchanged_but_visits_known_subdirs(db, cfg, music_dir):
    mk(music_dir, "A/a.mp3")
    mk(music_dir, "A/sub/b.mp3")
    first = scan(db, cfg, "full")
    assert first.dirs_listed == 3
    second = scan(db, cfg, "quick")
    assert second.dirs_skipped == 3 and second.dirs_listed == 0 and second.new == 0
    mk(music_dir, "A/sub/c.mp3")
    third = scan(db, cfg, "quick")
    assert third.dirs_skipped == 2 and third.dirs_listed == 1 and third.new == 1


def test_full_relists(db, cfg, music_dir):
    mk(music_dir, "A/a.mp3")
    scan(db, cfg, "full")
    res = scan(db, cfg, "full")
    assert res.dirs_skipped == 0 and res.dirs_listed == 2 and res.new == 0


def test_removed_file_missing(db, cfg, music_dir):
    for n in "abc":
        mk(music_dir, f"A/{n}.mp3")
    scan(db, cfg)
    os.remove(os.path.join(music_dir, "A", "a.mp3"))
    res = scan(db, cfg)
    assert res.missing == 1 and not res.errors
    assert statuses(db)["A/a.mp3"] == "missing"


def test_missing_guard_over_50_pct(db, cfg, music_dir):
    for n in "abc":
        mk(music_dir, f"A/{n}.mp3")
    scan(db, cfg)
    os.remove(os.path.join(music_dir, "A", "a.mp3"))
    os.remove(os.path.join(music_dir, "A", "b.mp3"))
    res = scan(db, cfg)
    assert res.missing == 0 and res.errors
    assert set(statuses(db).values()) == {"pending"}


def test_missing_guard_empty_root(db, cfg, music_dir):
    mk(music_dir, "a.mp3")
    scan(db, cfg)
    os.remove(os.path.join(music_dir, "a.mp3"))
    res = scan(db, cfg)
    assert res.missing == 0 and res.errors
    assert statuses(db) == {"a.mp3": "pending"}


def test_unlistable_root_marks_nothing(db, cfg, music_dir):
    mk(music_dir, "a.mp3")
    scan(db, cfg)
    cfg.music_dir = os.path.join(music_dir, "nope")
    res = scan(db, cfg)
    assert res.errors and res.missing == 0
    assert statuses(db) == {"a.mp3": "pending"}


def test_reappearing_file_restores_previous_status(db, cfg, music_dir):
    for n in "abc":
        mk(music_dir, f"A/{n}.mp3")
    scan(db, cfg)
    row = db.execute("SELECT id FROM tracks WHERE rel_path='A/a.mp3'").fetchone()
    db.update_track(row["id"], status="not_found")
    src = os.path.join(music_dir, "A", "a.mp3")
    os.rename(src, src + ".bak")  # rename keeps mtime/size
    assert scan(db, cfg).missing == 1
    assert statuses(db)["A/a.mp3"] == "missing"
    os.rename(src + ".bak", src)
    res = scan(db, cfg)
    assert res.restored == 1
    assert statuses(db)["A/a.mp3"] == "not_found"


def test_sidecar_appearing_and_disappearing(db, cfg, music_dir):
    mk(music_dir, "A/a.mp3")
    mk(music_dir, "A/b.mp3")
    scan(db, cfg)
    lrc = mk(music_dir, "A/a.lrc")
    scan(db, cfg)
    assert statuses(db)["A/a.mp3"] == "has_lrc"
    os.remove(lrc)
    scan(db, cfg)
    assert statuses(db)["A/a.mp3"] == "pending"


def test_changed_file_rereads_tags(db, cfg, music_dir):
    p = mk(music_dir, "A/a.mp3", b"x")
    scan(db, cfg)
    db.update_track(1, status="not_found", attempts=2)
    with open(p, "wb") as f:
        f.write(b"longer content")
    res = scan(db, cfg, "full")
    assert res.changed == 1
    assert statuses(db)["A/a.mp3"] == "pending"


def dir_mtime(db, rel):
    return db.execute("SELECT mtime_ns FROM dirs WHERE rel_path=?", (rel,)).fetchone()[0]


def test_vanished_dir_nulls_mtime_and_restores_on_return(db, cfg, music_dir):
    for n in "abcd":
        mk(music_dir, f"A/{n}.mp3")
    mk(music_dir, "B/b.mp3")
    mk(music_dir, "B/sub/c.mp3")
    scan(db, cfg)
    assert dir_mtime(db, "B") is not None
    parked = os.path.join(os.path.dirname(music_dir), "B.parked")  # outside the library
    os.rename(os.path.join(music_dir, "B"), parked)
    res = scan(db, cfg)
    assert res.missing == 2
    assert dir_mtime(db, "B") is None and dir_mtime(db, "A") is not None
    os.rename(parked, os.path.join(music_dir, "B"))
    res = scan(db, cfg)  # quick: mtime is unchanged by the rename back, but NULL forces a re-list
    assert res.restored == 2
    assert set(statuses(db).values()) == {"pending"}


def test_scan_error_marks_incomplete_and_skips_missing(db, cfg, music_dir, monkeypatch):
    for n in "abc":
        mk(music_dir, f"A/{n}.mp3")
    mk(music_dir, "B/z.mp3")
    scan(db, cfg)
    os.remove(os.path.join(music_dir, "A", "a.mp3"))
    real = os.scandir

    def flaky(path="."):
        if os.path.basename(str(path)) == "B":
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", flaky)
    logs = []
    res = Scanner(db, cfg, tag_reader=fake_tags, log=lambda lv, m: logs.append((lv, m))).run("full")
    assert any("B" in e for e in res.errors)
    assert res.missing == 0
    assert ("warn", "scan incomplete, missing-marking skipped") in logs
    assert statuses(db)["A/a.mp3"] == "pending"
    assert dir_mtime(db, "B") is not None  # untouched: not overwritten with a new value
    monkeypatch.undo()
    assert scan(db, cfg, "full").missing == 1


def test_stat_error_other_than_enoent_is_incomplete(db, cfg, music_dir, monkeypatch):
    mk(music_dir, "A/a.mp3")
    mk(music_dir, "B/b.mp3")
    scan(db, cfg)
    real = os.stat

    def flaky(path, *a, **k):
        if os.path.basename(str(path)) == "B":
            raise PermissionError(13, "denied")
        return real(path, *a, **k)

    monkeypatch.setattr(os, "stat", flaky)
    res = scan(db, cfg)
    assert res.errors and res.missing == 0
    assert statuses(db)["B/b.mp3"] == "pending"


def test_recent_dir_mtime_not_stored(db, cfg, music_dir, monkeypatch):
    monkeypatch.setattr(scanner_mod, "_now_ns", time.time_ns)  # real clock
    mk(music_dir, "A/a.mp3")
    scan(db, cfg)
    assert dir_mtime(db, "A") is None  # modified < 2 s ago
    old = time.time() - 60
    os.utime(os.path.join(music_dir, "A"), (old, old))
    scan(db, cfg)
    assert dir_mtime(db, "A") is not None
    # same-tick addition after an old-mtime scan is not an issue; a recent one is re-listed next time
    mk(music_dir, "A/b.mp3")
    assert scan(db, cfg).new == 1
    assert dir_mtime(db, "A") is None
    assert scan(db, cfg).dirs_listed >= 1

