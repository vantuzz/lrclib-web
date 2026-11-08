"""Track -> LRCLIB record matching (ARCHITECTURE.md §5, DECISIONS D5 + D12)."""

from __future__ import annotations

from dataclasses import dataclass

from .lrclib import LrclibClient, Response
from .normalize import has_explicit_feat, norm, request_artist, same_artist, split_title

DURATION_TOLERANCE_S = 2.0


@dataclass
class Match:
    kind: str  # synced | plain | instrumental | not_found | rejected | error
    record: dict | None = None
    reason: str | None = None
    transient: bool = False
    requests: int = 0

    @property
    def lyrics(self) -> str | None:
        if not self.record:
            return None
        if self.kind == "synced":
            return self.record.get("syncedLyrics")
        if self.kind == "plain":
            return self.record.get("plainLyrics")
        return None


def record_kind(rec: dict) -> str:
    if rec.get("instrumental"):
        return "instrumental"
    if (rec.get("syncedLyrics") or "").strip():
        return "synced"
    if (rec.get("plainLyrics") or "").strip():
        return "plain"
    return "empty"


_KIND_RANK = {"synced": 0, "plain": 1, "instrumental": 2}


def filter_candidates(
    cands: list[dict], title: str, artist: str, album: str | None, duration_s: float
) -> list[dict]:
    """Apply D5 + D12 rules; returns survivors ranked best-first."""
    our_core, our_q = split_title(title)
    our_album = norm(album)
    out = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        kind = record_kind(c)
        if kind not in _KIND_RANK:
            continue
        if not same_artist(artist, c.get("artistName")):
            continue
        their_core, their_q = split_title(c.get("trackName") or c.get("name"))
        if their_core != our_core:
            continue
        if our_q ^ their_q:  # qualifier present on exactly one side
            continue
        cd = c.get("duration")
        try:
            cd = float(cd) if cd is not None else None
        except (TypeError, ValueError):
            cd = None
        if duration_s and duration_s > 0:
            if cd is None or abs(cd - duration_s) > DURATION_TOLERANCE_S:
                continue
        else:
            if not our_album or norm(c.get("albumName")) != our_album:
                continue
        out.append(c)

    def key(c: dict):
        cd = c.get("duration")
        try:
            dd = abs(float(cd) - duration_s) if duration_s and cd is not None else 0.0
        except (TypeError, ValueError):
            dd = 0.0
        return (
            _KIND_RANK[record_kind(c)],
            0 if our_album and norm(c.get("albumName")) == our_album else 1,
            dd,
            c.get("id") or 0,
        )

    out.sort(key=key)
    return out


class Matcher:
    def __init__(self, client: LrclibClient, search_for_synced: bool = False):
        self.client = client
        self.search_for_synced = search_for_synced

    @staticmethod
    def _from_record(rec: dict, requests: int) -> Match:
        kind = record_kind(rec)
        if kind == "empty":
            return Match("not_found", rec, "record has no lyrics", requests=requests)
        return Match(kind, rec, requests=requests)

    @staticmethod
    def _err(r: Response, requests: int) -> Match:
        return Match("error", reason=r.error or f"HTTP {r.status}", transient=r.transient, requests=requests)

    def match(self, title: str, artist: str, album: str | None, duration_ms: int | None) -> Match:
        duration_s = (duration_ms or 0) / 1000.0
        dur_int = int(round(duration_s)) if duration_s else 0
        n = 0
        plain_from_get: Match | None = None
        primary = request_artist(artist)

        # LRCLIB /api/get requires album_name: without an album go straight to search
        if 1 <= dur_int <= 3600 and album and album.strip():
            r = self.client.get(title, artist, album, dur_int)
            n += 1
            if r.status == 200 and isinstance(r.data, dict):
                m = self._from_record(r.data, n)
                if m.kind != "plain" or not self.search_for_synced:
                    if m.kind != "not_found":
                        return m
                else:
                    plain_from_get = m
            elif r.status not in (404, 400):  # 400 falls through to the next step
                return self._err(r, n)

            if plain_from_get is None and has_explicit_feat(artist) and primary and primary != artist:
                r = self.client.get(title, primary, album, dur_int)
                n += 1
                if r.status == 200 and isinstance(r.data, dict):
                    m = self._from_record(r.data, n)
                    if m.kind != "not_found":
                        return m
                elif r.status not in (404, 400):
                    return self._err(r, n)

        r = self.client.search(title, primary or artist)
        n += 1
        if r.status != 200:
            if plain_from_get:
                plain_from_get.requests = n
                return plain_from_get
            if r.status == 404:
                return Match("not_found", requests=n)
            return self._err(r, n)
        cands = r.data if isinstance(r.data, list) else []
        if not cands:
            if plain_from_get:
                plain_from_get.requests = n
                return plain_from_get
            return Match("not_found", reason="no search results", requests=n)
        good = filter_candidates(cands, title, artist, album, duration_s)
        if not good:
            if plain_from_get:
                plain_from_get.requests = n
                return plain_from_get
            return Match("rejected", reason=f"{len(cands)} candidates, none matched", requests=n)
        best = self._from_record(good[0], n)
        if plain_from_get and best.kind != "synced":
            plain_from_get.requests = n
            return plain_from_get
        return best
