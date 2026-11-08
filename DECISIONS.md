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
