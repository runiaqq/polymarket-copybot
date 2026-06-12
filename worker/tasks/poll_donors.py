"""
Celery task: poll donor wallets for new trades every 30 seconds.
Replaces WebSocket approach — simpler and more reliable for MVP.
"""

from datetime import datetime, timezone

import structlog

from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

# In-memory cache of last seen trade IDs per donor to avoid duplicates
_seen_trades: dict[str, set[str]] = {}


@celery_app.task(name="worker.tasks.poll_donor_trades", queue="periodic")
def poll_donor_trades() -> dict:
    from core.db import get_active_donor_addresses, get_active_subscribers, get_supabase
    from core.polymarket import fetch_donor_recent_trades
    from worker.tasks import execute_copy_trade, run_ai_analysis

    sb = get_supabase()
    res = sb.table("donor_wallets").select("*").eq("active", True).execute()
    donors = res.data or []

    if not donors:
        log.debug("no_active_donors")
        return {"signals": 0}

    subscribers = get_active_subscribers()
    if not subscribers:
        log.debug("no_active_subscribers")
        return {"signals": 0}

    user_ids = [u["id"] for u in subscribers]
    signals_dispatched = 0

    # DB-level dedup: load recent tx hashes from DB to survive restarts
    try:
        recent_sigs = sb.table("trade_signals") \
            .select("source_tx_hash") \
            .not_.is_("source_tx_hash", "null") \
            .order("created_at", desc=True) \
            .limit(500) \
            .execute()
        db_seen = {r["source_tx_hash"] for r in (recent_sigs.data or [])}
    except Exception:
        db_seen = set()

    for donor in donors:
        address = donor["address"]
        trades = fetch_donor_recent_trades(address, limit=5)

        if address not in _seen_trades:
            # First poll — seed in-memory cache, don't copy
            _seen_trades[address] = {t.get("id", t.get("trade_id", "")) for t in trades}
            continue

        for trade in trades:
            trade_id = trade.get("id") or trade.get("trade_id", "")
            if not trade_id:
                continue
            # Check both in-memory and DB caches
            if trade_id in _seen_trades[address] or trade_id in db_seen:
                continue

            _seen_trades[address].add(trade_id)
            db_seen.add(trade_id)

            # Parse trade fields (size_usdc already normalised by fetch_donor_recent_trades)
            price = float(trade.get("price", 0))
            size_usdc = float(trade.get("size_usdc") or trade.get("size", 0))
            side = (trade.get("side") or "BUY").upper()

            if size_usdc < settings.min_trade_size_usdc:
                continue

            # Filter by market closing time if configured
            if settings.min_market_hours_to_close > 0:
                end_date = trade.get("end_date_iso") or trade.get("endDate") or trade.get("end_date")
                if end_date:
                    from dateutil.parser import parse as parse_dt
                    try:
                        closes_at = parse_dt(end_date).replace(tzinfo=timezone.utc) if parse_dt(end_date).tzinfo is None else parse_dt(end_date)
                        hours_left = (closes_at - datetime.now(timezone.utc)).total_seconds() / 3600
                        if hours_left < settings.min_market_hours_to_close:
                            continue
                    except Exception:
                        pass

            # Only copy BUY-side trades for MVP
            if side not in ("BUY",):
                continue

            signal = {
                "market_id":    trade.get("market") or trade.get("condition_id", ""),
                "token_id":     trade.get("asset_id") or trade.get("token_id"),
                "title":        trade.get("title", ""),
                "side":         side,
                "price":        price,
                "size_usdc":    size_usdc,
                "source_tx_hash": trade_id,
                "donor_address": address,
                "donor_db_id":  donor.get("id", 1),
                "donor_label":  donor.get("label") or address[:8],
                "donor_win_rate": donor.get("win_rate_30d"),
                "donor_roi":    donor.get("roi_30d"),
                "timestamp":    datetime.now(timezone.utc).isoformat(),
            }

            log.info("signal_found", market=signal["market_id"],
                     side=signal["side"], size=size_usdc, donor=address[:10])

            for user in subscribers:
                execute_copy_trade.delay(user["id"], signal)

            run_ai_analysis.delay(signal, user_ids)
            signals_dispatched += 1

    log.info("poll_complete", donors=len(donors), signals=signals_dispatched)
    return {"signals": signals_dispatched}
