# Changelog

## Unreleased — review fixes

- A crash in a background thread also prints the full traceback to stderr (`docker logs`), not only to the JSONL file
- JSONL log: a write error (disk full, unwritable dir) pauses the file log and retries every 60 s instead of
  disabling it until restart; `/api/diag` has a `log_sink` section (broken, since, last error, lost events)
- `/api/client-log`: server-side limit of 30 reports per 10 minutes (HTTP 429 above it); the next accepted report
  carries `refused_before`, so a flooding client cannot push job history out of the rotated log
- Tool caches (`graft/.cache/`, `.trail/cache/`) are no longer tracked

## Unreleased — resource priorities

- `compose.example.yml`: low CPU and disk priority (`cpu_shares: 256`, `blkio_config.weight: 100`) so Plex and other
  containers go first when the server is busy; `memswap_limit` equal to `mem_limit` (no swap: a leak ends in an OOM
  kill and a restart instead of a slow hang). Memory/CPU/pids ceilings unchanged until measured on a full library
- README "Resource limits": what each setting does, Unraid Extra Parameters equivalent, how to check limits and
  whether they are hit

## Unreleased — diagnostics

- Machine log: every event is also written as JSON Lines to `DATA_DIR/logs/app.jsonl` (5 x 5 MB rotation) with stable
  event names, `run_id` per job run and full tracebacks; UI log and stdout stay unchanged. Debug-level events
  (`lrclib.request`, `plex.request`, `tags.error`) only with `LOG_LEVEL=debug`
- New events: `job.*`, `scan.summary`, `download.*`, `match.decision`, `write.*`, `lrclib.*`, `plex.*`,
  `scheduler.*`, `http.error` / `http.5xx`, `thread.crash`, `app.start`
- `POST /api/client-log`: the web UI reports JS errors, unhandled rejections, failed requests and event-stream drops
  (rate limited, 16 KB cap) as `ui.error`
- `GET /api/diag` and a "Download diagnostics" button: one JSON file with versions, redacted config, filesystem and
  database summary, current status, recent events and errors of the last 7 days

## Unreleased — security hardening

- File-operation guard: only `.lrc` / `.txt` sidecars and own temp files are created or deleted, paths must resolve
  inside `MUSIC_DIR`, temp files open with `O_EXCL|O_NOFOLLOW`; the no-hard-link fallback creates the target with
  `O_EXCL` instead of `os.replace` (never clobbers)
- `PLEX_TOKEN` is optional: `PLEX_URL` alone enables Plex, so a gateway can inject the token; README has an example
  haproxy config that allows only the five Plex requests the app makes
- `compose.example.yml`: read-only root fs, `tmpfs /tmp`, `cap_drop: ALL`, `no-new-privileges`, pids/memory/CPU
  limits, port bound to a LAN IP, `/config` on `/mnt/cache`
- README "Security" section: threat model, Plex gateway, optional sticky-bit + separate UID recipe, backups

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
