# lrclib-web — веб-інтерфейс для масового завантаження текстів з LRCLIB
**Jira:** —
**Ціль:** окремий Docker-сервіс: мобільна сторінка (сканування /music, завантаження .lrc з LRCLIB, лог), протестовано
## Журнал
- #0 2026-11-08 01:58 [start] журнал створено | next: app.py (stdlib+mutagen), Dockerfile, тест на реальному LRCLIB | D: Vlad обрав варіант 2 (свій сервіс); дефолти: пропуск наявних .lrc, plain→.txt, окремий контейнер | s:88e39f71 | r:13
- #1 2026-11-08 02:12 [done] рішення Влада зібрано у DECISIONS.md; чернетка → draft_app.py | state: paused | owner: agent | next: архітектура з Fable-субагентом → ARCHITECTURE.md, показати Владу | D: пропуск наявних .lrc (оновлення — етап 2), fuzzy search ±2с, Plex refresh, EN UI, repo vantuzz/lrclib-web public | s:88e39f71 | r:22
- #2 2026-11-08 02:21 [done] ARCHITECTURE.md від Fable (356 рядків, 15 розділів) перевірено; виправлено Q3 (Plex refresh artist — є підтвердження форуму) | files: ARCHITECTURE.md | state: waiting-vlad | owner: vlad | next: Влад відповідає на відкриті питання Q1,Q2,Q5,Q12 + погоджує обсяг; потім реалізація по модулях | s:88e39f71 | r:30
- #3 2026-11-08 02:39 [end] дизайн-фаза закрита; README з відкритими питаннями 1-5; файли переносяться в репо vantuzz/lrclib-web (public) через веб GitHub | files: README.md,DECISIONS.md,ARCHITECTURE.md | state: waiting-vlad | owner: vlad | next: новий чат: відповіді Влада на README Open questions → реалізація за ARCHITECTURE.md §12 | D: Vlad: репо створити зараз; реалізація в новому чаті (r>27) | s:88e39f71 | r:33
