"""Configuration from environment variables (ARCHITECTURE.md §11)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

DEFAULT_EXTENSIONS = (
    "mp3,flac,m4a,mp4,aac,ogg,oga,opus,wma,wav,aiff,aif,ape,wv,mpc,dsf"
)


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _path_map(v: str) -> list[tuple[str, str]]:
    pairs = []
    for part in v.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        src, dst = part.split("=", 1)
        pairs.append((src.rstrip("/") or "/", dst.rstrip("/") or "/"))
    # longest container prefix first
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


@dataclass
class Config:
    music_dir: str = "/music"
    data_dir: str = "/config"
    port: int = 8080
    bind: str = "0.0.0.0"
    schedule_cron: str = "0 4 * * *"
    full_scan_cron: str = "0 3 * * 0"
    follow_symlinks: bool = False
    audio_extensions: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_EXTENSIONS.split(","))
    )
    missing_abort_pct: int = 50
    purge_missing_days: int = 30
    lrclib_url: str = "https://lrclib.net"
    lrclib_app_id: str = ""
    request_gap_ms: int = 500
    request_jitter_ms: int = 250
    notfound_recheck_days: int = 14
    plain_recheck_days: int = 30
    run_max_minutes: int = 0
    search_for_synced: bool = False
    txt_on_synced: str = "keep"
    umask: str = "022"
    warn_plex_path_len: int = 255
    plex_url: str = ""
    plex_token: str = ""
    plex_section_id: str = ""
    plex_path_map: list[tuple[str, str]] = field(default_factory=list)
    plex_refresh_level: str = "artist"
    plex_refresh_gap_ms: int = 1000
    plex_index_max_age_h: int = 24
    auth_user: str = ""
    auth_pass: str = ""
    log_level: str = "info"

    @property
    def plex_enabled(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_pass)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        kw: dict = {}
        for f in fields(cls):
            key = f.name.upper()
            if key not in env:
                continue
            raw = env[key]
            if f.name == "audio_extensions":
                kw[f.name] = frozenset(
                    e.strip().lower().lstrip(".") for e in raw.split(",") if e.strip()
                )
            elif f.name == "plex_path_map":
                kw[f.name] = _path_map(raw)
            elif f.type in ("int", int):
                kw[f.name] = int(raw)
            elif f.type in ("bool", bool):
                kw[f.name] = _bool(raw)
            else:
                kw[f.name] = raw.strip()
        cfg = cls(**kw)
        cfg.request_gap_ms = max(200, cfg.request_gap_ms)
        if cfg.txt_on_synced not in ("keep", "delete"):
            cfg.txt_on_synced = "keep"
        if cfg.plex_refresh_level not in ("artist", "album"):
            cfg.plex_refresh_level = "artist"
        cfg.lrclib_url = cfg.lrclib_url.rstrip("/")
        cfg.plex_url = cfg.plex_url.rstrip("/")
        return cfg

    def public(self) -> dict:
        """Effective config with secrets masked (GET /api/config)."""
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name in ("plex_token", "auth_pass", "lrclib_app_id"):
                v = "***" if v else ""
            elif isinstance(v, frozenset):
                v = sorted(v)
            elif f.name == "plex_path_map":
                v = ";".join(f"{a}={b}" for a, b in v)
            out[f.name] = v
        return out
