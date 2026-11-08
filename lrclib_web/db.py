"""SQLite state (ARCHITECTURE.md §3). One connection per thread; WAL mode."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS dirs(
  id INTEGER PRIMARY KEY, rel_path TEXT NOT NULL UNIQUE, mtime_ns INTEGER, seen_scan_id INTEGER);
CREATE TABLE IF NOT EXISTS tracks(
  id INTEGER PRIMARY KEY,
  rel_path TEXT NOT NULL UNIQUE,
  dir_id INTEGER NOT NULL REFERENCES dirs(id),
  mtime_ns INTEGER, size INTEGER,
  title TEXT, artist TEXT, album_artist TEXT, album TEXT, duration_ms INTEGER,
  tags_ok INTEGER NOT NULL DEFAULT 0,
  lrc_exists INTEGER NOT NULL DEFAULT 0, txt_exists INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  prev_status TEXT,
  reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  checked_at INTEGER, next_check_at INTEGER,
  lrclib_id INTEGER, lrclib_kind TEXT,
  remote_sha256 TEXT,
  written_file TEXT, written_sha256 TEXT, written_by_us INTEGER NOT NULL DEFAULT 0, written_at INTEGER,
  plex_pending INTEGER NOT NULL DEFAULT 0,
  lrc_resets INTEGER NOT NULL DEFAULT 0,
  missing_since INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tracks_due ON tracks(status, next_check_at);
CREATE INDEX IF NOT EXISTS ix_tracks_dir ON tracks(dir_id);
CREATE INDEX IF NOT EXISTS ix_tracks_plex ON tracks(plex_pending) WHERE plex_pending=1;
CREATE TABLE IF NOT EXISTS plex_items(
  file TEXT PRIMARY KEY, rating_key INTEGER, album_key INTEGER, artist_key INTEGER,
  section_id INTEGER, indexed_at INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

DUE_STATUSES = ("pending", "plain", "not_found", "rejected", "error", "instrumental")


def now() -> int:
    return int(time.time())


def data_dir_problem(path: str) -> str | None:
    """Return a one-line, actionable error if `path` cannot hold the database, else None."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".write-test-{os.getpid()}")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return None
    except OSError as e:
        who = f"uid {os.getuid()}, gid {os.getgid()}" if hasattr(os, "getuid") else "the container user"
        return (f"DATA_DIR {path} is not writable ({e.strerror or e}); the process runs as {who}. "
                f"Fix on the host: chown -R 99:100 <host path mounted at {path}>")


class DB:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        c = self.conn
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(tracks)")}
        if "lrc_resets" not in cols:  # D18: added after 0.1.0 drafts
            c.execute("ALTER TABLE tracks ADD COLUMN lrc_resets INTEGER NOT NULL DEFAULT 0")
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def get_meta(self, key: str, default=None):
        r = self.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_json(self, key: str, default=None):
        v = self.get_meta(key)
        if v is None:
            return default
        try:
            return json.loads(v)
        except ValueError:
            return default

    def set_json(self, key: str, value) -> None:
        self.set_meta(key, json.dumps(value, ensure_ascii=False))

    def status_counts(self) -> dict[str, int]:
        rows = self.execute(
            "SELECT status, COUNT(*) FROM tracks GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def writable(self) -> bool:
        try:
            self.set_meta("healthz", str(now()))
            return True
        except sqlite3.Error:
            return False

    def due_tracks(self, ts: int) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(DUE_STATUSES))
        q = (
            f"SELECT * FROM tracks WHERE status IN ({marks}) AND (next_check_at IS NULL OR next_check_at<=?) "
            "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'error' THEN 1 WHEN 'plain' THEN 2 "
            "WHEN 'instrumental' THEN 4 ELSE 3 END, rel_path"
        )
        return self.execute(q, (*DUE_STATUSES, ts)).fetchall()

    def update_track(self, track_id: int, **fields) -> None:
        fields["updated_at"] = now()
        cols = ",".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE tracks SET {cols} WHERE id=?", (*fields.values(), track_id)
        )

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
