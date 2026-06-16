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

# In-memory dedup: (wallet:market:token) -> last emit epoch. A sliced entry is
# one logical signal even though it lands as dozens of fills across poll cycles.
_seen: dict[str, float] = {}
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

    max_age = settings.tracked_max_trade_age_sec
    reentry_sec = settings.tracked_reentry_hours * 3600
    min_copy = settings.tracked_min_copy_usdc

    skipped_age_total = 0
    skipped_small_total = 0
    skipped_no_market_total = 0
    skipped_dedup_total = 0
    wallets_with_activity = 0

    for w in wallets:
        addr = (w.get("address") or "").lower()
        if not addr:
            continue
        try:
            trades = fetch_donor_recent_trades(addr, limit=settings.tracked_fetch_limit)
        except Exception:
            log.warning("tracked_fetch_failed", wallet=addr[:10])
            continue

        # Aggregate sliced fills: a whale that builds a position with dozens of
        # tiny buys in one market+outcome is ONE entry, not dozens of signals.
        groups: dict[tuple[str, str], dict] = {}
        skipped_old = 0
        for t in trades:
            if (t.get("side") or "").upper() != "BUY":
                continue
            cond = t.get("condition_id")
            token = t.get("token_id")
            tx = t.get("tx_hash") or t.get("trade_id") or t.get("id")
            if not (tx and cond and token):
                continue
            ts = int(t.get("timestamp") or 0)
            if ts and (now - ts) > max_age:
                skipped_old += 1
                continue  # only fresh fills count toward the burst
            size = float(t.get("size_usdc") or 0)
            price = float(t.get("price") or 0)
            g = groups.get((cond, token))
            if g is None:
                g = {"size": 0.0, "notional": 0.0, "fills": 0,
                     "last_ts": 0, "last_tx": tx, "title": t.get("title", "")}
                groups[(cond, token)] = g
            g["size"] += size
            g["notional"] += price * size
            g["fills"] += 1
            if ts >= g["last_ts"]:
                g["last_ts"] = ts
                g["last_tx"] = tx

        skipped_age_total += skipped_old
        if groups:
            wallets_with_activity += 1

        for (cond, token), g in groups.items():
            agg_size = g["size"]
            if agg_size < min_copy:
                skipped_small_total += 1
                continue  # below conviction floor — dust/noise

            key = f"{addr}:{cond}:{token}"
            last = _seen.get(key)
            if last and (now - last) < reentry_sec:
                skipped_dedup_total += 1
                continue  # already copied this entry burst

            meta = fast.get(cond)
            if meta is None:
                skipped_no_market_total += 1
                # Don't poison _seen here — we want to retry once this market
                # enters our resolution window (or config changes).
                continue  # not a fast/liquid market in range — skip

            # Cross-restart dedup: did we already signal this wallet→market+outcome?
            try:
                since = (datetime.now(timezone.utc)
                         - timedelta(hours=settings.tracked_reentry_hours)).isoformat()
                ex = (sb.table("trade_signals").select("id")
                      .eq("source_wallet", addr).eq("market_id", cond)
                      .eq("token_id", token).gte("created_at", since)
                      .limit(1).execute())
                if ex.data:
                    _seen[key] = now
                    continue
            except Exception:
                pass
            _seen[key] = now

            vwap = (g["notional"] / agg_size) if agg_size else 0
            tx = g["last_tx"]
            outcome = (meta.get("token_outcomes") or {}).get(token, "")
            consensus = _consensus_count(sb, cond, token, addr)

            signal = {
                "market_id":        cond,
                "token_id":         token,
                "title":            meta.get("title") or g["title"],
                "outcome":          outcome,
                "side":             "BUY",
                "price":            round(vwap, 4),
                "size_usdc":        round(agg_size, 2),
                "fills":            g["fills"],
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
                     outcome=outcome, size=round(agg_size, 2), fills=g["fills"],
                     consensus=consensus, subs=len(user_ids))

    if len(_seen) > _SEEN_MAX:
        cutoff = now - reentry_sec
        for k in [k for k, v in _seen.items() if v < cutoff]:
            del _seen[k]

    # Log a summary every cycle so we can diagnose why dispatched stays 0.
    if dispatched == 0 and (wallets_with_activity or skipped_age_total):
        log.info(
            "poll_no_dispatch",
            wallets=len(wallets),
            active=wallets_with_activity,
            skipped_age=skipped_age_total,
            skipped_small=skipped_small_total,
            skipped_no_market=skipped_no_market_total,
            skipped_dedup=skipped_dedup_total,
            fast_markets=len(fast),
        )
    return {"dispatched": dispatched}
