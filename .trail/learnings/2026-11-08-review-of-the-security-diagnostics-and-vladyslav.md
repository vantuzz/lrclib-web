---
title: "Review of the security, diagnostics and limits PRs: four fixes taken, narrow risks accepted on purpose"
author: Vladyslav
date: 2026-11-08
branch: fix/review-findings
cost: { minutes: 20, tokens: 151584 }
id: l-dc803998
scope: repo
status: active
src: agent
touches:
  - CHANGELOG.md
  - ai-journal/lrclib-web.md
  - lrclib_web/__main__.py
  - lrclib_web/diaglog.py
  - lrclib_web/web.py
  - tests/integration/test_diag_api.py
  - tests/unit/test_diaglog.py
---

## Decided
- The owner chose to merge the stacked PRs first and fix review findings in a follow-up PR.
- Fixed: thread-crash traceback also to stderr; the JSONL log pauses and retries after a write error instead of switching off until restart; a server-side rate limit on browser error reports; tool caches untracked.
- Accepted on purpose (home server, narrow cases): a symlinked folder outside the library is refused and the track sits in error for 90 days; a directory swapped for a symlink between check and write; a non-atomic write on filesystems without hard links (Unraid user shares have them); pids limit 64 to be checked during measurement.
- Debug-level events (each LRCLIB/Plex request) reach the file only with LOG_LEVEL=debug. This is documented, not a bug.

## Tried and ruled out
- Merging a stack locally and pushing main closes only the first PR. The later PRs target the previous stack branch, so that branch has to be fast-forwarded to the PR head. Moving a PR's own head branch at the same time leaves it open again.

## Watch out
- The temp file name uses the process id, which is 1 in every container. Two containers writing to the same library (the test one and the permanent one) can delete each other's temp files. Remove the test container before starting the permanent one.

In the code: lrclib_web/__main__.py thread_crash; lrclib_web/diaglog.py JsonlSink (RETRY_S 60, state() -> /api/diag log_sink); lrclib_web/web.py RateLimit, CLIENT_LOG_RATE (30, 600 s), HTTP 429, refused_before.
