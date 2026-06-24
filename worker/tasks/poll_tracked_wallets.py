"""
Model B — copy a curated whitelist of profitable wallets.

Blueprint 2 redesign:
  * In-memory `_seen` is replaced by a cross-process Redis accumulation bucket
    per (wallet, cond, token) so the signal fires **once after slicing settles**
    (quiet_period_sec since the last fill) rather than mid-slice on a partial sum.
  * VWAP of the completed burst is used as the signal price.
  * Pre-fan-out balance gate: users below min_balance_usdc are skipped at this
    layer with a throttled low-balance nudge, avoiding doomed task enqueues.
"""

import time
from datetime import datetime, timedelta, timezone

import structlog

from core.cache import accum_add, accum_get, accum_mark_fired, notify_once
from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)


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


def _threshold(wallet: dict) -> float:
    """
    Dynamic conviction threshold for this wallet:
      max(abs_floor, conviction_frac × wallet avg_trade_usdc)
    Falls back to abs_floor when avg_trade_usdc is not populated.
    """
    avg = float(wallet.get("avg_trade_usdc") or 0)
    dynamic = settings.slice_conviction_frac * avg if avg > 0 else 0
    return max(settings.tracked_min_copy_usdc, dynamic)


def _notify_low_balance_pre(telegram_id: int, balance: float, min_bal: float) -> None:
    """Throttled low-balance alert fired from the fan-out gate (not per-signal)."""
    from telegram import Bot
    import asyncio

    if not notify_once(f"lowbal:{telegram_id}", ttl=settings.lowbal_alert_throttle_sec):
        return

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"⚠️ <b>Недостаточно средств для копирования</b>\n\n"
                f"💰 Минимальный баланс: <b>${min_bal:.2f} USDC</b>\n"
                f"💼 На счету: <b>${balance:.2f} USDC</b>\n\n"
                "Пополни кошелёк через /wallet чтобы не пропускать сделки."
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.run(_send())
    except Exception:
        log.warning("pre_fanout_lowbal_failed", tg=telegram_id)


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

    max_age = settings.tracked_max_trade_age_sec
    reentry_sec = settings.tracked_reentry_hours * 3600
    # TTL for accumulator buckets: reentry window + max_window (some buffer).
    bucket_ttl = reentry_sec + settings.slice_max_window_sec + 60

    dispatched = 0
    skipped_age_total = 0
    skipped_small_total = 0
    skipped_no_market_total = 0
    skipped_dedup_total = 0
    wallets_with_activity = 0

    # ── Pre-fan-out balance check ─────────────────────────────────────────────
    # Skip users who cannot afford even the exchange minimum order ($1).
    # Users below the recommended balance get a soft warning but are NOT excluded —
    # execute_copy_trade will floor their size to the exchange minimum.
    eligible_users: list[dict] = []
    for user in subscribers:
        dw = user.get("deposit_wallet_address")
        if not dw:
            continue
        try:
            from core.polygon import get_balances as _gb
            bal = _gb(dw).get("pusd", 0.0)
        except Exception:
            bal = 0.0
        if bal < settings.exchange_min_order_usdc:
            _notify_low_balance_pre(user["telegram_id"], bal, settings.recommended_min_balance_usdc)
            log.debug("pre_fanout_skip_lowbal", user_id=user["id"],
                      balance=round(bal, 2))
        else:
            eligible_users.append(user)

    # All subscribers with any balance get dispatched; those below exchange_min
    # are filtered above. The soft recommended-balance warning fires from
    # execute_copy_trade for users below recommended_min_balance_usdc.
    user_ids = [u["id"] for u in eligible_users]
    if not user_ids:
        log.info("poll_no_eligible_users", total_subs=len(subscribers))
        return {"dispatched": 0, "skipped_no_balance": len(subscribers)}

    # ── Per-wallet fill collection → Redis accumulator ────────────────────────
    for w in wallets:
        addr = (w.get("address") or "").lower()
        if not addr:
            continue
        threshold = _threshold(w)

        try:
            trades = fetch_donor_recent_trades(addr, limit=settings.tracked_fetch_limit)
        except Exception:
            log.warning("tracked_fetch_failed", wallet=addr[:10])
            continue

        skipped_old = 0
        has_activity = False
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
                continue
            size = float(t.get("size_usdc") or 0)
            price = float(t.get("price") or 0)
            bucket_key = f"{addr}:{cond}:{token}"
            accum_add(bucket_key, str(tx), size, price, ts or int(now), ttl=bucket_ttl)
            has_activity = True

        skipped_age_total += skipped_old
        if has_activity:
            wallets_with_activity += 1

        # ── Evaluate firing condition for each (cond, token) this wallet has ──
        # We must enumerate possible keys. Since we just fed fills into buckets,
        # re-read them by scanning the fills we just saw.
        seen_keys: set[str] = set()
        for t in trades:
            cond = t.get("condition_id")
            token = t.get("token_id")
            if cond and token:
                seen_keys.add(f"{addr}:{cond}:{token}")

        for bucket_key in seen_keys:
            bkt = accum_get(bucket_key)
            if bkt is None or bkt["fired"]:
                skipped_dedup_total += 1
                continue

            parts = bucket_key.split(":", 2)
            if len(parts) != 3:
                continue
            _, cond, token = parts

            agg_size = bkt["acc_usdc"]
            if agg_size < threshold:
                skipped_small_total += 1
                continue

            age_since_first = now - bkt["first_ts"]
            age_since_last = now - bkt["last_ts"]

            fire = (
                age_since_last >= settings.slice_quiet_period_sec   # whale settled
                or age_since_first >= settings.slice_max_window_sec  # hard window
            )
            if not fire:
                log.debug("accum_waiting", wallet=addr[:10],
                          market=cond[:14], acc=round(agg_size, 2),
                          quiet=round(age_since_last, 0))
                continue

            meta = fast.get(cond)
            if meta is None:
                skipped_no_market_total += 1
                continue

            # Cross-restart dedup: DB check.
            try:
                since = (datetime.now(timezone.utc)
                         - timedelta(hours=settings.tracked_reentry_hours)).isoformat()
                ex = (sb.table("trade_signals").select("id")
                      .eq("source_wallet", addr).eq("market_id", cond)
                      .eq("token_id", token).gte("created_at", since)
                      .limit(1).execute())
                if ex.data:
                    accum_mark_fired(bucket_key, ttl=bucket_ttl)
                    skipped_dedup_total += 1
                    continue
            except Exception:
                pass

            # Atomic fire — only the process that wins this CAS actually emits.
            if not accum_mark_fired(bucket_key, ttl=bucket_ttl):
                skipped_dedup_total += 1
                continue

            vwap = (bkt["acc_notional"] / agg_size) if agg_size else 0
            outcome = (meta.get("token_outcomes") or {}).get(token, "")
            consensus = _consensus_count(sb, cond, token, addr)

            signal = {
                "market_id":      cond,
                "token_id":       token,
                "title":          meta.get("title") or "",
                "outcome":        outcome,
                "side":           "BUY",
                "price":          round(vwap, 4),
                "size_usdc":      round(agg_size, 2),
                "fills":          bkt["fills"],
                "tick_size":      meta.get("tick_size", "0.01"),
                "neg_risk":       bool(meta.get("neg_risk", False)),
                # BP5: carry the ISO end datetime so notification sites can
                # recompute "time left" fresh at send time (never a frozen scalar).
                "resolution_iso": meta.get("resolution_iso"),
                "event_slug":     meta.get("event_slug"),
                "source_tx_hash": f"{addr}:{cond}:{token}",
                "source_wallet":  addr,
                "consensus":      consensus,
                "whale_wallet":   addr,
            }
            try:
                row = insert_trade_signal({
                    "market_id":      cond,
                    "title":          signal["title"],
                    "side":           "BUY",
                    "price":          signal["price"],
                    "size_usdc":      signal["size_usdc"],
                    "token_id":       token,
                    "source_tx_hash": signal["source_tx_hash"],
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
            log.info(
                "tracked_signal_fired",
                wallet=addr[:10],
                market=cond[:14],
                outcome=outcome,
                size=round(agg_size, 2),
                fills=bkt["fills"],
                vwap=round(vwap, 4),
                quiet_s=round(age_since_last, 0),
                consensus=consensus,
                eligible_users=len(user_ids),
            )

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
            eligible_users=len(user_ids),
        )
    return {"dispatched": dispatched, "eligible_users": len(user_ids)}
