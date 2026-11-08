# lrclib-web

Self-hosted web UI for mass-downloading synced lyrics (`.lrc`) from [LRCLIB](https://lrclib.net) for a local music library.
Mobile-first single page: library scan, lyrics download with live log, nightly schedule, optional Plex metadata refresh.

> **Status:** MVP 0.1.0 in development. See [`DECISIONS.md`](DECISIONS.md) (owner decisions) and
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (design v0.1). Task journal: [`ai-journal/lrclib-web.md`](ai-journal/lrclib-web.md).

## Quick start

> Keep `/config` on a non-FUSE path (e.g. `/mnt/cache/appdata/lrclib-web`); SQLite on `/mnt/user` can corrupt.

1. Copy `compose.example.yml` to `compose.yml` and adjust the two volume paths (`/music` = your library, `/config` = app data).
2. Start the container:

   ```
   docker compose up -d
   ```

3. Open `http://<host>:8080` in a browser.

## Configuration

All settings are environment variables. Defaults shown are the built-in values.

| Variable | Default | Meaning |
|---|---|---|
| `MUSIC_DIR` | `/music` | Library root (mount your music here) |
| `DATA_DIR` | `/config` | SQLite database and logs |
| `PORT` | `8080` | HTTP listen port |
| `BIND` | `0.0.0.0` | HTTP listen address |
| `TZ` | `UTC` | Time zone for schedules and UI times |
| `SCHEDULE_CRON` | `0 4 * * *` | Nightly quick scan + download + Plex refresh; empty disables |
| `FULL_SCAN_CRON` | `0 3 * * 0` | Full scan schedule; empty disables |
| `FOLLOW_SYMLINKS` | `false` | Follow symlinked files and directories during scan |
| `AUDIO_EXTENSIONS` | `mp3,flac,m4a,…` | Comma-separated list of audio extensions to scan |
| `MISSING_ABORT_PCT` | `50` | Abort if this percentage of tracks looks missing (removal safety guard) |
| `PURGE_MISSING_DAYS` | `30` | Days to keep rows for missing files before purging |
| `LRCLIB_URL` | `https://lrclib.net` | LRCLIB API base URL (self-hosted instance possible) |
| `LRCLIB_APP_ID` | `` | Optional `Lrclib-App-Id` header value |
| `REQUEST_GAP_MS` | `500` | Minimum pause between LRCLIB requests (floor 200) |
| `REQUEST_JITTER_MS` | `250` | Random extra delay added to request pacing |
| `NOTFOUND_RECHECK_DAYS` | `14` | First re-check of tracks with no lyrics (not found / rejected); repeated misses back off ×2, ×4, then 180 days; reset when the audio file changes. `0` = never |
| `PLAIN_RECHECK_DAYS` | `30` | Days before re-checking tracks with plain-only lyrics; `0` = never |
| `RUN_MAX_MINUTES` | `0` | Time budget for the download step; when reached the run stops cleanly and the next run continues. `0` = no limit |
| `SEARCH_FOR_SYNCED` | `false` | Also query `/api/search` when `/api/get` returns plain lyrics only |
| `TXT_ON_SYNCED` | `keep` | `keep` or `delete` our `.txt` when a `.lrc` is written later |
| `UMASK` | `022` | File creation umask, applied at startup |
| `WARN_PLEX_PATH_LEN` | `255` | Warn when the Plex-side path is longer than this |
| `PLEX_URL` | `` | Plex server URL (needs `PLEX_TOKEN` too) |
| `PLEX_TOKEN` | `` | Plex token (needs `PLEX_URL` too) |
| `PLEX_SECTION_ID` | `` (auto) | Plex music library section ID |
| `PLEX_PATH_MAP` | `` | Container-to-Plex path mapping, e.g. `/music=/mnt/user/Music` (`;`-separated) |
| `PLEX_REFRESH_LEVEL` | `artist` | Plex refresh scope: `artist` or `album` |
| `PLEX_REFRESH_GAP_MS` | `1000` | Pause between Plex refresh requests |
| `PLEX_INDEX_MAX_AGE_H` | `24` | Hours before the Plex track index cache is rebuilt |
| `AUTH_USER` | `` | HTTP Basic auth user (auth is enabled only when `AUTH_PASS` is also set) |
| `AUTH_PASS` | `` | HTTP Basic auth password |
| `LOG_LEVEL` | `info` | Log verbosity |

## Development

```
pip install -e .[dev]
pytest
MUSIC_DIR=./music DATA_DIR=./data python -m lrclib_web
```

Lint with `ruff check .`.

## Open questions

Resolved 2026-11-08, see DECISIONS.md D11–D15.

## License

MIT
