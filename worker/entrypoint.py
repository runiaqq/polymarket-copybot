"""
Worker entrypoint: runs Celery worker + beat scheduler in one process.
Beat handles the 30-second donor polling schedule.
"""

import sys

from celery.__main__ import main as celery_main

if __name__ == "__main__":
    sys.argv = [
        "celery",
        "-A", "worker.celery_app.celery_app",
        "worker",
        "--beat",                       # embed beat scheduler
        "--loglevel=info",
        "--queues=trades,ai,periodic",
        # gevent pool: order placement is network I/O, so hundreds of greenlets
        # let ALL subscribers' orders fire near-simultaneously (no 8-slot queue).
        "--pool=gevent",
        "--concurrency=100",
    ]
    celery_main()
