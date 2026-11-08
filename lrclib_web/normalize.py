"""String normalization for matching (ARCHITECTURE.md §5)."""

from __future__ import annotations

import re
import unicodedata

QUALIFIERS = frozenset(
    {"live", "acoustic", "remix", "instrumental", "demo", "karaoke", "cover"}
)

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
# bracketed parts: (...), [...], {...}
_BRACKETS = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
# " - Live", " – Remastered 2011" style suffix (dash surrounded by spaces)
_DASH_SUFFIX = re.compile(r"\s[-–—]\s(.*)$")
# feat-separators, longest first; ' x ', ' & ' and ', ' need surrounding context
_FEAT_SPLIT = re.compile(
    r"\s*(?:\bfeaturing\b|\bfeat\.?|\bft\.|\bft\b|;|\sx\s|\s&\s|,\s|\svs\.?\s|\swith\s)\s*",
    re.IGNORECASE,
)
# explicit feat-markers only (used for LRCLIB requests); '&', ',', ' x ' etc. are not split
_EXPLICIT_FEAT = re.compile(r"\s*(?:\bfeaturing\b|\bfeat\b\.?|\bft\b\.?|;)\s*", re.IGNORECASE)


def norm(s: str | None) -> str:
    """NFKC -> casefold -> strip marks -> & to and -> punctuation to space -> drop leading 'the '."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c)
    )
    s = s.replace("&", " and ")
    s = _PUNCT.sub(" ", s).replace("_", " ")
    s = _SPACES.sub(" ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def split_title(title: str | None) -> tuple[str, frozenset[str]]:
    """Return (normalized core title, set of qualifiers found in stripped parts)."""
    if not title:
        return "", frozenset()
    extras: list[str] = []
    core = title
    m = _DASH_SUFFIX.search(core)
    if m:
        extras.append(m.group(1))
        core = core[: m.start()]
    extras.extend(_BRACKETS.findall(core))
    core = _BRACKETS.sub(" ", core)
    quals = set()
    for e in extras:
        for word in norm(e).split():
            if word in QUALIFIERS:
                quals.add(word)
            elif word in ("remixed", "unplugged"):
                quals.add("remix" if word == "remixed" else "acoustic")
    core_n = norm(core)
    if not core_n:  # title was entirely bracketed
        core_n = norm(title)
    return core_n, frozenset(quals)


def norm_core(title: str | None) -> str:
    return split_title(title)[0]


def artist_parts(artist: str | None) -> list[str]:
    """Split an artist credit on feat-separators; first item is the primary artist (raw)."""
    if not artist:
        return []
    parts = [p.strip() for p in _FEAT_SPLIT.split(artist) if p and p.strip()]
    return parts or [artist.strip()]


def has_feat_separator(artist: str | None) -> bool:
    return len(artist_parts(artist)) > 1


def primary_artist(artist: str | None) -> str:
    parts = artist_parts(artist)
    return parts[0] if parts else ""


def has_explicit_feat(artist: str | None) -> bool:
    """True when the credit contains feat., feat, ft., ft, featuring or ';'."""
    return bool(artist) and _EXPLICIT_FEAT.search(artist) is not None


def request_artist(artist: str | None) -> str:
    """Artist string to send to LRCLIB: the full credit, or the primary artist before an explicit feat-marker."""
    if not artist:
        return ""
    if not has_explicit_feat(artist):
        return artist.strip()
    parts = [p.strip() for p in _EXPLICIT_FEAT.split(artist) if p and p.strip()]
    return parts[0] if parts else artist.strip()


def artist_tokens(artist: str | None) -> frozenset[str]:
    return frozenset(n for n in (norm(p) for p in artist_parts(artist)) if n)


def same_artist(ours: str | None, theirs: str | None) -> bool:
    """D5 'same artist': normalized equality, or one artist-credit set contains the other."""
    a, b = norm(ours), norm(theirs)
    if a and a == b:
        return True
    ta, tb = artist_tokens(ours), artist_tokens(theirs)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta
