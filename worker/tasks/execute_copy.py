"""
Fast-path Celery task: copy a donor trade to a subscriber's wallet.
Uses Polymarket CLOB v2 for real order placement.
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
            # Position not visible yet — retry unless this is the last attempt.
            log.debug("confirm_fill_retry", token=token_id[:18], attempt=attempt + 1)
        except Exception:
            log.warning("confirm_fill_failed", token=token_id[:18], attempt=attempt + 1)

    # After all retries still nothing — could be the order truly didn't fill.
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

    user_max = float(user.get("max_position_usdc") or 25)
    # Whale-detector signals carry a depth-derived cap (max_copy_usdc); donor/REST
    # signals don't, so fall back to the whale size there.
    depth_cap = float(signal.get("max_copy_usdc") or signal.get("size_usdc") or 0)
    size_usdc = min(user_max, depth_cap) if depth_cap > 0 else user_max
    if size_usdc < 1.0:
        log.debug("skip_below_min", user_id=user_id, size=size_usdc)
        return {"skipped": True, "reason": "below_min_size"}

    # Check collateral: in V2 the tradeable pUSD lives in the DEPOSIT WALLET.
    # The deposit monitor sweeps EOA funds into it; if it's short, try a sweep
    # on demand before giving up.
    try:
        from core.polygon import get_balances, fund_deposit_wallet
        tradeable = get_balances(deposit_wallet).get("pusd", 0)
        if tradeable < size_usdc:
            eoa = get_balances(user["wallet_address"])
            on_eoa = eoa.get("pusd", 0) + eoa.get("usdc_e", 0) + eoa.get("usdc", 0)
            if (tradeable + on_eoa) >= size_usdc and on_eoa >= 0.5:
                try:
                    fund_deposit_wallet(user["wallet_private_key_enc"], user["wallet_address"], deposit_wallet)
                    tradeable = get_balances(deposit_wallet).get("pusd", 0)
                except Exception:
                    log.warning("ondemand_fund_failed", user_id=user_id)
            if tradeable < size_usdc:
                _notify_low_balance(user["telegram_id"], tradeable, size_usdc, signal)
                log.warning("skip_low_balance", user_id=user_id, pusd=tradeable, needed=size_usdc)
                return {"skipped": True, "reason": "low_balance"}
    except Exception:
        log.warning("balance_check_failed", user_id=user_id)

    # Portfolio cap: don't open more than max_open_positions simultaneously.
    try:
        from core.polymarket import get_positions
        open_count = sum(1 for p in get_positions(deposit_wallet) if p["shares"] > 0)
        if open_count >= settings.max_open_positions:
            log.info("skip_max_positions", user_id=user_id, open=open_count)
            return {"skipped": True, "reason": "max_positions"}
    except Exception:
        log.warning("positions_count_failed", user_id=user_id)

    # Ensure CLOB API credentials exist
    api_creds = {
        "clob_api_key":    user.get("clob_api_key"),
        "clob_secret":     user.get("clob_secret"),
        "clob_passphrase": user.get("clob_passphrase"),
    }
    if not api_creds["clob_api_key"]:
        try:
            api_creds = generate_api_creds(user["wallet_private_key_enc"], funder=deposit_wallet)
            sb.table("users").update(api_creds).eq("id", user_id).execute()
            log.info("clob_creds_generated", user_id=user_id)
        except Exception as exc:
            log.exception("clob_creds_failed", user_id=user_id)
            raise self.retry(exc=exc)

    # Resolve token_id (whale signals already carry the exact outcome token).
    token_id: str | None = signal.get("token_id")
    if not token_id:
        token_id = get_market_token_id(signal["market_id"], signal.get("side", "YES"))
    if not token_id:
        log.warning("skip_no_token_id", market_id=signal["market_id"])
        return {"skipped": True, "reason": "no_token_id"}

    # Fresh order-book re-check: price/depth may have moved since detection.
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

    if size_usdc < 1.0:
        log.debug("skip_below_min_after_book", user_id=user_id, size=size_usdc)
        return {"skipped": True, "reason": "below_min_after_book"}

    # Reuse the signal row persisted by the scanner; only insert for legacy donor mode.
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

    # ── Guard against Celery retry re-executing an already-placed order ──────
    # If a copy_trade row for this (user, signal) already exists with a non-failed
    # terminal status, this is a Celery retry after a transient error — skip.
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

    trade_row = insert_copy_trade({
        "user_id":   user["id"],
        "signal_id": signal_id,
        "status":    "executing",
        "size_usdc": size_usdc,
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

        # Mark as placed immediately so retries won't double-execute.
        try:
            sb.table("copy_trades").update({
                "status":   "placed",
                "order_id": order_id,
            }).eq("id", trade_row["id"]).execute()
        except Exception:
            pass

        # Confirm actual fill from on-chain positions (deposit wallet).
        filled, fill_status = _confirm_fill(deposit_wallet, token_id, size_usdc)

        # Remaining balance after the trade.
        try:
            from core.polygon import get_balances
            remaining = get_balances(deposit_wallet).get("pusd", 0.0)
        except Exception:
            remaining = 0.0

        # Get AI analysis to include in the single combined notification.
        score, verdict, reason = None, None, None
        try:
            from worker.tasks.ai_filter import _call_gpt
            score, verdict, reason = _call_gpt(signal)
        except Exception:
            log.warning("ai_inline_failed", user_id=user_id)

        final_status = "confirmed" if fill_status != "none" else "unfilled"
        try:
            sb.table("copy_trades").update({
                "status":   final_status,
                "size_usdc": round(filled, 2) if fill_status in ("full", "partial") else size_usdc,
            }).eq("id", trade_row["id"]).execute()
        except Exception:
            pass

        log.info("copy_trade_ok", user_id=user_id, order_id=order_id,
                 fill=fill_status, filled=round(filled, 2))
        _notify(user["telegram_id"], signal, order_id, size_usdc, filled,
                fill_status, remaining, score, verdict, reason)

        return {"order_id": order_id, "user_id": user_id, "fill": fill_status, "filled": filled}

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


def _notify_not_registered(telegram_id: int) -> None:
    """User hasn't set up their deposit wallet yet — can't trade."""
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
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("notify_not_registered_failed", telegram_id=telegram_id)


def _notify_low_balance(telegram_id: int, balance: float, needed: float, signal: dict) -> None:
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        title = signal.get("title") or "—"
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"⚠️ <b>Недостаточно средств для сделки</b>\n\n"
                f"📌 {title}\n\n"
                f"💰 Нужно: <b>${needed:.2f} USDC</b>\n"
                f"💼 На балансе: <b>${balance:.2f} USDC</b>\n\n"
                f"Пополни кошелёк через /wallet чтобы не пропускать сделки."
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("notify_low_balance_failed", telegram_id=telegram_id)


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
        bot = Bot(token=settings.telegram_bot_token)
        title = (signal.get("title") or "—")[:60]
        url = event_url(signal.get("event_slug"))
        title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
        price = float(signal.get("price") or 0)
        outcome = signal.get("outcome") or "—"
        prob = f"{price * 100:.0f}%"
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
            elif fill_status == "unknown":
                head = "✅ <b>Бот открыл позицию</b>"

            invested = filled_usdc if fill_status in ("full", "partial") else intended_usdc
            partial_note = f" из ${intended_usdc:.2f} (тонкий стакан)" if fill_status == "partial" else ""

            # AI block
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
                f"🎯 Исход: <b>{outcome}</b> @ {price:.3f} (~{prob})\n"
                f"💵 Вложено: <b>${invested:.2f}{partial_note}</b>\n"
                f"💼 Остаток: <b>${remaining:.2f} pUSD</b>"
                f"{ai_block}"
                f"{link_line}"
            )
        await bot.send_message(
            chat_id=telegram_id, text=msg, parse_mode="HTML", disable_web_page_preview=True
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("notify_failed", telegram_id=telegram_id)
