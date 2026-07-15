"""
Standalone Celery beat scheduler.

Runs as its own process/container because the embedded `--beat` flag is
incompatible with the worker's gevent pool. Only ONE beat instance must run
at a time (it owns the periodic schedule defined in worker.celery_app).

BP26.5: also hosts the real-time sniper WebSocket listener as a daemon
thread. Beat is the right home for it: exactly one replica (no duplicate
listeners when the worker scales) and a plain non-gevent interpreter (the
worker's gevent monkey-patching breaks asyncio/socket threads — see BP23).
"""

import sys
import threading

from celery.__main__ import main as celery_main


def _start_sniper_ws() -> None:
    from core.config import settings

    if not (settings.auto_copy_enabled and settings.sniper_ws_enabled):
        return

    def _run() -> None:
        from worker.sniper_ws import run_listener
        try:
            run_listener()
        except Exception:
            import structlog
            structlog.get_logger(__name__).exception("sniper_ws_thread_crashed")

    threading.Thread(target=_run, name="sniper-ws", daemon=True).start()


if __name__ == "__main__":
    _start_sniper_ws()
    sys.argv = [
        "celery",
        "-A", "worker.celery_app.celery_app",
        "beat",
        "--loglevel=info",
    ]
    celery_main()
