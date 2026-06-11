"""
Worker entrypoint: starts WebSocket listener in a background thread,
then launches Celery in the main thread.
"""

import asyncio
import sys
import threading

import structlog

log = structlog.get_logger(__name__)


def _run_ws() -> None:
    from core.polymarket import run_ws_listener
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    log.info("ws_listener_starting")
    loop.run_until_complete(run_ws_listener())


def main() -> None:
    # Start WS listener in background thread
    t = threading.Thread(target=_run_ws, daemon=True, name="ws-listener")
    t.start()
    log.info("ws_listener_thread_started")

    # Hand off to Celery (blocks until worker exits)
    sys.argv = [
        "celery",
        "-A", "worker.celery_app.celery_app",
        "worker",
        "--loglevel=info",
        "--queues=trades,ai,periodic",
        "--concurrency=4",
    ]
    from celery.__main__ import main as celery_main
    celery_main()


if __name__ == "__main__":
    main()
