"""Built-in scheduler: evaluates cron expressions once per minute in container TZ (ARCHITECTURE.md §8)."""

from __future__ import annotations

import threading
import traceback
from datetime import datetime

from .cron import Cron, CronError
from .diaglog import emit
from .jobs import Busy, JobRunner


class Scheduler:
    def __init__(self, runner: JobRunner, schedule_cron: str, full_scan_cron: str, log, clock=datetime.now):
        self.runner, self.log, self.clock = runner, log, clock
        self.entries: list[tuple[Cron, str, str]] = []  # (cron, job type, mode)
        for expr, jtype, mode, name in (
            (schedule_cron, "run", "quick", "SCHEDULE_CRON"),
            (full_scan_cron, "scan", "full", "FULL_SCAN_CRON"),
        ):
            if not expr.strip():
                continue
            try:
                self.entries.append((Cron(expr), jtype, mode))
            except CronError as e:
                emit(log, "error", "scheduler.config_error", f"{name}={expr!r} ignored: {e}", name=name, expr=expr)
        self._stop = threading.Event()
        self._last_minute: datetime | None = None

    def next_runs(self) -> list[dict]:
        t = self.clock()
        out = []
        for cron, jtype, mode in self.entries:
            nxt = cron.next_after(t)
            out.append({"cron": cron.expr, "type": jtype, "mode": mode,
                        "next_at": nxt.isoformat(timespec="minutes") if nxt else None})
        return out

    def tick(self) -> None:
        t = self.clock().replace(second=0, microsecond=0)
        if t == self._last_minute:
            return
        self._last_minute = t
        for cron, jtype, mode in self.entries:
            if cron.matches(t):
                try:
                    job_id = self.runner.start(jtype, mode, trigger="schedule")
                    emit(self.log, "info", "scheduler.fire", f"schedule {cron.expr}: started {jtype} {mode}",
                         ui=False, cron=cron.expr, type=jtype, mode=mode, job_id=job_id)
                except Busy:
                    emit(self.log, "warn", "scheduler.skipped",
                         f"schedule {cron.expr}: skipped ({jtype} {mode}), a job is running",
                         cron=cron.expr, type=jtype, mode=mode)

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception as e:  # noqa: BLE001
            emit(self.log, "error", "scheduler.error", f"scheduler: {e}", error=f"{type(e).__name__}: {e}",
                 traceback=traceback.format_exc())

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._safe_tick()
            now = self.clock()
            self._stop.wait(max(1.0, 60 - now.second - now.microsecond / 1e6 + 0.5))

    def start(self) -> None:
        threading.Thread(target=self._loop, name="scheduler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
