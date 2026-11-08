import json
import logging.handlers
import os
import re

import pytest

from lrclib_web import __version__, diaglog
from lrclib_web.jobs import LogBus
from lrclib_web.lrclib import LrclibClient
from lrclib_web.scanner import Scanner
from lrclib_web.scheduler import Scheduler
from lrclib_web.tags import read_tags

REQUIRED = {"ts", "level", "event", "msg", "version", "run_id"}
ISO_MS_TZ = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")


def read_log(bus):
    with open(bus.sink.path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def bus(tmp_path):
    return LogBus(log_dir=str(tmp_path / "logs"))


def test_lines_are_valid_json_with_required_keys(bus):
    bus("info", "plain call")
    bus.event("warn", "x.thing", "structured", a=1, nested={"b": [1, 2]})
    bus("ok", "with fields", event="y.thing", rel_path="a/b.mp3")
    evs = read_log(bus)
    assert len(evs) == 3
    for ev in evs:
        assert REQUIRED <= set(ev)
        assert ISO_MS_TZ.match(ev["ts"]), ev["ts"]
        assert ev["version"] == __version__
        assert ev["run_id"] is None
    assert evs[0]["event"] == "log" and evs[0]["msg"] == "plain call"
    assert evs[1]["event"] == "x.thing" and evs[1]["a"] == 1 and evs[1]["nested"] == {"b": [1, 2]}
    assert evs[2]["rel_path"] == "a/b.mp3" and evs[2]["level"] == "ok"


def test_reserved_fields_cannot_be_overridden(bus):
    bus.event("info", "e.x", "m", version="evil", run_id="evil", ts="evil")
    ev = read_log(bus)[0]
    assert ev["version"] == __version__ and ev["run_id"] is None and ISO_MS_TZ.match(ev["ts"])


def test_run_id_is_attached(bus):
    bus.run_id = "abc123"
    bus("info", "x")
    assert read_log(bus)[0]["run_id"] == "abc123"


def test_ui_log_unchanged_and_file_only_events_stay_out_of_it(bus, capsys):
    bus("info", "hello")
    bus.event("info", "file.only", "hidden")
    assert [x["msg"] for x in bus.since(0)] == ["hello"]
    assert "hidden" not in capsys.readouterr().out
    assert len(read_log(bus)) == 2


def test_rotation_config(bus):
    h = bus.sink.handler
    assert isinstance(h, logging.handlers.RotatingFileHandler)
    assert h.maxBytes == 5 * 1024 * 1024
    assert h.backupCount == 4  # current + 4 rotated = 5 files


def test_rotation_happens(tmp_path):
    bus = LogBus()
    assert bus.attach_file(str(tmp_path / "logs"), max_bytes=600, backups=2)
    for i in range(60):
        bus("info", f"line {i} " + "x" * 40)
    names = sorted(os.listdir(tmp_path / "logs"))
    assert names == ["app.jsonl", "app.jsonl.1", "app.jsonl.2"]
    assert [os.path.basename(p) for p in bus.sink.paths()] == ["app.jsonl.2", "app.jsonl.1", "app.jsonl"]
    bus.sink.close()


def test_unusable_log_dir_runs_without_file(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    bus = LogBus()
    assert bus.attach_file(str(blocker / "logs")) is False
    bus("info", "still works")
    assert bus.since(0)[0]["msg"] == "still works"
    assert bus.sink is None
    assert "running without file log" in capsys.readouterr().out


def test_write_failure_is_reported_once_and_never_raises(bus, capsys, monkeypatch):
    def boom(record):
        raise OSError("disk full")

    monkeypatch.setattr(bus.sink.handler, "emit", boom)
    for _ in range(3):
        bus("info", "x")
        bus.event("error", "e.x", "y")
    out = capsys.readouterr().out
    assert out.count("cannot write") == 1
    assert len(bus.since(0)) == 3  # the UI log is unaffected


def test_unserialisable_field_does_not_break(bus):
    bus.event("info", "e.x", "m", obj=object())
    assert "object object" in read_log(bus)[0]["obj"]


def test_debug_is_filtered_unless_log_level_debug(tmp_path):
    info = LogBus(level="info", log_dir=str(tmp_path / "i"))
    info("debug", "no")
    info.event("debug", "d.x", "no")
    info("info", "yes")
    assert [e["msg"] for e in read_log(info)] == ["yes"]
    dbg = LogBus(level="debug", log_dir=str(tmp_path / "d"))
    dbg("debug", "d1")
    dbg.event("debug", "d.x", "d2")
    assert [e["event"] for e in read_log(dbg)] == ["log", "d.x"]


def test_long_strings_are_clipped(bus):
    bus.event("info", "e.x", "m", big="z" * 50000)
    assert len(read_log(bus)[0]["big"]) < 17000


def test_emit_falls_back_for_plain_loggers():
    lines = []
    diaglog.emit(lambda lv, m: lines.append((lv, m)), "info", "e.x", "shown", a=1)
    diaglog.emit(lambda lv, m: lines.append((lv, m)), "info", "e.x", "file only", ui=False)
    assert lines == [("info", "shown")]


def test_scheduler_crash_records_traceback(bus):
    class Runner:
        def start(self, *a, **kw):
            raise RuntimeError("kaboom")

    sched = Scheduler(Runner(), "* * * * *", "", bus)
    sched._safe_tick()
    ev = [e for e in read_log(bus) if e["event"] == "scheduler.error"][0]
    assert ev["level"] == "error" and "RuntimeError: kaboom" in ev["traceback"] and "Traceback" in ev["traceback"]
    assert [x["msg"] for x in bus.since(0)] == ["scheduler: kaboom"]  # UI keeps the short message


def test_lrclib_request_event_at_debug(tmp_path):
    bus = LogBus(level="debug", log_dir=str(tmp_path / "logs"))
    body = json.dumps([{"id": 1}, {"id": 2}]).encode()
    c = LrclibClient(log=bus, transport=lambda url, headers, timeout: (200, {}, body), rand=lambda: 0.0)
    c.search("Song", "Artist")
    ev = [e for e in read_log(bus) if e["event"] == "lrclib.request"][0]
    assert ev["level"] == "debug" and ev["endpoint"] == "/api/search" and ev["status"] == 200
    assert ev["result_count"] == 2 and ev["params"] == {"title": "Song", "artist": "Artist"}
    assert isinstance(ev["duration_ms"], int)
    assert bus.since(0) == []  # file only


def test_lrclib_request_event_absent_at_info(bus):
    c = LrclibClient(log=bus, transport=lambda url, headers, timeout: (404, {}, b""), rand=lambda: 0.0)
    c.get("T", "A", "Al", 100)
    assert not [e for e in read_log(bus) if e["event"] == "lrclib.request"]


def test_mutagen_failure_keeps_traceback_and_scanner_logs_it_at_debug(tmp_path, music_dir, db, monkeypatch):
    import mutagen

    def boom(path, easy=True):
        raise ValueError("bad frame")

    monkeypatch.setattr(mutagen, "File", boom)
    p = os.path.join(music_dir, "x.mp3")
    open(p, "wb").write(b"junk")
    t = read_tags(p)
    assert t.error == "unreadable: ValueError" and "ValueError: bad frame" in t.traceback

    from lrclib_web.config import Config

    bus = LogBus(level="debug", log_dir=str(tmp_path / "logs"))
    Scanner(db, Config(music_dir=music_dir, data_dir=str(tmp_path)), log=bus).run("full")
    evs = [e for e in read_log(bus) if e["event"] == "tags.error"]
    assert len(evs) == 1 and evs[0]["rel_path"] == "x.mp3" and "bad frame" in evs[0]["traceback"]
    assert evs[0]["level"] == "debug"


def test_read_events_tail_and_error_window(tmp_path):
    bus = LogBus()
    bus.attach_file(str(tmp_path / "logs"), max_bytes=2000, backups=3)
    for i in range(40):
        bus("error" if i % 10 == 0 else "info", f"m{i}", n=i)
    paths = bus.sink.paths()
    tail = diaglog.read_events(paths, 5, 10**6)
    assert [e["n"] for e in tail] == [35, 36, 37, 38, 39]
    errs = diaglog.read_events(paths, 500, 10**6, only_errors_since=0)
    assert errs and all(e["level"] == "error" for e in errs)
    assert diaglog.read_events(paths, 500, 10**6, only_errors_since=10**11) == []
    bus.sink.close()


def test_write_failure_is_retried_and_logging_resumes(bus, capsys, monkeypatch):
    sink = bus.sink

    def boom(record):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sink.handler, "emit", boom)
    bus.event("info", "e.a", "lost 1")
    bus.event("info", "e.b", "lost 2")  # inside the retry window: dropped without trying
    assert sink.broken and sink.dropped == 2 and "No space left" in sink.last_error
    monkeypatch.undo()
    bus.event("info", "e.c", "still paused")  # disk is fine again, but the retry time has not come
    assert sink.broken and sink.dropped == 3
    sink._retry_at = 0  # retry time reached
    bus.event("info", "e.d", "back")
    assert not sink.broken and sink.broken_since is None
    assert [e["event"] for e in read_log(bus)][-1] == "e.d"
    out = capsys.readouterr().out
    assert out.count("cannot write") == 1 and "writable again; 3 event(s) lost" in out
    assert sink.state()["dropped_events"] == 3


def test_concurrent_failing_writes_are_all_counted_and_never_reported_as_recovered(bus, capsys, monkeypatch):
    import threading
    import time

    sink = bus.sink

    def boom(record):
        time.sleep(0.002)  # another writer reaches this point while the first one checks its result
        raise OSError(28, "No space left on device")

    on_error = sink._on_error

    def slow_on_error(*a, **kw):
        on_error(*a, **kw)
        time.sleep(0.001)  # widen the window between recording a failure and checking it

    monkeypatch.setattr(sink.handler, "emit", boom)
    monkeypatch.setattr(sink, "_on_error", slow_on_error)
    monkeypatch.setattr(sink, "RETRY_S", 0.0)  # every write is a real retry that fails

    def worker():
        for _ in range(20):
            sink.write("{}")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sink.broken and sink.dropped == 8 * 20
    assert "writable again" not in capsys.readouterr().out
