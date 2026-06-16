"""
Standalone Celery beat scheduler.

Runs as its own process/container because the embedded `--beat` flag is
incompatible with the worker's gevent pool. Only ONE beat instance must run
at a time (it owns the periodic schedule defined in worker.celery_app).
"""

import sys

from celery.__main__ import main as celery_main

if __name__ == "__main__":
    sys.argv = [
        "celery",
        "-A", "worker.celery_app.celery_app",
        "beat",
        "--loglevel=info",
    ]
    celery_main()
