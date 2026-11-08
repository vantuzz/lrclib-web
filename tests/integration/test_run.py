import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_lrclib import FakeLrclib  # noqa: E402

from lrclib_web.config import Config  # noqa: E402
from lrclib_web.db import DB  # noqa: E402
from lrclib_web.jobs import JobRunner, LogBus  # noqa: E402
from lrclib_web.web import App, serve  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

SYNCED = "[00:01.00]hello\r\n[00:02.00]world"


def rec(name, **kw):
    base = {"id": abs(hash(name)) % 10000 + 1, "trackName": name, "artistName": "Test Artist",
            "albumName": "Test Album", "duration": 5.0, "instrumental": False,
            "syncedLyrics": None, "plainLyrics": None}
    base.update(kw)
    return base


SCRIPT = {
    ("/api/get", "Synced Song"): (200, rec("Synced Song", syncedLyrics=SYNCED, plainLyrics="hello\nworld")),
    ("/api/get", "Plain Song"): (200, rec("Plain Song", plainLyrics="just words")),
    ("/api/get", "Instro"): (200, rec("Instro", instrumental=True)),
    ("/api/search", "Nothing"): (200, []),
}


def make_audio(path, title, ext="mp3"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "5",
           "-metadata", f"title={title}", "-metadata", "artist=Test Artist", "-metadata", "album=Test Album", path]
    subprocess.run(cmd, check=True, timeout=30)


@pytest.fixture
def library(music_dir):
    make_audio(os.path.join(music_dir, "Test Artist", "Test Album", "01 Synced.mp3"), "Synced Song")
    make_audio(os.path.join(music_dir, "Test Artist", "Test Album", "02 Plain.mp3"), "Plain Song")
    make_audio(os.path.join(music_dir, "Test Artist", "Test Album", "03 Instro.mp3"), "Instro")
    make_audio(os.path.join(music_dir, "Test Artist", "Test Album", "04 Nothing.flac"), "Nothing", "flac")
    return music_dir


def make_cfg(music_dir, data_dir, url, **kw):
    return Config(music_dir=music_dir, data_dir=data_dir, lrclib_url=url, request_gap_ms=200,
                  request_jitter_ms=0, bind="127.0.0.1", port=0, **kw)


def run_job(runner, jtype="run", mode="quick"):
    runner.start(jtype, mode)
    runner.join(60)
    assert not runner.busy
    return runner.snapshot()


def test_full_run_and_rerun(library, data_dir):
    with FakeLrclib(SCRIPT) as fake:
        cfg = make_cfg(library, data_dir, fake.url)
        db = DB(os.path.join(data_dir, "s.sqlite3"))
        runner = JobRunner(cfg, db, LogBus())
        snap = run_job(runner)
        assert snap["state"] == "done", snap["summary"]

        d = os.path.join(library, "Test Artist", "Test Album")
        lrc = os.path.join(d, "01 Synced.lrc")
        assert open(lrc, "rb").read() == b"[00:01.00]hello\n[00:02.00]world\n"
        assert not os.path.exists(os.path.join(d, "01 Synced.txt"))
        assert open(os.path.join(d, "02 Plain.txt"), "rb").read() == b"just words\n"
        assert not os.path.exists(os.path.join(d, "02 Plain.lrc"))
        assert not os.path.exists(os.path.join(d, "03 Instro.lrc"))
        assert not os.path.exists(os.path.join(d, "03 Instro.txt"))
        assert not os.path.exists(os.path.join(d, "04 Nothing.lrc"))
        assert not os.path.exists(os.path.join(d, "04 Nothing.txt"))
        assert not [f for f in os.listdir(d) if f.startswith(".")]

        st = {r["rel_path"].split("/")[-1]: r["status"] for r in db.execute("SELECT rel_path, status FROM tracks")}
        assert st == {"01 Synced.mp3": "synced", "02 Plain.mp3": "plain", "03 Instro.mp3": "instrumental",
                      "04 Nothing.flac": "not_found"}
        c = snap["counters"]
        assert (c["synced"], c["plain"], c["instrumental"], c["not_found"]) == (1, 1, 1, 1)

        # pacing (server-side arrival times carry connect jitter, hence 0.15) and per-track request budget
        ts = [r["t"] for r in fake.requests]
        gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
        assert all(g >= 0.15 for g in gaps), gaps
        for name in ("Synced Song", "Plain Song", "Instro", "Nothing"):
            assert 1 <= fake.count(name) <= 3
        assert all("lrclib-web/" in r["headers"].get("User-Agent", "") for r in fake.requests)

        # second run: nothing new written, no requests (all terminal / not due)
        n_req = len(fake.requests)
        mtime = os.stat(lrc).st_mtime_ns
        snap2 = run_job(runner)
        assert snap2["state"] == "done"
        assert len(fake.requests) == n_req
        assert os.stat(lrc).st_mtime_ns == mtime
        assert snap2["counters"]["synced"] == 0
        db.close()


def test_existing_lrc_never_overwritten(library, data_dir):
    d = os.path.join(library, "Test Artist", "Test Album")
    with open(os.path.join(d, "01 Synced.lrc"), "wb") as f:
        f.write(b"mine\n")
    with FakeLrclib(SCRIPT) as fake:
        db = DB(os.path.join(data_dir, "s.sqlite3"))
        runner = JobRunner(make_cfg(library, data_dir, fake.url), db, LogBus())
        run_job(runner)
        assert fake.count("Synced Song") == 0
        assert open(os.path.join(d, "01 Synced.lrc"), "rb").read() == b"mine\n"
        db.close()


# -- web API ----------------------------------------------------------------
class Server:
    def __init__(self, app):
        self.srv = serve(app)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.t.start()

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


CSRF = {"X-Requested-With": "lrclib-web"}


def build(cfg, data_dir):
    db = DB(os.path.join(data_dir, "w.sqlite3"))
    log = LogBus()
    runner = JobRunner(cfg, db, log)
    return db, runner, Server(App(cfg, db, log, runner, None, None))


def test_web_api_jobs_and_csrf(library, data_dir):
    with FakeLrclib(SCRIPT, delay=0.6) as fake:
        cfg = make_cfg(library, data_dir, fake.url)
        db, runner, srv = build(cfg, data_dir)
        try:
            code, raw = srv.req("GET", "/healthz")
            assert code == 200 and json.loads(raw)["status"] == "ok"
            code, raw = srv.req("GET", "/api/status")
            st = json.loads(raw)
            assert code == 200 and st["busy"] is False and "library" in st and st["job"] is None
            code, raw = srv.req("GET", "/api/config")
            assert code == 200 and json.loads(raw)["music_dir"] == library

            code, _ = srv.req("POST", "/api/jobs", body={"type": "run"})
            assert code == 403
            code, raw = srv.req("POST", "/api/jobs", CSRF, {"type": "run", "mode": "quick"})
            assert code == 202 and "job_id" in json.loads(raw)
            code, _ = srv.req("POST", "/api/jobs", CSRF, {"type": "run"})
            assert code == 409
            code, raw = srv.req("GET", "/api/status")
            assert json.loads(raw)["busy"] is True

            code, _ = srv.req("POST", "/api/jobs/current/stop", CSRF)
            assert code == 202
            runner.join(30)
            assert not runner.busy
            code, raw = srv.req("GET", "/api/status")
            st = json.loads(raw)
            assert st["job"]["state"] in ("stopped", "done")
            code, _ = srv.req("POST", "/api/jobs", CSRF, {"type": "bogus"})
            assert code == 400
            code, raw = srv.req("GET", "/api/log")
            assert code == 200 and json.loads(raw)["lines"]
        finally:
            runner.stop()
            runner.join(30)
            srv.close()
            db.close()


def test_cross_origin_post_rejected(library, data_dir):
    with FakeLrclib(SCRIPT) as fake:
        db, runner, srv = build(make_cfg(library, data_dir, fake.url), data_dir)
        try:
            h = {**CSRF, "Origin": "http://evil.example"}
            code, _ = srv.req("POST", "/api/jobs", h, {"type": "scan"})
            assert code == 403
        finally:
            srv.close()
            db.close()


def test_basic_auth(library, data_dir):
    with FakeLrclib(SCRIPT) as fake:
        cfg = make_cfg(library, data_dir, fake.url, auth_user="bob", auth_pass="s3cret")
        db, runner, srv = build(cfg, data_dir)
        try:
            assert srv.req("GET", "/healthz")[0] == 200  # health stays open
            assert srv.req("GET", "/api/status")[0] == 401
            bad = base64.b64encode(b"bob:wrong").decode()
            assert srv.req("GET", "/api/status", {"Authorization": f"Basic {bad}"})[0] == 401
            good = base64.b64encode(b"bob:s3cret").decode()
            assert srv.req("GET", "/api/status", {"Authorization": f"Basic {good}"})[0] == 200
            assert srv.req("POST", "/api/jobs", {**CSRF}, {"type": "scan"})[0] == 401
            code, raw = srv.req("GET", "/api/config", {"Authorization": f"Basic {good}"})
            assert json.loads(raw)["auth_pass"] == "***"
        finally:
            srv.close()
            db.close()
