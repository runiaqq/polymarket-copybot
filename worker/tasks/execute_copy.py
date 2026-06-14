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
    on-chain position. Returns (filled_usdc, status) where status is
    'full' | 'partial' | 'none' | 'unknown'.
    """
    try:
        from core.polymarket import get_positions
        time.sleep(3)  # give the data API a moment to reflect the fill
        pos = next(
            (p for p in get_positions(wallet_address) if p["token_id"] == token_id),
            None,
        )
        if not pos or pos["shares"] <= 0:
            return 0.0, "none"
        filled = pos.get("current_value") or (pos["shares"] * pos["avg_price"])
        if filled < 0.05 * intended_usdc:
            return filled, "none"
        if filled < 0.9 * intended_usdc:
            return filled, "partial"
        return filled, "full"
    except Exception:
        log.warning("confirm_fill_failed", token=token_id[:18])
        return intended_usdc, "unknown"


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

    # Check balance. In Polymarket V2 the tradeable collateral is pUSD (USDC.e must
    # be wrapped first). Deposits are auto-wrapped, but guard here too.
    try:
        from core.polygon import get_balances
        balances = get_balances(user["wallet_address"])
        tradeable = balances.get("pusd", 0)
        usdc_e = balances.get("usdc_e", 0)
        if tradeable < size_usdc:
            if (tradeable + usdc_e) >= size_usdc:
                _notify_needs_wrap(user["telegram_id"], balances)
                log.warning("skip_needs_wrap", user_id=user_id, pusd=tradeable, usdc_e=usdc_e)
                return {"skipped": True, "reason": "needs_wrap"}
            _notify_low_balance(user["telegram_id"], balances.get("total_usdc", 0), size_usdc, signal)
            log.warning("skip_low_balance", user_id=user_id, pusd=tradeable, needed=size_usdc)
            return {"skipped": True, "reason": "low_balance"}
    except Exception:
        log.warning("balance_check_failed", user_id=user_id)

    # Portfolio cap: don't open more than max_open_positions simultaneously.
    try:
        from core.polymarket import get_positions
        open_count = sum(1 for p in get_positions(user["wallet_address"]) if p["shares"] > 0)
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
            api_creds = generate_api_creds(user["wallet_private_key_enc"])
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
        )

        order_id = result.get("orderID") or result.get("order_id") or ""

        # FAK orders can partial-fill (or fill nothing) on thin books.
        # Confirm the actual fill from on-chain positions.
        filled, fill_status = _confirm_fill(user["wallet_address"], token_id, size_usdc)

        status = "confirmed" if fill_status != "none" else "unfilled"
        sb.table("copy_trades").update({
            "status":   status,
            "order_id": order_id,
            "size_usdc": round(filled, 2) if fill_status in ("full", "partial") else size_usdc,
        }).eq("id", trade_row["id"]).execute()

        log.info("copy_trade_ok", user_id=user_id, order_id=order_id,
                 fill=fill_status, filled=round(filled, 2))
        _notify(user["telegram_id"], signal, order_id, size_usdc, filled, fill_status)

        return {"order_id": order_id, "user_id": user_id, "fill": fill_status, "filled": filled}

    except Exception as exc:
        sb.table("copy_trades").update({
            "status":    "failed",
            "error_msg": str(exc)[:1000],
        }).eq("id", trade_row["id"]).execute()
        log.exception("copy_trade_failed", user_id=user_id)
        raise self.retry(exc=exc)


def _notify_needs_wrap(telegram_id: int, balances: dict) -> None:
    """User has USDC.e that isn't yet wrapped into the tradeable pUSD collateral."""
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        pusd = balances.get("pusd", 0)
        usdc_e = balances.get("usdc_e", 0)
        to_convert = usdc_e + balances.get("usdc", 0)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"♻️ <b>Нужно конвертировать в pUSD</b>\n\n"
                f"Polymarket V2 торгует в <b>pUSD</b>:\n"
                f"• pUSD (готово к торговле): <b>${pusd:.2f}</b>\n"
                f"• USDC (надо конвертировать): <b>${to_convert:.2f}</b>\n\n"
                f"Нажми /wrap чтобы конвертировать USDC → pUSD "
                f"(нужен POL на газ), и сделки снова будут исполняться."
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("notify_needs_wrap_failed", telegram_id=telegram_id)


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
) -> None:
    """Send the entry notification, reflecting full / partial / no fill, with event link."""
    from telegram import Bot
    from core.config import settings
    from core.polymarket import event_url

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        title = signal.get("title") or "—"
        url = event_url(signal.get("event_slug"))
        title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
        price = signal.get("price", 0)
        link_line = f"\n🔗 <a href=\"{url}\">Открыть на Polymarket</a>" if url else ""

        if fill_status == "none":
            msg = (
                f"⚠️ <b>Не удалось наполнить ордер</b>\n\n"
                f"📌 {title_html}\n\n"
                f"Стакан слишком тонкий — позиция не открыта. "
                f"Сделка пропущена.{link_line}"
            )
        else:
            head = "⚡️ <b>Сделка скопирована!</b>"
            if fill_status == "partial":
                head = "⚡️ <b>Сделка скопирована частично</b>"
            fill_line = (
                f"💵 Вложено: <b>${filled_usdc:.2f}</b>"
                + (f" из <b>${intended_usdc:.2f}</b> (тонкий стакан)" if fill_status == "partial" else " USDC")
            )
            msg = (
                f"{head}\n\n"
                f"📌 {title_html}\n\n"
                f"🟢 BUY @ <code>{price:.4f}</code>\n"
                f"{fill_line}{link_line}"
            )
        await bot.send_message(
            chat_id=telegram_id, text=msg, parse_mode="HTML", disable_web_page_preview=True
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("notify_failed", telegram_id=telegram_id)
