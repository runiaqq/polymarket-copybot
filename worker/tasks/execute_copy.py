"""
Fast-path task: copy a donor trade to a single subscriber.
No AI gate — executes immediately when dispatched.
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
    from core.db import get_supabase, insert_copy_trade, insert_trade_signal
    from core.privy import privy_client

    sb = get_supabase()

    # Load user by id
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None

    if not user or not user.get("wallet_address") or not user.get("privy_user_id"):
        log.warning("skip_no_wallet", user_id=user_id)
        return {"skipped": True, "reason": "no_wallet"}

    size_usdc = min(float(signal["size_usdc"]), float(user.get("max_position_usdc", 25)))

    # Save signal
    sig_row = insert_trade_signal({
        "donor_id": signal.get("donor_db_id", 1),
        "market_id": signal["market_id"],
        "side": signal["side"],
        "price": signal["price"],
        "size_usdc": size_usdc,
    })

    # Save copy trade as executing
    trade_row = insert_copy_trade({
        "user_id": user["id"],
        "signal_id": sig_row["id"],
        "status": "executing",
        "size_usdc": size_usdc,
    })

    try:
        # Sign + submit via Privy
        tx_hash = asyncio.get_event_loop().run_until_complete(
            privy_client.sign_and_send_transaction(
                privy_user_id=user["privy_user_id"],
                wallet_address=user["wallet_address"],
                tx={
                    "to": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # Polymarket exchange
                    "data": "0x",
                    "value": "0x0",
                    "chainId": 137,
                },
            )
        )

        sb.table("copy_trades").update({
            "tx_hash": tx_hash,
            "status": "confirmed",
        }).eq("id", trade_row["id"]).execute()

        log.info("copy_trade_confirmed", user_id=user_id, tx=tx_hash)

        # Notify user
        _notify(user["telegram_id"], signal, tx_hash)

        return {"tx_hash": tx_hash, "user_id": user_id}

    except Exception as exc:
        sb.table("copy_trades").update({
            "status": "failed",
            "error_msg": str(exc)[:500],
        }).eq("id", trade_row["id"]).execute()
        log.exception("copy_trade_failed", user_id=user_id)
        raise self.retry(exc=exc)


def _notify(telegram_id: int, signal: dict, tx_hash: str) -> None:
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        msg = (
            f"Сделка исполнена\n"
            f"Рынок: `{signal['market_id'][:40]}`\n"
            f"Направление: {signal['side']} @ {signal['price']:.4f}\n"
            f"Донор: {signal.get('donor_label', '?')} "
            f"(ROI {(signal.get('donor_roi') or 0)*100:+.0f}%)\n"
            f"TX: [Polygonscan](https://polygonscan.com/tx/{tx_hash})"
        )
        await bot.send_message(chat_id=telegram_id, text=msg, parse_mode="Markdown")

    asyncio.get_event_loop().run_until_complete(_send())
