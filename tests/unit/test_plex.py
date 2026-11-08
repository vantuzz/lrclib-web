import urllib.parse

import pytest

from lrclib_web import plex as plex_mod
from lrclib_web.config import Config
from lrclib_web.db import now
from lrclib_web.plex import Plex, map_path

SECTIONS = (
    '<MediaContainer><Directory key="1" type="artist" title="Music"/>'
    '<Directory key="2" type="movie" title="Films"/></MediaContainer>'
)

# (file, ratingKey, albumKey, artistKey)
TRACKS = [
    ("/data/music/A/one.mp3", 11, 21, 31),
    ("/data/music/A/two.mp3", 12, 21, 31),
    ("/data/music/B/three.mp3", 13, 22, 32),
]


def tracks_xml(rows, total):
    parts = "".join(
        f'<Track ratingKey="{rk}" parentRatingKey="{al}" grandparentRatingKey="{ar}">'
        f'<Media><Part file="{f}"/></Media></Track>'
        for f, rk, al, ar in rows
    )
    return f'<MediaContainer size="{len(rows)}" totalSize="{total}">{parts}</MediaContainer>'.encode()


class FakePlexServer:
    def __init__(self, tracks=TRACKS, token="tok"):
        self.tracks = tracks
        self.token = token
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url))
        assert headers.get("X-Plex-Token") == self.token
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/identity":
            return 200, b"<MediaContainer/>"
        if u.path == "/library/sections":
            return 200, SECTIONS.encode()
        if u.path == "/library/sections/1/all":
            start = int(q["X-Plex-Container-Start"][0])
            size = int(q["X-Plex-Container-Size"][0])
            return 200, tracks_xml(self.tracks[start:start + size], len(self.tracks))
        if method == "PUT" and u.path.startswith("/library/metadata/"):
            return 200, b""
        if u.path == "/library/sections/1/refresh":
            return 200, b""
        return 404, b""

    def puts(self):
        return [u for m, u in self.calls if m == "PUT"]


@pytest.fixture
def cfg(music_dir, data_dir):
    return Config(music_dir="/music", data_dir=data_dir, plex_url="http://plex:32400", plex_token="tok",
                  plex_path_map=[("/music", "/data/music")], plex_refresh_gap_ms=0)


def add_tracks(db, rels):
    ts = now()
    db.execute("INSERT INTO dirs(rel_path) VALUES('')")
    for r in rels:
        db.execute(
            "INSERT INTO tracks(rel_path, dir_id, status, plex_pending, created_at, updated_at) "
            "VALUES(?,1,'synced',1,?,?)", (r, ts, ts))


def test_map_path():
    cfg = Config(music_dir="/music", plex_path_map=[("/music/pop", "/p"), ("/music", "/data/m")])
    assert map_path(cfg, "pop/x.mp3") == "/p/x.mp3"
    assert map_path(cfg, "rock/x.mp3") == "/data/m/rock/x.mp3"
    assert map_path(Config(music_dir="/music"), "a/b.mp3") == "/music/a/b.mp3"


def test_check_discovers_section(db, cfg):
    srv = FakePlexServer()
    p = Plex(cfg, db, transport=srv)
    assert p.check() and p.section_id == "1" and p.ok


def test_token_optional_for_gateway(db, cfg):
    cfg.plex_token = ""  # D22: a gateway in front of Plex injects X-Plex-Token
    assert cfg.plex_enabled
    srv = FakePlexServer(token=None)
    p = Plex(cfg, db, transport=srv)
    assert p.check() and p.section_id == "1"


def test_plex_disabled_without_url(data_dir):
    assert not Config(data_dir=data_dir, plex_token="tok").plex_enabled


def test_check_unauthorized(db, cfg):
    p = Plex(cfg, db, transport=lambda m, u, h, t: (401, b""))
    assert not p.check() and "unauthorized" in p.error


def test_build_index_paginated(db, cfg, monkeypatch):
    monkeypatch.setattr(plex_mod, "PAGE", 2)
    srv = FakePlexServer()
    p = Plex(cfg, db, transport=srv)
    p.check()
    assert p.build_index() == 3
    row = db.execute("SELECT * FROM plex_items WHERE file=?", ("/data/music/B/three.mp3",)).fetchone()
    assert (row["rating_key"], row["album_key"], row["artist_key"]) == (13, 22, 32)
    assert len([c for c in srv.calls if c[1].startswith("http://plex:32400/library/sections/1/all")]) == 2


def test_refresh_one_per_artist_and_clears_pending(db, cfg):
    add_tracks(db, ["A/one.mp3", "A/two.mp3", "B/three.mp3"])
    srv = FakePlexServer()
    p = Plex(cfg, db, transport=srv)
    stats = p.refresh_pending()
    assert stats == {"refreshed": 2, "unknown": 0, "failed": 0}
    assert sorted(srv.puts()) == ["http://plex:32400/library/metadata/31/refresh",
                                  "http://plex:32400/library/metadata/32/refresh"]
    assert db.execute("SELECT COUNT(*) FROM tracks WHERE plex_pending=1").fetchone()[0] == 0


def test_refresh_album_level(db, cfg):
    cfg.plex_refresh_level = "album"
    add_tracks(db, ["A/one.mp3", "A/two.mp3"])
    srv = FakePlexServer()
    Plex(cfg, db, transport=srv).refresh_pending()
    assert srv.puts() == ["http://plex:32400/library/metadata/21/refresh"]


def test_unknown_file_triggers_partial_scan_and_stays_pending(db, cfg):
    add_tracks(db, ["A/one.mp3", "C/new.mp3"])
    srv = FakePlexServer()
    stats = Plex(cfg, db, transport=srv).refresh_pending()
    assert stats["refreshed"] == 1 and stats["unknown"] == 1
    assert any("/library/sections/1/refresh" in u and "path=" in u for _, u in srv.calls)
    left = db.execute("SELECT rel_path FROM tracks WHERE plex_pending=1").fetchall()
    assert [r[0] for r in left] == ["C/new.mp3"]


def test_refresh_failure_never_raises(db, cfg):
    add_tracks(db, ["A/one.mp3"])
    p = Plex(cfg, db, transport=lambda m, u, h, t: (500, b""))
    stats = p.refresh_pending()
    assert stats["refreshed"] == 0
    assert db.execute("SELECT COUNT(*) FROM tracks WHERE plex_pending=1").fetchone()[0] == 1


def test_disabled_plex_noop(db):
    p = Plex(Config(), db, transport=lambda *a: pytest.fail("no calls expected"))
    assert p.refresh_pending() == {"refreshed": 0, "unknown": 0, "failed": 0}
