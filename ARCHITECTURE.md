# lrclib-web — architecture (design, v0.1, 2026-11-08)

Binding product decisions: DECISIONS.md (D1..D10). Evidence tags: [docs Lnn] = /tmp/lr/docs.txt (LRCLIB API docs),
[lrcget path:Lnn] = upstream LRCGET 2.2.2 source, [verified: url] = checked on the web, [assumption] = not verified.

## MVP status (2026-11-08)

Design changes from review, applied in the MVP code (they supersede the text below where it differs):

1. Before writing any sidecar the audio file is re-checked; if it vanished, nothing is written and the track becomes `missing` (counted as skipped).
2. No album -> both `/api/get` steps are skipped (LRCLIB requires `album_name`); an HTTP 400 on a get step falls through to the next step/search; "permanent" errors are retried after 90 days, not 10 years.
3. No-clobber publish via `os.link` + unlink (fallback: exists-check + `os.replace` where hard links are unsupported); the download step holds an exclusive `<data_dir>/download.lock` (POSIX `flock`), a second process fails the job.
4. A known directory that disappears gets its stored mtime cleared, so quick mode re-lists it (and restores its tracks) when it returns.
5. Any directory stat/list error other than "not found" makes the scan incomplete: recorded in errors, no missing-marking for the whole run, mtime of that dir not stored.
6. A directory's mtime is stored only if it is more than 2 s old (the stat is taken before listing), so same-second additions are not lost.
7. The second `/api/get` (primary artist) and the primary-artist search happen only for an explicit feat-marker (`feat.`, `feat`, `ft.`, `featuring`, `;`); "Earth, Wind & Fire" is sent as is. Candidate matching (`same_artist`) still uses the broad separators.
8. Adaptive pacing: each 429 doubles the request gap (cap 5 s), 20 consecutive successes halve it back to the configured gap; 503 without Retry-After backs off 1, 2, 4, 8, 16 s; Retry-After is also parsed in HTTP-date form.
9. `TXT_ON_SYNCED=delete` removes our `.txt` only if its current sha256 equals the stored `written_sha256`; otherwise it is kept and logged.

## 1. Overview

Single Python process in one container. Scans `/music`, keeps per-track state in SQLite under `/config`, queries LRCLIB
sequentially, writes `.lrc`/`.txt` sidecars next to the audio, then asks Plex to refresh affected artists. One job at a time.

```
 phone/browser ──HTTP/SSE──> [web.py  ThreadingHTTPServer]──┐          ┌──> /music (ro listing, rw sidecars)
                              Basic auth (opt), static UI    │          │
                                                             v          │
 [scheduler.py cron] ──start──> [jobs.py  JobRunner, 1 thread] ──> [scanner.py] ──> mutagen tags
                                   │  progress/log bus (in-mem ring + /config/logs)
                                   ├──> [matcher.py] ──> [lrclib.py client: pacing, Retry-After, breaker] ──> https://lrclib.net
                                   ├──> [writer.py atomic sidecars]
                                   └──> [plex.py: index cache, PUT .../refresh] ──> http://PLEX_URL (LAN)
                                            all state: [db.py SQLite WAL, /config/lrclib-web.sqlite3]
```
Outbound hosts needed: `lrclib.net:443` (or LRCLIB_URL host); Plex at PLEX_URL (LAN, usually host IP:32400). Nothing else.
Image pulls (ghcr.io) are done by the host, not by the container.

## 2. Tech stack

| Item | Choice | Why (1 line) |
|---|---|---|
| Language | Python 3.12+ | expected stack; mutagen is the only mature pure-Python tag reader; draft already in Python |
| HTTP server | stdlib `http.server.ThreadingHTTPServer` | one dependency fewer; load is 1-3 LAN clients; thread-per-connection suits SSE |
| HTTP client | stdlib `urllib.request` (+`http.client` keep-alive optional) | sequential requests, no pooling needed; trivial to fake in tests |
| Tags | `mutagen` (`File(easy=True)` + `.info.length`) | the one runtime dependency; covers mp3/flac/m4a/ogg/opus/wv/ape/aiff/wav/dsf |
| DB | stdlib `sqlite3`, WAL, single file in /config | 50k rows is tiny; survives restarts; enables phase 2 history |
| Scheduler | ~60-line built-in 5-field cron parser (`* , - /`) | avoids croniter dependency; fully unit-tested |
| UI | one static `index.html` + vanilla JS + CSS, SSE | no build step, no framework; mobile-first single page (D9) |
| Image | `python:3.12-slim`, non-root | small, glibc, pure-Python deps |
Rejected: FastAPI/uvicorn (2 deps + async for 3 users), Go/Rust rewrite (owner stack is Python; perf is network-bound).

Draft (`draft_app.py`) reuse/critique: keep Retry-After+jitter loop (L212-231), `X-Requested-With` CSRF trick (L420),
atomic write (L240-244), easy-tag reader (L120-129), job/stop skeleton. Fix: Ukrainian log strings (UI is English, D9);
immediate DELETE of vanished rows (L185: an unmounted /music wipes state); `os.path.exists(.lrc)` per track per run (L258:
one stat per track every night, spins disks; move sidecar detection into the directory listing); `has_lrc` rows re-selected
each run (L252); fixed-interval scheduler instead of cron (L348); `WRITE_INSTRUMENTAL` contradicts D6 (drop); no job
history/persistence; single global mutable `S`; polling-only UI; no search fallback (D5).

## 3. Data model (SQLite)

```sql
CREATE TABLE tracks(
  id INTEGER PRIMARY KEY,
  rel_path TEXT NOT NULL UNIQUE,          -- relative to /music, bytes preserved (surrogateescape)
  dir_id INTEGER NOT NULL REFERENCES dirs(id),
  mtime_ns INTEGER, size INTEGER,         -- change detection
  title TEXT, artist TEXT, album_artist TEXT, album TEXT, duration_ms INTEGER, -- from tags; duration from stream info
  tags_ok INTEGER NOT NULL DEFAULT 0,     -- title+artist present
  lrc_exists INTEGER NOT NULL DEFAULT 0, txt_exists INTEGER NOT NULL DEFAULT 0, -- from dir listing, not stat
  status TEXT NOT NULL DEFAULT 'pending', -- see state machine
  reason TEXT,                            -- short human text for UI/log
  attempts INTEGER NOT NULL DEFAULT 0,    -- consecutive error attempts
  checked_at INTEGER, next_check_at INTEGER,          -- unix s; NULL = never / now
  lrclib_id INTEGER, lrclib_kind TEXT,    -- 'synced'|'plain'|'instrumental'; phase 2
  remote_sha256 TEXT,                     -- sha256 of normalized remote lyrics we last saw; phase 2
  written_file TEXT, written_sha256 TEXT, written_by_us INTEGER NOT NULL DEFAULT 0, written_at INTEGER, -- phase 2
  plex_pending INTEGER NOT NULL DEFAULT 0,-- sidecar written but Plex not yet refreshed
  missing_since INTEGER,                  -- set when not seen by a scan; row purged after PURGE_MISSING_DAYS
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX ix_tracks_due ON tracks(status, next_check_at);
CREATE INDEX ix_tracks_dir ON tracks(dir_id);
CREATE INDEX ix_tracks_plex ON tracks(plex_pending) WHERE plex_pending=1;
CREATE TABLE dirs(id INTEGER PRIMARY KEY, rel_path TEXT NOT NULL UNIQUE, mtime_ns INTEGER, seen_scan_id INTEGER);
CREATE TABLE jobs(id INTEGER PRIMARY KEY, type TEXT, trigger TEXT, started_at INTEGER, finished_at INTEGER,
  state TEXT,  -- running|done|stopped|interrupted|failed
  counters_json TEXT, summary TEXT, log_file TEXT);
CREATE TABLE plex_items(file TEXT PRIMARY KEY,   -- Plex-side path (Media/Part@file)
  rating_key INTEGER, album_key INTEGER, artist_key INTEGER, section_id INTEGER, indexed_at INTEGER);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);  -- schema_version, last_full_scan, plex_index_at, ...
```

Track status state machine:
```
 scan: new file ─┬─ tags_ok=0 ──────────────> untagged      (re-read only when mtime/size changes)
                 ├─ lrc_exists=1 ──────────> has_lrc       (D1: never queried; phase 2 'update' mode works on these)
                 └─ else ──────────────────> pending
 pending ──download──> synced (.lrc written)            terminal until file changes
                   ├─> plain  (.txt written or exists)  re-check after PLAIN_RECHECK_DAYS (default 30): synced now? -> write .lrc -> synced
                   ├─> not_found                        re-check after NOTFOUND_RECHECK_DAYS (default 14) [docs L191-193]
                   ├─> instrumental                     terminal (D6), no file
                   ├─> rejected (candidates failed duration/artist rule)  re-check like not_found
                   └─> error (transient/permanent)       transient: next run, attempts++ , backoff 1,2,4 days after 3; permanent (400): until tags change
 any ──scan sees mtime/size change──> re-read tags ──> pending/untagged/has_lrc (lrclib_* cleared, written_* kept)
 any ──scan: file absent──> missing (missing_since) ──PURGE_MISSING_DAYS (30)──> row deleted; reappears -> back to previous status
 txt_exists=1 and lrc_exists=0: still queried (synced may exist); plain result writes nothing (status plain, reason "txt exists").
```
`next_check_at` is the single scheduling field; the download query is
`SELECT ... WHERE status IN ('pending','plain','not_found','rejected','error') AND (next_check_at IS NULL OR next_check_at<=now)
 ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'error' THEN 1 WHEN 'plain' THEN 2 ELSE 3 END, rel_path`.

## 4. Scan algorithm

1. Walk `/music` depth-first, sorted; `os.scandir` per directory (one syscall per dir gives names+d_type). Skip entries
   starting with `.` or `@` (`._*` AppleDouble, `.stfolder`, `@eaDir`), and symlinks (FOLLOW_SYMLINKS=false default; loops
   and cross-share paths confuse Plex mapping). Audio extensions: mp3 flac m4a mp4 aac ogg oga opus wma wav aiff aif ape wv
   mpc dsf (case-insensitive; LRCGET uses a narrower set [lrcget src-tauri/src/scanner/scan.rs:66]).
2. Sidecar detection from the same listing: `stem.lrc`/`stem.txt` present (exact-case stem; Plex requires exact match
   [owner forum note]). No extra stat.
3. Two modes. `quick` (default for nightly): stat each directory only; if `dirs.mtime_ns` unchanged, skip the directory
   entirely (adding/removing/renaming entries changes dir mtime; in-place retagging does not — accepted gap). `full`
   (manual button, optional FULL_SCAN_CRON): stat every audio file; tags re-read only if `(mtime_ns,size)` differs from DB.
   Cost on spun-down disks: both modes touch inodes; quick mode does ~1 stat per dir (~1-5k) instead of per file (5-50k),
   and dir inodes are the ones most likely cached. Unavoidable spin-up is documented; scheduling at 04:00 is the owner's call.
4. Tag reading (only new/changed): mutagen easy tags `title`, `artist` (fallback `albumartist`), `albumartist`, `album`;
   duration = `f.info.length` from stream info (not a tag), stored in ms. No title or artist -> `untagged`, no filename
   parsing (too noisy; listed in UI counter so the owner can fix tags). Corrupt/unreadable -> `untagged`, reason set.
5. Removal: tracks whose dir was listed this scan but which were not seen -> `missing`. Safety guard: if the root lists 0
   entries, or missing count > MISSING_ABORT_PCT (50%) of known tracks, mark nothing missing and log an error (unmounted
   share). Purge after PURGE_MISSING_DAYS. Dirs not seen -> their tracks `missing` under the same guard.
6. Single thread, sequential per directory (HDD-friendly). Commit every 500 rows. Progress: dirs done / dirs known.
7. Paths stored relative, as `os.fsdecode` with surrogateescape so non-UTF-8 names survive the round trip.

## 5. Matching algorithm

Per track (budget: max 3 HTTP requests, typical 1):
1. `GET /api/get?track_name&artist_name&album_name&duration` with duration rounded to int and only if 1..3600
   [docs L184-206; lrcget src-tauri/src/lrclib/get.rs:106-108]. 200 -> accept as is (server already applies the ±2 s rule
   [docs L195-197]); `instrumental:true` -> instrumental; else synced > plain by content.
2. 404 and artist contains a feat-separator (`feat.`, `ft.`, `featuring`, `;`, ` x `, ` & `, `, `) -> one more `/api/get`
   with the primary artist only (request 2).
3. Still 404 -> `GET /api/search?track_name=<title>&artist_name=<primary artist>` (structured, not `q`; ≤20 results,
   no pagination [docs L279-297]). Candidate filter (all must hold):
   - `norm(artist)` equal, or candidate primary-artist token set ⊆ ours / ours ⊆ theirs (D5 "same artist");
   - `|duration - ours| <= 2.0 s` (D5); if our duration is unknown (0) accept only if norm(title) and norm(album) also equal;
   - `norm_core(title)` equal, where core strips bracketed/dashed qualifiers; a qualifier present on exactly one side from
     {live, acoustic, remix, instrumental, demo, karaoke, cover} -> reject (design addition to D5, see Q2).
4. Ranking among survivors: synced > plain > instrumental; then norm(album) equal; then smaller |Δduration|; then lower id.
   Reject all -> `rejected` (shown with candidate count). `/api/get` returned plain-only: no extra search by default
   (SEARCH_FOR_SYNCED=false; plain re-check after 30 days covers it cheaply).
`norm(s)`: NFKC -> casefold -> strip combining marks (NFD) -> `&`->`and` -> remove punctuation to spaces -> collapse
spaces -> drop leading `the `. Compare strings, not fuzzy ratios (deterministic, testable). Budget exhaustion by 429/503
retries counts per request, not per attempt.

## 6. LRCLIB client policy

- Headers: `User-Agent: lrclib-web/<ver> (+https://github.com/vantuzz/lrclib-web)` (identified client ~90 req/min
  [docs L82-83]; LRCGET format [lrcget src-tauri/src/lrclib/client.rs:35-39]); `Accept: application/json`;
  `Lrclib-App-Id` only when LRCLIB_APP_ID set and host is lrclib.net [docs L64-65; lrcget client.rs:62-68]. 401 with
  `InvalidAppIdError` [docs L160] -> drop the header for the rest of the run, log error once, do not retry the request.
- Sequential, one in flight; gap REQUEST_GAP_MS (500) + U(0, REQUEST_JITTER_MS=250) between requests, floor 200 ms
  [docs L117-119; lrcget src/composables/downloader.js:6-7,60]. Timeouts: connect 10 s, read 30 s.
- 429/503: wait `max(Retry-After, 1) * (1 + U(0, 0.3))` [docs L105-107, L164, L166]; missing/invalid header -> 429: 10 s
  doubling to 120 s, 503: 1 s. Up to 6 attempts per request, then the track -> `error` (transient) and the breaker counts it.
- 500/502/504 and network/timeout errors: backoff 0.5,1,2,4,8 s +30% jitter, max 4 attempts
  [lrcget client.rs:19,119,448-456]. 400 -> `error` permanent (reason = message [docs L158]). 404 -> not found [docs L163].
- Non-JSON bodies: decide on status code + Retry-After only, never parse first [docs L111-113, L168-175]; 200 with
  non-JSON body -> transient error.
- Global cooldown: any Retry-After sets `cooldown_until` shown in UI (LRCGET pattern [downloader.js:39-57]).
  Circuit breaker: 5 consecutive request-level failures after retries, or cumulative 429 wait > 10 min in a run, ->
  pause 15 min; third pause in one run -> job `failed: LRCLIB unavailable`, remaining tracks untouched (re-run picks up).
- Stop/cancel interrupts any wait via `Event.wait(timeout)`.

## 7. File writing

- Name: `<audio stem><.lrc|.txt>`, stem byte-identical to the audio file name, lower-case extension, same directory
  [owner forum note]. UTF-8 without BOM, `\n` line endings (CRLF normalized), exactly one trailing newline.
- Atomic: write to `.<stem>.lrc.tmp-<pid>` in the same directory, `fsync`, `os.replace`. Never overwrite: re-check
  `exists()` immediately before replace (single writer, TOCTOU acceptable); existing `.lrc` -> skip (D1); existing `.txt`
  -> never written (txt is written only when neither sidecar exists).
- Synced -> `.lrc`; plain-only -> `.txt` (D2); instrumental -> nothing (D6). Later synced for a `plain` track: write `.lrc`,
  keep `.txt` unless TXT_ON_SYNCED=delete and `written_by_us=1` (Q5).
- Permissions: image runs as `USER 99:100` by default (unRAID nobody:users), overridable with `--user`; `UMASK` env
  (default `022` -> 0644 files). No PUID/PGID entrypoint (would need root at start; Q8).
- Long-path guard: Linux NAME_MAX 255 bytes per component -> ENAMETOOLONG -> `error` permanent. Plex-side full path
  > 255 chars [owner forum note] -> still write, set `reason="plex: path>255"`, log warn (WARN_PLEX_PATH_LEN=255).
- Record `written_file`, `written_sha256`, `written_by_us=1`, `remote_sha256`, `lrclib_id` (phase 2).

## 8. Jobs & concurrency

- Types: `scan` (quick|full), `download`, `run` = scan(quick) + download + plex_refresh. Plex refresh is a step of
  `download`/`run`, not a user-facing job. Trigger recorded: `manual|schedule`.
- Single runner: `JobRunner` holds one worker thread; `start()` under a lock returns 409 if a job is running. Jobs table
  row created at start, updated every 2 s / 100 items with counters, finished with state+summary.
- Stop: UI sets `stop_event`; checked between directories, between tracks, and inside every wait; in-flight HTTP request
  is allowed to finish (≤30 s). State `stopped`; nothing partial on disk thanks to atomic writes.
- Restart: on boot, rows with `state='running'` -> `interrupted`. No auto-resume (safe default); all steps are idempotent
  via track status, so the next manual/scheduled run continues where it stopped. Optional RESUME_ON_START=true.
- Scheduler thread: evaluates SCHEDULE_CRON (default `0 4 * * *`) once per minute in container TZ (`TZ` env, default
  UTC); starts `run`; skipped with a log line if busy; no catch-up for missed slots (container down) by default.
  FULL_SCAN_CRON (default `0 3 * * 0`) starts `scan full`.
- Progress model (in memory, pushed over SSE): `{job, phase, done, total, current, counters{synced,plain,not_found,
  rejected,instrumental,untagged,error,skipped}, cooldown_until, breaker_state, started_at, eta_s}`; ETA from moving
  average of per-track time.

## 9. Plex integration (optional: active only if PLEX_URL and PLEX_TOKEN are set)

Verified endpoints (python-plexapi source, [verified: https://raw.githubusercontent.com/pkkid/python-plexapi/master/plexapi/base.py]
`refresh()` = `PUT {key}/refresh` where key = `/library/metadata/<ratingKey>`; docstring: use it after adding local media
assets; [verified: .../plexapi/library.py] `GET /library/sections/<id>/refresh?path=<quoted>` = partial scan,
`GET /library/sections/<id>/all?type=<n>` listing, type codes artist=8 album=9 track=10 ([verified: .../plexapi/utils.py]);
[verified: .../plexapi/server.py] `GET /identity`; [verified: https://developer.plex.tv/pms/] `X-Plex-Token` header,
`X-Plex-Container-Start/Size` pagination headers, `/library/sections`, track `Media[].Part[].file`; forum confirms one
item per refresh call [verified: https://forums.plex.tv/t/plex-api-how-to-refresh-multiple-items-at-the-same-time/122230]).
Responses parsed as XML with stdlib ElementTree (default Plex format; JSON via Accept header is [assumption], not used).

1. Startup check: `GET /identity`; resolve section: PLEX_SECTION_ID or the single section with `type="artist"` from
   `/library/sections` (several -> error, Plex disabled for this process, UI banner).
2. Path mapping: PLEX_PATH_MAP=`/music=/mnt/user/Music` (`;`-separated pairs, container prefix -> Plex prefix). Written
   sidecar dir -> audio path on Plex side.
3. Index: `plex_items` filled from `GET /library/sections/<id>/all?type=10` paginated by 1000 (50k tracks ≈ 50 calls,
   ~1-2 min, once); rebuilt incrementally when a written file is not in the cache (new music) or cache older than
   PLEX_INDEX_MAX_AGE_H (24). Lookup written audio file -> `artist_key` (grandparentRatingKey) / `album_key`.
4. After the download step, if ≥1 sidecar written or any `plex_pending=1`: distinct keys at PLEX_REFRESH_LEVEL (`artist`
   default per D8; `album` option, Q3) -> `PUT /library/metadata/<key>/refresh` sequentially, PLEX_REFRESH_GAP_MS (1000),
   each tracked once per run (debounce). Success clears `plex_pending` for the tracks under that key.
5. Audio file unknown to Plex (not yet scanned): `GET /library/sections/<id>/refresh?path=<plex dir>` once per dir,
   keep `plex_pending=1`; next run re-indexes and refreshes. [assumption] that Plex picks up the sidecar on the item
   refresh at artist level — owner forum note; album-level fallback is the switch. Forum thread shows local lyrics pickup
   bug fixed in PMS 1.41.6 (PM-3185) [verified: https://forums.plex.tv/t/newly-added-lrc-lyrics-files-not-recognized-by-plex/910448].
6. Failures (connection refused, 401, 5xx): log warn, leave `plex_pending`, job still `done`; never block downloads.
   Token never logged; `/api/config` masks it.

## 10. HTTP API + UI

| Method/path | Auth | Payload / notes |
|---|---|---|
| GET `/` , `/static/*` | yes | page, `app.js`, `style.css`; CSP `default-src 'self'`, no inline JS |
| GET `/healthz` | no | `{"status":"ok","db":true}`; 503 if DB unwritable |
| GET `/api/status` | yes | snapshot: `{version, job|null, library:{counts by status}, last_runs[5], plex:{enabled, ok}, schedule:{cron,next_at}}` |
| GET `/api/events` | yes | SSE: `event: status` (every 1 s while a job runs, else on change), `event: log` lines with `id`; honors `Last-Event-ID`, `retry: 3000` |
| POST `/api/jobs` | yes+CSRF | `{"type":"scan|download|run","mode":"quick|full"}` -> 202 `{job_id}` / 409 busy |
| POST `/api/jobs/current/stop` | yes+CSRF | 202; idempotent |
| GET `/api/jobs?limit=20` | yes | history rows from `jobs` |
| GET `/api/log?since=<id>&limit=500` | yes | ring buffer fallback for clients without SSE |
| GET `/api/config` | yes | effective config, secrets masked |

Live updates: **SSE** (one GET, server pushes; EventSource auto-reconnects with `Last-Event-ID`, so background-tab drops on
mobile heal themselves; thread-per-connection server makes it ~30 lines). Polling kept only as `/api/log?since` fallback.
Auth: optional Basic (AUTH_USER/AUTH_PASS, `hmac.compare_digest`), everything except `/healthz` (D7); 401 on `/api/events`
is handled by the browser's cached credentials. CSRF: POST requires `X-Requested-With: lrclib-web` (forms cannot set it;
cross-origin fetch triggers preflight, and no CORS headers are emitted) plus `Origin`/`Host` equality when `Origin` present.
Logs: in-memory ring 2000 lines; per-run file `/config/logs/run-<id>.log`, keep LOG_KEEP_RUNS (30); stdout mirrors INFO+.

Mobile layout (≤ 430 px wide, single column; wider screens just get margins):
```
┌──────────────────────────────┐
│ lrclib-web        v1.0  ● OK │  header: version, LRCLIB/Plex health dots
├──────────────────────────────┤
│ LIBRARY           12 340 trk │  counts: synced 8 120 · plain 610 · has .lrc 2 400 · not found 900 · untagged 210
│ last scan 04:02 (quick)      │
│ [ Scan ]  [ Full scan ]      │  big 48 px buttons; disabled while a job runs
├──────────────────────────────┤
│ LYRICS DOWNLOAD              │
│ ▶ running · 2 310 / 3 004    │  progress bar; ETA; "cooldown 27 s" badge when Retry-After active
│ synced 1 802 plain 190 …     │  live counters chips
│ [ Download ]  [ Scan+DL ]  [ ■ Stop ] │
│ next schedule: 04:00 (Europe/Kyiv)   │
├──────────────────────────────┤
│ LOG                 [pause]  │  monospace, newest at bottom, auto-scroll, 300 lines visible
│ 04:12:03 ok  Artist — Title: synced → .lrc │
│ 04:12:04 warn … not found    │
└──────────────────────────────┘
```

## 11. Configuration (env)

| Env | Default | Meaning |
|---|---|---|
| MUSIC_DIR | `/music` | library root (mount) |
| DATA_DIR | `/config` | SQLite, logs |
| PORT / BIND | `8080` / `0.0.0.0` | listener |
| TZ | `UTC` | container time zone for schedule and UI times |
| SCHEDULE_CRON | `0 4 * * *` | nightly `run` (quick scan + download + plex); empty disables |
| FULL_SCAN_CRON | `0 3 * * 0` | full scan schedule (weekly, Sunday 03:00; D11); empty disables |
| RESUME_ON_START | `false` | continue an interrupted job on boot |
| FOLLOW_SYMLINKS | `false` | follow symlinked files/dirs during scan |
| AUDIO_EXTENSIONS | `mp3,flac,m4a,…` | override list |
| MISSING_ABORT_PCT / PURGE_MISSING_DAYS | `50` / `30` | removal safety guard / retention of missing rows |
| LRCLIB_URL | `https://lrclib.net` | API base (self-hosted instance possible) |
| LRCLIB_APP_ID | `` | optional `Lrclib-App-Id` [docs L64] |
| REQUEST_GAP_MS / REQUEST_JITTER_MS | `500` / `250` | pacing; gap floor 200 |
| NOTFOUND_RECHECK_DAYS / PLAIN_RECHECK_DAYS | `14` / `30` | re-check cadence; `0` = never |
| SEARCH_FOR_SYNCED | `false` | extra `/api/search` when `/api/get` is plain-only |
| TXT_ON_SYNCED | `keep` | `keep|delete` our `.txt` when `.lrc` gets written later |
| UMASK | `022` | applied at startup |
| WARN_PLEX_PATH_LEN | `255` | warn when Plex-side path longer |
| PLEX_URL / PLEX_TOKEN | `` | both required to enable Plex |
| PLEX_SECTION_ID | `` (auto) | music section id |
| PLEX_PATH_MAP | `` | `/music=/mnt/user/Music[;...]` |
| PLEX_REFRESH_LEVEL / PLEX_REFRESH_GAP_MS | `artist` / `1000` | `artist|album` |
| PLEX_INDEX_MAX_AGE_H | `24` | rebuild track index cache |
| AUTH_USER / AUTH_PASS | `` | Basic auth when both set |
| LOG_LEVEL / LOG_KEEP_RUNS | `info` / `30` | |

## 12. Packaging & CI

Dockerfile outline: `FROM python:3.12-slim` -> `pip install --no-cache-dir mutagen==<pinned>` -> `COPY lrclib_web/ /app/`
-> `ENV PYTHONUNBUFFERED=1 MUSIC_DIR=/music DATA_DIR=/config` -> `VOLUME ["/music","/config"]` -> `EXPOSE 8080` ->
`USER 99:100` -> `HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)"`
-> `CMD ["python","-m","lrclib_web"]`. No curl, no entrypoint script. Compose example: `/mnt/user/Music:/music`,
`/mnt/user/appdata/lrclib-web:/config`, `user: "99:100"`, env from table; network per owner policy (allow lrclib.net + Plex).
GitHub Actions (`.github/workflows/ci.yml`: ruff + pytest on PR/push; `release.yml`: on `v*` tags and `main`):
docker/login-action to ghcr with `GITHUB_TOKEN` (`permissions: packages: write`), docker/metadata-action tags
`latest` (main), `vX.Y.Z`, `vX.Y`, `vX`, `sha-…`; docker/build-push-action, `linux/amd64` (+`arm64` optional, QEMU), gha cache.
Repo layout:
```
lrclib_web/{__main__.py, config.py, db.py, scanner.py, tags.py, normalize.py, matcher.py, lrclib.py, writer.py,
            jobs.py, scheduler.py, cron.py, plex.py, web.py, static/{index.html,app.js,style.css}}
tests/{unit/…, integration/{fake_lrclib.py, fake_plex.py, test_run.py}, fixtures/gen_audio.sh, live/test_smoke.py}
Dockerfile  compose.example.yml  pyproject.toml  README.md  ARCHITECTURE.md  DECISIONS.md  CHANGELOG.md  .github/workflows/
```

## 13. Testing plan

- Unit: `normalize` (diacritics, `&`, `the`, feat splitting, bracket stripping); `matcher` candidate filter + ranking
  (duration ±2 s edges, unknown duration rule, qualifier mismatch, tie-break order); `lrclib` retry timing with injected
  clock/sleep/random (429 with/without Retry-After, 503 1 s, 5xx backoff caps, non-JSON 429, 401 app-id drop, breaker
  thresholds, stop interrupts wait); `cron` parser vs. table of (expr, now, next); `writer` (atomic, no-clobber, CRLF, BOM,
  umask, ENAMETOOLONG); `scanner` incremental logic on tmp trees (quick vs full, dir mtime, missing guard, sidecar
  detection, hidden/symlink skip); Plex path mapping and XML parsing from recorded fixtures.
- Integration: fake LRCLIB (`http.server` in a thread, scripted per-query responses: 200 synced/plain/instrumental, 404,
  429+Retry-After:2, 503, 500, non-JSON 429, 200 non-JSON, socket timeout, search arrays) + fake Plex (sections, paginated
  tracks XML, records PUT refresh calls) + generated audio: `ffmpeg -f lavfi -i anullsrc -t 5 -metadata title=… -metadata
  artist=… out.{mp3,flac,m4a,ogg,opus}` produced at test time (`pytest.skip` if ffmpeg absent; CI installs ffmpeg).
  Asserts: statuses, files written once, pacing ≥ gap, requests ≤ 3/track, refresh called once per artist, resume after
  simulated interrupt.
- Live smoke (opt-in `LRCLIB_LIVE=1`, not in CI): one `/api/get` for the docs example (Borislav Slavov / I Want to Live /
  233 s [docs L211]) with the real User-Agent; asserts 200, `id`, `syncedLyrics`.

## 14. Phase 2 hook (update existing .lrc)

New job type `update` (not implemented now): candidates = `has_lrc` rows with `written_by_us=1` (optionally all, by
flag). For each: one request `GET /api/get/{lrclib_id}` [docs L253] when known, else the normal matcher; compute
`sha256(normalize_lrc(syncedLyrics))`, compare with `remote_sha256`; if changed and `sha256(local file) == written_sha256`
(user has not edited it) -> atomic replace, update both hashes; else leave file and log "locally modified". Files not
written by us can be "adopted" by hashing them and storing `written_sha256` without `written_by_us`. LRCLIB exposes no
dates/ETag (D1), so content hashing is the only change signal; the plain re-check path in §3 already uses the same fields.

## 15. Risks & open questions

1. Nightly 04:00 scan spins up all array disks (stat on FUSE). Rec: keep default, nightly `quick` mode, full scan weekly
   or manual; mention Dynamix Cache Dirs in README. [assumption about unRAID inode caching behaviour]
2. D5 accepts search candidates by artist+duration only; two different songs of one artist with equal length would match.
   Rec: add the normalized title-core check (§5.3) to D5.
3. Artist-level refresh: forum moderator advice "refresh the album artist, not the album" for new .lrc [verified: https://forums.plex.tv/t/suddenly-lrc-files-arent-showing-up/938145]; still not confirmed by Plex docs, and it may re-download
   artist metadata for large artists. Rec: ship `PLEX_REFRESH_LEVEL=artist` per D8, test on 2-3 artists, switch to `album`
   if equivalent. Requires PMS ≥ 1.41.6 for reliable local-lyrics pickup (PM-3185).
4. Initial run of 50k tracks ≈ 8-10 h at ~0.6 s/request (+search fallbacks). Rec: accept (resumable); optionally add
   RUN_MAX_MINUTES (D17, done); re-check ordering already puts new tracks first.
5. `.txt` left behind when `.lrc` is written later: Plex preference between `.lrc` and `.txt` unknown. Rec: default keep
   (TXT_ON_SYNCED=keep); flip to delete if Plex shows plain text.
6. Untagged files are never looked up (no filename parsing). Rec: keep; expose count and a CSV export later.
7. `missing` guard at 50% may block legitimate large removals. Rec: keep 50%, log the exact count and a hint to raise it.
8. Non-root `USER 99:100` vs LinuxServer-style PUID/PGID entrypoint (needs root). Rec: `USER` + `--user` override.
9. `/api/search` v1 returns full lyrics for up to 20 records (heavy); v2 beta is lighter but needs a second request and
   may change [docs L471-473]. Rec: v1 now, revisit when v2 leaves beta.
10. Single `/music` root; multiple shares need multiple binds under `/music/<name>` (works with PLEX_PATH_MAP pairs). Rec: document.
11. Hardlink no-clobber (`os.link`) would be stronger than exists+replace but shfs hardlink support is a tunable. Rec: exists+replace.
12. Registering an `Lrclib-App-Id` doubles the allowance [docs L84]; needs a portal registration. Rec: optional, owner decides.
