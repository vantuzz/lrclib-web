import os

import pytest

from lrclib_web import jobs
from lrclib_web.config import Config
from lrclib_web.db import now
from lrclib_web.jobs import COUNTER_KEYS, DAY, JobRunner
from lrclib_web.matcher import Match
from lrclib_web.scanner import Scanner
from lrclib_web.tags import Tags
from lrclib_web.writer import PermanentWriteError, sha256_text

SYNCED = {"id": 7, "syncedLyrics": "[00:01.00]hi", "plainLyrics": "hi"}


def fake_tags(path):
    return Tags(title="Song", artist="Art", album="Alb", duration_ms=200000)


def counters():
    return dict.fromkeys(COUNTER_KEYS, 0)


class Env:
    def __init__(self, music_dir, data_dir, db, **cfg_kw):
        self.cfg = Config(music_dir=music_dir, data_dir=data_dir, **cfg_kw)
        self.db = db
        self.lines = []
        self.runner = JobRunner(self.cfg, db, lambda lv, m: self.lines.append((lv, m)))
        self.audio = os.path.join(music_dir, "a.mp3")
        with open(self.audio, "wb") as f:
            f.write(b"x")
        Scanner(db, self.cfg, tag_reader=fake_tags).run("full")

    def row(self):
        return self.db.execute("SELECT * FROM tracks").fetchone()

    def apply(self, m):
        c = counters()
        self.runner._apply(self.row(), m, c, "Art — Song")
        return c, self.row()

    def logged(self, text):
        return any(text in m for _, m in self.lines)


@pytest.fixture
def env(music_dir, data_dir, db):
    return Env(music_dir, data_dir, db)


def test_vanished_audio_marks_missing_and_writes_nothing(env):
    os.remove(env.audio)
    for kind, rec in (("synced", SYNCED), ("plain", {"id": 7, "plainLyrics": "words"})):
        c, row = env.apply(Match(kind, rec))
        assert row["status"] == "missing" and row["prev_status"] == "pending"
        assert row["missing_since"] is not None
        assert c["skipped"] == 1 and c["synced"] == 0 and c["plain"] == 0
        assert env.logged("audio vanished")
        env.db.update_track(row["id"], status="pending", prev_status=None, missing_since=None)
    assert os.listdir(os.path.dirname(env.audio)) == []  # no sidecar written


def test_audio_present_still_writes(env):
    c, row = env.apply(Match("synced", SYNCED))
    assert row["status"] == "synced" and c["synced"] == 1
    assert os.path.exists(os.path.join(os.path.dirname(env.audio), "a.lrc"))


def test_permanent_errors_retry_in_90_days(env):
    before = now()
    _, row = env.apply(Match("error", reason="HTTP 400", transient=False))
    assert row["status"] == "error"
    assert 90 * DAY <= row["next_check_at"] - before <= 90 * DAY + 5


def test_permanent_write_error_retry_in_90_days(env, monkeypatch):
    def boom(*a, **k):
        raise PermanentWriteError("file name too long")

    monkeypatch.setattr(jobs, "write_sidecar", boom)
    before = now()
    c, row = env.apply(Match("synced", SYNCED))
    assert c["error"] == 1 and row["status"] == "error"
    assert 90 * DAY <= row["next_check_at"] - before <= 90 * DAY + 5


def prep_txt(env, content):
    txt = os.path.join(os.path.dirname(env.audio), "a.txt")
    with open(txt, "wb") as f:
        f.write(content)
    row = env.row()
    env.db.update_track(row["id"], txt_exists=1, written_by_us=1, written_file="a.txt",
                        written_sha256=sha256_text("words"))
    return txt


def test_txt_deleted_when_unmodified(music_dir, data_dir, db):
    env = Env(music_dir, data_dir, db, txt_on_synced="delete")
    txt = prep_txt(env, b"words\n")
    _, row = env.apply(Match("synced", SYNCED))
    assert not os.path.exists(txt) and row["txt_exists"] == 0
    assert row["status"] == "synced"


def test_txt_kept_when_modified_locally(music_dir, data_dir, db):
    env = Env(music_dir, data_dir, db, txt_on_synced="delete")
    txt = prep_txt(env, b"words\nmy edit\n")
    _, row = env.apply(Match("synced", SYNCED))
    assert os.path.exists(txt) and row["txt_exists"] == 1
    assert env.logged("txt modified locally, kept")


class FakeFcntl:
    LOCK_EX, LOCK_NB = 2, 4

    def __init__(self, held):
        self.held, self.calls = held, []

    def flock(self, f, flags):
        self.calls.append(flags)
        if self.held:
            raise BlockingIOError(11, "locked")


def run_download(env):
    env.db.update_track(env.row()["id"], status="synced")  # nothing due: only the lock matters
    env.runner.client_factory = lambda **kw: type("C", (), {"requests": 0})()
    env.runner.start("download")
    env.runner.join(30)
    return env.runner.snapshot()


def test_download_lock_held_fails_job(env, monkeypatch):
    fake = FakeFcntl(held=True)
    monkeypatch.setattr(jobs, "fcntl", fake)
    snap = run_download(env)
    assert snap["state"] == "failed"
    assert ("error", "another lrclib-web process is downloading") in env.lines
    assert fake.calls == [fake.LOCK_EX | fake.LOCK_NB]
    assert os.path.exists(os.path.join(env.cfg.data_dir, "download.lock"))


def test_download_lock_free_runs(env, monkeypatch):
    monkeypatch.setattr(jobs, "fcntl", FakeFcntl(held=False))
    assert run_download(env)["state"] == "done"


def test_download_without_fcntl_skips_lock(env, monkeypatch):
    monkeypatch.setattr(jobs, "fcntl", None)
    assert run_download(env)["state"] == "done"
    assert not os.path.exists(os.path.join(env.cfg.data_dir, "download.lock"))


# --- D17: run budget -------------------------------------------------------------------------------------------

class _NoClient:
    requests = 0

    def __init__(self, **kw):
        pass


def test_run_budget_stops_cleanly_and_leaves_tracks_due(music_dir, data_dir, db, monkeypatch):
    env = Env(music_dir, data_dir, db, run_max_minutes=1)
    env.runner.client_factory = _NoClient
    env.runner.state = {"counters": counters()}
    env.runner._bump = lambda **kw: None
    ticks = iter([0.0] + [120.0] * 10)
    monkeypatch.setattr(jobs.time, "monotonic", lambda: next(ticks))
    summary = []
    assert env.runner._download_locked(summary) == "done"
    assert "run budget reached, 1 left" in summary[0]
    assert env.logged("RUN_MAX_MINUTES=1 reached")
    assert env.row()["status"] == "pending"  # untouched, due again next run


# --- D18: terminal states --------------------------------------------------------------------------------------

def test_instrumental_rechecked_after_90_days(env):
    before = now()
    _, row = env.apply(Match("instrumental", {"id": 7, "instrumental": True}))
    assert row["status"] == "instrumental"
    assert 90 * DAY <= row["next_check_at"] - before <= 90 * DAY + 5
    assert [r["id"] for r in env.db.due_tracks(row["next_check_at"])] == [row["id"]]
    assert env.db.due_tracks(before) == []


def test_removed_lrc_redownloaded_once(env):
    from lrclib_web.scanner import LRC_REMOVED_AGAIN
    lrc = os.path.join(os.path.dirname(env.audio), "a.lrc")
    scan = Scanner(env.db, env.cfg, tag_reader=fake_tags)
    env.apply(Match("synced", SYNCED))
    os.remove(lrc)
    scan.run("full")
    row = env.row()
    assert (row["status"], row["lrc_resets"], row["lrc_exists"]) == ("pending", 1, 0)
    env.apply(Match("synced", SYNCED))
    assert os.path.exists(lrc)
    os.remove(lrc)
    scan.run("full")
    row = env.row()
    assert (row["status"], row["reason"], row["lrc_resets"]) == ("synced", LRC_REMOVED_AGAIN, 1)
    assert env.db.due_tracks(now() + 3650 * DAY) == []


# --- D19: not_found / rejected escalation ----------------------------------------------------------------------

def test_notfound_delay_days():
    assert [jobs.notfound_delay_days(14, n) for n in range(1, 6)] == [14, 28, 56, 180, 180]
    assert jobs.notfound_delay_days(0, 3) == 0
    assert jobs.notfound_delay_days(60, 4) == 240


def test_notfound_escalates_and_resets_on_audio_change(env):
    seen = []
    for m in (Match("not_found"), Match("rejected", reason="artist differs"), Match("not_found"), Match("not_found")):
        before = now()
        _, row = env.apply(m)
        seen.append(round((row["next_check_at"] - before) / DAY))
    assert seen == [14, 28, 56, 180]
    assert env.row()["attempts"] == 4
    with open(env.audio, "ab") as f:
        f.write(b"more")  # size change = audio changed
    Scanner(env.db, env.cfg, tag_reader=fake_tags).run("full")
    assert (env.row()["status"], env.row()["attempts"]) == ("pending", 0)
    before = now()
    _, row = env.apply(Match("not_found"))
    assert round((row["next_check_at"] - before) / DAY) == 14


def test_error_between_misses_restarts_escalation(env):
    env.apply(Match("not_found"))
    env.apply(Match("error", reason="HTTP 400", transient=False))
    before = now()
    _, row = env.apply(Match("not_found"))
    assert round((row["next_check_at"] - before) / DAY) == 14
