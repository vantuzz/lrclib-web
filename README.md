# lrclib-web

Self-hosted web UI for mass-downloading synced lyrics (`.lrc`) from [LRCLIB](https://lrclib.net) for a local music library.
Mobile-first single page: library scan, lyrics download with live log, nightly schedule, optional Plex metadata refresh.

> **Status:** design phase — no code yet. See [`DECISIONS.md`](DECISIONS.md) (owner decisions) and
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (design v0.1). Task journal: [`ai-journal/lrclib-web.md`](ai-journal/lrclib-web.md).

## Open questions (waiting for owner before implementation)

1. Nightly 04:00 `quick` scan + weekly `full` scan (e.g. Sun 03:00)? Any scan at 04:00 spins up array disks. Recommended: yes.
2. Search fallback: additionally require normalized title-core equality and reject one-sided qualifiers
   (live/remix/acoustic/…) — extends D5. Recommended: yes.
3. When `.lrc` appears later for a track that has our `.txt`: keep `.txt` (default) or delete it? Recommended: keep, revisit after Plex test.
4. Register an LRCLIB application (`Lrclib-App-Id`, ~2x rate limit)? Optional, owner decides.
5. Scope: full design (~1.5–2 days) or MVP cut (defer job history table, per-run log files, FULL_SCAN_CRON, RESUME_ON_START)?

## Next step

Implementation per ARCHITECTURE.md §12 repo layout, in a new chat:

```
Continue task lrclib-web from journal ai-journal/lrclib-web.md in repo vantuzz/lrclib-web (skill task-journal).
First resolve README "Open questions" with Vlad, then implement per ARCHITECTURE.md.
```

## License

MIT
