"""Tag reading via mutagen (ARCHITECTURE.md §4.4)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Tags:
    title: str | None = None
    artist: str | None = None
    album_artist: str | None = None
    album: str | None = None
    duration_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.title and self.artist)


def _first(tags, key: str) -> str | None:
    try:
        v = tags.get(key)
    except Exception:  # noqa: BLE001 - mutagen backends raise varied errors
        return None
    if not v:
        return None
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    v = str(v).strip() if v is not None else None
    return v or None


def read_tags(path: str) -> Tags:
    try:
        import mutagen
    except ImportError as e:  # pragma: no cover
        return Tags(error=f"mutagen missing: {e}")
    try:
        f = mutagen.File(path, easy=True)
    except Exception as e:  # noqa: BLE001
        return Tags(error=f"unreadable: {type(e).__name__}")
    if f is None:
        return Tags(error="unsupported format")
    t = Tags()
    length = getattr(getattr(f, "info", None), "length", 0) or 0
    t.duration_ms = int(round(float(length) * 1000))
    tags = f.tags
    if tags is not None:
        t.title = _first(tags, "title")
        t.album_artist = _first(tags, "albumartist")
        t.artist = _first(tags, "artist") or t.album_artist
        t.album = _first(tags, "album")
    if not t.ok:
        t.error = "missing title/artist tag"
    return t
