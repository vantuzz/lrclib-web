# lrclib-web

Self-hosted web UI for mass-downloading synced lyrics (`.lrc`) from [LRCLIB](https://lrclib.net) for a local music library.
Mobile-first single page: library scan, lyrics download with live log, nightly schedule, optional Plex metadata refresh.

> **Status:** MVP 0.1.0 in development. See [`DECISIONS.md`](DECISIONS.md) (owner decisions) and
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (design v0.1). Task journal: [`ai-journal/lrclib-web.md`](ai-journal/lrclib-web.md).

## Quick start

> Keep `/config` on a non-FUSE path (e.g. `/mnt/cache/appdata/lrclib-web`); SQLite on `/mnt/user` can corrupt.

1. Copy `compose.example.yml` to `compose.yml` and adjust the two volume paths (`/music` = your library, `/config` = app data).
2. Create the app data folder and give it to the container user (`99:100` = `nobody:users` on Unraid). Docker would
   otherwise create it owned by root, and the app stops with "DATA_DIR … is not writable":

   ```
   mkdir -p /mnt/cache/appdata/lrclib-web && chown 99:100 /mnt/cache/appdata/lrclib-web
   ```

   The music folder must be writable by the same user too, since `.lrc` files are written next to the audio.
3. Start the container:

   ```
   docker compose up -d
   ```

4. Open `http://<host>:8080` in a browser. The example binds the port to one LAN IP (`192.168.1.10:8080:8080`);
   change the IP to your host's, and the middle `8080` if the port is taken.

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
| `PLEX_URL` | `` | Plex server URL; setting it enables Plex refresh |
| `PLEX_TOKEN` | `` | Plex token; optional when a gateway injects it (see Security) |
| `PLEX_SECTION_ID` | `` (auto) | Plex music library section ID |
| `PLEX_PATH_MAP` | `` | Container-to-Plex path mapping, e.g. `/music=/mnt/user/Music` (`;`-separated) |
| `PLEX_REFRESH_LEVEL` | `artist` | Plex refresh scope: `artist` or `album` |
| `PLEX_REFRESH_GAP_MS` | `1000` | Pause between Plex refresh requests |
| `PLEX_INDEX_MAX_AGE_H` | `24` | Hours before the Plex track index cache is rebuilt |
| `AUTH_USER` | `` | HTTP Basic auth user (auth is enabled only when `AUTH_PASS` is also set) |
| `AUTH_PASS` | `` | HTTP Basic auth password |
| `LOG_LEVEL` | `info` | Log verbosity; `debug` also records every LRCLIB/Plex request in the JSONL log |

## Security

What the app touches: it reads audio files (tags, duration) under `/music`, creates `.lrc` / `.txt` files next to
them and may delete only its own `.txt` (`TXT_ON_SYNCED=delete`). The code enforces this (DECISIONS.md D21): it
creates or deletes only `.lrc`, `.txt` and its own `.<name>.tmp-<pid>` files, refuses any path that resolves
outside `MUSIC_DIR`, never follows a symlink at the final path component, and never overwrites an existing file.
Its only other writes go to `/config` (SQLite database). Outbound traffic: LRCLIB and, if enabled, Plex.

### Container hardening

`compose.example.yml` runs the container with a read-only root filesystem (`read_only`, `tmpfs /tmp`), all Linux
capabilities dropped, `no-new-privileges`, pids/memory/CPU limits and the web port bound to one LAN IP. Keep these
unless something breaks; if the container fails to start with `read_only: true`, report it as a bug. Set
`AUTH_USER` / `AUTH_PASS` if other people share your LAN, and do not expose the port to the internet.

### Resource limits

The container should never slow down Plex or other services on the same server. `compose.example.yml` sets two
kinds of limits:

- **Priorities** (always useful, independent of library size): `cpu_shares: 256` (default 1024) and
  `blkio_config.weight: 100` (default 500). They change nothing while the server is idle; when CPU or disks are
  busy, other containers get them first, so a full scan waits instead of stalling Plex. The I/O weight works only
  if the kernel supports it (BFQ scheduler / cgroup I/O weight); otherwise Docker prints a warning and ignores it.
- **Ceilings**: `mem_limit: 256m` with `memswap_limit: 256m` (the same value means no swap), `cpus: 1.0`,
  `pids_limit: 64`. On a memory leak the kernel kills the process at the limit, the container exits and
  `restart: unless-stopped` starts it again; an interrupted job continues on the next run. Note that Docker does
  not restart a container that is merely "unhealthy". These values are rough: a short test on ~34k tracks peaked
  at 63 MiB, 5 pids and never hit the CPU limit, but a full scan and a long download run are not measured yet.

Unraid template (no compose): put the same values into **Extra Parameters**:

```
--memory=256m --memory-swap=256m --cpus=1 --cpu-shares=256 --blkio-weight=100 --pids-limit=64
```

To check the limits are applied and whether the container ever hits them (replace the name):

```bash
docker inspect -f 'mem={{.HostConfig.Memory}} swap={{.HostConfig.MemorySwap}} cpus={{.HostConfig.NanoCpus}} shares={{.HostConfig.CpuShares}} blkio={{.HostConfig.BlkioWeight}} pids={{.HostConfig.PidsLimit}}' lrclib-web
docker exec lrclib-web sh -c 'cd /sys/fs/cgroup; cat memory.peak; grep oom_kill memory.events; grep nr_throttled cpu.stat'
```

`oom_kill` above 0 means the memory ceiling was reached; a growing `nr_throttled` means the CPU ceiling slows the
jobs. In either case raise the value or report it.

### Plex through a gateway

With `PLEX_TOKEN` in the container, a leak gives full control of the Plex server. Instead, put a small HTTP proxy
in front of Plex that adds the token and allows only the requests this app makes, and set only `PLEX_URL` to the
proxy. Example `haproxy.cfg` (run haproxy with `PLEX_TOKEN` in its environment; change the Plex address):

```
defaults
    mode http
    timeout connect 5s
    timeout client 60s
    timeout server 60s

frontend plex_api
    bind :32400
    acl m_get   method GET
    acl m_put   method PUT
    acl p_ident path /identity
    acl p_secs  path /library/sections
    acl p_all   path_reg ^/library/sections/[0-9]+/all$
    acl p_scan  path_reg ^/library/sections/[0-9]+/refresh$
    acl p_meta  path_reg ^/library/metadata/[0-9]+/refresh$
    http-request deny unless m_get p_ident or m_get p_secs or m_get p_all or m_get p_scan or m_put p_meta
    http-request set-header X-Plex-Token "${PLEX_TOKEN}"
    default_backend plex

backend plex
    server plex 192.168.1.10:32400
```

Put the gateway and lrclib-web on one Docker network and set `PLEX_URL: "http://<gateway name>:32400"`.

### Optional: stop the app from deleting or changing audio (sticky bit + separate UID)

The guard above lives in the app's code. To have the kernel enforce it as well, run the container as a dedicated
user that can create files in the library folders but not change or delete files it does not own. This changes
permissions on your whole library, so it is your choice and is not done for you. Sketch for Unraid (`users` = gid
100, app user uid 1500):

```
# folders: group-writable with the sticky bit, so only a file's owner can delete or rename it
find /mnt/user/Music -type d -exec chmod 1775 {} +
# audio and other existing files: read-only for the group
find /mnt/user/Music -type f -exec chmod 644 {} +
```

Then set `user: "1500:100"` in `compose.yml`. Files written earlier by uid 99 are no longer deletable by the app
(`TXT_ON_SYNCED=delete` will log a failure for them). Other tools that write to the library (Lidarr, beets, file
shares) must still be able to: check them before applying this.

### Backups

The app never overwrites or deletes audio, but keep a backup of the library anyway: any process with write access
to it is a risk. The database in `/config` holds only state that a full scan rebuilds; backing it up is optional.

## Debugging and diagnostics

Besides the human-readable log in the UI (and on stdout), every event is written as JSON Lines to
`$DATA_DIR/logs/app.jsonl` (default `/config/logs/app.jsonl`; 5 files x 5 MB, rotated to `app.jsonl.1` ... `.4`).
If the directory cannot be created the app runs without the file. One JSON object per line with `ts` (ISO 8601 with
offset, ms), `level`, `event` (stable dotted name such as `job.start`, `job.finish`, `match.decision`, `write.ok`,
`write.error`, `lrclib.request`, `scan.summary`, `plex.refresh`, `scheduler.fire`, `http.error`, `ui.error`), `msg`,
`version`, `run_id` (shared by all events of one job run, `null` outside runs) and event-specific fields. Failures carry
a full `traceback` field. Secrets and lyrics text are never logged.

```
grep '"event": "match.decision"' /config/logs/app.jsonl | tail
jq -c 'select(.level=="error")' /config/logs/app.jsonl
```

Browser errors (JavaScript errors, failed requests, lost event stream) are reported to `POST /api/client-log` and
appear as `ui.error` events.

To get a bug report in one file, press the pulse-shaped **Download diagnostics** button next to *Download log* (or
`GET /api/diag`). The JSON contains versions, the config with secrets masked, process uid/gid/umask, free space and
filesystem of `MUSIC_DIR` and `DATA_DIR`, database counts, the last runs, the current job status, the last 2000
events and the errors of the last 7 days. It needs the same login as the rest of the UI.

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
