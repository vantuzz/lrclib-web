"""Diagnostics end to end: JSONL events from a real job run, /api/client-log and /api/diag over HTTP.

No ffmpeg needed: audio files are dummies and tags come from an injected reader."""

import base64
import http.client
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_lrclib import FakeLrclib  # noqa: E402

from lrclib_web.config import Config  # noqa: E402
from lrclib_web.db import DB  # noqa: E402
from lrclib_web.jobs import JobRunner, LogBus  # noqa: E402
from lrclib_web.scanner import Scanner  # noqa: E402
from lrclib_web.tags import Tags  # noqa: E402
from lrclib_web.web import App, serve  # noqa: E402

CSRF = {"X-Requested-With": "lrclib-web"}
SYNCED = "[00:01.00]hello\n[00:02.00]world"


def rec(name, **kw):
    base = {"id": abs(hash(name)) % 10000 + 1, "trackName": name, "artistName": "Test Artist",
            "albumName": "Test Album", "duration": 5.0, "instrumental": False,
            "syncedLyrics": None, "plainLyrics": None}
    base.update(kw)
    return base


SCRIPT = {
    ("/api/get", "Synced Song"): (200, rec("Synced Song", syncedLyrics=SYNCED, plainLyrics="hello")),
    ("/api/get", "Plain Song"): (200, rec("Plain Song", plainLyrics="just words")),
    ("/api/search", "Nothing"): (200, []),
}


def fake_tags(path):
    name = os.path.basename(path).rsplit(".", 1)[0]
    return Tags(title=name, artist="Test Artist", album="Test Album", duration_ms=5000)


def scanner_factory(db, cfg, **kw):
    return Scanner(db, cfg, tag_reader=fake_tags, **kw)


@pytest.fixture
def library(music_dir):
    d = os.path.join(music_dir, "Test Artist", "Test Album")
    os.makedirs(d)
    for n in ("Synced Song", "Plain Song", "Nothing"):
        open(os.path.join(d, n + ".mp3"), "wb").write(b"\0" * 16)
    return music_dir


def make_cfg(music_dir, data_dir, url="http://127.0.0.1:9", **kw):
    return Config(music_dir=music_dir, data_dir=data_dir, lrclib_url=url, request_gap_ms=200, request_jitter_ms=0,
                  bind="127.0.0.1", port=0, **kw)


def events(data_dir):
    with open(os.path.join(data_dir, "logs", "app.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_job_run_writes_correlated_events(library, data_dir):
    with FakeLrclib(SCRIPT) as fake:
        cfg = make_cfg(library, data_dir, fake.url, log_level="debug")
        db = DB(os.path.join(data_dir, "s.sqlite3"))
        log = LogBus(level="debug", log_dir=os.path.join(data_dir, "logs"))
        runner = JobRunner(cfg, db, log, scanner_factory=scanner_factory)
        runner.start("run", "quick")
        runner.join(60)
        assert not runner.busy
        snap = runner.snapshot()
        assert snap["state"] == "done", snap["summary"]
    evs = events(data_dir)
    by = {}
    for e in evs:
        by.setdefault(e["event"], []).append(e)

    start, finish = by["job.start"][0], by["job.finish"][0]
    assert start["run_id"] and start["run_id"] == finish["run_id"] == snap["run_id"]
    assert finish["state"] == "done" and finish["duration_s"] >= 0 and finish["counters"]["synced"] == 1
    assert log.run_id is None  # cleared after the run

    decisions = {e["rel_path"].rsplit("/", 1)[-1]: e for e in by["match.decision"]}
    assert set(decisions) == {"Synced Song.mp3", "Plain Song.mp3", "Nothing.mp3"}
    assert decisions["Synced Song.mp3"]["kind"] == "synced" and decisions["Synced Song.mp3"]["record_id"]
    assert decisions["Synced Song.mp3"]["duration_diff_s"] == 0
    assert decisions["Plain Song.mp3"]["kind"] == "plain"
    assert decisions["Nothing.mp3"]["kind"] == "not_found" and decisions["Nothing.mp3"]["candidates"] == 0
    assert all(d["run_id"] == start["run_id"] for d in decisions.values())

    oks = {e["rel_path"]: e["kind"] for e in by["write.ok"]}
    assert oks == {"Test Artist/Test Album/Synced Song.lrc": "synced",
                   "Test Artist/Test Album/Plain Song.txt": "plain"}
    assert by["scan.summary"][0]["new"] == 3
    assert by["lrclib.request"] and by["lrclib.request"][0]["endpoint"] in ("/api/get", "/api/search")
    assert not any("lyrics" in json.dumps(e).lower() and "hello" in json.dumps(e) for e in evs)  # no lyrics text
    db.close()


def test_write_error_event_has_traceback(library, data_dir, monkeypatch):
    from lrclib_web import jobs

    def deny(*a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(jobs, "write_sidecar", deny)
    with FakeLrclib(SCRIPT) as fake:
        db = DB(os.path.join(data_dir, "s.sqlite3"))
        log = LogBus(log_dir=os.path.join(data_dir, "logs"))
        runner = JobRunner(make_cfg(library, data_dir, fake.url), db, log, scanner_factory=scanner_factory)
        runner.start("run", "quick")
        runner.join(60)
    errs = [e for e in events(data_dir) if e["event"] == "write.error"]
    assert errs and errs[0]["errno"] == 13 and "PermissionError" in errs[0]["traceback"]
    assert errs[0]["rel_path"].endswith((".lrc", ".txt"))
    db.close()


class Server:
    def __init__(self, app):
        self.srv = serve(app)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def req(self, method, path, headers=None, body=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, out, hdrs

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def web(library, data_dir):
    made = []

    def build(**cfg_kw):
        cfg = make_cfg(library, data_dir, **cfg_kw)
        db = DB(os.path.join(data_dir, "w.sqlite3"))
        log = LogBus(log_dir=os.path.join(data_dir, "logs"))
        runner = JobRunner(cfg, db, log, scanner_factory=scanner_factory)
        srv = Server(App(cfg, db, log, runner, None, None))
        made.append((srv, db))
        return srv, log, db

    yield build
    for srv, db in made:
        srv.close()
        db.close()


def test_client_log_accepts_and_writes_ui_error(web, data_dir):
    srv, log, _ = web()
    report = {"kind": "window.onerror", "message": "x is not defined", "stack": "at app.js:1", "url": "http://h/",
              "state": {"lastLogId": 7, "viewport": {"w": 375, "h": 600}}}
    code, raw, _ = srv.req("POST", "/api/client-log", CSRF, report)
    assert code == 200 and json.loads(raw) == {"ok": True}
    ev = [e for e in events(data_dir) if e["event"] == "ui.error"][0]
    assert ev["level"] == "error" and ev["client"]["kind"] == "window.onerror"
    assert ev["client"]["state"]["lastLogId"] == 7 and ev["client"]["message"] == "x is not defined"
    assert log.since(0) == []  # not in the UI log


def test_client_log_rejects_oversized_and_bad_bodies(web):
    srv, _, _ = web()
    big = json.dumps({"kind": "x", "message": "y" * 20000}).encode()
    code, _, _ = srv.req("POST", "/api/client-log", CSRF, raw=big)
    assert code == 413
    assert srv.req("POST", "/api/client-log", CSRF, raw=b"not json")[0] == 400
    assert srv.req("POST", "/api/client-log", CSRF, raw=b"[1]")[0] == 400
    assert srv.req("POST", "/api/client-log", {})[0] in (400, 403)
    assert srv.req("POST", "/api/client-log", {}, {"kind": "x"})[0] == 403  # CSRF header required


def test_client_log_requires_auth(web, data_dir):
    srv, _, _ = web(auth_user="bob", auth_pass="s3cret")
    assert srv.req("POST", "/api/client-log", CSRF, {"kind": "x", "message": "m"})[0] == 401
    good = base64.b64encode(b"bob:s3cret").decode()
    h = {**CSRF, "Authorization": f"Basic {good}"}
    assert srv.req("POST", "/api/client-log", h, {"kind": "x", "message": "m"})[0] == 200
    assert srv.req("GET", "/api/diag")[0] == 401
    assert srv.req("GET", "/api/diag", {"Authorization": f"Basic {good}"})[0] == 200


def test_diag_document(web, data_dir, library):
    srv, log, db = web(auth_user="bob", auth_pass="s3cret", plex_token="PLEXSECRET",
                       lrclib_app_id="APPIDSECRET", plex_url="http://plex:32400/?X-Plex-Token=URLSECRET")
    log("error", "something broke", event="x.broke", traceback="Traceback...\nValueError: nope")
    log("info", "fine")
    srv.req("POST", "/api/client-log", {**CSRF, "Authorization": "Basic " + base64.b64encode(b"bob:s3cret").decode()},
            {"kind": "fetch", "message": "HTTP 500"})
    auth = {"Authorization": "Basic " + base64.b64encode(b"bob:s3cret").decode()}
    code, raw, hdrs = srv.req("GET", "/api/diag", auth)
    assert code == 200
    assert hdrs["Content-Disposition"].startswith('attachment; filename="lrclib-web-diag-')
    assert hdrs["Content-Disposition"].endswith('.json"')
    for secret in (b"PLEXSECRET", b"APPIDSECRET", b"s3cret", b"URLSECRET"):
        assert secret not in raw
    d = json.loads(raw)
    assert {"app", "config", "process", "dirs", "db", "status", "recent_events", "recent_errors"} <= set(d)
    assert d["app"]["python"] and d["app"]["platform"] and d["app"]["mutagen"]
    assert d["config"]["plex_token"] == "***" and d["config"]["auth_pass"] == "***"
    assert d["config"]["plex_url"] == "http://plex:32400/"
    assert d["dirs"]["music_dir"]["exists"] and d["dirs"]["music_dir"]["writable"]
    assert d["dirs"]["data_dir"]["free_bytes"] > 0
    assert d["process"]["umask"]["configured"] == "022"
    assert "track_counts" in d["db"] and d["db"]["plex_pending"] == 0 and d["db"]["meta"]["schema_version"] == "1"
    assert "last_runs" in d["db"] and "library" in d["status"]
    assert any(e["event"] == "x.broke" for e in d["recent_events"])
    errs = [e["event"] for e in d["recent_errors"]]
    assert "x.broke" in errs and "ui.error" in errs and all(e["level"] == "error" for e in d["recent_errors"])


def test_handler_crash_is_logged_with_traceback(web, data_dir, monkeypatch):
    srv, _, _ = web()
    from lrclib_web import web as webmod

    def boom(app):
        raise RuntimeError("diag exploded")

    monkeypatch.setattr(webmod, "build_diag", boom)
    code, raw, _ = srv.req("GET", "/api/diag")
    assert code == 500 and json.loads(raw) == {"error": "internal error"}
    ev = [e for e in events(data_dir) if e["event"] == "http.error"][0]
    assert ev["path"] == "/api/diag" and "RuntimeError: diag exploded" in ev["traceback"]
    assert any(e["event"] == "http.5xx" and e["status"] == 500 for e in events(data_dir))
