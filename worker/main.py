"""
Starts the Polymarket WebSocket listener when the Celery worker becomes ready.
"""

import asyncio
import threading

import structlog
from celery.signals import worker_ready

log = structlog.get_logger(__name__)


def _run_ws_loop() -> None:
    from core.polymarket import run_ws_listener
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    log.info("ws_listener_starting")
    loop.run_until_complete(run_ws_listener())


@worker_ready.connect
def start_ws_listener(**kwargs) -> None:  # type: ignore[no-untyped-def]
    t = threading.Thread(target=_run_ws_loop, daemon=True)
    t.start()
    log.info("ws_listener_thread_started")
