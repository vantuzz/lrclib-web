"""Entry point: python -m lrclib_web"""

from __future__ import annotations

import os
import signal
import sys
import threading
import traceback

from . import __version__
from .config import Config
from .db import DB, data_dir_problem
from .diaglog import emit
from .jobs import JobRunner, LogBus
from .plex import Plex
from .scheduler import Scheduler
from .web import App, serve


def main() -> int:
    cfg = Config.from_env()
    try:
        os.umask(int(cfg.umask, 8))
    except ValueError:
        pass
    log = LogBus(level=cfg.log_level)
    problem = data_dir_problem(cfg.data_dir)
    if not problem:
        log.attach_file(os.path.join(cfg.data_dir, "logs"))  # JSONL event log; runs without it if impossible
    log("info", f"lrclib-web {__version__} starting; music={cfg.music_dir} data={cfg.data_dir}",
        event="app.start", python=sys.version.split()[0], pid=os.getpid(),
        uid=os.getuid() if hasattr(os, "getuid") else None, log_file=log.sink.path if log.sink else None)
    if not os.path.isdir(cfg.music_dir):
        log("error", f"MUSIC_DIR {cfg.music_dir} is not a directory", event="app.config_error")
    if problem:
        log("error", problem, event="app.config_error")
        return 2

    def thread_crash(args):
        emit(log, "error", "thread.crash", f"thread {args.thread.name if args.thread else '?'} crashed: "
             f"{args.exc_type.__name__}: {args.exc_value}", ui=True,
             error=f"{args.exc_type.__name__}: {args.exc_value}",
             traceback="".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    threading.excepthook = thread_crash
    db = DB(os.path.join(cfg.data_dir, "lrclib-web.sqlite3"))
    plex = Plex(cfg, db, log) if cfg.plex_enabled else None
    if plex:
        threading.Thread(target=plex.check, name="plex-check", daemon=True).start()
    runner = JobRunner(cfg, db, log, plex=plex)
    sched = Scheduler(runner, cfg.schedule_cron, cfg.full_scan_cron, log)
    sched.start()
    srv = serve(App(cfg, db, log, runner, plex, sched))
    log("info", f"listening on http://{cfg.bind}:{cfg.port}"
        + (" (basic auth on)" if cfg.auth_enabled else " (no auth)"))

    def shutdown(*_):
        log("info", "shutting down")
        runner.stop()
        sched.stop()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        runner.join(35)
    return 0


if __name__ == "__main__":
    sys.exit(main())
