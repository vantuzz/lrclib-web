"""Optional Plex metadata refresh after new sidecars (ARCHITECTURE.md §9, D8)."""

from __future__ import annotations

import posixpath
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable

from .config import Config
from .db import DB, now
from .diaglog import emit

PAGE = 1000


class PlexError(Exception):
    pass


def _transport(method: str, url: str, headers: dict[str, str], timeout: float):
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def map_path(cfg: Config, rel_path: str) -> str:
    """Container-relative track path -> Plex-side absolute path via PLEX_PATH_MAP."""
    container = posixpath.join(cfg.music_dir.replace("\\", "/"), rel_path)
    for src, dst in cfg.plex_path_map:
        if container == src or container.startswith(src.rstrip("/") + "/"):
            return dst.rstrip("/") + container[len(src.rstrip("/")):]
    return container


class Plex:
    def __init__(
        self,
        cfg: Config,
        db: DB,
        log: Callable[[str, str], None] = lambda level, msg: None,
        transport: Callable = _transport,
        stop: threading.Event | None = None,
    ):
        self.cfg, self.db, self.log, self.transport = cfg, db, log, transport
        self.stop = stop or threading.Event()
        self.section_id: str | None = cfg.plex_section_id or None
        self.ok: bool | None = None
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.plex_enabled

    def _call(self, method: str, path: str, params: dict | None = None, headers: dict | None = None) -> bytes:
        url = self.cfg.plex_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        h = {"Accept": "application/xml"}
        if self.cfg.plex_token:
            h["X-Plex-Token"] = self.cfg.plex_token
        h.update(headers or {})
        t0 = time.monotonic()
        try:
            status, body = self.transport(method, url, h, 30.0)
        except (urllib.error.URLError, OSError) as e:
            emit(self.log, "debug", "plex.request", f"{method} {path} failed: {e}", ui=False, method=method,
                 path=path, status=0, duration_ms=int((time.monotonic() - t0) * 1000), error=str(e),
                 traceback=traceback.format_exc())
            raise PlexError(f"connection failed: {getattr(e, 'reason', e)}") from None
        emit(self.log, "debug", "plex.request", f"{method} {path} -> {status}", ui=False, method=method, path=path,
             status=status, duration_ms=int((time.monotonic() - t0) * 1000), bytes=len(body or b""))
        if status == 401:
            raise PlexError("unauthorized (check PLEX_TOKEN, or the token injected by your gateway)")
        if status >= 400:
            raise PlexError(f"HTTP {status} on {path}")
        return body

    def check(self) -> bool:
        if not self.enabled:
            return False
        t0 = time.monotonic()
        try:
            self._call("GET", "/identity")
            if not self.section_id:
                root = ET.fromstring(self._call("GET", "/library/sections"))
                music = [d.get("key") for d in root.iter("Directory") if d.get("type") == "artist"]
                if len(music) != 1:
                    raise PlexError(
                        f"{len(music)} music sections found; set PLEX_SECTION_ID"
                    )
                self.section_id = music[0]
            self.ok, self.error = True, None
            emit(self.log, "info", "plex.check", "Plex: connection OK", ui=False, ok=True,
                 section_id=self.section_id, duration_ms=int((time.monotonic() - t0) * 1000))
        except (PlexError, ET.ParseError) as e:
            self.ok, self.error = False, str(e)
            emit(self.log, "warn", "plex.check", f"Plex: {e}", ok=False, error=str(e),
                 duration_ms=int((time.monotonic() - t0) * 1000), traceback=traceback.format_exc())
        return bool(self.ok)

    def build_index(self) -> int:
        start, n = 0, 0
        ts = now()
        t0 = time.monotonic()
        self.db.execute("BEGIN")
        try:
            while True:
                body = self._call(
                    "GET",
                    f"/library/sections/{self.section_id}/all",
                    {"type": 10, "X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE},
                    {"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(PAGE)},
                )
                root = ET.fromstring(body)
                tracks = list(root.iter("Track"))
                for t in tracks:
                    for part in t.iter("Part"):
                        f = part.get("file")
                        if not f:
                            continue
                        self.db.execute(
                            "INSERT OR REPLACE INTO plex_items"
                            "(file, rating_key, album_key, artist_key, section_id, indexed_at) "
                            "VALUES(?,?,?,?,?,?)",
                            (f, t.get("ratingKey"), t.get("parentRatingKey"), t.get("grandparentRatingKey"),
                             self.section_id, ts),
                        )
                        n += 1
                total = int(root.get("totalSize") or root.get("size") or 0)
                start += PAGE
                if not tracks or len(tracks) < PAGE or (total and start >= total):
                    break
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.set_meta("plex_index_at", str(ts))
        emit(self.log, "info", "plex.index", f"Plex index built: {n} file(s)", ui=False, files=n,
             duration_ms=int((time.monotonic() - t0) * 1000))
        return n

    def _lookup(self, plex_file: str):
        return self.db.execute(
            "SELECT album_key, artist_key FROM plex_items WHERE file=?", (plex_file,)
        ).fetchone()

    def refresh_pending(self) -> dict[str, int]:
        """Refresh Plex items for tracks with plex_pending=1. Never raises."""
        stats = {"refreshed": 0, "unknown": 0, "failed": 0}
        if not self.enabled:
            return stats
        rows = self.db.execute("SELECT id, rel_path FROM tracks WHERE plex_pending=1").fetchall()
        if not rows:
            return stats
        try:
            if not self.ok and not self.check():
                return stats
            age = now() - int(self.db.get_meta("plex_index_at", "0"))
            reindexed = False
            if age > self.cfg.plex_index_max_age_h * 3600:
                self.log("info", f"Plex: indexed {self.build_index()} tracks")
                reindexed = True
            keys: dict[str, list[int]] = {}
            unknown: list[tuple[int, str]] = []
            for r in rows:
                pf = map_path(self.cfg, r["rel_path"])
                if len(pf) > self.cfg.warn_plex_path_len:
                    self.log("warn", f"Plex path longer than {self.cfg.warn_plex_path_len}: {pf}")
                hit = self._lookup(pf)
                if hit is None and not reindexed:
                    self.log("info", f"Plex: indexed {self.build_index()} tracks")
                    reindexed = True
                    hit = self._lookup(pf)
                if hit is None:
                    unknown.append((r["id"], pf))
                    continue
                key = hit["artist_key"] if self.cfg.plex_refresh_level == "artist" else hit["album_key"]
                if key is None:
                    unknown.append((r["id"], pf))
                    continue
                keys.setdefault(str(key), []).append(r["id"])

            for d in sorted({posixpath.dirname(pf) for _, pf in unknown}):
                try:
                    self._call("GET", f"/library/sections/{self.section_id}/refresh", {"path": d})
                    emit(self.log, "info", "plex.partial_scan", f"Plex: file not in library yet, scanning {d}",
                         dir=d)
                except PlexError as e:
                    emit(self.log, "warn", "plex.error", f"Plex: partial scan failed for {d}: {e}", op="partial_scan",
                         dir=d, error=str(e), traceback=traceback.format_exc())
            stats["unknown"] = len(unknown)

            gap = self.cfg.plex_refresh_gap_ms / 1000.0
            first = True
            for key, ids in keys.items():
                if self.stop.is_set():
                    break
                if not first and self.stop.wait(gap):
                    break
                first = False
                try:
                    self._call("PUT", f"/library/metadata/{key}/refresh")
                except PlexError as e:
                    stats["failed"] += 1
                    emit(self.log, "warn", "plex.error", f"Plex: refresh {key} failed: {e}", op="refresh", key=key,
                         error=str(e), traceback=traceback.format_exc())
                    continue
                stats["refreshed"] += 1
                self.db.execute(
                    f"UPDATE tracks SET plex_pending=0, updated_at=? WHERE id IN ({','.join('?' * len(ids))})",
                    (int(time.time()), *ids),
                )
            emit(
                self.log, "info", "plex.refresh",
                f"Plex: refreshed {stats['refreshed']} {self.cfg.plex_refresh_level}(s), "
                f"{stats['unknown']} file(s) not yet in Plex, {stats['failed']} failed",
                refresh_level=self.cfg.plex_refresh_level, pending=len(rows), **stats,
            )
        except (PlexError, ET.ParseError) as e:
            self.ok, self.error = False, str(e)
            emit(self.log, "warn", "plex.error", f"Plex: {e}; will retry next run", op="refresh_pending",
                 error=str(e), traceback=traceback.format_exc())
        return stats
