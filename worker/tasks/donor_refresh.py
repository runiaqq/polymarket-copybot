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


def check_donor_streaks() -> dict:
    """BP42: pause a tracked wallet whose last N unique copied markets all lost.

    Plain function (not a Celery task) — called at the end of sync_positions
    every 2 min, right after resolutions land in copy_trades. Cheap: two
    indexed reads over the last 7 days.
    """
    import time
    from datetime import datetime, timedelta, timezone

    from core.config import settings
    from core.db import get_supabase, list_tracked_wallets
    from core.donor_guard import notify_admins, parse_ts, pause_decision

    wallets = list_tracked_wallets()
    if not wallets:
        return {"skipped": "no_tracked_wallets"}

    sb = get_supabase()
    now_ts = time.time()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    trades = (
        sb.table("copy_trades")
        .select("condition_id,realized_pnl,resolved_at,signal_id")
        .gte("resolved_at", cutoff)
        .not_.is_("realized_pnl", "null")
        .order("resolved_at", desc=True)
        .limit(2000)
        .execute()
        .data or []
    )
    sig_ids = sorted({t["signal_id"] for t in trades if t.get("signal_id")})
    sig_to_donor: dict[int, str] = {}
    for i in range(0, len(sig_ids), 200):
        rows = (
            sb.table("trade_signals").select("id,source_wallet")
            .in_("id", sig_ids[i:i + 200]).execute().data or []
        )
        for r in rows:
            if r.get("source_wallet"):
                sig_to_donor[r["id"]] = r["source_wallet"].lower()

    by_donor: dict[str, list[tuple[str, float, float]]] = {}
    for t in trades:  # already newest-first
        donor = sig_to_donor.get(t.get("signal_id"))
        ts = parse_ts(t.get("resolved_at"))
        if not donor or ts is None or not t.get("condition_id"):
            continue
        by_donor.setdefault(donor, []).append(
            (t["condition_id"], ts, float(t["realized_pnl"] or 0))
        )

    paused = []
    for w in wallets:
        addr = (w.get("address") or "").lower()
        if not addr:
            continue
        if pause_decision(
            by_donor.get(addr, []),
            settings.donor_pause_loss_streak,
            parse_ts(w.get("paused_until")),
            now_ts,
        ):
            until = datetime.now(timezone.utc) + timedelta(hours=settings.donor_pause_hours)
            sb.table("tracked_wallets").update(
                {"paused_until": until.isoformat()}
            ).eq("id", w["id"]).execute()
            paused.append(addr)
            label = w.get("label") or addr[:10]
            log.warning("donor_loss_streak_paused", wallet=addr[:10],
                        streak=settings.donor_pause_loss_streak,
                        until=until.isoformat())
            notify_admins(
                f"⏸ <b>Донор на паузе: {label}</b>\n"
                f"<code>{addr}</code>\n\n"
                f"{settings.donor_pause_loss_streak} подряд убыточных рынков — "
                f"копирование остановлено до "
                f"{until.strftime('%d.%m %H:%M')} UTC "
                f"(возобновится автоматически)."
            )
    return {"paused": paused}


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
