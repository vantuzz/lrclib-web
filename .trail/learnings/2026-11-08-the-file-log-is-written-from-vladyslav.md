---
title: "The file log is written from several threads, so its error state is guarded by a lock, and the race test needs forced pauses"
author: Vladyslav
date: 2026-11-08
branch: fix/review-findings
cost: { minutes: 8, tokens: 26752 }
id: l-1f361ee6
scope: repo
status: active
src: agent
touches:
  - CHANGELOG.md
  - ai-journal/lrclib-web.md
  - lrclib_web/__main__.py
  - lrclib_web/diaglog.py
  - tests/unit/test_diaglog.py
---

## Decided
- The JSONL log sink keeps shared state (paused or not, lost-event count, retry time). Several threads write to it, so every write and every state read holds one re-entrant lock. Without the lock, two writes failing together could report a false recovery and undercount lost events.
- The thread-crash hook writes the structured event first and then prints the traceback to stderr inside a try. A broken stderr must not stop the event.

## Tried and ruled out
- A plain multi-threaded test (8 threads x 200 failing writes) passed even with the lock removed, because the GIL rarely switches threads inside the tiny window. Adding a pause only inside the failing write was not enough either.

## Watch out
- The race test only catches the bug with two forced pauses: one inside the failing write (2 ms) and one right after the failure is recorded (1 ms). Keep both. Check that any new race test fails with the fix removed.

In the code: lrclib_web/diaglog.py JsonlSink._lock, write() -> _write(); lrclib_web/__main__.py thread_crash; tests/unit/test_diaglog.py test_concurrent_failing_writes_are_all_counted_and_never_reported_as_recovered.
