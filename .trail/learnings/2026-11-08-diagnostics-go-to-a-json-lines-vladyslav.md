---
title: "Diagnostics go to a JSON-lines file, not the on-screen log; manual API tests need the anti-CSRF header"
author: Vladyslav
date: 2026-11-08
branch: feat/diagnostics-logging
cost: { minutes: 16, tokens: 85046 }
id: l-fa551c07
scope: repo
status: active
src: agent
touches:
  - CHANGELOG.md
  - README.md
  - ai-journal/lrclib-web.md
  - lrclib_web/__main__.py
  - lrclib_web/diaglog.py
  - lrclib_web/jobs.py
  - lrclib_web/lrclib.py
  - lrclib_web/matcher.py
  - lrclib_web/plex.py
  - lrclib_web/scanner.py
---

## Decided
- Two outputs from one logger: the on-screen log and SSE stay short and human-readable; the JSON-lines file under the data dir gets every event with a stable dotted name, run id and fields. Detailed events (match decisions, LRCLIB/Plex requests) are file-only so the UI log does not change.
- Problems are handed to an AI agent as one "Download diagnostics" file (versions, redacted config, dir checks, DB summary, recent events and errors), so there is no need to shell into the container.

## Watch out
- Every POST needs the header X-Requested-With: lrclib-web (cross-origin protection). A curl test without it gets 403, which looks like an auth bug but is not.
- The diagnostics config also strips query strings and userinfo from URLs, since a Plex URL can carry a token.

In the code: lrclib_web/diaglog.py (JsonlSink, emit, build_diag), LogBus.event in lrclib_web/jobs.py, POST check in lrclib_web/web.py, reporter in lrclib_web/static/app.js.
