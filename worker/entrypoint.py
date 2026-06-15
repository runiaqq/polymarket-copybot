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
        "--concurrency=8",
    ]
    celery_main()
