"""
Model B — copy a curated whitelist of profitable wallets.

Every few seconds we poll each tracked wallet's recent activity. When a tracked
("pro") wallet makes a fresh BUY on a fast-resolving market, we copy it to every
subscriber. If multiple tracked wallets back the SAME market, that's "consensus"
(higher confidence) — surfaced in the AI score / notification, never blocks entry.
"""

import time
from datetime import datetime, timedelta, timezone

import structlog

from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

# In-memory dedup of (wallet:tx) already processed within a worker run.
_seen: set[str] = set()
_SEEN_MAX = 8000


def _consensus_count(sb, condition_id: str, token_id: str, this_wallet: str) -> int:
    """Distinct tracked wallets that entered this market+outcome recently (incl. current)."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=settings.consensus_window_hours)).isoformat()
        res = (
            sb.table("trade_signals")
            .select("source_wallet")
            .eq("market_id", condition_id)
            .eq("token_id", token_id)
            .gte("created_at", since)
            .execute()
        )
        wallets = {r["source_wallet"] for r in (res.data or []) if r.get("source_wallet")}
    except Exception:
        wallets = set()
    wallets.add(this_wallet)
    return len(wallets)


@celery_app.task(name="worker.tasks.poll_tracked_wallets", queue="periodic")
def poll_tracked_wallets() -> dict:
    if not settings.auto_copy_enabled:
        return {"skipped": "auto_copy_off"}

    from core.db import get_active_subscribers, get_supabase, insert_trade_signal, list_tracked_wallets
    from core.polymarket import fetch_donor_recent_trades, get_fast_markets
    from worker.tasks import execute_copy_trade

    wallets = list_tracked_wallets()
    if not wallets:
        return {"skipped": "no_tracked_wallets"}
    subscribers = get_active_subscribers()
    if not subscribers:
        return {"skipped": "no_subscribers"}

    fast = get_fast_markets()
    sb = get_supabase()
    now = time.time()
    user_ids = [u["id"] for u in subscribers]
    dispatched = 0

    for w in wallets:
        addr = (w.get("address") or "").lower()
        if not addr:
            continue
        try:
            trades = fetch_donor_recent_trades(addr, limit=15)
        except Exception:
            log.warning("tracked_fetch_failed", wallet=addr[:10])
            continue

        for t in trades:
            if (t.get("side") or "").upper() != "BUY":
                continue
            tx = t.get("tx_hash") or t.get("trade_id") or t.get("id")
            cond = t.get("condition_id")
            token = t.get("token_id")
            if not (tx and cond and token):
                continue
            ts = int(t.get("timestamp") or 0)
            if ts and (now - ts) > settings.tracked_max_trade_age_sec:
                continue
            key = f"{addr}:{tx}"
            if key in _seen:
                continue

            meta = fast.get(cond)
            if meta is None:
                _seen.add(key)
                continue  # not a fast/liquid market in range — skip

            # Cross-restart dedup via the persisted signal row.
            try:
                ex = sb.table("trade_signals").select("id").eq("source_tx_hash", tx).limit(1).execute()
                if ex.data:
                    _seen.add(key)
                    continue
            except Exception:
                pass
            _seen.add(key)

            outcome = (meta.get("token_outcomes") or {}).get(token, "")
            consensus = _consensus_count(sb, cond, token, addr)

            signal = {
                "market_id":        cond,
                "token_id":         token,
                "title":            meta.get("title") or t.get("title", ""),
                "outcome":          outcome,
                "side":             "BUY",
                "price":            t.get("price", 0),
                "size_usdc":        t.get("size_usdc", 0),
                "tick_size":        meta.get("tick_size", "0.01"),
                "neg_risk":         bool(meta.get("neg_risk", False)),
                "hours_to_resolve": meta.get("hours_to_resolve"),
                "event_slug":       meta.get("event_slug"),
                "source_tx_hash":   tx,
                "source_wallet":    addr,
                "consensus":        consensus,
                "whale_wallet":     addr,
            }
            try:
                row = insert_trade_signal({
                    "market_id":      cond,
                    "title":          signal["title"],
                    "side":           "BUY",
                    "price":          signal["price"],
                    "size_usdc":      signal["size_usdc"],
                    "token_id":       token,
                    "source_tx_hash": tx,
                    "source_wallet":  addr,
                    "consensus":      consensus,
                })
                signal["signal_id"] = row["id"]
            except Exception:
                log.exception("tracked_signal_insert_failed", market=cond[:14])
                continue

            for uid in user_ids:
                execute_copy_trade.delay(uid, signal)
            dispatched += 1
            log.info("tracked_signal", wallet=addr[:10], market=cond[:14],
                     outcome=outcome, consensus=consensus, subs=len(user_ids))

    if len(_seen) > _SEEN_MAX:
        _seen.clear()
    return {"dispatched": dispatched}
