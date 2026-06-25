import ssl

from celery import Celery

from core.config import settings

_redis_url = settings.redis_url

# AUTO_COPY_ENABLED is the master switch. Keep it OFF until the worker is hosted in
# a non-geoblocked region (Polymarket blocks order placement from US/DE/NL/etc.).
# When ON: the WS whale listener + periodic position/deposit/subscription tasks run.
_AUTO = settings.auto_copy_enabled

_includes = [
    "worker.tasks",
    "worker.tasks.scan_markets",
    "worker.tasks.manage_positions",
    "worker.tasks.subscriptions",
    "worker.tasks.wallet_ops",
    "worker.tasks.poll_donors",
    "worker.tasks.poll_tracked_wallets",
    "worker.tasks.monitor_deposits",
    "worker.tasks.execute_copy",
]
# Model B copies a curated whitelist via polling (poll_tracked_wallets), so the
# anonymous WS large-buy listener is no longer the entry source and stays off.

celery_app = Celery(
    "copybot",
    broker=_redis_url,
    backend=_redis_url,
    include=_includes,
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
    # BP7 hygiene: expire task result keys after 1 h to prevent the
    # celery-task-meta-* key explosion observed in prod (7800+ keys).
    result_expires=3600,
    task_routes={
        "worker.tasks.execute_copy_trade": {"queue": "trades"},
        "worker.tasks.close_position": {"queue": "trades"},
        "worker.tasks.redeem_position": {"queue": "trades"},
        "worker.tasks.wrap_collateral": {"queue": "trades"},
        "worker.tasks.withdraw_funds": {"queue": "trades"},
        "worker.tasks.run_ai_analysis": {"queue": "ai"},
        "worker.tasks.sync_positions": {"queue": "periodic"},
        "worker.tasks.reconcile_settlements": {"queue": "periodic"},
        "worker.tasks.backfill_legacy_redemptions": {"queue": "periodic"},
        "worker.tasks.check_subscription_expiry": {"queue": "periodic"},
        "worker.tasks.scan_whale_trades": {"queue": "periodic"},
        "worker.tasks.poll_donor_trades": {"queue": "periodic"},
        "worker.tasks.refresh_donor_stats": {"queue": "periodic"},
        "worker.tasks.deactivate_underperforming_donors": {"queue": "periodic"},
    },
    # Periodic tasks only run in auto-copy mode (and thus only on a non-geoblocked host).
    beat_schedule=({
        "poll-tracked-wallets": {
            "task": "worker.tasks.poll_tracked_wallets",
            "schedule": settings.tracked_poll_sec,  # copy the whitelist (Model B)
        },
        "sync-positions": {
            "task": "worker.tasks.sync_positions",
            "schedule": 120.0,  # TP/SL + resolution checks every 2 min
        },
        # BP1: on-chain settlement reconciler — catches neg-risk positions that
        # vanish from the Data API before appearing as 'redeemable'.
        "reconcile-settlements": {
            "task": "worker.tasks.reconcile_settlements",
            "schedule": 120.0,
        },
        # BP1 GAP: backfill legacy positions (NULL ledger fields) and self-heal
        # stranded USDC.e on deposit wallets.  Runs every 10 min — lightweight
        # because it only acts on positions the Data API marks redeemable or that
        # on-chain state confirms as won.
        "backfill-legacy-redemptions": {
            "task": "worker.tasks.backfill_legacy_redemptions",
            "schedule": 600.0,
        },
        "monitor-deposits": {
            "task": "worker.tasks.monitor_deposits",
            "schedule": 120.0,  # detect deposits, auto-fund the deposit wallet
        },
        "check-subscription-expiry": {
            "task": "worker.tasks.check_subscription_expiry",
            "schedule": 21600.0,  # every 6 hours
        },
    } if _AUTO else {}),
)
