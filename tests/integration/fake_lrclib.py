"""A real HTTP server standing in for lrclib.net, with scripted per-(path, track_name) responses."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLrclib:
    def __init__(self, script: dict[tuple[str, str], tuple[int, object]], delay: float = 0.0):
        """script maps (path, track_name) -> (status, json body). Unscripted -> 404."""
        self.script = script
        self.delay = delay
        self.requests: list[dict] = []  # {"t", "path", "track", "headers"}
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(u.query)
                track = (q.get("track_name") or [""])[0]
                with outer._lock:
                    outer.requests.append({"t": time.monotonic(), "path": u.path, "track": track,
                                           "headers": dict(self.headers.items())})
                if outer.delay:
                    time.sleep(outer.delay)
                status, body = outer.script.get((u.path, track), (404, {"message": "nope"}))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def count(self, track: str | None = None) -> int:
        return len([r for r in self.requests if track is None or r["track"] == track])
