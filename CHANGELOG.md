# Changelog

## 0.1.0 — unreleased

- Library scan in quick and full modes
- LRCLIB lyrics lookup via `/api/get` with `/api/search` fallback
- `.lrc` sidecar files (plus `.txt` for plain lyrics)
- Nightly scheduled run and weekly full scan (cron)
- Plex metadata refresh after downloads
- Optional HTTP Basic auth
- Mobile-first single-page UI with live log over SSE
- `RUN_MAX_MINUTES` time budget for the download step (off by default)
- Re-check schedule: instrumental after 90 days; not found / rejected back off 14 → 28 → 56 → 180 days, reset on audio change
- A removed `.lrc` written by lrclib-web is re-downloaded once, then left alone
- Clear startup error when `DATA_DIR` is not writable (hint: `chown 99:100`)
- "Download log" button: saves the on-page log as a `.txt` file

Deferred to phase 2: job history API, per-run log files, `RESUME_ON_START`, update-existing-`.lrc` mode.
