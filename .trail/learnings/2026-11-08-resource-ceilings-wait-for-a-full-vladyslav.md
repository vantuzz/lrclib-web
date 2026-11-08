---
title: Resource ceilings wait for a full-library measurement; only priorities are set for now
author: Vladyslav
date: 2026-11-08
branch: feat/resource-limits
cost: { minutes: 17, tokens: 123914 }
id: l-3038c019
scope: repo
status: active
src: agent
touches:
  - ai-journal/lrclib-web.md
  - CHANGELOG.md
  - DECISIONS.md
  - README.md
  - compose.example.yml
---

## Decided
- The owner deferred measuring memory/CPU until the first run of the final version (security + diagnostics merged), because diagnostics then log each job's duration. Until then only size-independent settings ship: low CPU share, low disk I/O weight, no swap. The memory/CPU/pids ceilings are rough guesses, not measurements.
- A 2-minute sample on the whole library (~34k tracks) peaked at 63 MiB, 5 processes, no CPU throttling. A full scan and a long download were never observed.

## Tried and ruled out
- Measuring on the owner's test container as it was: it mounted one artist folder (39 tracks), so the numbers meant nothing. It had to be recreated with the whole library.

## Watch out
- The owner's test container is named lrclib-web-test (not lrclib-web) and was created with plain `docker run`, so Unraid has no Edit form for it. To change mounts, read its settings with `docker inspect` and recreate it. Copy PLEX_TOKEN from the old container into a shell variable, never print it.
- The Unraid host clock is UTC and the container uses Kyiv time, so CSV samples lag the app log by 2-3 hours.
- The server load average is already 24-28 when idle, which is why low priority matters.
- Docker does not restart an "unhealthy" container; only an exit (for example an OOM kill) triggers `restart: unless-stopped`.

In the code: compose.example.yml (cpu_shares 256, blkio_config.weight 100, memswap_limit = mem_limit 256m); README "Resource limits" has the Unraid Extra Parameters and the check commands (memory.peak, oom_kill, nr_throttled). Sampler: `docker stats --no-stream` every 5 s into /tmp/lrclib-stats.csv.
