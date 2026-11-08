# lrclib-web — decisions (Vlad, 2026-11-08)
1. Existing .lrc next to audio: SKIP. Phase 2 (later): separate "update existing .lrc" mode (content-hash based; LRCLIB has no dates — verified: no date field in API/lyricsfile YAML, no Last-Modified/ETag).
2. Output: sidecar files only — synced -> .lrc, plain-only -> .txt. No tag embedding.
3. Trigger: manual buttons + built-in schedule, default nightly 04:00 (container TZ), configurable.
4. Deployment: separate container, new public repo vantuzz/lrclib-web, image on GHCR (GitHub Actions).
5. Matching: GET /api/get exact (title, artist, album, duration); on 404 -> fallback /api/search, accept only same artist (normalized) and |duration diff| <= 2 s.
6. Instrumental: remember in state, write no file, do not re-query.
7. Auth: optional Basic auth via env; open when unset (LAN/VPN).
8. Integration: Plex — after a run that wrote new files, trigger Plex "Refresh Metadata" for affected items (forum: sidecar lyrics need refresh on album-artist level; not picked up on playback). Needs PLEX_URL, PLEX_TOKEN, path mapping container->Plex. No Telegram.
9. UI language: English. Mobile-first single page: library update (scan) block, lyrics download block with progress, counters, live log. No library browsing.
10. LRCLIB rules: identify via User-Agent; sequential requests, 200-500 ms gap; honor Retry-After (+<=30% jitter) on 429/503; optional Lrclib-App-Id.
11. Schedule (Q1): nightly 04:00 `quick` run + weekly `full` scan, FULL_SCAN_CRON default `0 3 * * 0`.
12. Search fallback (Q2): also require equal normalized title core; reject a qualifier (live/remix/acoustic/…) present on one side only.
13. `.txt` when `.lrc` arrives later (Q3): keep (TXT_ON_SYNCED=keep); revisit after Plex test.
14. LRCLIB App-Id (Q4): optional env LRCLIB_APP_ID, empty by default; registration is the owner's call later.
15. Scope (Q5): MVP — deferred to phase 2: job history API/table, per-run log files, RESUME_ON_START. FULL_SCAN_CRON stays (D11).
16. Missing guard (review #1): keep refusing when >MISSING_ABORT_PCT or root empty; the owner raises the env var for intentional removals. Download skips vanished audio.
17. Run budget (review #6): optional RUN_MAX_MINUTES, off by default; the download step stops cleanly, the next run continues.
18. Terminal states (review #12): re-check `instrumental` after 90 days; if a .lrc we wrote disappears, move synced -> pending once (no loop).
19. not_found/rejected re-check (review #13): escalate 14 -> 28 -> 56 -> 180 days; reset when the audio file changes.
20. Security review (2026-11-08): the app needs read of audio content (tags, duration) and directory write in MUSIC_DIR (sidecars); delete is allowed for lyrics only. Phase "security hardening" = items 21-23, one PR, no host changes.
21. File-operation guard in code: create/delete only `.lrc`, `.txt` and own `.<name>.tmp-<pid>` files; resolved path must stay inside MUSIC_DIR (realpath); tmp opened with O_EXCL|O_NOFOLLOW; drop the `os.replace` fallback that can clobber (no-hard-link FS: create target with O_EXCL instead). Tests for each rule.
22. PLEX_TOKEN optional: Plex is enabled by PLEX_URL alone, so a gateway (haproxy `mode http`) can inject X-Plex-Token and allow only GET /identity, GET /library/sections, GET /library/sections/{id}/all, GET /library/sections/{id}/refresh, PUT /library/metadata/{id}/refresh. README: example haproxy.cfg with these ACLs.
23. Container hardening defaults: compose.example.yml gets read_only, tmpfs /tmp, cap_drop ALL, no-new-privileges, pids/memory/cpu limits, port bound to a LAN IP; README "Security" section incl. optional sticky-bit + separate UID recipe (manual, owner's choice) and backups. Verify `--read-only` works on Unraid.
24. Resource limits (2026-11-08): measuring on a full library is deferred to the first run of the final version (PR #2 + #3, diagnostics give job durations). Now only size-independent settings: cpu_shares 256, blkio weight 100, memswap_limit = mem_limit (no swap -> OOM kill + restart); ceilings 256m / 1 CPU / 64 pids stay rough. A 2-minute sample on ~34k tracks: 63 MiB, 5 pids, no CPU throttling.
Deferred: pin base image digest + tagged releases; strict egress (internal network + allowlist proxy); sticky bit on the library (owner decides separately).
