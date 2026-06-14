import ssl

from celery import Celery

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
        # "worker.signals",  # PAUSED for V2 deposit-wallet rework — WS listener disabled
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
    # PAUSED for V2 deposit-wallet rework — no periodic tasks run.
    # Restore monitor-deposits / sync-positions / check-subscription-expiry when re-enabling.
    beat_schedule={},
)
