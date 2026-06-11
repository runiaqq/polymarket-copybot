"""
Nightly tasks: refresh donor stats, deactivate underperformers.
"""

import structlog

from worker.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(name="worker.tasks.refresh_donor_stats", queue="periodic")
def refresh_donor_stats() -> dict:
    """Placeholder — wire to Dune Analytics when query ID is configured."""
    log.info("donor_stats_refresh_skipped", reason="dune_query_not_configured")
    return {"skipped": True}


@celery_app.task(name="worker.tasks.deactivate_underperforming_donors", queue="periodic")
def deactivate_underperforming_donors() -> dict:
    from core.db import get_supabase

    sb = get_supabase()
    res = sb.table("donor_wallets").select("id,address,win_rate_30d,roi_30d").eq("active", True).execute()

    deactivated = []
    for donor in res.data:
        win_rate = donor.get("win_rate_30d")
        roi = donor.get("roi_30d")
        should_deactivate = (
            (win_rate is not None and win_rate < 0.45)
            or (roi is not None and roi < -0.20)
        )
        if should_deactivate:
            sb.table("donor_wallets").update({"active": False}).eq("id", donor["id"]).execute()
            deactivated.append(donor["address"])
            log.warning("donor_deactivated", address=donor["address"], win_rate=win_rate, roi=roi)

    return {"deactivated": deactivated}
