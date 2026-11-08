"""Library scan (ARCHITECTURE.md §4).

quick: a directory whose mtime is unchanged is not listed again; its known subdirectories (from the DB) are
still visited.
full: every directory is listed and every audio file stat'ed; tags are re-read only when (mtime_ns, size) changed.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Config
from .db import DB, now
from .diaglog import emit
from .tags import Tags, read_tags

COMMIT_EVERY = 500
MTIME_SETTLE_NS = 2_000_000_000  # a dir modified this recently may still gain files within the same timestamp tick


LRC_REMOVED_AGAIN = ".lrc removed again, not re-downloaded"


def _now_ns() -> int:
    return time.time_ns()


@dataclass
class ScanResult:
    dirs_listed: int = 0
    dirs_skipped: int = 0
    files_seen: int = 0
    new: int = 0
    changed: int = 0
    untagged: int = 0
    missing: int = 0
    restored: int = 0
    purged: int = 0
    stopped: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        s = (
            f"dirs listed {self.dirs_listed}, skipped {self.dirs_skipped}; files {self.files_seen}; "
            f"new {self.new}, changed {self.changed}, untagged {self.untagged}, missing {self.missing}, "
            f"restored {self.restored}, purged {self.purged}"
        )
        return s + (" (stopped)" if self.stopped else "")


def _skip_name(name: str) -> bool:
    return name.startswith(".") or name.startswith("@")


def _join(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def _abs(root: str, rel: str) -> str:
    return os.path.join(root, *rel.split("/")) if rel else root


def _sidecars(names: set[str], stem: str) -> tuple[bool, bool]:
    """Exact-case stem, extension case-insensitive."""
    lrc = txt = False
    plen = len(stem) + 1
    for n in names:
        if len(n) == plen + 3 and n.startswith(stem) and n[len(stem)] == ".":
            ext = n[plen:].lower()
            if ext == "lrc":
                lrc = True
            elif ext == "txt":
                txt = True
    return lrc, txt


class Scanner:
    def __init__(
        self,
        db: DB,
        cfg: Config,
        log: Callable[[str, str], None] = lambda level, msg: None,
        progress: Callable[[int, int, str], None] = lambda done, total, cur: None,
        stop: threading.Event | None = None,
        tag_reader: Callable[[str], Tags] = read_tags,
    ):
        self.db, self.cfg, self.log, self.progress = db, cfg, log, progress
        self.stop = stop or threading.Event()
        self.read_tags = tag_reader
        self._pending_writes = 0

    # -- helpers ---------------------------------------------------------
    def _tick(self) -> None:
        self._pending_writes += 1
        if self._pending_writes >= COMMIT_EVERY:
            self.db.execute("COMMIT")
            self.db.execute("BEGIN")
            self._pending_writes = 0

    def _status_for(self, tags: Tags, lrc: bool) -> tuple[str, str | None]:
        if not tags.ok:
            return "untagged", tags.error or "missing title/artist tag"
        if lrc:
            return "has_lrc", ".lrc exists"
        return "pending", None

    # -- main ------------------------------------------------------------
    def run(self, mode: str = "quick") -> ScanResult:
        res = ScanResult()
        root = self.cfg.music_dir
        db = self.db
        scan_id = int(db.get_meta("scan_id", "0")) + 1
        db.set_meta("scan_id", str(scan_id))
        ts = now()

        try:
            root_entries = [e for e in os.scandir(root) if not _skip_name(e.name)]
        except OSError as e:
            res.errors.append(f"cannot list {root}: {e}")
            emit(self.log, "error", "scan.aborted", f"scan aborted: cannot list music dir {root}: {e}", root=root,
                 reason=str(e))
            return res
        root_empty = not root_entries

        known_dirs = {
            r["rel_path"]: (r["id"], r["mtime_ns"])
            for r in db.execute("SELECT id, rel_path, mtime_ns FROM dirs")
        }
        children: dict[str, list[str]] = {}
        for rel in known_dirs:
            if rel:
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                children.setdefault(parent, []).append(rel)
        total = max(1, len(known_dirs))
        done = 0
        missing_candidates: list[int] = []
        incomplete = False  # a dir could not be stat'ed/listed (not ENOENT): never mark anything missing
        exts = self.cfg.audio_extensions

        def vanished(rel: str) -> None:
            """A known dir is gone: clear its stored mtime so quick mode re-lists it when it reappears."""
            if rel in known_dirs:
                db.execute("UPDATE dirs SET mtime_ns=NULL WHERE id=?", (known_dirs[rel][0],))

        db.execute("BEGIN")
        try:
            stack = [""]
            while stack:
                if self.stop.is_set():
                    res.stopped = True
                    break
                rel = stack.pop()
                path = _abs(root, rel)
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    vanished(rel)  # removed directory; its tracks become missing below
                    continue
                except OSError as e:
                    res.errors.append(f"{rel or '/'}: {e}")
                    incomplete = True
                    continue
                known = known_dirs.get(rel)
                if known is None:
                    cur = db.execute(
                        "INSERT INTO dirs(rel_path, mtime_ns, seen_scan_id) VALUES(?,?,?)",
                        (rel, None, scan_id),
                    )
                    dir_id = cur.lastrowid
                else:
                    dir_id = known[0]
                    db.execute(
                        "UPDATE dirs SET seen_scan_id=? WHERE id=?", (scan_id, dir_id)
                    )
                done += 1
                self.progress(done, max(total, done), rel or "/")

                if mode == "quick" and known is not None and known[1] == st.st_mtime_ns:
                    res.dirs_skipped += 1
                    # tracks in this dir count as seen; descend into known subdirs
                    stack.extend(sorted(children.get(rel, []), reverse=True))
                    continue

                # list directory
                try:
                    entries = sorted(os.scandir(path), key=lambda e: e.name)
                except FileNotFoundError:
                    vanished(rel)
                    continue
                except OSError as e:
                    res.errors.append(f"{rel or '/'}: {e}")
                    incomplete = True
                    continue
                res.dirs_listed += 1
                names = {e.name for e in entries}
                subdirs: list[str] = []
                audio: list[os.DirEntry] = []
                dir_ok = True
                for e in entries:
                    if _skip_name(e.name):
                        continue
                    try:
                        if e.is_symlink() and not self.cfg.follow_symlinks:
                            continue
                        if e.is_dir():
                            subdirs.append(_join(rel, e.name))
                        elif e.is_file():
                            ext = e.name.rsplit(".", 1)[-1].lower() if "." in e.name else ""
                            if ext in exts:
                                audio.append(e)
                    except OSError as err:
                        res.errors.append(f"{_join(rel, e.name)}: {err}")
                        incomplete = dir_ok = False
                        continue

                rows = {
                    r["rel_path"]: r
                    for r in db.execute("SELECT * FROM tracks WHERE dir_id=?", (dir_id,))
                }
                seen: set[str] = set()
                for e in audio:
                    if self.stop.is_set():
                        res.stopped = True
                        break
                    trel = _join(rel, e.name)
                    seen.add(trel)
                    res.files_seen += 1
                    self._file(e, trel, dir_id, names, rows.get(trel), res, ts)
                if res.stopped:
                    break
                missing_candidates.extend(
                    r["id"] for k, r in rows.items() if k not in seen and r["status"] != "missing"
                )
                # st was taken before listing; skip storing it for incomplete listings and for dirs modified
                # within the last 2 s (a same-tick addition would otherwise never be noticed by quick mode)
                recent = _now_ns() - st.st_mtime_ns < MTIME_SETTLE_NS
                db.execute(
                    "UPDATE dirs SET mtime_ns=? WHERE id=?",
                    (st.st_mtime_ns if dir_ok and not recent else None, dir_id),
                )
                # unknown-to-DB children appear only via listing; known ones too
                stack.extend(sorted(subdirs, reverse=True))

            if not res.stopped:
                # known dirs not reached this scan (removed, or below a removed dir): forget their mtime
                db.execute("UPDATE dirs SET mtime_ns=NULL WHERE seen_scan_id IS NOT ?", (scan_id,))
                # tracks of dirs not seen in this scan
                for r in db.execute(
                    "SELECT t.id FROM tracks t JOIN dirs d ON d.id=t.dir_id "
                    "WHERE d.seen_scan_id IS NOT ? AND t.status!='missing'",
                    (scan_id,),
                ):
                    missing_candidates.append(r[0])
                if incomplete:
                    emit(self.log, "warn", "scan.incomplete", "scan incomplete, missing-marking skipped")
                else:
                    self._mark_missing(missing_candidates, root_empty, res, ts)
                self._purge(res, ts, scan_id)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        if not res.stopped:
            db.set_meta("last_scan", f"{ts}:{mode}")
        return res

    def _read_tags(self, path: str, rel: str) -> Tags:
        tags = self.read_tags(path)
        if tags.error:
            # one debug line per unreadable / untagged file; the traceback only exists for mutagen failures
            emit(self.log, "debug", "tags.error", f"tags {rel}: {tags.error}", ui=False, rel_path=rel,
                 reason=tags.error, traceback=getattr(tags, "traceback", None))
        return tags

    def _file(self, e, trel, dir_id, names, row, res: ScanResult, ts: int) -> None:
        db = self.db
        try:
            st = e.stat()
        except OSError as err:
            res.errors.append(f"{trel}: {err}")
            return
        stem = e.name.rsplit(".", 1)[0]
        lrc, txt = _sidecars(names, stem)
        if row is None:
            tags = self._read_tags(e.path, trel)
            status, reason = self._status_for(tags, lrc)
            db.execute(
                "INSERT INTO tracks(rel_path, dir_id, mtime_ns, size, title, artist, album_artist, album, "
                "duration_ms, tags_ok, lrc_exists, txt_exists, status, reason, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trel, dir_id, st.st_mtime_ns, st.st_size, tags.title, tags.artist, tags.album_artist,
                 tags.album, tags.duration_ms, int(tags.ok), int(lrc), int(txt), status, reason, ts, ts),
            )
            res.new += 1
            if status == "untagged":
                res.untagged += 1
            self._tick()
            return

        changed = (row["mtime_ns"], row["size"]) != (st.st_mtime_ns, st.st_size)
        if changed:
            tags = self._read_tags(e.path, trel)
            status, reason = self._status_for(tags, lrc)
            db.update_track(
                row["id"], mtime_ns=st.st_mtime_ns, size=st.st_size, title=tags.title, artist=tags.artist,
                album_artist=tags.album_artist, album=tags.album, duration_ms=tags.duration_ms,
                tags_ok=int(tags.ok), lrc_exists=int(lrc), txt_exists=int(txt), status=status, reason=reason,
                attempts=0, next_check_at=None, lrclib_id=None, lrclib_kind=None, remote_sha256=None,
                missing_since=None, prev_status=None, lrc_resets=0,
            )
            res.changed += 1
            if status == "untagged":
                res.untagged += 1
            self._tick()
            return

        upd: dict = {}
        status = row["status"]
        if status == "missing":
            status = row["prev_status"] or "pending"
            upd.update(status=status, missing_since=None, prev_status=None)
            res.restored += 1
        if bool(row["lrc_exists"]) != lrc or bool(row["txt_exists"]) != txt:
            upd.update(lrc_exists=int(lrc), txt_exists=int(txt))
        if lrc and status in ("pending", "plain", "not_found", "rejected", "error"):
            upd.update(status="has_lrc", reason=".lrc exists")
        elif not lrc and status == "has_lrc":
            upd.update(status="pending", reason=None, next_check_at=None)
        elif not lrc and status == "synced" and row["written_by_us"]:  # D18: our .lrc was removed
            if not row["lrc_resets"]:
                upd.update(status="pending", reason=".lrc removed, re-downloading once", next_check_at=None,
                           lrc_resets=1)
            elif row["reason"] != LRC_REMOVED_AGAIN:
                upd.update(reason=LRC_REMOVED_AGAIN)
        if upd:
            db.update_track(row["id"], **upd)
            self._tick()

    def _mark_missing(self, ids: list[int], root_empty: bool, res: ScanResult, ts: int) -> None:
        if not ids:
            return
        known = self.db.execute("SELECT COUNT(*) FROM tracks WHERE status!='missing'").fetchone()[0]
        pct = 100.0 * len(ids) / max(1, known)
        if root_empty or pct > self.cfg.missing_abort_pct:
            msg = (
                f"missing guard: {len(ids)} of {known} tracks ({pct:.0f}%) not found"
                + (" and music dir is empty" if root_empty else "")
                + "; nothing marked missing (unmounted share? "
                + f"raise MISSING_ABORT_PCT={self.cfg.missing_abort_pct} if intended)"
            )
            res.errors.append(msg)
            emit(self.log, "error", "scan.missing_guard", msg, missing=len(ids), known=known, percent=round(pct, 1))
            return
        for i in ids:
            self.db.execute(
                "UPDATE tracks SET prev_status=status, status='missing', missing_since=?, updated_at=? "
                "WHERE id=? AND status!='missing'",
                (ts, ts, i),
            )
        res.missing += len(ids)

    def _purge(self, res: ScanResult, ts: int, scan_id: int) -> None:
        days = self.cfg.purge_missing_days
        if days <= 0:
            return
        cur = self.db.execute(
            "DELETE FROM tracks WHERE status='missing' AND missing_since<?", (ts - days * 86400,)
        )
        res.purged += cur.rowcount
        self.db.execute(
            "DELETE FROM dirs WHERE seen_scan_id IS NOT ? AND id NOT IN (SELECT dir_id FROM tracks)",
            (scan_id,),
        )
