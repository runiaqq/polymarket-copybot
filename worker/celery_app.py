import ssl

from celery import Celery
from celery.schedules import crontab

from core.config import settings

_redis_url = settings.redis_url

celery_app = Celery(
    "copybot",
    broker=_redis_url,
    backend=_redis_url,
    include=[
        "worker.tasks",
        "worker.tasks.scan_markets",
        "worker.tasks.manage_positions",
        "worker.tasks.subscriptions",
        "worker.tasks.wallet_ops",
        "worker.tasks.poll_donors",
        "worker.tasks.monitor_deposits",
        "worker.signals",  # registers the worker_ready hook that starts the WS listener
    ],
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
        "worker.tasks.close_position": {"queue": "trades"},
        "worker.tasks.wrap_collateral": {"queue": "trades"},
        "worker.tasks.withdraw_funds": {"queue": "trades"},
        "worker.tasks.run_ai_analysis": {"queue": "ai"},
        "worker.tasks.sync_positions": {"queue": "periodic"},
        "worker.tasks.check_subscription_expiry": {"queue": "periodic"},
        "worker.tasks.scan_whale_trades": {"queue": "periodic"},
        "worker.tasks.poll_donor_trades": {"queue": "periodic"},
        "worker.tasks.refresh_donor_stats": {"queue": "periodic"},
        "worker.tasks.deactivate_underperforming_donors": {"queue": "periodic"},
    },
    beat_schedule={
        # Detection runs in real time over WebSocket (worker/ws_listener.py),
        # not via periodic polling. The REST scan_whale_trades task remains
        # available as a manual fallback but is intentionally not scheduled.
        "monitor-deposits": {
            "task": "worker.tasks.monitor_deposits",
            "schedule": 120.0,  # every 2 minutes
        },
        "sync-positions": {
            "task": "worker.tasks.sync_positions",
            "schedule": float(settings.positions_sync_sec),
        },
        "check-subscription-expiry": {
            "task": "worker.tasks.check_subscription_expiry",
            "schedule": 21600.0,  # every 6 hours
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
