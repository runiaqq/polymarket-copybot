"""
Blueprint 26 — sniper-mode donor mirroring.

A 'sniper' tracked wallet enters 5-min BTC markets in the last ~30 seconds.
This fast loop (every sniper_poll_sec) mirrors its BUY fills to an explicit
allowlist of users with NO sizing/risk gates except the 30% Delta-Drop stop.
The slow Model-B path (poll_tracked_wallets) excludes mode='sniper' wallets.
"""

import time

import structlog

from core.cache import notify_once
from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)


def _sniper_subscribers(allowed_tg_ids: list[int]) -> list[dict]:
    """Allowlisted users with an active paid sub. Deliberately IGNORES
    copy_paused_until (BP26: only the 30% stop is kept as risk control)."""
    from datetime import datetime, timezone
    from core.db import get_supabase

    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    rows = (
        sb.table("users").select("*")
        .in_("telegram_id", allowed_tg_ids)
        .neq("sub_tier", "free")
        .gt("sub_expires_at", now)
        .execute()
    ).data or []
    return [
        u for u in rows
        if u.get("is_signal_only")
        or (u.get("copy_active") and u.get("wallet_address"))
    ]


@celery_app.task(name="worker.tasks.poll_sniper_wallets", queue="periodic")
def poll_sniper_wallets() -> dict:
    if not settings.auto_copy_enabled:
        return {"skipped": "auto_copy_off"}

    from core.clob import get_clob_market
    from core.db import insert_trade_signal, list_tracked_wallets
    from core.polymarket import fetch_donor_recent_trades
    from worker.tasks import execute_copy_trade

    wallets = [w for w in list_tracked_wallets()
               if (w.get("mode") or "default") == "sniper"]
    if not wallets:
        return {"skipped": "no_sniper_wallets"}

    now = time.time()
    dispatched = 0
    for w in wallets:
        addr = (w.get("address") or "").lower()
        allowed = [int(x) for x in (w.get("allowed_telegram_ids") or [])]
        if not addr or not allowed:
            continue
        try:
            trades = fetch_donor_recent_trades(addr, limit=settings.sniper_fetch_limit)
        except Exception:
            log.warning("sniper_fetch_failed", wallet=addr[:10])
            continue

        # Group fresh BUY fills by (condition, token) — one batch = one mirror entry.
        groups: dict[tuple, list[dict]] = {}
        for t in trades:
            if (t.get("side") or "").upper() != "BUY":
                continue
            cond, token = t.get("condition_id"), t.get("token_id")
            ts = int(t.get("timestamp") or 0)
            if not (cond and token):
                continue
            if ts and (now - ts) > settings.sniper_max_trade_age_sec:
                continue
            groups.setdefault((cond, token), []).append(t)

        for (cond, token), fills in groups.items():
            # Atomic per-market dedup (SETNX) — one entry per market instance.
            if not notify_once(f"sniper:{addr}:{cond}:{token}",
                               ttl=settings.sniper_dedup_ttl_sec):
                continue

            meta = get_clob_market(cond)
            if not meta:
                log.info("sniper_skip", reason="no_market_meta", market=cond[:14])
                continue
            if not meta["accepting_orders"]:
                log.info("sniper_skip", reason="market_closed", market=cond[:14])
                continue

            size = sum(float(f.get("size_usdc") or 0) for f in fills)
            notional = sum(float(f.get("size_usdc") or 0) * float(f.get("price") or 0)
                           for f in fills)
            vwap = (notional / size) if size > 0 else 0.0
            if size <= 0 or vwap <= 0:
                continue

            f0 = fills[0]
            outcome = (meta.get("token_outcomes") or {}).get(token) or f0.get("outcome") or ""
            signal = {
                "mode":           "sniper",           # BP26 branch flag in execute_copy_trade
                "market_id":      cond,
                "token_id":       token,
                "title":          meta.get("question") or f0.get("title") or "",
                "outcome":        outcome,
                "side":           "BUY",
                "price":          round(vwap, 4),
                "size_usdc":      round(size, 2),
                "fills":          len(fills),
                "tick_size":      meta.get("tick_size", "0.01"),
                "neg_risk":       bool(meta.get("neg_risk", False)),
                "resolution_iso": meta.get("end_date_iso"),
                "event_slug":     f0.get("event_slug") or "",
                "outcome_index":  f0.get("outcome_index"),
                "source_tx_hash": str(f0.get("tx_hash") or f0.get("id") or f"{addr}:{cond}"),
                "source_wallet":  addr,
                "consensus":      1,
                "whale_wallet":   addr,
            }
            try:
                row = insert_trade_signal({
                    "market_id":      cond,
                    "title":          signal["title"],
                    "outcome":        outcome or None,
                    "side":           "BUY",
                    "price":          signal["price"],
                    "size_usdc":      signal["size_usdc"],
                    "token_id":       token,
                    "source_tx_hash": signal["source_tx_hash"],
                    "source_wallet":  addr,
                    "consensus":      1,
                })
                signal["signal_id"] = row["id"]
            except Exception:
                log.exception("sniper_signal_insert_failed", market=cond[:14])
                continue

            users = _sniper_subscribers(allowed)
            for u in users:
                execute_copy_trade.delay(u["id"], signal)
            dispatched += 1
            log.info("sniper_signal_fired", wallet=addr[:10], market=cond[:14],
                     outcome=outcome, size=round(size, 2), vwap=round(vwap, 4),
                     fills=len(fills), users=len(users))

    return {"dispatched": dispatched}
