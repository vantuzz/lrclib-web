"""Job runner, download step, progress and log bus (ARCHITECTURE.md §8)."""

from __future__ import annotations

import collections
import hashlib
import os
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable

from .config import Config
from .db import DB, now
from .diaglog import BACKUPS, MAX_BYTES, JsonlSink, emit, make_line
from .lrclib import LrclibClient, LrclibUnavailable, Stopped
from .matcher import Match, Matcher
from .plex import Plex
from .scanner import Scanner

try:  # POSIX only; on Windows the download lock is skipped
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .writer import (
    PermanentWriteError,
    UnsafePathError,
    WriteSkipped,
    remove_sidecar,
    sha256_text,
    sidecar_path,
    write_sidecar,
)

DAY = 86400
PERMANENT_RETRY_DAYS = 90  # "permanent" errors are retried after this long, not never
INSTRUMENTAL_RECHECK_DAYS = 90  # D18
NOTFOUND_CAP_DAYS = 180  # D19: 14 -> 28 -> 56 -> 180


def notfound_delay_days(base: int, n: int) -> int:
    """D19: n-th consecutive not_found/rejected (n >= 1) -> base, 2*base, 4*base, then max(180, 4*base)."""
    if base <= 0:
        return base
    return base * 2 ** (n - 1) if n <= 3 else max(NOTFOUND_CAP_DAYS, base * 4)
COUNTER_KEYS = ("synced", "plain", "not_found", "rejected", "instrumental", "untagged", "error", "skipped")


class LogBus:
    """In-memory ring buffer of log lines with monotonically increasing ids.

    Human-readable lines go to the UI ring buffer and stdout. Every event (also the structured, file-only ones from
    `event()`) is additionally written as one JSON object per line to <log_dir>/app.jsonl once `attach_file` ran.
    """

    def __init__(self, size: int = 2000, level: str = "info", log_dir: str | None = None):
        self.lines: collections.deque = collections.deque(maxlen=size)
        self.next_id = 1
        self.cond = threading.Condition()
        self.debug = level == "debug"
        self.run_id: str | None = None
        self.sink: JsonlSink | None = None
        if log_dir:
            self.attach_file(log_dir)

    def attach_file(self, log_dir: str, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> bool:
        """Start writing JSONL events into log_dir. Returns False (and runs without a file) if that is impossible."""
        try:
            os.makedirs(log_dir, exist_ok=True)
            self.sink = JsonlSink(log_dir, max_bytes, backups)
            return True
        except OSError as e:
            self.sink = None
            print(f"diagnostics: cannot use log dir {log_dir}: {e.strerror or e}; running without file log",
                  flush=True)
            return False

    def _write_file(self, ts: float, level: str, event: str, msg: str, fields: dict) -> None:
        if self.sink is None:
            return
        try:
            self.sink.write(make_line(ts, level, event, msg, self.run_id, fields))
        except Exception as e:  # noqa: BLE001 - e.g. unserialisable field; never break the caller
            print(f"diagnostics: cannot format event {event}: {e}", flush=True)

    def __call__(self, level: str, msg: str, event: str = "log", **fields) -> None:
        self._log(level, msg, event, True, fields)

    def event(self, level: str, event: str, msg: str, ui: bool = False, **fields) -> None:
        """Structured event. ui=False keeps it out of the UI log and stdout (file only)."""
        self._log(level, msg, event, ui, fields)

    def _log(self, level: str, msg: str, event: str, ui: bool, fields: dict) -> None:
        if level == "debug" and not self.debug:
            return
        ts = time.time()
        if ui:
            with self.cond:
                item = {"id": self.next_id, "ts": ts, "level": level, "msg": msg}
                self.next_id += 1
                self.lines.append(item)
                self.cond.notify_all()
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level:5} {msg}", file=sys.stdout, flush=True)
        self._write_file(ts, level, event, msg, fields)

    def since(self, last_id: int, limit: int = 500) -> list[dict]:
        with self.cond:
            out = [x for x in self.lines if x["id"] > last_id]
        return out[-limit:]

    def wait(self, last_id: int, timeout: float) -> None:
        with self.cond:
            if self.next_id - 1 <= last_id:
                self.cond.wait(timeout)


class Busy(Exception):
    pass


class DownloadLocked(Exception):
    pass


class JobRunner:
    def __init__(
        self,
        cfg: Config,
        db: DB,
        log: LogBus,
        client_factory: Callable[..., LrclibClient] | None = None,
        plex: Plex | None = None,
        scanner_factory: Callable[..., Scanner] | None = None,
    ):
        self.cfg, self.db, self.log = cfg, db, log
        self.client_factory = client_factory or self._default_client
        self.scanner_factory = scanner_factory or Scanner
        self.plex = plex
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.state: dict | None = None
        self.version = 0  # bumped on every state change (SSE)
        self._job_seq = int(db.get_meta("job_seq", "0"))
        self._recover()

    # -- setup -----------------------------------------------------------
    def _default_client(self, **kw) -> LrclibClient:
        return LrclibClient(
            self.cfg.lrclib_url, self.cfg.lrclib_app_id, self.cfg.request_gap_ms, self.cfg.request_jitter_ms, **kw
        )

    def _recover(self) -> None:
        cur = self.db.get_json("current_job")
        if cur:
            emit(self.log, "warn", "job.interrupted",
                 f"job #{cur.get('id')} ({cur.get('type')}) was interrupted by a restart",
                 job_id=cur.get("id"), type=cur.get("type"), prev_run_id=cur.get("run_id"))
            self._remember({**cur, "state": "interrupted", "finished_at": now()})
            self.db.execute("DELETE FROM meta WHERE key='current_job'")

    def _remember(self, job: dict) -> None:
        runs = self.db.get_json("last_runs", [])
        runs.insert(0, {k: job.get(k) for k in ("id", "run_id", "type", "mode", "trigger", "state", "started_at",
                                                 "finished_at", "summary", "counters")})
        self.db.set_json("last_runs", runs[:5])

    # -- public ----------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self) -> dict | None:
        if self.state is None:
            return None
        s = dict(self.state)
        s["counters"] = dict(s["counters"])
        return s

    def start(self, jtype: str, mode: str = "quick", trigger: str = "manual") -> int:
        if jtype not in ("scan", "download", "run"):
            raise ValueError("type must be scan|download|run")
        if mode not in ("quick", "full"):
            raise ValueError("mode must be quick|full")
        with self.lock:
            if self.busy:
                raise Busy()
            self._job_seq += 1
            self.db.set_meta("job_seq", str(self._job_seq))
            self.stop_event = threading.Event()
            self.state = {
                "id": self._job_seq, "run_id": uuid.uuid4().hex[:12], "type": jtype,
                "mode": mode if jtype != "download" else None,
                "trigger": trigger, "state": "running", "phase": "starting", "done": 0, "total": 0,
                "current": "", "counters": dict.fromkeys(COUNTER_KEYS, 0), "cooldown_until": None,
                "breaker_state": "closed", "started_at": now(), "finished_at": None, "eta_s": None,
                "summary": None,
            }
            self.db.set_json("current_job", self.state)
            self._bump()
            self.thread = threading.Thread(target=self._run, name=f"job-{self._job_seq}", daemon=True)
            self.thread.start()
            return self._job_seq

    def stop(self) -> None:
        if self.busy:
            self.stop_event.set()
            self.log("info", "stop requested")

    def join(self, timeout: float | None = None) -> None:
        if self.thread:
            self.thread.join(timeout)

    # -- internals -------------------------------------------------------
    def _bump(self, **upd) -> None:
        if self.state is not None:
            self.state.update(upd)
        self.version += 1

    def _run(self) -> None:
        st = self.state
        assert st is not None
        summary: list[str] = []
        final = "done"
        t_start = time.monotonic()
        self._set_run_id(st.get("run_id"))
        try:
            mode = f" {st['mode']}" if st["mode"] else ""
            emit(self.log, "info", "job.start", f"job #{st['id']} {st['type']}{mode} started ({st['trigger']})",
                 job_id=st["id"], type=st["type"], mode=st["mode"], trigger=st["trigger"])
            if st["type"] in ("scan", "run"):
                res = self._scan(st["mode"])
                summary.append("scan: " + res.summary())
                if res.stopped:
                    final = "stopped"
            if st["type"] in ("download", "run") and final == "done":
                final = self._download(summary)
                if self.plex and self.plex.enabled and final == "done":
                    self._bump(phase="plex", current="")
                    stats = self.plex.refresh_pending()
                    summary.append(f"plex: {stats['refreshed']} refreshed, {stats['unknown']} not yet in Plex")
        except Exception as e:  # noqa: BLE001
            final = "failed"
            summary.append(f"failed: {e}")
            emit(self.log, "error", "job.error", f"job failed: {type(e).__name__}: {e}",
                 job_id=st["id"], error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc())
        finally:
            self._bump(state=final, phase="finished", finished_at=now(), summary="; ".join(summary),
                       current="", cooldown_until=None, eta_s=None)
            self._remember(self.state)
            self.db.execute("DELETE FROM meta WHERE key='current_job'")
            emit(self.log, "info" if final == "done" else "warn", "job.finish",
                 f"job #{st['id']} {final}: {'; '.join(summary)}", job_id=st["id"], type=st["type"], state=final,
                 duration_s=round(time.monotonic() - t_start, 3), counters=dict(st["counters"]),
                 summary="; ".join(summary))
            self._set_run_id(None)
            self.db.close()

    def _set_run_id(self, run_id: str | None) -> None:
        if hasattr(self.log, "run_id"):
            self.log.run_id = run_id

    def _scan(self, mode: str):
        self._bump(phase=f"scan ({mode})", done=0, total=0)
        last = [0.0]

        def progress(done, total, cur):
            if time.monotonic() - last[0] > 0.5:
                last[0] = time.monotonic()
                self._bump(done=done, total=total, current=cur)

        sc = self.scanner_factory(self.db, self.cfg, log=self.log, progress=progress, stop=self.stop_event)
        t0 = time.monotonic()
        res = sc.run(mode)
        for e in res.errors[:20]:
            self.log("warn", f"scan: {e}")
        self.state["counters"]["untagged"] += res.untagged
        emit(self.log, "info", "scan.summary", "scan: " + res.summary(), mode=mode,
             duration_s=round(time.monotonic() - t0, 3), errors=res.errors[:20], error_count=len(res.errors),
             **{k: getattr(res, k) for k in ("dirs_listed", "dirs_skipped", "files_seen", "new", "changed",
                                             "untagged", "missing", "restored", "purged", "stopped")})
        return res

    def _cooldown(self, until):
        self._bump(cooldown_until=until)

    def _lock_download(self):
        """Exclusive <data_dir>/download.lock (POSIX flock); returns the open file or None when unsupported."""
        if fcntl is None:
            return None
        f = open(os.path.join(self.cfg.data_dir, "download.lock"), "a+")  # noqa: SIM115
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            f.close()
            raise DownloadLocked() from e
        except BaseException:
            f.close()
            raise
        return f

    def _download(self, summary: list[str]) -> str:
        try:
            lock = self._lock_download()
        except DownloadLocked:
            emit(self.log, "error", "download.locked", "another lrclib-web process is downloading")
            summary.append("download: another lrclib-web process is downloading")
            return "failed"
        try:
            return self._download_locked(summary)
        finally:
            if lock is not None:
                lock.close()  # closing releases the flock

    def _download_locked(self, summary: list[str]) -> str:
        cfg, db = self.cfg, self.db
        tracks = db.due_tracks(now())
        total = len(tracks)
        self._bump(phase="download", done=0, total=total, current="")
        emit(self.log, "info", "download.start", f"download: {total} track(s) due", due=total)
        client = self.client_factory(stop=self.stop_event, log=self.log, on_cooldown=self._cooldown)
        matcher = Matcher(client, cfg.search_for_synced)
        c = self.state["counters"]
        t0 = time.monotonic()
        budget = cfg.run_max_minutes * 60
        state = "done"
        for i, row in enumerate(tracks):
            if self.stop_event.is_set():
                state = "stopped"
                break
            if budget > 0 and time.monotonic() - t0 >= budget:  # D17: clean stop, next run continues
                left = total - i
                emit(self.log, "warn", "download.budget",
                     f"download: RUN_MAX_MINUTES={cfg.run_max_minutes} reached, {left} track(s) left", left=left)
                summary.append(f"download: run budget reached, {left} left for the next run")
                break
            label = f"{row['artist']} — {row['title']}"
            self._bump(done=i, current=label)
            try:
                m = matcher.match(row["title"], row["artist"], row["album"], row["duration_ms"])
            except Stopped:
                state = "stopped"
                break
            except LrclibUnavailable:
                emit(self.log, "error", "lrclib.unavailable", "LRCLIB unavailable, stopping the download",
                     ui=False, requests=client.requests)
                self._bump(breaker_state="open")
                summary.append("download: LRCLIB unavailable, stopped early")
                state = "failed"
                break
            self._decision(row, m, matcher)
            self._apply(row, m, c, label)
            elapsed = time.monotonic() - t0
            per = elapsed / (i + 1)
            self._bump(done=i + 1, eta_s=int(per * (total - i - 1)))
            if i % 100 == 0:
                db.set_json("current_job", self.state)
        emit(self.log, "info", "download.summary", f"download {state}", ui=False, state=state,
             counters=dict(c), requests=client.requests, duration_s=round(time.monotonic() - t0, 3))
        summary.append(
            "download: " + ", ".join(f"{k} {c[k]}" for k in COUNTER_KEYS if c[k]) + f"; {client.requests} request(s)"
        )
        return state

    def _decision(self, row, m: Match, matcher) -> None:
        rec = m.record or {}
        diff = None
        try:
            if rec.get("duration") is not None and row["duration_ms"]:
                diff = round(float(rec["duration"]) - row["duration_ms"] / 1000.0, 3)
        except (TypeError, ValueError):
            pass
        stats = getattr(matcher, "last_stats", None) or {}
        emit(self.log, "warn" if m.kind == "error" else "info", "match.decision",
             f"match {row['rel_path']}: {m.kind}" + (f" ({m.reason})" if m.reason else ""), ui=False,
             rel_path=row["rel_path"], kind=m.kind, reason=m.reason, record_id=rec.get("id"),
             duration_diff_s=diff, track_duration_s=round((row["duration_ms"] or 0) / 1000.0, 3),
             candidates=stats.get("candidates"), survivors=stats.get("survivors"), requests=m.requests,
             transient=m.transient or None)

    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.cfg.music_dir).replace(os.sep, "/")
        except ValueError:
            return path

    def _apply(self, row, m: Match, c: dict, label: str) -> None:
        cfg, db = self.cfg, self.db
        ts = now()
        audio = os.path.join(cfg.music_dir, *row["rel_path"].split("/"))
        upd: dict = {"checked_at": ts, "attempts": 0}
        if m.record:
            upd["lrclib_id"] = m.record.get("id")
            upd["lrclib_kind"] = m.kind if m.kind in ("synced", "plain", "instrumental") else None

        def recheck(days: int):
            return ts + days * DAY if days > 0 else ts + 3650 * DAY

        def vanished() -> bool:
            """Audio file disappeared since the scan: do not write a sidecar, mark the track missing."""
            if os.path.exists(audio):
                return False
            db.update_track(row["id"], status="missing", prev_status=row["status"], missing_since=ts)
            c["skipped"] += 1
            emit(self.log, "warn", "write.skipped", f"audio vanished {label}", rel_path=row["rel_path"],
                 reason="audio vanished")
            return True

        if m.kind in ("not_found", "rejected"):  # D19: escalate re-check while the miss repeats
            n = (row["attempts"] or 0) + 1 if row["status"] in ("not_found", "rejected") else 1
            upd.update(attempts=n, next_check_at=recheck(notfound_delay_days(cfg.notfound_recheck_days, n)))

        if m.kind == "synced":
            text = m.lyrics or ""
            target = sidecar_path(audio, "lrc")
            if vanished():
                return
            try:
                sha = write_sidecar(target, text, cfg.music_dir)
            except WriteSkipped:
                upd.update(status="has_lrc", reason=".lrc exists", lrc_exists=1)
                c["skipped"] += 1
                emit(self.log, "info", "write.skipped", f"skip  {label}: .lrc already exists",
                     rel_path=self._rel(target), reason=".lrc exists")
            except PermanentWriteError as e:
                upd.update(status="error", reason=str(e), next_check_at=ts + PERMANENT_RETRY_DAYS * DAY)
                c["error"] += 1
                emit(self.log, "error", "write.error", f"error {label}: {e}", rel_path=self._rel(target),
                     reason=str(e), permanent=True)
            except OSError as e:
                upd.update(status="error", reason=f"write: {e.strerror or e}", next_check_at=ts + DAY)
                c["error"] += 1
                emit(self.log, "error", "write.error", f"error {label}: cannot write .lrc: {e}",
                     rel_path=self._rel(target), reason=str(e), errno=e.errno, traceback=traceback.format_exc())
            else:
                upd.update(status="synced", reason=None, lrc_exists=1, written_file=os.path.basename(target),
                           written_sha256=sha, written_by_us=1, written_at=ts, remote_sha256=sha256_text(text),
                           plex_pending=1 if cfg.plex_enabled else 0, next_check_at=None)
                c["synced"] += 1
                emit(self.log, "ok", "write.ok", f"ok    {label}: synced → .lrc", rel_path=self._rel(target),
                     kind="synced", bytes=len(text.encode("utf-8")), lrclib_id=(m.record or {}).get("id"))
                if (row["txt_exists"] and cfg.txt_on_synced == "delete" and row["written_by_us"]
                        and (row["written_file"] or "").lower().endswith(".txt")):
                    txt = sidecar_path(audio, "txt")
                    try:
                        with open(txt, "rb") as f:
                            current = hashlib.sha256(f.read()).hexdigest()
                        if current == row["written_sha256"]:
                            remove_sidecar(txt, cfg.music_dir)
                            upd["txt_exists"] = 0
                            emit(self.log, "info", "write.txt_removed",
                                 f"      {label}: removed our .txt (TXT_ON_SYNCED=delete)", rel_path=self._rel(txt))
                        else:
                            emit(self.log, "info", "write.skipped", f"      {label}: txt modified locally, kept",
                                 rel_path=self._rel(txt), reason="txt modified locally")
                    except UnsafePathError as e:
                        emit(self.log, "error", "write.error", f"error {label}: {e}", rel_path=self._rel(txt),
                             reason=str(e), traceback=traceback.format_exc())
                    except OSError:
                        pass
        elif m.kind == "plain":
            upd.update(status="plain", next_check_at=recheck(cfg.plain_recheck_days))
            if row["txt_exists"]:
                upd["reason"] = "txt exists"
                c["plain"] += 1
                emit(self.log, "info", "write.skipped", f"plain {label}: plain only, .txt already exists",
                     rel_path=self._rel(sidecar_path(audio, "txt")), reason="txt exists")
            else:
                target = sidecar_path(audio, "txt")
                if vanished():
                    return
                try:
                    sha = write_sidecar(target, m.lyrics or "", cfg.music_dir)
                except WriteSkipped:
                    upd.update(reason="txt exists", txt_exists=1)
                    c["plain"] += 1
                    emit(self.log, "info", "write.skipped", f"plain {label}: .txt exists", ui=False,
                         rel_path=self._rel(target), reason="txt exists")
                except OSError as e:
                    upd.update(status="error", reason=f"write: {e}", next_check_at=ts + DAY)
                    c["error"] += 1
                    emit(self.log, "error", "write.error", f"error {label}: cannot write .txt: {e}",
                         rel_path=self._rel(target), reason=str(e), errno=e.errno, traceback=traceback.format_exc())
                else:
                    upd.update(reason="plain only", txt_exists=1, written_file=os.path.basename(target),
                               written_sha256=sha, written_by_us=1, written_at=ts,
                               remote_sha256=sha256_text(m.lyrics or ""),
                               plex_pending=1 if cfg.plex_enabled else 0)
                    c["plain"] += 1
                    emit(self.log, "ok", "write.ok", f"ok    {label}: plain only → .txt", rel_path=self._rel(target),
                         kind="plain", bytes=len((m.lyrics or "").encode("utf-8")),
                         lrclib_id=(m.record or {}).get("id"))
        elif m.kind == "instrumental":
            upd.update(status="instrumental", reason="instrumental",
                       next_check_at=ts + INSTRUMENTAL_RECHECK_DAYS * DAY)
            c["instrumental"] += 1
            self.log("info", f"instr {label}: instrumental, no file")
        elif m.kind == "not_found":
            upd.update(status="not_found", reason=m.reason or "not found")
            c["not_found"] += 1
            self.log("warn", f"miss  {label}: not found")
        elif m.kind == "rejected":
            upd.update(status="rejected", reason=m.reason)
            c["rejected"] += 1
            self.log("warn", f"rej   {label}: {m.reason}")
        else:  # error
            c["error"] += 1
            if m.transient:
                attempts = (row["attempts"] or 0) + 1
                delay = 0 if attempts < 3 else min(4, 2 ** (attempts - 3)) * DAY
                upd.update(status="error", reason=m.reason, attempts=attempts, next_check_at=ts + delay)
            else:
                upd.update(status="error", reason=m.reason, next_check_at=ts + PERMANENT_RETRY_DAYS * DAY)
            emit(self.log, "error", "match.error", f"error {label}: {m.reason}", rel_path=row["rel_path"],
                 reason=m.reason, transient=m.transient)
        db.update_track(row["id"], **upd)
