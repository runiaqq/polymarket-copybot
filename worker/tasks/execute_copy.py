"""
Fast-path Celery task: copy a donor trade to a subscriber's wallet.
Uses Polymarket CLOB v2 for real order placement.

Integrations in this file:
  BP1 — denormalize condition_id / token_id / outcome_index / neg_risk /
         entry_price / shares onto copy_trades row for on-chain reconciliation.
  BP2 — throttle _notify_low_balance to ≤1 alert per lowbal_alert_throttle_sec.
  BP3 — fractional Kelly sizing (sizing_mode="kelly"); "fixed" keeps legacy behavior.
  BP4 — tail-risk gates (exposure cap, event cap, drawdown, daily loss) evaluated
         before place_order; pauses stored on users table.
"""

import asyncio
import time

import structlog
from celery import Task

from worker.celery_app import celery_app

log = structlog.get_logger(__name__)


def _confirm_fill(wallet_address: str, token_id: str, intended_usdc: float) -> tuple[float, str]:
    """
    After a market FAK order, confirm how much actually filled by reading the
    on-chain position. Retries up to 5 times with 4-second intervals to absorb
    Polymarket Data API indexing lag (positions typically appear within 5-15s).
    Returns (filled_usdc, status) where status is 'full' | 'partial' | 'none' | 'unknown'.
    """
    from core.polymarket import get_positions

    for attempt in range(5):
        try:
            time.sleep(4)
            positions = get_positions(wallet_address)
            pos = next(
                (p for p in positions if p["token_id"] == token_id),
                None,
            )
            if pos and pos["shares"] > 0:
                filled = pos.get("current_value") or (pos["shares"] * pos["avg_price"])
                if filled < 0.05 * intended_usdc:
                    return filled, "none"
                if filled < 0.9 * intended_usdc:
                    return filled, "partial"
                return filled, "full"
            log.debug("confirm_fill_retry", token=token_id[:18], attempt=attempt + 1)
        except Exception:
            log.warning("confirm_fill_failed", token=token_id[:18], attempt=attempt + 1)

    return 0.0, "none"


class ExecuteCopyTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 5


@celery_app.task(
    bind=True,
    base=ExecuteCopyTask,
    name="worker.tasks.execute_copy_trade",
    queue="trades",
)
def execute_copy_trade(self: ExecuteCopyTask, user_id: int, signal: dict) -> dict:
    from core.config import settings
    from core.clob import generate_api_creds, get_market_token_id, place_order
    from core.db import get_supabase, insert_copy_trade, insert_trade_signal

    sb = get_supabase()

    # Load user
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None

    if not user or not user.get("wallet_private_key_enc"):
        log.warning("skip_no_wallet", user_id=user_id)
        return {"skipped": True, "reason": "no_wallet"}

    # V2: trading goes through the user's deposit wallet (POLY_1271 funder).
    deposit_wallet = user.get("deposit_wallet_address")
    if not deposit_wallet:
        log.warning("skip_not_registered", user_id=user_id)
        _notify_not_registered(user["telegram_id"])
        return {"skipped": True, "reason": "not_registered"}

    # Only copy BUY signals — SELL requires owning the token first
    if signal.get("side", "").upper() in ("SELL", "NO"):
        log.debug("skip_sell_signal", user_id=user_id)
        return {"skipped": True, "reason": "sell_not_supported"}

    # Honor Blueprint 4 drawdown / daily-loss pause (belt-and-suspenders: the
    # fan-out in get_active_subscribers also excludes paused users, but a
    # concurrent pause could arrive between fan-out and task execution).
    from datetime import datetime, timezone
    paused_until = user.get("copy_paused_until")
    if paused_until:
        try:
            from dateutil.parser import parse as _parse_dt
            pu = _parse_dt(paused_until)
            if pu.tzinfo is None:
                pu = pu.replace(tzinfo=timezone.utc)
            if pu > datetime.now(timezone.utc):
                log.info("skip_copy_paused", user_id=user_id,
                         paused_until=paused_until)
                return {"skipped": True, "reason": "copy_paused"}
        except Exception:
            pass

    # ── Check collateral ─────────────────────────────────────────────────────
    try:
        from core.polygon import get_balances, fund_deposit_wallet
        tradeable = get_balances(deposit_wallet).get("pusd", 0)
    except Exception:
        log.warning("balance_check_failed", user_id=user_id)
        tradeable = 0.0

    # ── BP3: Kelly sizing or fixed cap ───────────────────────────────────────
    # Per-user setting takes priority over the global config default.
    user_max = float(user.get("max_position_usdc") or 25)
    depth_cap = float(signal.get("max_copy_usdc") or signal.get("size_usdc") or 0)
    user_sizing_mode = user.get("sizing_mode") or settings.sizing_mode

    if user_sizing_mode == "kelly":
        from core.sizing import kelly_stake
        from core.wallet_score import score_wallet
        try:
            score = score_wallet(signal.get("source_wallet") or signal.get("whale_wallet") or "")
        except Exception:
            score = None
        # Equity = free pUSD + open-position value (positions loaded below for
        # risk gates anyway; use free_pusd as a conservative lower bound here).
        try:
            from core.polymarket import get_positions as _gp
            positions = _gp(deposit_wallet)
            open_value = sum(
                float(p.get("current_value") or 0)
                for p in positions if p.get("shares", 0) > 0
            )
        except Exception:
            positions = []
            open_value = 0.0
        equity = tradeable + open_value
        k_stake = kelly_stake(
            p=float(signal.get("price") or 0),
            score=score,
            consensus=int(signal.get("consensus") or 1),
            equity=equity,
            free_pusd=tradeable,
            cfg=settings,
        )
        if k_stake > 0:
            size_usdc = min(k_stake, user_max)
            if depth_cap > 0:
                size_usdc = min(size_usdc, depth_cap)
            log.info("sizing_kelly", stake=round(k_stake, 2), capped=round(size_usdc, 2),
                     equity=round(equity, 2), user_id=user.get("id"))
        else:
            # Kelly returned 0 (no edge detected) — fall back to fixed cap
            size_usdc = min(user_max, depth_cap) if depth_cap > 0 else user_max
            log.info("sizing_kelly_no_edge_fallback", fixed_cap=round(size_usdc, 2),
                     user_id=user.get("id"))
    else:
        # Fixed cap (user chose fixed or global default)
        size_usdc = min(user_max, depth_cap) if depth_cap > 0 else user_max
        log.debug("sizing_fixed", cap=round(size_usdc, 2), user_id=user.get("id"))
        try:
            from core.polymarket import get_positions as _gp
            positions = _gp(deposit_wallet)
            open_value = sum(
                float(p.get("current_value") or 0)
                for p in positions if p.get("shares", 0) > 0
            )
        except Exception:
            positions = []
            open_value = 0.0
        equity = tradeable + open_value
        score = None

    # ── BP3.1: soft balance warning — warn but never hard-block ─────────────
    # A $3 wallet trades at the $1 platform minimum; we only skip when the wallet
    # genuinely cannot afford even that minimum order.
    exchange_min = settings.exchange_min_order_usdc
    if equity < settings.recommended_min_balance_usdc:
        from core.cache import notify_once as _no
        if _no(f"trading_min:{user_id}", ttl=settings.lowbal_alert_throttle_sec):
            _notify_trading_at_minimum(
                user["telegram_id"], tradeable, settings.recommended_min_balance_usdc,
            )
        log.info("equity_below_recommended", user_id=user_id,
                 equity=round(equity, 2),
                 recommended=settings.recommended_min_balance_usdc)

    # Floor size up to the exchange minimum so Kelly's tiny fractions still execute.
    size_usdc = max(size_usdc, exchange_min)
    # Never spend more than we actually have free.
    size_usdc = min(size_usdc, tradeable)

    # ── Fund deposit wallet on demand if short ───────────────────────────────
    if tradeable < exchange_min:
        eoa = {}
        try:
            from core.polygon import get_balances as _gb
            eoa = _gb(user["wallet_address"])
        except Exception:
            pass
        on_eoa = eoa.get("pusd", 0) + eoa.get("usdc_e", 0) + eoa.get("usdc", 0)
        if on_eoa >= 0.5:
            try:
                from core.polygon import fund_deposit_wallet
                fund_deposit_wallet(
                    user["wallet_private_key_enc"],
                    user["wallet_address"],
                    deposit_wallet,
                )
                tradeable = get_balances(deposit_wallet).get("pusd", 0)
            except Exception:
                log.warning("ondemand_fund_failed", user_id=user_id)

    # Re-clamp after possible sweep.
    size_usdc = min(max(size_usdc, exchange_min), tradeable)

    # Only skip if the wallet genuinely cannot afford the platform minimum.
    if tradeable < exchange_min:
        _notify_low_balance(user["telegram_id"], tradeable, exchange_min, signal)
        log.warning("skip_insufficient_for_min_order", user_id=user_id,
                    pusd=round(tradeable, 4), min_order=exchange_min)
        return {"skipped": True, "reason": "insufficient_for_min_order"}

    # ── Portfolio guards: already-in-market + max open positions ─────────────
    cond = signal.get("market_id")
    consensus = int(signal.get("consensus") or 1)
    try:
        if any(p for p in positions if p.get("condition_id") == cond and p["shares"] > 0):
            if consensus >= 2:
                _notify_consensus(user["telegram_id"], signal, consensus)
            log.info("skip_already_in_market", user_id=user_id,
                     market=(cond or "")[:14], consensus=consensus)
            return {"skipped": True, "reason": "already_in_market"}

        open_count = sum(1 for p in positions if p["shares"] > 0)
        if open_count >= settings.max_open_positions:
            log.info("skip_max_positions", user_id=user_id, open=open_count)
            return {"skipped": True, "reason": "max_positions"}
    except Exception:
        log.warning("positions_count_failed", user_id=user_id)

    # ── Ensure CLOB API credentials ──────────────────────────────────────────
    api_creds = {
        "clob_api_key":    user.get("clob_api_key"),
        "clob_secret":     user.get("clob_secret"),
        "clob_passphrase": user.get("clob_passphrase"),
    }
    if not api_creds["clob_api_key"]:
        try:
            api_creds = generate_api_creds(user["wallet_private_key_enc"],
                                           funder=deposit_wallet)
            sb.table("users").update(api_creds).eq("id", user_id).execute()
            log.info("clob_creds_generated", user_id=user_id)
        except Exception as exc:
            log.exception("clob_creds_failed", user_id=user_id)
            raise self.retry(exc=exc)

    # ── Resolve token_id ─────────────────────────────────────────────────────
    token_id: str | None = signal.get("token_id")
    if not token_id:
        token_id = get_market_token_id(signal["market_id"], signal.get("side", "YES"))
    if not token_id:
        log.warning("skip_no_token_id", market_id=signal["market_id"])
        return {"skipped": True, "reason": "no_token_id"}

    # ── Fresh order-book re-check ────────────────────────────────────────────
    entry_price = float(signal.get("price") or 0)
    try:
        from core.polymarket import get_order_book
        book = get_order_book(token_id)
        if book and book.get("best_ask"):
            entry_price = float(book["best_ask"])
            if entry_price > settings.max_entry_price or entry_price < settings.min_entry_price:
                log.info("skip_price_out_of_range", user_id=user_id, price=entry_price)
                return {"skipped": True, "reason": "price_out_of_range"}
            band = entry_price * (1.0 + settings.order_slippage_pct)
            fillable = sum(
                lvl["price"] * lvl["size"] for lvl in book["asks"] if lvl["price"] <= band
            )
            cap = fillable * settings.book_safe_frac
            if cap > 0:
                size_usdc = min(size_usdc, cap)
    except Exception:
        log.warning("exec_book_check_failed", user_id=user_id)

    # After book re-check, ensure we still meet the platform minimum.
    # Also re-apply the exchange_min floor (book cap may have pushed below it).
    size_usdc = max(size_usdc, exchange_min)
    size_usdc = min(size_usdc, tradeable)
    if size_usdc < exchange_min or tradeable < exchange_min:
        log.debug("skip_depth_below_min", user_id=user_id,
                  size=round(size_usdc, 4), exchange_min=exchange_min)
        return {"skipped": True, "reason": "depth_below_min_order"}

    # ── BP4: tail-risk gates ─────────────────────────────────────────────────
    try:
        from core.risk import check_risk_gates
        from core.db import get_user_equity_hwm, get_daily_realized_pnl
        hwm = get_user_equity_hwm(user_id)
        daily_pnl = get_daily_realized_pnl(user_id)
        decision = check_risk_gates(
            signal=signal,
            stake=size_usdc,
            open_positions=positions,
            equity=equity,
            equity_hwm=hwm,
            daily_pnl=daily_pnl,
            cfg=settings,
        )
        if not decision.allowed:
            from core.cache import notify_once
            gate = decision.gate
            if notify_once(f"risk_gate:{user_id}:{gate}", ttl=3600):
                _notify_risk_pause(user["telegram_id"], decision.reason)

            # Drawdown breaker: record pause in DB so fan-out excludes next signals.
            if gate == "drawdown":
                from datetime import timedelta
                from core.db import pause_user_copying
                from core.db import update_user_equity_hwm
                pause_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=settings.drawdown_cooldown_sec)
                ).isoformat()
                pause_user_copying(user_id, pause_until)
                update_user_equity_hwm(user_id, max(hwm, equity))
                log.info("drawdown_pause_set", user_id=user_id, until=pause_until)

            log.info("skip_risk_gate", user_id=user_id,
                     gate=gate, reason=decision.reason[:80])
            return {"skipped": True, "reason": f"risk_gate:{gate}"}
    except Exception:
        log.warning("risk_gate_check_failed", user_id=user_id)

    # ── Idempotency guard & signal insert ────────────────────────────────────
    signal_id = signal.get("signal_id")
    if not signal_id:
        sig_row = insert_trade_signal({
            "market_id":      signal["market_id"],
            "title":          signal.get("title", ""),
            "side":           signal["side"],
            "price":          signal["price"],
            "size_usdc":      size_usdc,
            "token_id":       token_id,
            "source_tx_hash": signal.get("source_tx_hash", ""),
        })
        signal_id = sig_row["id"]

    try:
        existing = (
            sb.table("copy_trades")
            .select("id,status")
            .eq("user_id", user["id"])
            .eq("signal_id", signal_id)
            .neq("status", "failed")
            .neq("status", "executing")
            .maybe_single()
            .execute()
        )
        if existing and existing.data:
            log.info("skip_already_executed", user_id=user_id, signal_id=signal_id)
            return {"skipped": True, "reason": "already_executed"}
    except Exception:
        pass

    # ── BP1: denormalize settlement fields onto copy_trades row ──────────────
    outcome_index = signal.get("outcome_index")
    if outcome_index is None:
        # Infer from outcome name when not explicitly provided.
        outcome_name = str(signal.get("outcome") or "").strip().lower()
        outcome_index = 0 if outcome_name.startswith("yes") else 1

    trade_row = insert_copy_trade({
        "user_id":        user["id"],
        "signal_id":      signal_id,
        "status":         "executing",
        "size_usdc":      size_usdc,
        # Settlement ledger fields (migration 008)
        "condition_id":   cond,
        "token_id":       token_id,
        "outcome_index":  int(outcome_index),
        "neg_risk":       bool(signal.get("neg_risk", False)),
        "entry_price":    round(entry_price, 6),
    })

    try:
        result = place_order(
            private_key_enc=user["wallet_private_key_enc"],
            api_creds=api_creds,
            token_id=token_id,
            side=signal["side"],
            price=entry_price,
            size_usdc=size_usdc,
            tick_size=str(signal.get("tick_size", "0.01")),
            neg_risk=bool(signal.get("neg_risk", False)),
            slippage_pct=settings.order_slippage_pct,
            deposit_wallet=deposit_wallet,
        )

        order_id = result.get("orderID") or result.get("order_id") or ""

        try:
            sb.table("copy_trades").update({
                "status":   "placed",
                "order_id": order_id,
            }).eq("id", trade_row["id"]).execute()
        except Exception:
            pass

        filled, fill_status = _confirm_fill(deposit_wallet, token_id, size_usdc)

        # BP1: persist shares filled (needed for on-chain reconciliation).
        shares_filled = round(filled / entry_price, 6) if entry_price > 0 and filled > 0 else 0.0

        try:
            from core.polygon import get_balances
            remaining = get_balances(deposit_wallet).get("pusd", 0.0)
        except Exception:
            remaining = 0.0

        score_val, verdict, reason = None, None, None
        try:
            from worker.tasks.ai_filter import _call_gpt
            score_val, verdict, reason = _call_gpt(signal)
        except Exception:
            log.warning("ai_inline_failed", user_id=user_id)

        final_status = "confirmed" if fill_status != "none" else "unfilled"
        try:
            update_payload: dict = {
                "status":   final_status,
                "size_usdc": round(filled, 2) if fill_status in ("full", "partial") else size_usdc,
            }
            if fill_status in ("full", "partial") and shares_filled > 0:
                update_payload["shares"] = shares_filled
            sb.table("copy_trades").update(update_payload).eq("id", trade_row["id"]).execute()
        except Exception:
            pass

        # BP4: update HWM after a successful trade (equity may have changed).
        try:
            from core.db import get_user_equity_hwm, update_user_equity_hwm
            current_hwm = get_user_equity_hwm(user_id)
            if equity > current_hwm:
                update_user_equity_hwm(user_id, equity)
        except Exception:
            pass

        log.info("copy_trade_ok", user_id=user_id, order_id=order_id,
                 fill=fill_status, filled=round(filled, 2),
                 shares=shares_filled, cond=(cond or "")[:14])
        _notify(user["telegram_id"], signal, order_id, size_usdc, filled,
                fill_status, remaining, score_val, verdict, reason)

        return {"order_id": order_id, "user_id": user_id,
                "fill": fill_status, "filled": filled}

    except Exception as exc:
        try:
            sb.table("copy_trades").update({
                "status":    "failed",
                "error_msg": str(exc)[:1000],
            }).eq("id", trade_row["id"]).execute()
        except Exception:
            pass
        log.exception("copy_trade_failed", user_id=user_id)
        raise self.retry(exc=exc)


# ── Notifications ─────────────────────────────────────────────────────────────

def _notify_consensus(telegram_id: int, signal: dict, consensus: int) -> None:
    from telegram import Bot
    from core.config import settings
    from core.cache import notify_once
    from core.polymarket import event_url

    cond = signal.get("market_id", "")
    if not notify_once(f"consensus:{telegram_id}:{cond}:{consensus}"):
        return

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        title = (signal.get("title") or "—")[:60]
        outcome = signal.get("outcome") or "—"
        url = event_url(signal.get("event_slug"))
        link = f"\n🔗 <a href=\"{url}\">Смотреть позицию</a>" if url else ""
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🔥 <b>Ещё один профи зашёл!</b>\n\n"
                f"📌 {title}\n"
                f"🎯 Исход: <b>{outcome}</b>\n\n"
                f"Уже <b>{consensus} проверенных кита</b> в этом исходе — "
                f"уверенность в победе растёт. Твоя позиция уже открыта, "
                f"повторно не входим.{link}"
            ),
            parse_mode="HTML", disable_web_page_preview=True,
        )

    try:
        asyncio.run(_send())
    except Exception:
        log.exception("notify_consensus_failed", telegram_id=telegram_id)


def _notify_not_registered(telegram_id: int) -> None:
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                "⚙️ <b>Нужна настройка кошелька</b>\n\n"
                "Чтобы бот копировал сделки, разверни торговый кошелёк: /register "
                "(без газа с твоей стороны). После этого пополни баланс и включи /resume."
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.run(_send())
    except Exception:
        log.exception("notify_not_registered_failed", telegram_id=telegram_id)


def _notify_low_balance(
    telegram_id: int,
    balance: float,
    needed: float,
    signal: dict,
    reason: str = "low_balance",
) -> None:
    """
    BP2: at most one low-balance alert per user per lowbal_alert_throttle_sec.
    The throttle key is per-user, independent of the signal, so a high-volume
    signal day does not produce alert spam.
    """
    from telegram import Bot
    from core.config import settings
    from core.cache import notify_once

    # Throttled per-user regardless of which signal triggered it.
    if not notify_once(f"lowbal:{telegram_id}", ttl=settings.lowbal_alert_throttle_sec):
        return

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        title = signal.get("title") or "—"
        if reason == "min_balance":
            text = (
                f"⚠️ <b>Баланс ниже минимума для копирования</b>\n\n"
                f"📌 {title}\n\n"
                f"💰 Минимальный баланс: <b>${needed:.2f} USDC</b>\n"
                f"💼 Текущий капитал: <b>${balance:.2f} USDC</b>\n\n"
                f"Пополни кошелёк через /wallet чтобы не пропускать сделки."
            )
        else:
            text = (
                f"⚠️ <b>Недостаточно средств для сделки</b>\n\n"
                f"📌 {title}\n\n"
                f"💰 Нужно: <b>${needed:.2f} USDC</b>\n"
                f"💼 На балансе: <b>${balance:.2f} USDC</b>\n\n"
                f"Пополни кошелёк через /wallet чтобы не пропускать сделки."
            )
        await bot.send_message(chat_id=telegram_id, text=text, parse_mode="HTML")

    try:
        asyncio.run(_send())
    except Exception:
        log.exception("notify_low_balance_failed", telegram_id=telegram_id)


def _notify_trading_at_minimum(telegram_id: int, balance: float, recommended: float) -> None:
    """
    BP3.1: soft warning — balance below recommended, but we still trade at minimum.
    Throttled via notify_once in the caller (once per lowbal_alert_throttle_sec).
    """
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"⚠️ <b>Торгуем на минимальном объёме</b>\n\n"
                f"💼 Баланс: <b>${balance:.2f} pUSD</b> "
                f"(рекомендуется ≥ ${recommended:.0f})\n\n"
                f"Бот продолжает копировать сделки, но использует минимально "
                f"допустимый размер ордера.\n"
                f"Пополни кошелёк через /wallet для нормального риск-менеджмента."
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.run(_send())
    except Exception:
        log.exception("notify_trading_at_minimum_failed", telegram_id=telegram_id)


def _notify_risk_pause(telegram_id: int, reason: str) -> None:
    """BP4: notify user that copying was paused by a risk gate."""
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🛡 <b>Риск-защита сработала</b>\n\n"
                f"{reason}\n\n"
                f"Копирование приостановлено автоматически для защиты депозита. "
                f"После отдыха бот возобновится сам."
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.run(_send())
    except Exception:
        log.exception("notify_risk_pause_failed", telegram_id=telegram_id)


def _notify(
    telegram_id: int, signal: dict, order_id: str,
    intended_usdc: float, filled_usdc: float, fill_status: str,
    remaining: float = 0.0,
    ai_score: int | None = None,
    ai_verdict: str | None = None,
    ai_reason: str | None = None,
) -> None:
    """One combined message: trade result + AI analysis + balance remaining."""
    from telegram import Bot
    from core.config import settings
    from core.polymarket import event_url

    async def _send() -> None:
        from core.polymarket import format_time_left
        bot = Bot(token=settings.telegram_bot_token)
        title = (signal.get("title") or "—")[:60]
        url = event_url(signal.get("event_slug"))
        title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
        price = float(signal.get("price") or 0)
        outcome = signal.get("outcome") or "—"
        prob = f"{price * 100:.0f}%"
        whale_usdc = float(signal.get("size_usdc") or 0)
        fills = int(signal.get("fills") or 0)
        consensus = int(signal.get("consensus") or 1)
        whale_line = f"🐳 Профи вошёл на: <b>${whale_usdc:,.0f}</b>" if whale_usdc else "🐳 Сигнал от профи-кошелька"
        if fills > 1:
            whale_line += f" ({fills} сделок)"
        if consensus >= 2:
            whale_line += f"\n🔥 <b>Консенсус: {consensus} профи</b> в этом исходе"
        # BP5: compute time-left fresh at notification time, never from a cached scalar.
        time_left = format_time_left(signal.get("resolution_iso"))
        hours_line = f" · ⏳ {time_left}"
        link_line = f"\n🔗 <a href=\"{url}\">Смотреть позицию</a>" if url else ""

        if fill_status == "none":
            msg = (
                f"⚠️ <b>Сделка не прошла</b>\n\n"
                f"📌 {title_html}\n"
                f"🎯 Исход: <b>{outcome}</b>\n\n"
                f"Стакан слишком тонкий — ордер не наполнился, позиция не открыта.{link_line}"
            )
        else:
            head = "✅ <b>Бот открыл позицию</b>"
            if fill_status == "partial":
                head = "✅ <b>Бот открыл позицию (частично)</b>"

            invested = filled_usdc if fill_status in ("full", "partial") else intended_usdc
            partial_note = f" из ${intended_usdc:.2f} (тонкий стакан)" if fill_status == "partial" else ""

            ai_block = ""
            if ai_score is not None and ai_verdict and ai_reason:
                risk_icon = "🟢" if ai_score <= 4 else ("🟡" if ai_score <= 6 else "🔴")
                ai_block = (
                    f"\n\n━━━━━━━━━━━━━━━━━\n"
                    f"🧠 <b>ИИ-анализ</b>\n"
                    f"{risk_icon} <b>{ai_verdict}</b> · риск {ai_score}/10\n"
                    f"💬 {ai_reason}"
                )

            msg = (
                f"{head}\n\n"
                f"📌 {title_html}\n"
                f"🎯 Исход: <b>{outcome}</b> @ {price:.3f} (~{prob}){hours_line}\n"
                + (f"{whale_line}\n" if whale_line else "")
                + "━━━━━━━━━━━━━━━━━\n"
                f"💵 Бот вложил: <b>${invested:.2f}{partial_note}</b>\n"
                f"💼 Остаток: <b>${remaining:.2f} pUSD</b>"
                f"{ai_block}"
                f"{link_line}"
            )
        await bot.send_message(
            chat_id=telegram_id, text=msg, parse_mode="HTML",
            disable_web_page_preview=True,
        )

    try:
        asyncio.run(_send())
    except Exception:
        log.exception("notify_failed", telegram_id=telegram_id)
