"""
Celery worker startup hook: launch the real-time WebSocket whale detector
in a background daemon thread when the worker becomes ready.
"""

import threading

import structlog
from celery.signals import worker_ready

log = structlog.get_logger(__name__)

_started = False


@worker_ready.connect
def start_ws_listener(**kwargs) -> None:  # type: ignore[no-untyped-def]
    global _started
    if _started:
        return
    _started = True

    def _run() -> None:
        from worker.ws_listener import run_listener
        try:
            run_listener()
        except Exception:
            log.exception("ws_listener_crashed")

    thread = threading.Thread(target=_run, name="ws-whale-listener", daemon=True)
    thread.start()
    log.info("ws_listener_thread_started")
