from celery import Celery
from celery.schedules import crontab

from core.config import settings

celery_app = Celery(
    "copybot",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["worker.tasks", "worker.main"],
)

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
        "worker.tasks.refresh_donor_stats": {"queue": "periodic"},
    },
    beat_schedule={
        "refresh-donor-stats-nightly": {
            "task": "worker.tasks.refresh_donor_stats",
            "schedule": crontab(hour=3, minute=0),  # 03:00 UTC nightly
        },
        "deactivate-bad-donors": {
            "task": "worker.tasks.deactivate_underperforming_donors",
            "schedule": crontab(hour=4, minute=0),
        },
    },
)
