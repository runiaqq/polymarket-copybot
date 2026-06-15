"""
Whale-tracking scanner (primary strategy).

Every 30s:
  1. Pull the global feed of large BUY trades (server-side cash-filtered).
  2. Keep only trades on "fast" markets (resolve within MARKET_MAX_HOURS_TO_RESOLVE).
  3. De-duplicate by tx hash (in-memory + DB) and apply a per-market cooldown.
  4. Persist ONE trade_signal, dispatch an auto-copy to every active subscriber,
     and kick off a parallel AI analysis that is sent to users afterwards.
"""

import time

import structlog

from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

# In-memory dedup of tx hashes already processed (survives within a worker run).
_seen_tx: set[str] = set()
# market_id -> last signal timestamp (cooldown).
_market_last_signal: dict[str, float] = {}
# Bound the in-memory set so it can't grow forever.
_SEEN_TX_MAX = 5000


@celery_app.task(name="worker.tasks.dispatch_signal", queue="periodic")
def dispatch_signal(signal: dict) -> dict:
    """
    Persist a detected signal once and fan it out to all active subscribers.
    Called by the WebSocket listener (non-blocking via .delay).
    """
    from core.db import get_active_subscribers, get_supabase, insert_trade_signal
    from worker.tasks import execute_copy_trade, run_ai_analysis

    sb = get_supabase()
    tx = signal.get("source_tx_hash", "")

    # DB-level dedup guard (the WS loop also dedups in-memory).
    if tx:
        try:
            existing = (
                sb.table("trade_signals").select("id").eq("source_tx_hash", tx).limit(1).execute()
            )
            if existing.data:
                return {"skipped": True, "reason": "duplicate_tx"}
        except Exception:
            pass

    subscribers = get_active_subscribers()
    if not subscribers:
        return {"skipped": True, "reason": "no_subscribers"}

    try:
        sig_row = insert_trade_signal({
            "market_id":      signal["market_id"],
            "title":          signal.get("title", ""),
            "side":           signal["side"],
            "price":          signal["price"],
            "size_usdc":      signal.get("size_usdc", 0),
            "token_id":       signal.get("token_id"),
            "source_tx_hash": tx,
        })
        signal["signal_id"] = sig_row["id"]
    except Exception:
        log.exception("signal_insert_failed", market=signal.get("market_id"))
        return {"skipped": True, "reason": "insert_failed"}

    from core.config import settings

    # ── Wallet track-record filter (observe: log only; enforce: gate entries) ────
    if settings.wallet_filter_mode != "off":
        try:
            from core import wallet_score
            addr, score, passed = wallet_score.evaluate(signal["market_id"], tx)
            if addr:
                try:
                    sb.table("trade_signals").update({
                        "whale_wallet":        addr,
                        "whale_realized_pnl":  (score or {}).get("realized_pnl"),
                        "whale_resolved_count": (score or {}).get("resolved_count"),
                        "whale_winrate":       (score or {}).get("winrate"),
                        "whale_passed":        passed,
                    }).eq("id", signal["signal_id"]).execute()
                except Exception:
                    log.warning("whale_score_persist_failed", signal=signal["signal_id"])
                log.info("whale_scored", wallet=addr[:10], passed=passed,
                         realized=(score or {}).get("realized_pnl"),
                         resolved=(score or {}).get("resolved_count"))
            if settings.wallet_filter_mode == "enforce" and not passed:
                log.info("signal_blocked_by_wallet_filter", market=signal["market_id"][:18],
                         wallet=(addr or "?")[:10])
                return {"skipped": True, "reason": "wallet_filter", "whale_passed": False}
        except Exception:
            log.warning("wallet_filter_error", market=signal.get("market_id"))

    user_ids = [u["id"] for u in subscribers]
    if settings.auto_copy_enabled:
        # Auto-copy: AI analysis is embedded inside each execute_copy_trade task
        # so the user gets ONE combined message (trade + analysis). No separate AI task.
        for uid in user_ids:
            execute_copy_trade.delay(uid, signal)
    else:
        # Signals mode: no trade executed, send standalone AI analysis.
        run_ai_analysis.delay(signal, user_ids)

    log.info("signal_dispatched", market=signal.get("market_id", "")[:18],
             whale_usdc=signal.get("size_usdc"), subs=len(user_ids), auto_copy=settings.auto_copy_enabled)
    return {"dispatched": len(user_ids), "auto_copy": settings.auto_copy_enabled}


@celery_app.task(name="worker.tasks.scan_whale_trades", queue="periodic")
def scan_whale_trades() -> dict:
    from core.db import get_active_subscribers, get_supabase, insert_trade_signal
    from core.polymarket import fetch_whale_trades, get_fast_markets
    from worker.tasks import execute_copy_trade, run_ai_analysis

    subscribers = get_active_subscribers()
    if not subscribers:
        log.debug("no_active_subscribers")
        return {"signals": 0}

    user_ids = [u["id"] for u in subscribers]
    fast_markets = get_fast_markets()
    if not fast_markets:
        log.debug("no_fast_markets")
        return {"signals": 0}

    trades = fetch_whale_trades()
    if not trades:
        return {"signals": 0}

    # DB-level dedup across restarts: recent source tx hashes already signalled.
    sb = get_supabase()
    try:
        recent = (
            sb.table("trade_signals")
            .select("source_tx_hash")
            .not_.is_("source_tx_hash", "null")
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        db_seen = {r["source_tx_hash"] for r in (recent.data or [])}
    except Exception:
        db_seen = set()

    now = time.time()
    signals = 0

    for trade in trades:
        tx = trade.get("tx_hash", "")
        if not tx or tx in _seen_tx or tx in db_seen:
            continue

        # Only copy BUY entries; SELL requires already holding the token.
        if trade.get("side") != "BUY":
            _seen_tx.add(tx)
            continue

        condition_id = trade.get("condition_id", "")
        market = fast_markets.get(condition_id)
        if market is None:
            # Not a fast market (or not tradeable) — ignore, but remember the tx.
            _seen_tx.add(tx)
            continue

        # Per-market cooldown.
        if now - _market_last_signal.get(condition_id, 0) < settings.market_signal_cooldown_sec:
            _seen_tx.add(tx)
            continue

        _seen_tx.add(tx)
        _market_last_signal[condition_id] = now

        signal = {
            "market_id":        condition_id,
            "token_id":         trade.get("token_id"),
            "title":            trade.get("title") or market.get("title", ""),
            "side":             "BUY",
            "outcome":          trade.get("outcome", ""),
            "price":            trade.get("price", 0.0),
            "size_usdc":        trade.get("size_usdc", 0.0),
            "source_tx_hash":   tx,
            "tick_size":        market.get("tick_size", "0.01"),
            "neg_risk":         market.get("neg_risk", False),
            "min_size":         market.get("min_size", 5),
            "hours_to_resolve": market.get("hours_to_resolve"),
            "whale_wallet":     trade.get("whale_wallet", ""),
            "whale_name":       trade.get("whale_name", ""),
        }

        # Persist the signal ONCE, then reuse its id for every copy.
        try:
            sig_row = insert_trade_signal({
                "market_id":      signal["market_id"],
                "title":          signal["title"],
                "side":           signal["side"],
                "price":          signal["price"],
                "size_usdc":      signal["size_usdc"],
                "token_id":       signal["token_id"],
                "source_tx_hash": signal["source_tx_hash"],
            })
            signal["signal_id"] = sig_row["id"]
        except Exception:
            log.exception("signal_insert_failed", market=condition_id)
            continue

        db_seen.add(tx)
        log.info(
            "whale_signal",
            market=condition_id[:18],
            size=signal["size_usdc"],
            price=signal["price"],
            hours=signal["hours_to_resolve"],
            whale=signal["whale_wallet"][:10],
        )

        for uid in user_ids:
            execute_copy_trade.delay(uid, signal)
        run_ai_analysis.delay(signal, user_ids)
        signals += 1

    # Trim the in-memory dedup set if it grows too large.
    if len(_seen_tx) > _SEEN_TX_MAX:
        _seen_tx.clear()

    log.info("scan_complete", markets=len(fast_markets), trades=len(trades), signals=signals)
    return {"signals": signals}
