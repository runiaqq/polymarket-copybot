"""
Worker entry point.
Runs the Polymarket WebSocket listener in an asyncio event loop
alongside the Celery worker process.
"""

import asyncio
import threading

import structlog

log = structlog.get_logger(__name__)


def start_ws_listener_thread() -> None:
    """Run the WebSocket listener in a background thread with its own event loop."""
    from core.polymarket import run_ws_listener

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    log.info("ws_listener_thread_starting")
    loop.run_until_complete(run_ws_listener())


# Start the WS listener when the worker process boots
_ws_thread = threading.Thread(target=start_ws_listener_thread, daemon=True)
_ws_thread.start()
log.info("ws_listener_thread_started")
