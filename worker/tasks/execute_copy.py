"""
Fast-path Celery task: copy a donor trade to a subscriber's wallet.
Uses Polymarket CLOB v2 for real order placement.
"""

import asyncio

import structlog
from celery import Task

from worker.celery_app import celery_app

log = structlog.get_logger(__name__)


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
    from core.clob import generate_api_creds, get_market_token_id, place_order
    from core.db import get_supabase, insert_copy_trade, insert_trade_signal

    sb = get_supabase()

    # Load user
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None

    if not user or not user.get("wallet_private_key_enc"):
        log.warning("skip_no_wallet", user_id=user_id)
        return {"skipped": True, "reason": "no_wallet"}

    size_usdc = min(
        float(signal["size_usdc"]),
        float(user.get("max_position_usdc") or 25),
    )

    # Only copy BUY signals — SELL requires owning the token first
    if signal.get("side", "").upper() in ("SELL", "NO"):
        log.debug("skip_sell_signal", user_id=user_id)
        return {"skipped": True, "reason": "sell_not_supported"}

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

    # Resolve token_id
    token_id: str | None = signal.get("token_id")
    if not token_id:
        token_id = get_market_token_id(signal["market_id"], signal.get("side", "YES"))
    if not token_id:
        log.warning("skip_no_token_id", market_id=signal["market_id"])
        return {"skipped": True, "reason": "no_token_id"}

    # Save signal + trade record
    sig_row = insert_trade_signal({
        "donor_id":       signal.get("donor_db_id", 1),
        "market_id":      signal["market_id"],
        "title":          signal.get("title", ""),
        "side":           signal["side"],
        "price":          signal["price"],
        "size_usdc":      size_usdc,
        "source_tx_hash": signal.get("source_tx_hash", ""),
    })
    trade_row = insert_copy_trade({
        "user_id":   user["id"],
        "signal_id": sig_row["id"],
        "status":    "executing",
        "size_usdc": size_usdc,
    })

    try:
        result = place_order(
            private_key_enc=user["wallet_private_key_enc"],
            api_creds=api_creds,
            token_id=token_id,
            side=signal["side"],
            price=signal["price"],
            size_usdc=size_usdc,
        )

        order_id = result.get("orderID") or result.get("order_id") or ""
        sb.table("copy_trades").update({
            "status":   "confirmed",
            "order_id": order_id,
        }).eq("id", trade_row["id"]).execute()

        log.info("copy_trade_ok", user_id=user_id, order_id=order_id)
        _notify(user["telegram_id"], signal, order_id)

        return {"order_id": order_id, "user_id": user_id}

    except Exception as exc:
        sb.table("copy_trades").update({
            "status":    "failed",
            "error_msg": str(exc)[:1000],
        }).eq("id", trade_row["id"]).execute()
        log.exception("copy_trade_failed", user_id=user_id)
        raise self.retry(exc=exc)


def _notify(telegram_id: int, signal: dict, order_id: str) -> None:
    """Send async Telegram notification from sync Celery context."""
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        side_icon = "🟢" if signal["side"].upper() in ("BUY", "YES") else "🔴"
        donor = signal.get("donor_label") or signal.get("donor_address", "—")[:10]
        title = signal.get("title") or "—"
        msg = (
            f"⚡️ <b>Сделка скопирована!</b>\n\n"
            f"📌 <b>{title}</b>\n\n"
            f"{side_icon} {signal['side']} @ <code>{signal['price']:.4f}</code>\n"
            f"💵 Вложено: <b>${signal['size_usdc']:.2f} USDC</b>\n"
            f"👤 Донор: <b>{donor}</b>\n\n"
            f"📋 <code>{order_id[:24] if order_id else '—'}</code>"
        )
        await bot.send_message(
            chat_id=telegram_id,
            text=msg,
            parse_mode="HTML",
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("notify_failed", telegram_id=telegram_id)
