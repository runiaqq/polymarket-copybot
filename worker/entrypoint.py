"""
Worker entrypoint: runs the Celery worker (gevent pool).

Beat runs as a SEPARATE process (see worker/beat.py + the `beat` compose
service) — Celery forbids the embedded `--beat` flag with the gevent pool.
"""

import sys

from celery.__main__ import main as celery_main

if __name__ == "__main__":
    sys.argv = [
        "celery",
        "-A", "worker.celery_app.celery_app",
        "worker",
        "--loglevel=info",
        "--queues=trades,ai,periodic",
        # gevent pool: order placement is network I/O, so hundreds of greenlets
        # let ALL subscribers' orders fire near-simultaneously (no 8-slot queue).
        "--pool=gevent",
        "--concurrency=100",
    ]
    celery_main()
