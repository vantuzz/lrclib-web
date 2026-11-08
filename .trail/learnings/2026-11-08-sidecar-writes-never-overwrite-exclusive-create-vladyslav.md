---
title: "Sidecar writes never overwrite: exclusive create everywhere, and a leftover temp file with our pid is ours to delete"
author: Vladyslav
date: 2026-11-08
branch: feat/security-hardening
cost: { minutes: 6, tokens: 84993 }
id: l-6e93f24c
scope: repo
status: active
src: agent
touches:
  - CHANGELOG.md
  - README.md
  - ai-journal/lrclib-web.md
  - compose.example.yml
  - lrclib_web/config.py
  - lrclib_web/jobs.py
  - lrclib_web/plex.py
  - lrclib_web/writer.py
  - tests/unit/test_plex.py
  - tests/unit/test_writer.py
---

## Decided
- Every file the app creates is opened exclusive + no-follow, including the fallback for filesystems without hard links (it creates the target itself instead of renaming over it). Not atomic there, but never clobbers; a partial target is removed on failure.
- A temp file that already exists with our name is deleted and recreated. In the container the app is always pid 1, so a crash leaves a temp with the same name the next run wants; refusing it would block that track forever.
- Only the directory part of a path is resolved for the inside-MUSIC_DIR check; the last component is never followed (writes use no-follow, deletes refuse symlinks).

## Tried and ruled out
- Keeping os.replace as the no-hard-link fallback: it overwrites a target created between the exists-check and the rename.

## Watch out
- Guard violations raise UnsafePathError, a subclass of PermanentWriteError, so the job loop records them as permanent errors without new handling.

In the code: lrclib_web/writer.py (guard_sidecar, _open_tmp, _publish, remove_sidecar); call sites in lrclib_web/jobs.py pass cfg.music_dir as root.
