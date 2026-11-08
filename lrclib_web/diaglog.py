"""Machine-readable diagnostics: JSON Lines event log, event helper and the /api/diag document."""

from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import os
import platform
import shutil
import sys
import time
from datetime import datetime

from . import __version__

LOG_NAME = "app.jsonl"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 4  # current + 4 rotated = 5 files x 5 MB
MAX_STR = 16000  # per string field in one event
RESERVED = ("ts", "level", "event", "msg", "version", "run_id")

DIAG_TAIL_EVENTS = 2000
DIAG_ERROR_EVENTS = 500
DIAG_ERROR_DAYS = 7
DIAG_TAIL_BYTES = 6 * 1024 * 1024
DIAG_ERROR_BYTES = 2 * 1024 * 1024


def _clip(v):
    if isinstance(v, str) and len(v) > MAX_STR:
        return v[:MAX_STR] + f"...[+{len(v) - MAX_STR} chars]"
    return v


class JsonlSink:
    """Rotating JSON Lines file. Never raises: a failing write is reported once to stdout and then ignored."""

    def __init__(self, log_dir: str, max_bytes: int = MAX_BYTES, backups: int = BACKUPS):
        self.path = os.path.join(log_dir, LOG_NAME)
        self.broken = False
        self.handler = logging.handlers.RotatingFileHandler(
            self.path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.handler.handleError = self._on_error  # type: ignore[method-assign]

    def _on_error(self, record) -> None:
        if not self.broken:
            self.broken = True
            print(f"diagnostics: cannot write {self.path}: {sys.exc_info()[1]}; file log disabled", flush=True)

    def write(self, line: str) -> None:
        if self.broken:
            return
        try:
            self.handler.handle(logging.makeLogRecord({"msg": line, "levelno": logging.INFO}))
        except Exception as e:  # noqa: BLE001 - logging must never break the app
            if not self.broken:
                self.broken = True
                print(f"diagnostics: cannot write {self.path}: {e}; file log disabled", flush=True)

    def paths(self) -> list[str]:
        """Existing log files, oldest first."""
        out = []
        for i in range(self.handler.backupCount, 0, -1):
            p = f"{self.path}.{i}"
            if os.path.isfile(p):
                out.append(p)
        if os.path.isfile(self.path):
            out.append(self.path)
        return out

    def close(self) -> None:
        try:
            self.handler.close()
        except Exception:  # noqa: BLE001
            pass


def make_line(ts: float, level: str, event: str, msg: str, run_id, fields: dict) -> str:
    obj: dict = {
        "ts": datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
        "msg": _clip(msg),
        "version": __version__,
        "run_id": run_id,
    }
    for k, v in fields.items():
        if k not in RESERVED:
            obj[k] = _clip(v)
    return json.dumps(obj, ensure_ascii=False, default=str)


def emit(log, level: str, event: str, msg: str, ui: bool = True, **fields) -> None:
    """Structured event on any log callable. A LogBus gets the event; a plain (level, msg) callable gets the text
    (only when ui=True, so file-only events stay out of simple loggers)."""
    try:
        ev = getattr(log, "event", None)
        if ev is not None:
            ev(level, event, msg, ui=ui, **fields)
        elif ui:
            log(level, msg)
    except Exception:  # noqa: BLE001 - diagnostics must never break the caller
        pass


# -- reading ------------------------------------------------------------------
def _tail_lines(path: str, n: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return list(collections.deque(f, maxlen=n))
    except OSError:
        return []


def read_events(
    paths: list[str], max_events: int, max_bytes: int, only_errors_since: float | None = None
) -> list[dict]:
    """Newest `max_events` events across the files (given oldest first), returned oldest first."""
    out: list[dict] = []
    size = 0
    for p in reversed(paths):
        lines = _tail_lines(p, max_events * 4 if only_errors_since else max_events)
        for line in reversed(lines):
            if len(out) >= max_events or size > max_bytes:
                break
            if only_errors_since is not None and '"error"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if only_errors_since is not None:
                if ev.get("level") != "error":
                    continue
                try:
                    if datetime.fromisoformat(ev["ts"]).timestamp() < only_errors_since:
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
            out.append(ev)
            size += len(line)
        if len(out) >= max_events or size > max_bytes:
            break
    out.reverse()
    return out


def _fs_type(path: str) -> str | None:
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            best, fs = "", None
            real = os.path.realpath(path)
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (real == mnt or real.startswith(mnt.rstrip("/") + "/") or mnt == "/") and len(mnt) >= len(best):
                    best, fs = mnt, parts[2]
            return fs
    except OSError:
        return None


def _dir_info(path: str) -> dict:
    info: dict = {"path": path, "exists": os.path.isdir(path)}
    if not info["exists"]:
        return info
    info["writable"] = os.access(path, os.W_OK | os.X_OK)
    try:
        u = shutil.disk_usage(path)
        info["free_bytes"], info["total_bytes"] = u.free, u.total
    except OSError as e:
        info["disk_error"] = str(e)
    if not sys.platform.startswith("win"):
        info["fs_type"] = _fs_type(path)
    return info


def _umask(cfg) -> dict:
    out: dict = {"configured": cfg.umask}
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("Umask:"):
                    out["effective"] = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return out


def _mutagen_version() -> str | None:
    try:
        import mutagen

        return getattr(mutagen, "version_string", None) or ".".join(map(str, mutagen.version))
    except Exception:  # noqa: BLE001
        return None


def _strip_url(u: str) -> str:
    """Drop userinfo and query from a URL: Plex URLs sometimes carry ?X-Plex-Token=..."""
    from urllib.parse import urlsplit, urlunsplit

    if not u:
        return u
    try:
        p = urlsplit(u)
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
        return urlunsplit((p.scheme, host, p.path, "", ""))
    except ValueError:
        return "<unparsable>"


def build_diag(app) -> dict:
    cfg, db = app.cfg, app.db
    conf = cfg.public()
    for k in ("plex_url", "lrclib_url"):
        conf[k] = _strip_url(conf.get(k, ""))
    meta = {}
    for k in ("schema_version", "scan_id", "last_scan", "job_seq", "plex_index_at"):
        meta[k] = db.get_meta(k)
    try:
        db_info: dict = {
            "path": db.path,
            "size_bytes": os.path.getsize(db.path) if os.path.exists(db.path) else None,
            "track_counts": db.status_counts(),
            "plex_pending": db.execute("SELECT COUNT(*) FROM tracks WHERE plex_pending=1").fetchone()[0],
            "plex_items": db.execute("SELECT COUNT(*) FROM plex_items").fetchone()[0],
            "meta": meta,
            "last_runs": db.get_json("last_runs", []),
        }
    except Exception as e:  # noqa: BLE001
        db_info = {"error": f"{type(e).__name__}: {e}"}
    sink = getattr(app.log, "sink", None)
    paths = sink.paths() if sink else []
    until = time.time() - DIAG_ERROR_DAYS * 86400
    try:
        status = app.status()
    except Exception as e:  # noqa: BLE001
        status = {"error": f"{type(e).__name__}: {e}"}
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app": {"version": __version__, "python": sys.version, "mutagen": _mutagen_version(),
                "platform": platform.platform()},
        "config": conf,
        "process": {
            "pid": os.getpid(),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "gid": os.getgid() if hasattr(os, "getgid") else None,
            "umask": _umask(cfg),
        },
        "dirs": {"music_dir": _dir_info(cfg.music_dir), "data_dir": _dir_info(cfg.data_dir)},
        "db": db_info,
        "status": status,
        "log_files": [{"path": p, "size_bytes": os.path.getsize(p)} for p in paths if os.path.exists(p)],
        "recent_events": read_events(paths, DIAG_TAIL_EVENTS, DIAG_TAIL_BYTES),
        "recent_errors": read_events(paths, DIAG_ERROR_EVENTS, DIAG_ERROR_BYTES, only_errors_since=until),
        "limits": {"recent_events": DIAG_TAIL_EVENTS, "recent_errors": DIAG_ERROR_EVENTS,
                   "recent_errors_days": DIAG_ERROR_DAYS},
    }


def diag_filename() -> str:
    return "lrclib-web-diag-" + time.strftime("%Y%m%d-%H%M%S") + ".json"

