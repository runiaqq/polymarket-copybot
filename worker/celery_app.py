import ssl

from celery import Celery
from celery.schedules import crontab

from core.config import settings

_redis_url = settings.redis_url

celery_app = Celery(
    "copybot",
    broker=_redis_url,
    backend=_redis_url,
    include=["worker.tasks", "worker.tasks.poll_donors"],
)

# Upstash Redis uses TLS — Celery needs explicit SSL config for rediss://
if _redis_url.startswith("rediss://"):
    _ssl_opts = {"ssl_cert_reqs": ssl.CERT_NONE}
    celery_app.conf.broker_use_ssl = _ssl_opts
    celery_app.conf.redis_backend_use_ssl = _ssl_opts

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,   # fair dispatch — important for trade tasks
    task_routes={
        "worker.tasks.execute_copy_trade": {"queue": "trades"},
        "worker.tasks.run_ai_analysis": {"queue": "ai"},
        "worker.tasks.poll_donor_trades": {"queue": "periodic"},
        "worker.tasks.refresh_donor_stats": {"queue": "periodic"},
        "worker.tasks.deactivate_underperforming_donors": {"queue": "periodic"},
    },
    beat_schedule={
        "poll-donor-trades": {
            "task": "worker.tasks.poll_donor_trades",
            "schedule": 30.0,
        },
        "refresh-donor-stats-nightly": {
            "task": "worker.tasks.refresh_donor_stats",
            "schedule": crontab(hour=3, minute=0),
        },
        "deactivate-bad-donors": {
            "task": "worker.tasks.deactivate_underperforming_donors",
            "schedule": crontab(hour=4, minute=0),
        },
    },
)
