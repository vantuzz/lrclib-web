"""HTTP API, SSE and static UI on stdlib ThreadingHTTPServer (ARCHITECTURE.md §10)."""

from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .config import Config
from .db import DB
from .diaglog import build_diag, diag_filename, emit
from .jobs import Busy, JobRunner, LogBus
from .plex import Plex
from .scheduler import Scheduler

CLIENT_LOG_MAX = 16 * 1024  # POST /api/client-log body cap
CLIENT_LOG_RATE = (30, 600.0)  # at most 30 accepted reports per 10 minutes, whatever the client does


class RateLimit:
    """Sliding-window counter shared by all request threads; counts what it refused."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: list[float] = []
        self.refused = 0
        self.lock = threading.Lock()

    def take(self) -> tuple[bool, int]:
        """(allowed, refused since the last allowed call)."""
        now = time.monotonic()
        with self.lock:
            self.hits = [t for t in self.hits if now - t < self.window]
            if len(self.hits) >= self.limit:
                self.refused += 1
                return False, 0
            self.hits.append(now)
            refused, self.refused = self.refused, 0
            return True, refused
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class App:
    def __init__(self, cfg: Config, db: DB, log: LogBus, runner: JobRunner, plex: Plex | None,
                 scheduler: Scheduler | None):
        self.cfg, self.db, self.log, self.runner, self.plex, self.scheduler = cfg, db, log, runner, plex, scheduler
        self.client_log_limit = RateLimit(*CLIENT_LOG_RATE)

    def status(self) -> dict:
        counts = self.db.status_counts()
        last_scan = self.db.get_meta("last_scan")
        ls = None
        if last_scan:
            ts, _, mode = last_scan.partition(":")
            ls = {"at": int(ts), "mode": mode}
        return {
            "version": __version__,
            "tz": time.strftime("%Z"),
            "job": self.runner.snapshot(),
            "busy": self.runner.busy,
            "library": {"total": sum(v for k, v in counts.items() if k != "missing"), "counts": counts,
                        "last_scan": ls},
            "last_runs": self.db.get_json("last_runs", []),
            "plex": {"enabled": bool(self.plex and self.plex.enabled), "ok": self.plex.ok if self.plex else None,
                     "error": self.plex.error if self.plex else None},
            "schedule": self.scheduler.next_runs() if self.scheduler else [],
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"lrclib-web/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet access log
            if app.cfg.log_level == "debug":
                app.log("debug", "http " + fmt % args)

        # -- helpers -------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            if code >= 500:
                emit(app.log, "warn", "http.5xx", f"HTTP {code} on {self.command} {self.path.split('?', 1)[0]}",
                     ui=False, method=self.command, path=self.path.split("?", 1)[0], status=code)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _authorized(self) -> bool:
            if not app.cfg.auth_enabled:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                except (ValueError, UnicodeDecodeError):
                    return False
                ok_u = hmac.compare_digest(user.encode(), app.cfg.auth_user.encode())
                ok_p = hmac.compare_digest(pw.encode(), app.cfg.auth_pass.encode())
                return ok_u and ok_p
            return False

        def _deny(self) -> None:
            self._send(401, b"authentication required\n", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="lrclib-web", charset="UTF-8"'})

        def _csrf_ok(self) -> bool:
            if self.headers.get("X-Requested-With") != "lrclib-web":
                return False
            origin = self.headers.get("Origin")
            if origin:
                host = urllib.parse.urlsplit(origin).netloc
                return host == self.headers.get("Host")
            return True

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                return {}
            try:
                v = json.loads(self.rfile.read(n).decode("utf-8"))
                return v if isinstance(v, dict) else {}
            except (ValueError, UnicodeDecodeError):
                return {}

        def _guard(self, fn) -> None:
            """Run a route; an unexpected crash is logged with its traceback and answered with a JSON 500."""
            try:
                fn()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            except Exception as e:  # noqa: BLE001
                emit(app.log, "error", "http.error", f"{self.command} {self.path.split('?', 1)[0]} crashed: "
                     f"{type(e).__name__}: {e}", ui=False, method=self.command, path=self.path.split("?", 1)[0],
                     error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc())
                try:
                    self._json(500, {"error": "internal error"})
                except Exception:  # noqa: BLE001 - headers may already be on the wire
                    self.close_connection = True

        # -- routes --------------------------------------------------------
        def do_HEAD(self):
            self._guard(self._get)

        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def _get(self):
            url = urllib.parse.urlsplit(self.path)
            path, qs = url.path, urllib.parse.parse_qs(url.query)
            if path == "/healthz":
                ok = app.db.writable()
                return self._json(200 if ok else 503, {"status": "ok" if ok else "error", "db": ok})
            if not self._authorized():
                return self._deny()
            if path == "/":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/status":
                return self._json(200, app.status())
            if path == "/api/config":
                return self._json(200, app.cfg.public())
            if path == "/api/log":
                since = int((qs.get("since") or ["0"])[0] or 0)
                limit = min(2000, int((qs.get("limit") or ["500"])[0] or 500))
                return self._json(200, {"lines": app.log.since(since, limit)})
            if path == "/api/diag":
                body = json.dumps(build_diag(app), ensure_ascii=False, default=str).encode("utf-8")
                return self._send(200, body, "application/json; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{diag_filename()}"'})
            if path == "/api/events":
                return self._events()
            self._json(404, {"error": "not found"})

        def _client_log(self):
            """Browser-side error report -> ui.error event in the JSONL log."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > CLIENT_LOG_MAX:
                self.close_connection = True  # the oversized body stays unread
                return self._send(413, b'{"error": "report too large"}', "application/json; charset=utf-8",
                                  {"Connection": "close"})
            if n <= 0:
                return self._json(400, {"error": "empty body"})
            try:
                b = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "invalid JSON"})
            if not isinstance(b, dict):
                return self._json(400, {"error": "expected an object"})

            def s(key: str, limit: int) -> str:
                v = b.get(key)
                return v[:limit] if isinstance(v, str) else ("" if v is None else str(v)[:limit])

            state = b.get("state")
            client = {"kind": s("kind", 64), "message": s("message", 2000), "stack": s("stack", 8000),
                      "url": s("url", 500), "state": state if isinstance(state, dict) else None,
                      "user_agent": (self.headers.get("User-Agent") or "")[:300]}
            allowed, refused = app.client_log_limit.take()
            if not allowed:
                return self._json(429, {"error": "too many reports"})
            if refused:
                client["refused_before"] = refused  # reports dropped by the rate limit since the last one
            emit(app.log, "error", "ui.error", f"UI {client['kind'] or 'error'}: {client['message']}"[:300],
                 ui=False, client=client)
            self._json(200, {"ok": True})

        def _post(self):
            path = urllib.parse.urlsplit(self.path).path
            if not self._authorized():
                return self._deny()
            if not self._csrf_ok():
                return self._json(403, {"error": "missing X-Requested-With or cross-origin request"})
            if path == "/api/client-log":
                return self._client_log()
            if path == "/api/jobs":
                b = self._body()
                try:
                    job_id = app.runner.start(str(b.get("type", "")), str(b.get("mode") or "quick"), "manual")
                except Busy:
                    return self._json(409, {"error": "a job is already running"})
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(202, {"job_id": job_id})
            if path == "/api/jobs/current/stop":
                app.runner.stop()
                return self._json(202, {"ok": True})
            self._json(404, {"error": "not found"})

        def _static(self, name: str):
            name = os.path.normpath(name).replace("\\", "/")
            if name.startswith("..") or name.startswith("/"):
                return self._json(404, {"error": "not found"})
            p = os.path.join(STATIC_DIR, name)
            ext = os.path.splitext(p)[1]
            if ext not in STATIC_TYPES or not os.path.isfile(p):
                return self._json(404, {"error": "not found"})
            with open(p, "rb") as f:
                body = f.read()
            self._send(200, body, STATIC_TYPES[ext])

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            try:
                last_id = int(self.headers.get("Last-Event-ID") or 0)
            except ValueError:
                last_id = 0
            if last_id == 0:  # new client: send the tail of the log
                last_id = max(0, app.log.next_id - 1 - 300)
            sent_version = -1
            last_status = 0.0
            try:
                self.wfile.write(b"retry: 3000\n\n")
                while True:
                    lines = app.log.since(last_id, 500)
                    chunks = []
                    for ln in lines:
                        chunks.append(f"id: {ln['id']}\nevent: log\ndata: {json.dumps(ln, ensure_ascii=False)}\n\n")
                        last_id = ln["id"]
                    t = time.monotonic()
                    running = app.runner.busy
                    if (app.runner.version != sent_version or (running and t - last_status >= 1.0)
                            or t - last_status >= 15):
                        sent_version = app.runner.version
                        last_status = t
                        chunks.append(f"event: status\ndata: {json.dumps(app.status(), ensure_ascii=False)}\n\n")
                    if chunks:
                        self.wfile.write("".join(chunks).encode("utf-8"))
                        self.wfile.flush()
                    app.log.wait(last_id, 1.0)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

    return Handler


def serve(app: App) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((app.cfg.bind, app.cfg.port), make_handler(app))
    srv.daemon_threads = True
    return srv
