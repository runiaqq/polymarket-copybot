"""
Position management (Phase 3): real P&L sync, hybrid exits, resolution detection.

Hybrid exit policy:
  * markets with >= tp_sl_min_hours left  → dynamic take-profit / stop-loss
  * markets closer to resolution          → hold to settle
  * resolved (redeemable) positions        → notify user (on-chain redeem is gated
                                             behind AUTO_REDEEM_ENABLED, off by default,
                                             pending live V2/pUSD verification).

Blueprint 1: reconcile_settlements scans copy_trades rows on-chain directly, so
  neg-risk positions that disappear from the Data API are never missed.

Blueprint 4: sync_positions updates equity_hwm per user and triggers the drawdown /
  daily-loss circuit breaker.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import structlog

from core.cache import claim, notify_once
from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

# Best-effort in-process guard for in-flight closes (per worker child).
_closing: set[tuple] = set()
# When a position was first observed (resets on restart — that's fine: old
# positions are assumed mature and immediately eligible for TP/SL evaluation).
_first_seen: dict[str, float] = {}


def _notify_once(key: str) -> bool:
    return notify_once(key)


def _claim_settled(user_id: int, condition_id: str) -> None:
    claim(f"settle:{user_id}:{condition_id}")


def _hours_left(end_date: str | None) -> float | None:
    if not end_date:
        return None
    try:
        from dateutil.parser import parse as parse_dt

        dt = parse_dt(end_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


@celery_app.task(name="worker.tasks.sync_positions", queue="periodic")
def sync_positions() -> dict:
    from core.db import get_active_subscribers
    from core.polymarket import get_closed_positions, get_order_book, get_positions

    subscribers = get_active_subscribers()
    if not subscribers:
        return {"users": 0}

    now = time.time()
    actions = 0
    for user in subscribers:
        # NB: do NOT skip is_signal_only users here. Signal-Only Mode gates only
        # ENTRY into new trades (execute_copy). Open positions a user accumulated
        # before switching must keep being monitored, exited on TP/SL, and redeemed.
        wallet = user.get("deposit_wallet_address")
        if not wallet:
            continue
        uid = user["id"]
        tg = user["telegram_id"]

        # ── Open positions: win-on-resolution (redeemable) + TP/SL ──────────────
        try:
            positions = get_positions(wallet)
        except Exception:
            log.warning("positions_fetch_failed", user_id=uid)
            positions = None

        for p in (positions or []):
            if p["shares"] <= 0:
                continue
            token_id = p["token_id"]
            condition_id = p["condition_id"]

            if p.get("redeemable"):
                # "redeemable" only means the market resolved — it does NOT mean
                # we won. The losing outcome token is also redeemable (for $0).
                # Decide win/loss by the resolved price (≈1 = won, ≈0 = lost).
                cur = float(p.get("cur_price") or 0)
                pnl = p.get("cash_pnl", 0)
                won = cur >= 0.5

                if won and settings.auto_redeem_enabled:
                    # BP9 Layer 3: claim the redeem slot first so we know whether
                    # a concurrent process (reconcile_settlements) is already
                    # handling this — if so, skip the pending message entirely to
                    # avoid an out-of-order "processing…" after "зачислено".
                    redeem_claimed = _notify_once(f"redeem:{uid}:{condition_id}")
                    if _notify_once(f"settle:{uid}:{condition_id}"):
                        if redeem_claimed:
                            # We own the redeem — send the pending message.
                            # Final "✅ Выигрыш зачислен" comes from redeem_position.
                            _emit_win_pending(tg, p.get("title"), p.get("outcome"),
                                             event_slug=p.get("event_slug"))
                        # else: another process already dispatched and may have
                        # sent the final notification — stay silent.
                        actions += 1
                    if redeem_claimed:
                        redeem_position.delay(
                            uid, token_id, condition_id,
                            bool(p.get("neg_risk", False)), p.get("outcome"),
                            p.get("title"), p.get("event_slug"),
                            p.get("outcome_index"),
                        )
                        actions += 1
                elif won:
                    # auto_redeem disabled: send the terminal win notification
                    # with a manual-claim instruction (no on-chain tx expected).
                    if _notify_once(f"settle:{uid}:{condition_id}"):
                        _emit_win(tg, p.get("title"), p.get("outcome"), pnl,
                                  claimable=True, event_slug=p.get("event_slug"))
                        actions += 1
                else:
                    if _notify_once(f"settle:{uid}:{condition_id}"):
                        _emit_loss(tg, p.get("title"), p.get("outcome"), pnl,
                                   event_slug=p.get("event_slug"))
                        actions += 1
                continue

            # ── Blueprint 10: Delta-Drop stop-loss ────────────────────────
            # Primary strategy: hold to resolution (binary pays $1 win / $0 loss).
            # Exception 1 — Delta-Drop: exit when the live CLOB best_bid has fallen
            #   >= delta_drop_stop_pct (30%) from entry.  tp_sl_min_hours guard is
            #   intentionally NOT applied — it caused the 2026-06-30 incident.
            # Exception 2 — hard_stop floor: residual absolute safety net at 0.07.
            #
            # BP13.3 invariant: this stop fires on entry_price vs best_bid only.
            # Do NOT branch on users.sizing_mode here — the stop is sizing-agnostic.

            hours = _hours_left(p.get("end_date"))

            # Enforce minimum hold time (avoids tick-level entry whipsaw).
            fkey = f"{uid}:{token_id}"
            seen_at = _first_seen.setdefault(fkey, now)
            age_sec = now - seen_at
            if age_sec < settings.delta_drop_min_hold_sec:
                log.debug("exit_skipped_too_new",
                          user_id=uid, token=token_id[:14],
                          age_min=round(age_sec / 60, 1))
                continue

            ckey = (uid, token_id)
            if ckey in _closing:
                continue

            # Live best_bid from CLOB order book.  The book does not delist
            # neg-risk tokens early (unlike Data-API cur_price).
            try:
                book = get_order_book(token_id)
                best_bid = float(book.get("best_bid") or 0) if book else 0.0
            except Exception:
                best_bid = float(p.get("cur_price") or p.get("best_bid") or 0)

            # Cost-basis entry price from Data-API avg_price.
            entry_px = float(p.get("avg_price") or 0)

            # Mark logging — accumulate to calibrate delta_drop_stop_pct over time.
            if settings.log_position_marks and entry_px > 0 and best_bid > 0:
                log.info("position_mark",
                         user_id=uid, token=token_id[:14],
                         entry=entry_px, best_bid=round(best_bid, 4),
                         drop=round(1.0 - best_bid / entry_px, 3),
                         hours=round(hours, 2) if hours is not None else None)

            # Delta-Drop trigger: exit if best_bid fell >= X from entry.
            if entry_px > 0 and best_bid > 0:
                drop_pct = 1.0 - best_bid / entry_px
                if drop_pct >= settings.delta_drop_stop_pct:
                    log.info("delta_drop_triggered",
                             user_id=uid, token=token_id[:14],
                             entry=entry_px, best_bid=round(best_bid, 4),
                             drop=round(drop_pct, 3),
                             threshold=settings.delta_drop_stop_pct)
                    _closing.add(ckey)
                    close_position.delay(uid, token_id, "delta_drop_stop")
                    actions += 1
                    continue

            # Hard-stop floor: residual safety net when book is near-zero.
            if best_bid > 0 and best_bid < settings.hard_stop_abs_price:
                log.info("hard_stop_triggered",
                         user_id=uid, token=token_id[:14],
                         cur_price=best_bid, threshold=settings.hard_stop_abs_price)
                _closing.add(ckey)
                close_position.delay(uid, token_id, "hard_stop")
                actions += 1

        # ── Closed positions: resolution win/loss notices ───────────────────────
        try:
            closed = get_closed_positions(wallet)
        except Exception:
            closed = []
        for c in closed:
            if now - c["timestamp"] > settings.settlement_lookback_sec:
                continue  # too old — avoid restart spam
            cur = c["cur_price"]
            resolved_win = cur >= 0.98
            resolved_loss = cur <= 0.02
            if not (resolved_win or resolved_loss):
                continue  # mid-market sell (TP/SL/manual) — already notified at close
            cond_id = c["condition_id"]

            if resolved_win and settings.auto_redeem_enabled:
                # BP9 Layer 3: this branch caused the prod bug (sent "выиграно"
                # with no corresponding redeem dispatch).  Same guard logic as the
                # redeemable branch: claim redeem slot first, then send pending.
                redeem_claimed = _notify_once(f"redeem:{uid}:{cond_id}")
                if _notify_once(f"settle:{uid}:{cond_id}"):
                    if redeem_claimed:
                        _emit_win_pending(tg, c.get("title"), c.get("outcome"),
                                         event_slug=c.get("event_slug"))
                    actions += 1
                if redeem_claimed:
                    # Try to look up ledger fields and dispatch the redeem.
                    # Falls back to backfill_legacy_redemptions for legacy rows
                    # with NULL token_id (those have no ledger entry).
                    try:
                        from core.db import get_supabase as _gsb
                        _sb = _gsb()
                        _tr = (
                            _sb.table("copy_trades")
                            .select("id,token_id,neg_risk,outcome_index")
                            .eq("user_id", uid)
                            .eq("condition_id", cond_id)
                            .eq("status", "confirmed")
                            .is_("redeemed_at", "null")
                            .limit(1)
                            .execute()
                        )
                        _row = (_tr.data or [None])[0]
                        if _row and _row.get("token_id"):
                            redeem_position.delay(
                                uid, _row["token_id"], cond_id,
                                bool(_row.get("neg_risk", False)),
                                c.get("outcome"), c.get("title"), c.get("event_slug"),
                                _row.get("outcome_index"),
                            )
                            actions += 1
                    except Exception:
                        log.warning("closed_win_redeem_dispatch_failed",
                                    user_id=uid, cond=cond_id[:14])
            elif resolved_win:
                # auto_redeem disabled — terminal notification with manual-claim note.
                if _notify_once(f"settle:{uid}:{cond_id}"):
                    _emit_win(tg, c.get("title"), c.get("outcome"),
                              c.get("realized_pnl", 0), event_slug=c.get("event_slug"))
                    actions += 1
            else:
                if _notify_once(f"settle:{uid}:{cond_id}"):
                    _emit_loss(tg, c.get("title"), c.get("outcome"),
                               c.get("realized_pnl", 0), event_slug=c.get("event_slug"))
                    actions += 1

        # ── BP4/BP8: update HWM + check circuit breakers (cost-basis equity) ────
        try:
            from core.polygon import get_balances as _gb
            from core.risk import total_equity
            from core.db import get_open_trades_cost
            free_pusd = _gb(wallet).get("pusd", 0.0) if wallet else 0.0
            ledger_cost = get_open_trades_cost(uid)
            equity = total_equity(
                free_pusd,
                positions or [],
                ledger_cost,
                mode=settings.drawdown_equity_mode,
            )
            _update_hwm_and_check_breakers(user, equity, free_pusd, positions or [])
        except Exception:
            log.warning("hwm_update_failed", user_id=uid)

    log.info("sync_positions_done", users=len(subscribers), actions=actions)
    return {"users": len(subscribers), "actions": actions}


@celery_app.task(
    bind=True, name="worker.tasks.close_position", queue="trades", max_retries=2,
    default_retry_delay=5,
)
def close_position(self, user_id: int, token_id: str, reason: str = "manual") -> dict:
    from core.clob import generate_api_creds, sell_position
    from core.db import get_supabase
    from core.polymarket import get_order_book, get_positions

    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None
    if not user or not user.get("wallet_private_key_enc"):
        return {"skipped": True, "reason": "no_wallet"}

    deposit_wallet = user.get("deposit_wallet_address")
    if not deposit_wallet:
        _closing.discard((user_id, token_id))
        return {"skipped": True, "reason": "not_registered"}

    # Find the live position to know how many shares to sell.
    position = next(
        (p for p in get_positions(deposit_wallet)
         if p["token_id"] == token_id and p["shares"] > 0),
        None,
    )
    if position is None:
        if reason == "manual":
            _notify(
                user["telegram_id"],
                "ℹ️ <b>Позиция не найдена</b>\n\n"
                "Возможно, она уже закрылась или разрезолвилась.\n"
                "Проверь баланс через /balance.",
            )
        _closing.discard((user_id, token_id))
        return {"skipped": True, "reason": "no_position"}

    condition_id = position.get("condition_id", "")
    title = (position.get("title") or "—")[:50]
    outcome = position.get("outcome") or "—"
    cur_price = float(position.get("cur_price") or 0)

    book = get_order_book(token_id)
    best_bid = book.get("best_bid") if book else None

    # At very high prices (>0.93) the order book is usually empty —
    # buyers won't offer 0.93+ for a 0.07 return. Tell the user to wait for resolution.
    if not best_bid or float(best_bid) < 0.01:
        if cur_price >= 0.90:
            _notify(
                user["telegram_id"],
                f"📊 <b>Стакан пустой — это нормально</b>\n\n"
                f"📌 {title} · <b>{outcome}</b>\n"
                f"💹 Текущая цена: <b>{cur_price:.3f} (~{cur_price*100:.0f}%)</b>\n\n"
                f"При цене {cur_price*100:.0f}% никто не хочет покупать — "
                f"слишком маленькая потенциальная прибыль для покупателя.\n\n"
                f"✅ <b>Ничего делать не нужно</b> — при резолве бот автоматически "
                f"зачислит выигрыш. Просто жди результата события.",
            )
        else:
            url = _event_link(position.get("event_slug"))
            _notify(
                user["telegram_id"],
                f"⚠️ <b>Нет покупателей для продажи</b>\n\n"
                f"📌 {title} · <b>{outcome}</b>\n\n"
                f"Стакан пустой — ордер не пройдёт. "
                f"Попробуй позже или дождись резолва события.{url}",
            )
        _closing.discard((user_id, token_id))
        return {"skipped": True, "reason": "no_bid"}

    api_creds = {
        "clob_api_key":    user.get("clob_api_key"),
        "clob_secret":     user.get("clob_secret"),
        "clob_passphrase": user.get("clob_passphrase"),
    }
    if not api_creds["clob_api_key"]:
        try:
            api_creds = generate_api_creds(user["wallet_private_key_enc"], funder=deposit_wallet)
            sb.table("users").update(api_creds).eq("id", user_id).execute()
        except Exception as exc:
            raise self.retry(exc=exc)

    try:
        import math
        # Truncate shares DOWN to 2 decimal places to avoid "not enough balance"
        # errors caused by the positions API rounding up vs actual on-chain token balance.
        shares_to_sell = math.floor(position["shares"] * 100) / 100
        if shares_to_sell <= 0:
            _closing.discard((user_id, token_id))
            return {"skipped": True, "reason": "zero_shares_after_truncation"}

        # Blueprint 6: look up the copy_trades row BEFORE selling so we can book
        # real P&L from the actual sale proceeds (not from a later on-chain resolution).
        from core.db import get_open_trade_by_token, mark_trade_closed
        trade_to_close = None
        try:
            trade_to_close = get_open_trade_by_token(user_id, token_id)
        except Exception:
            log.warning("close_position_trade_lookup_failed", user_id=user_id,
                        token=token_id[:18])

        sell_result = sell_position(
            private_key_enc=user["wallet_private_key_enc"],
            api_creds=api_creds,
            token_id=token_id,
            shares=shares_to_sell,
            price=float(best_bid),
            tick_size=str(book.get("tick_size", "0.01")),
            neg_risk=bool(book.get("neg_risk", position.get("neg_risk", False))),
            slippage_pct=settings.exit_slippage_pct,
            deposit_wallet=deposit_wallet,
        )

        # Blueprint 6: write realized P&L and mark the ledger row terminal so
        # reconcile_settlements never re-claims this position later.
        if trade_to_close:
            try:
                entry_cost = float(trade_to_close.get("size_usdc") or 0)
                proceeds = shares_to_sell * float(best_bid)
                realized_pnl = proceeds - entry_cost
                exit_tx = ""
                if isinstance(sell_result, dict):
                    exit_tx = (sell_result.get("orderID")
                               or sell_result.get("tx")
                               or sell_result.get("transactionHash") or "")
                mark_trade_closed(trade_to_close["id"],
                                  realized_pnl=realized_pnl,
                                  exit_tx=exit_tx)
                log.info("trade_closed_booked", user_id=user_id,
                         trade_id=trade_to_close["id"],
                         realized_pnl=round(realized_pnl, 4), exit_tx=exit_tx[:18])
            except Exception:
                log.warning("close_position_book_pnl_failed", user_id=user_id,
                            token=token_id[:18])

        # Claim the settlement key so the resolution scanner won't double-notify
        # if this exit fills near 0/1.
        if condition_id:
            _claim_settled(user_id, condition_id)
        # Read the new trading balance so the user sees the funds returned.
        try:
            import time as _t
            _t.sleep(4)  # let the sale settle to pUSD
            from core.polymarket import get_positions as _gp  # noqa: F401
            from core.polygon import get_balances
            remaining = get_balances(deposit_wallet).get("pusd", 0.0)
        except Exception:
            remaining = None
        _notify_closed(user["telegram_id"], position, reason, remaining)
        log.info("position_closed", user_id=user_id, token=token_id[:18], reason=reason)
        return {"closed": True, "reason": reason}
    except Exception as exc:
        log.exception("close_position_failed", user_id=user_id, token=token_id[:18])
        err_str = str(exc).lower()
        # After max retries, notify the user so they know what happened.
        if self.request.retries >= self.max_retries:
            if "geoblock" in err_str or "trading restricted" in err_str:
                msg = "⚠️ <b>Не удалось закрыть позицию</b>\n\nГео-блок на сервере. Обратись к администратору."
            elif "no_bid" in err_str or "liquidity" in err_str:
                msg = (
                    f"⚠️ <b>Не удалось закрыть позицию</b>\n\n"
                    f"Стакан пустой. Попробуй позже или дождись резолва события."
                )
            else:
                msg = f"⚠️ <b>Не удалось закрыть позицию</b>\n\n<code>{str(exc)[:200]}</code>"
            _notify(user["telegram_id"], msg)
        raise self.retry(exc=exc)
    finally:
        _closing.discard((user_id, token_id))


@celery_app.task(
    bind=True, name="worker.tasks.redeem_position", queue="trades", max_retries=2,
    default_retry_delay=15,
)
def redeem_position(self, user_id: int, token_id: str, condition_id: str,
                    neg_risk: bool, outcome: str | None,
                    title: str | None = None, event_slug: str | None = None,
                    outcome_index: int | None = None,
                    trade_id: int | None = None,
                    entry_cost: float | None = None) -> dict:
    """
    Redeem a resolved winning position into pUSD and notify the user.

    BP1 extra args:
      trade_id   — copy_trades.id to update with result + redeem_tx.
      entry_cost — original cost basis for P&L calculation.
    """
    from core.db import get_supabase
    from core.polygon import get_balances
    from core.relayer import convert_dw_usdce_to_pusd, redeem_winnings

    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None
    if not user or not user.get("wallet_private_key_enc"):
        return {"skipped": True, "reason": "no_wallet"}
    dw = user.get("deposit_wallet_address")
    if not dw:
        return {"skipped": True, "reason": "not_registered"}

    # Blueprint 6 (1): terminal-state guard — if the trade was already closed by
    # close_position (status='closed') or previously redeemed, do nothing.
    if trade_id:
        try:
            tr = (sb.table("copy_trades")
                  .select("status, redeemed_at")
                  .eq("id", trade_id)
                  .maybe_single()
                  .execute())
            if tr.data:
                td = tr.data
                if td.get("status") == "closed" or td.get("redeemed_at"):
                    log.info("redeem_skip_terminal", user_id=user_id, trade_id=trade_id)
                    return {"skipped": True, "reason": "terminal_state"}
        except Exception:
            log.warning("redeem_terminal_check_failed", user_id=user_id)

    # Blueprint 6 (2): dust guard — skip if on-chain ERC-1155 balance is negligible.
    # Outcome tokens have 6 decimal places: 1 share = 1_000_000 units.
    shares_bal: float = 0.0  # initialised here so BP11 P&L calc can reference it safely
    try:
        from core.relayer import ctf_token_balance
        raw_bal = ctf_token_balance(dw, token_id)
        shares_bal = raw_bal / 1_000_000
        if shares_bal < settings.claim_dust_min_shares:
            log.info("redeem_skip_dust", user_id=user_id, token=token_id[:18],
                     shares=round(shares_bal, 6))
            return {"skipped": True, "reason": "dust_below_min"}
    except Exception:
        log.warning("redeem_dust_check_failed", user_id=user_id, token=token_id[:18])

    # Blueprint 6 (3): hydrate title from trade_signals when called from the
    # on-chain reconciler path (which passes title=None).
    if not title and trade_id:
        try:
            tr = (sb.table("copy_trades")
                  .select("signal_id")
                  .eq("id", trade_id)
                  .maybe_single()
                  .execute())
            if tr.data and tr.data.get("signal_id"):
                sig = (sb.table("trade_signals")
                       .select("title")
                       .eq("id", tr.data["signal_id"])
                       .maybe_single()
                       .execute())
                if sig.data:
                    title = sig.data.get("title") or title
        except Exception:
            pass

    # Prefer the API's outcomeIndex; fall back to the name (0 = "Yes", 1 = "No").
    if outcome_index is None:
        outcome_index = 0 if str(outcome or "").strip().lower().startswith("yes") else 1
    outcome_index = int(outcome_index)
    try:
        bal_before = get_balances(dw).get("pusd", 0.0)
        r = redeem_winnings(user["wallet_private_key_enc"], condition_id,
                            bool(neg_risk), outcome_index, token_id)
        if r.get("skipped"):
            log.info("redeem_skipped", user_id=user_id, reason=r.get("reason"))
            return r
        import time as _t
        _t.sleep(5)
        try:
            convert_dw_usdce_to_pusd(user["wallet_private_key_enc"])
            _t.sleep(4)
        except Exception:
            log.warning("post_redeem_wrap_failed", user_id=user_id)
        bal_after = get_balances(dw).get("pusd", 0.0)
        credited = max(0.0, bal_after - bal_before)
        redeem_tx = r.get("tx") or ""

        # BP1 + BP11: write result + redeem_tx back onto the copy_trades ledger row.
        # BP11 fix: use on-chain share count (shares_bal × $1.00/share) as gross
        # proceeds instead of the fragile wallet balance-delta (credited).  The
        # balance-delta is unreliable because USDC.e→pUSD wrapping often settles
        # after the snapshot, causing wins to appear as -100%.
        if trade_id:
            try:
                from core.db import mark_trade_settled
                gross = shares_bal if shares_bal > 0 else credited
                pnl = gross - float(entry_cost or 0)
                mark_trade_settled(trade_id, result="win",
                                   realized_pnl=pnl, redeem_tx=redeem_tx)
            except Exception:
                log.warning("mark_trade_settled_failed", trade_id=trade_id)

        _notify(
            user["telegram_id"],
            f"💸 <b>Выигрыш зачислен</b>\n\n"
            f"📌 {(title or '—')[:50]}\n"
            f"🎯 Исход: <b>{outcome or '—'}</b>\n"
            f"➕ Зачислено: <b>+${credited:.2f} pUSD</b>\n"
            f"💼 Торговый баланс: <b>${bal_after:.2f} pUSD</b>"
            f"{_event_link(event_slug)}",
        )
        log.info("redeem_done", user_id=user_id, credited=round(credited, 2),
                 tx=redeem_tx[:14])
        return {"redeemed": True, "credited": credited}
    except Exception as exc:
        log.exception("redeem_failed", user_id=user_id, token=token_id[:14])
        # BP9 Layer 3: on final retry exhaustion, tell the user their credit is
        # delayed rather than leaving them with only the pending message.
        if self.request.retries >= self.max_retries:
            try:
                tg_id = user.get("telegram_id")
                if tg_id:
                    _emit_win_retry_failed(tg_id, title, outcome, event_slug)
            except Exception:
                pass
        raise self.retry(exc=exc)


# ── Notifications ────────────────────────────────────────────────────────────

def _notify(telegram_id: int, text: str) -> None:
    from telegram import Bot
    from core.config import settings as s

    async def _send() -> None:
        await Bot(token=s.telegram_bot_token).send_message(
            chat_id=telegram_id, text=text, parse_mode="HTML"
        )

    try:
        asyncio.run(_send())
    except Exception:
        log.warning("notify_failed", telegram_id=telegram_id)


def _event_link(event_slug: str | None) -> str:
    from core.polymarket import event_url
    url = event_url(event_slug)
    return f"\n🔗 <a href=\"{url}\">Открыть на Polymarket</a>" if url else ""


def _notify_closed(telegram_id: int, position: dict, reason: str,
                   remaining: float | None = None) -> None:
    labels = {
        "take_profit":     "🎯 Тейк-профит",
        "stop_loss":       "🛑 Стоп-лосс",
        "hard_stop":       "🚨 Жёсткий стоп (цена ~0)",
        "delta_drop_stop": "📉 Дельта-дроп стоп (-30%)",
        "manual":          "✋ Вручную",
    }
    pnl = position.get("cash_pnl", 0)
    pct = max(-1.0, min(10.0, position.get("percent_pnl", 0)))
    icon = "📈" if pnl >= 0 else "📉"
    outcome = position.get("outcome") or "—"
    title = (position.get("title") or "—")[:50]
    bal_line = f"\n💼 Торговый баланс: <b>${remaining:.2f} pUSD</b>" if remaining is not None else ""
    _notify(
        telegram_id,
        f"✅ <b>Позиция закрыта</b> ({labels.get(reason, reason)})\n\n"
        f"📌 {title}\n"
        f"🎯 Исход: <b>{outcome}</b>\n"
        f"{icon} P&L: <b>{pnl:+.2f}$</b> ({pct:+.0%})"
        f"{bal_line}"
        f"{_event_link(position.get('event_slug'))}",
    )


def _emit_win(telegram_id: int, title: str | None, outcome: str | None,
              pnl: float, claimable: bool = False, event_slug: str | None = None) -> None:
    note = ""
    if claimable:
        note = (
            "\n\nСредства зачислятся автоматически после расчёта."
            if settings.auto_redeem_enabled
            else "\n\nЗабери выигрыш на Polymarket (Portfolio → Claim)."
        )
    _notify(
        telegram_id,
        f"🏆 <b>Событие выиграно!</b>\n\n"
        f"📌 {(title or '—')[:50]}\n"
        f"🎯 Исход: <b>{outcome or '—'}</b>\n"
        f"📈 Результат: <b>{pnl:+.2f}$</b>"
        f"{_event_link(event_slug)}{note}",
    )


def _emit_loss(telegram_id: int, title: str | None, outcome: str | None,
               pnl: float, event_slug: str | None = None) -> None:
    _notify(
        telegram_id,
        f"💔 <b>Событие проиграно</b>\n\n"
        f"📌 {(title or '—')[:50]}\n"
        f"🎯 Исход: <b>{outcome or '—'}</b>\n"
        f"📉 Результат: <b>{pnl:+.2f}$</b>"
        f"{_event_link(event_slug)}",
    )


def _emit_win_pending(telegram_id: int, title: str | None, outcome: str | None,
                      event_slug: str | None = None) -> None:
    """BP9 Layer 3 — interim 'processing' notification sent at resolution detection.

    The final '✅ Выигрыш зачислен' comes only from redeem_position after the
    on-chain tx + pUSD balance change are confirmed.  Never call _emit_win from a
    branch that also dispatches auto-redeem — this is the replacement.
    """
    _notify(
        telegram_id,
        f"🏁 <b>Событие выиграно — оформляю зачисление</b>\n\n"
        f"📌 {(title or '—')[:50]}\n"
        f"🎯 Исход: <b>{outcome or '—'}</b>\n"
        f"⏳ Средства будут зачислены после подтверждения on-chain транзакции."
        f"{_event_link(event_slug)}",
    )


def _emit_win_retry_failed(telegram_id: int, title: str | None, outcome: str | None,
                           event_slug: str | None = None) -> None:
    """BP9 Layer 3 — sent when redeem_position exhausts all retries without success."""
    _notify(
        telegram_id,
        f"⏳ <b>Выигрыш определён, зачисление задерживается</b>\n\n"
        f"📌 {(title or '—')[:50]}\n"
        f"🎯 Исход: <b>{outcome or '—'}</b>\n"
        f"Повторяем попытку автоматически. Если баланс не изменится в течение "
        f"нескольких минут — обратись в поддержку."
        f"{_event_link(event_slug)}",
    )


# ── Blueprint 1: on-chain settlement reconciler ──────────────────────────────

@celery_app.task(name="worker.tasks.reconcile_settlements", queue="periodic")
def reconcile_settlements() -> dict:
    """
    Source-of-truth settlement pass: reads CTF contract payoutDenominator on-chain
    for every confirmed-but-unredeemed copy_trade.  Handles neg-risk positions that
    disappear from the Data API before they appear as 'redeemable'.

    Dedup with Redis so a concurrent sync_positions pass never double-redeems.
    """
    from core.db import get_outstanding_copy_trades, get_supabase, mark_trade_settled
    from core.relayer import is_condition_resolved, get_payout_numerator

    trades = get_outstanding_copy_trades()
    if not trades:
        return {"checked": 0}

    sb = get_supabase()
    processed = 0

    for trade in trades:
        uid = trade["user_id"]
        cond = trade.get("condition_id")
        token_id = trade.get("token_id")
        outcome_idx = trade.get("outcome_index")
        neg_risk = bool(trade.get("neg_risk", False))

        if not (cond and token_id and outcome_idx is not None):
            continue

        # Redis dedup: only one process handles each (user, condition) at a time.
        if not notify_once(f"reconcile:{uid}:{cond}", ttl=300):
            continue

        try:
            if not is_condition_resolved(cond):
                continue  # not resolved yet — check again next cycle

            won = get_payout_numerator(cond, int(outcome_idx)) > 0
            entry_cost = float(trade.get("size_usdc") or 0)

            if not won:
                mark_trade_settled(
                    trade["id"],
                    result="loss",
                    realized_pnl=-entry_cost,
                )
                if _notify_once(f"settle:{uid}:{cond}"):
                    _resolve_user_for_notification(sb, uid, cond, won=False,
                                                   entry_cost=entry_cost)
                log.info("reconcile_loss", user_id=uid, cond=cond[:14],
                         cost=round(entry_cost, 2))
            else:
                # Win: dispatch the existing idempotent redeem_position task.
                # mark_trade_settled is called inside _on_redeem_done after tx confirmed.
                if not notify_once(f"redeem:{uid}:{cond}"):
                    continue  # already dispatched

                # Blueprint 6: terminal guard — if close_position already exited
                # this trade (status='closed'), skip redemption entirely.
                from core.db import has_terminal_trade
                if has_terminal_trade(uid, cond):
                    log.info("reconcile_skip_terminal", user_id=uid, cond=cond[:14])
                    continue

                res = sb.table("users").select("*").eq("id", uid).maybe_single().execute()
                user = res.data if res else None
                if not user:
                    continue

                # Blueprint 6: dust guard — check on-chain ERC-1155 balance before
                # dispatching.  Prevents empty "$0.01 win" notifications for dust
                # left by close_position's 2-dp truncation.
                dw = user.get("deposit_wallet_address")
                if dw:
                    try:
                        from core.relayer import ctf_token_balance
                        raw_bal = ctf_token_balance(dw, token_id)
                        shares_bal = raw_bal / 1_000_000
                        if shares_bal < settings.claim_dust_min_shares:
                            log.info("reconcile_skip_dust", user_id=uid, cond=cond[:14],
                                     shares=round(shares_bal, 6))
                            continue
                    except Exception:
                        log.warning("reconcile_dust_check_failed", user_id=uid)

                # BP9 Layer 3: send pending notification before dispatching so the
                # user knows resolution was detected (final "зачислено" comes from
                # redeem_position after the on-chain tx confirms).
                if _notify_once(f"settle:{uid}:{cond}"):
                    _resolve_user_for_notification(
                        sb, uid, cond, won=True, entry_cost=entry_cost, pending=True
                    )

                redeem_position.delay(
                    uid, token_id, cond, neg_risk,
                    outcome=None,     # resolve_name not needed for on-chain path
                    title=None,       # hydrated inside redeem_position via trade_id
                    event_slug=None,
                    outcome_index=int(outcome_idx),
                    trade_id=trade["id"],
                    entry_cost=entry_cost,
                )
                log.info("reconcile_win_dispatched", user_id=uid, cond=cond[:14])

            processed += 1
        except Exception:
            log.exception("reconcile_error", user_id=uid, cond=(cond or "")[:14])

    log.info("reconcile_settlements_done", checked=len(trades), processed=processed)
    return {"checked": len(trades), "processed": processed}


# ── Blueprint 1 REMAINING GAP: backfill legacy positions ─────────────────────

@celery_app.task(name="worker.tasks.backfill_legacy_redemptions", queue="periodic")
def backfill_legacy_redemptions() -> dict:
    """
    Recover funds for positions opened **before** migration 008 was applied.

    Those copy_trades rows have NULL condition_id/token_id, so reconcile_settlements
    skips them entirely. This task goes directly to the Data API + on-chain state
    to find resolved-won holdings that have not yet been redeemed, and dispatches
    redeem_position for each one.

    Also catches positions that Polymarket's own keeper already redeemed as USDC.e
    on the deposit wallet — convert_dw_usdce_to_pusd handles those.

    Safe to run repeatedly: deduped by Redis notify_once "redeem:{uid}:{cond}".
    """
    from core.db import get_supabase
    from core.polymarket import get_positions, get_closed_positions
    from core.relayer import is_condition_resolved, get_payout_numerator

    sb = get_supabase()
    # All users with deposit wallets (not just active subscribers — legacy funds
    # can be recovered regardless of subscription status).
    res = sb.table("users").select(
        "id, telegram_id, wallet_private_key_enc, deposit_wallet_address"
    ).not_.is_("deposit_wallet_address", "null").execute()
    users = res.data or []

    dispatched = 0
    wrapped = 0

    for user in users:
        uid = user["id"]
        dw = user.get("deposit_wallet_address")
        if not dw or not user.get("wallet_private_key_enc"):
            continue

        # ── 1. Open positions: check if redeemable but not yet redeemed ──────
        try:
            positions = get_positions(dw)
        except Exception:
            log.warning("backfill_positions_failed", user_id=uid)
            positions = []

        for p in positions:
            if p.get("shares", 0) <= 0:
                continue
            token_id = p.get("token_id")
            condition_id = p.get("condition_id")
            if not (token_id and condition_id):
                continue

            # Blueprint 6: terminal guard — skip positions already exited/redeemed
            # in our ledger.  backfill uses the Data API (unaware of our ledger),
            # so this check is mandatory to avoid re-claiming closed positions.
            from core.db import has_terminal_trade
            if has_terminal_trade(uid, condition_id):
                log.info("backfill_skip_terminal", user_id=uid,
                         cond=condition_id[:14])
                continue

            # Blueprint 6: dust guard — skip if on-chain balance is negligible.
            try:
                from core.relayer import ctf_token_balance
                raw_bal = ctf_token_balance(dw, token_id)
                shares_bal = raw_bal / 1_000_000
                if shares_bal < settings.claim_dust_min_shares:
                    log.info("backfill_skip_dust", user_id=uid,
                             cond=condition_id[:14], shares=round(shares_bal, 6))
                    continue
            except Exception:
                log.warning("backfill_dust_check_failed", user_id=uid,
                            cond=condition_id[:14])

            # Data API says redeemable — use existing fast path.
            if p.get("redeemable"):
                cur = float(p.get("cur_price") or 0)
                if cur < 0.5:
                    continue  # loss — nothing to redeem
                if not notify_once(f"redeem:{uid}:{condition_id}"):
                    continue
                outcome_idx = p.get("outcome_index")
                if outcome_idx is None:
                    outcome_name = str(p.get("outcome") or "").strip().lower()
                    outcome_idx = 0 if outcome_name.startswith("yes") else 1
                redeem_position.delay(
                    uid, token_id, condition_id,
                    bool(p.get("neg_risk", False)),
                    p.get("outcome"), p.get("title"), p.get("event_slug"),
                    int(outcome_idx),
                )
                dispatched += 1
                log.info("backfill_redeemable_dispatched", user_id=uid,
                         cond=condition_id[:14])
                continue

            # Not marked redeemable by API — check on-chain directly (catches
            # neg-risk positions that vanish from the Data API).
            try:
                if not is_condition_resolved(condition_id):
                    continue
                outcome_idx = p.get("outcome_index")
                if outcome_idx is None:
                    outcome_name = str(p.get("outcome") or "").strip().lower()
                    outcome_idx = 0 if outcome_name.startswith("yes") else 1
                won = get_payout_numerator(condition_id, int(outcome_idx)) > 0
                if not won:
                    continue
                if not notify_once(f"redeem:{uid}:{condition_id}"):
                    continue
                redeem_position.delay(
                    uid, token_id, condition_id,
                    bool(p.get("neg_risk", False)),
                    p.get("outcome"), p.get("title"), p.get("event_slug"),
                    int(outcome_idx),
                )
                dispatched += 1
                log.info("backfill_onchain_won_dispatched", user_id=uid,
                         cond=condition_id[:14])
            except Exception:
                log.warning("backfill_onchain_check_failed", user_id=uid,
                            cond=condition_id[:14])

        # ── 2. Self-healing USDC.e sweep ──────────────────────────────────────
        # If the deposit wallet holds USDC.e (e.g. already redeemed by Polymarket's
        # keeper or a prior partial run), wrap it to pUSD so it becomes tradeable.
        try:
            from core.polygon import get_balances as _gb
            dw_bals = _gb(dw)
            if dw_bals.get("usdc_e", 0) >= 0.10:
                from core.relayer import convert_dw_usdce_to_pusd
                r = convert_dw_usdce_to_pusd(user["wallet_private_key_enc"])
                if not r.get("skipped"):
                    wrapped += 1
                    log.info("backfill_usdc_e_wrapped", user_id=uid,
                             amount=round(dw_bals["usdc_e"], 4))
        except Exception:
            log.warning("backfill_wrap_failed", user_id=uid)

    log.info("backfill_legacy_redemptions_done",
             users=len(users), dispatched=dispatched, wrapped=wrapped)
    return {"users": len(users), "dispatched": dispatched, "wrapped": wrapped}


def _resolve_user_for_notification(sb, uid: int, cond: str,
                                   won: bool, entry_cost: float,
                                   pending: bool = False) -> None:
    """Load the user record and send a win/loss notification.

    Blueprint 6: hydrates title/outcome from trade_signals via the copy_trades
    ledger so the notification always carries a human-readable market name.

    BP9 Layer 3: when pending=True and won=True, sends _emit_win_pending instead
    of the terminal _emit_win, so the final "зачислено" is reserved for
    redeem_position after the on-chain tx confirms.
    """
    try:
        res = sb.table("users").select("telegram_id").eq("id", uid).maybe_single().execute()
        user = res.data if res else None
        if not user:
            return
        tg = user["telegram_id"]

        # Hydrate title from the copy_trades → trade_signals join.
        title: str | None = None
        try:
            tr = (sb.table("copy_trades")
                  .select("signal_id")
                  .eq("user_id", uid)
                  .eq("condition_id", cond)
                  .order("created_at", desc=True)
                  .limit(1)
                  .execute())
            if tr.data and tr.data[0].get("signal_id"):
                sig = (sb.table("trade_signals")
                       .select("title")
                       .eq("id", tr.data[0]["signal_id"])
                       .maybe_single()
                       .execute())
                if sig.data:
                    title = sig.data.get("title")
        except Exception:
            pass

        if won and pending:
            _emit_win_pending(tg, title, None)
        elif won:
            _emit_win(tg, title, None, entry_cost)
        else:
            _emit_loss(tg, title, None, -entry_cost)
    except Exception:
        log.warning("resolve_user_notify_failed", user_id=uid)


# ── Blueprint 4: HWM + circuit-breaker update in sync_positions ──────────────

def _notify_with_markup(telegram_id: int, text: str, reply_markup=None) -> None:
    """Send a Telegram message, optionally with an InlineKeyboardMarkup."""
    from telegram import Bot
    from core.config import settings as s

    async def _send() -> None:
        bot = Bot(token=s.telegram_bot_token)
        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    try:
        asyncio.run(_send())
    except Exception:
        log.exception("notify_with_markup_failed", telegram_id=telegram_id)


def _update_hwm_and_check_breakers(
    user: dict,
    equity: float,
    free_pusd: float = 0.0,
    positions: list | None = None,
) -> None:
    """BP4/BP8 — Compare-and-set state machine for the drawdown / daily-loss breakers.

    Key BP8 changes vs legacy:
    - Notification sent ONLY on state TRANSITION (active → paused_*), never on repeat.
    - Auto-resume transition (paused_* → active after cooldown) also notifies once.
    - Inline "Снять блокировку" button attached to the pause message.
    - Re-evaluating while already paused does nothing (no re-pause, no re-notify).
    """
    from core.db import (
        get_daily_realized_pnl, get_risk_state, get_user_equity_hwm,
        pause_user_copying, resume_user_copying, set_risk_state,
        update_user_equity_hwm,
    )
    from core.cache import notify_once as _no
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    uid = user["id"]
    tg = user["telegram_id"]

    try:
        current_state = get_risk_state(uid)

        # ── Update HWM when equity is at a new peak ───────────────────────────
        stored_hwm = get_user_equity_hwm(uid)
        hwm = max(stored_hwm, equity)
        if equity > stored_hwm:
            update_user_equity_hwm(uid, equity)

        # ── Auto-resume: transition paused_* → active when cooldown elapsed ──
        paused_until = user.get("copy_paused_until")
        if paused_until and current_state in ("paused_drawdown", "paused_daily_loss"):
            try:
                from dateutil.parser import parse as _parse_dt
                pu = _parse_dt(paused_until)
                if pu.tzinfo is None:
                    pu = pu.replace(tzinfo=timezone.utc)
                if pu <= datetime.now(timezone.utc):
                    resume_user_copying(uid)
                    set_risk_state(uid, "active")
                    if _no(f"resume_alert:{uid}", ttl=3600):
                        _notify(tg,
                            "▶️ <b>Копирование возобновлено</b>\n\n"
                            "Период охлаждения истёк. Бот снова следит за китами."
                        )
                    log.info("copy_pause_expired_auto_resume", user_id=uid)
                    return
            except Exception:
                pass

        # ── If already paused — do nothing (no re-pause, no re-notify) ────────
        if current_state in ("paused_drawdown", "paused_daily_loss"):
            return

        if equity <= 0:
            return

        # ── Drawdown circuit breaker ──────────────────────────────────────────
        drawdown = (hwm - equity) / hwm if hwm > 0 else 0.0
        if drawdown >= settings.max_drawdown_pct:
            pause_until = (
                datetime.now(timezone.utc) + timedelta(seconds=settings.drawdown_cooldown_sec)
            ).isoformat()
            pause_user_copying(uid, pause_until)
            set_risk_state(uid, "paused_drawdown")
            # Notify exactly once on the active → paused_drawdown transition.
            unblock_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 Снять блокировку", callback_data="unlock_drawdown")
            ]])
            _notify_with_markup(
                tg,
                f"🛑 <b>Просадка {drawdown*100:.1f}%</b>\n\n"
                f"Капитал: <b>${equity:.2f}</b> от пика <b>${hwm:.2f}</b>.\n"
                f"Копирование приостановлено на 24 ч для защиты депозита.\n\n"
                f"Бот возобновится автоматически через 24 ч, или нажми кнопку ниже.",
                reply_markup=unblock_kb,
            )
            log.info("drawdown_breaker_tripped", user_id=uid,
                     drawdown=round(drawdown, 4), equity=round(equity, 2))
            return

        # ── Daily loss limit ──────────────────────────────────────────────────
        daily_pnl = get_daily_realized_pnl(uid)
        if daily_pnl <= -(settings.daily_loss_limit_pct * equity):
            next_utc_day = (
                datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
            ).isoformat()
            pause_user_copying(uid, next_utc_day)
            set_risk_state(uid, "paused_daily_loss")
            unblock_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 Снять блокировку", callback_data="unlock_drawdown")
            ]])
            _notify_with_markup(
                tg,
                f"📉 <b>Дневной лимит убытков</b>\n\n"
                f"Убыток сегодня: <b>${abs(daily_pnl):.2f}</b> "
                f"(>{settings.daily_loss_limit_pct*100:.0f}% капитала).\n"
                f"Копирование остановлено до 00:00 UTC.\n\n"
                f"Можешь снять блокировку вручную, приняв риск на себя.",
                reply_markup=unblock_kb,
            )
            log.info("daily_loss_limit_tripped", user_id=uid,
                     daily_pnl=round(daily_pnl, 2), equity=round(equity, 2))
    except Exception:
        log.warning("hwm_check_failed", user_id=uid)
