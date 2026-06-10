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
    default_retry_delay = 5  # seconds


@celery_app.task(
    bind=True,
    base=ExecuteCopyTask,
    name="worker.tasks.execute_copy_trade",
    queue="trades",
)
def execute_copy_trade(self: ExecuteCopyTask, user_id: int, signal: dict) -> dict:
    """
    Sign and submit a copy trade on behalf of one subscriber.

    signal keys: market_id, side, price, size_usdc, donor_address,
                 donor_label, donor_win_rate, donor_roi, timestamp
    """
    return asyncio.get_event_loop().run_until_complete(
        _execute(self, user_id, signal)
    )


async def _execute(task: ExecuteCopyTask, user_id: int, signal: dict) -> dict:
    from py_clob_client.client import ClobClient
    from py_clob_client.order_builder.constants import BUY

    from core.db import AsyncSessionLocal
    from core.db.models import CopyTrade, TradeSignal, TradeStatus
    from core.db.queries import get_user_by_telegram_id
    from core.privy import privy_client
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        # Load user
        result = await session.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(
                __import__("core.db.models", fromlist=["User"]).User
            ).where(
                __import__("core.db.models", fromlist=["User"]).User.id == user_id
            )
        )
        user = result.scalar_one_or_none()
        if not user or not user.wallet_address or not user.privy_user_id:
            log.warning("skip_no_wallet", user_id=user_id)
            return {"skipped": True, "reason": "no_wallet"}

        # Create signal record
        trade_signal = TradeSignal(
            donor_id=0,  # will be resolved properly in production
            market_id=signal["market_id"],
            side=signal["side"],
            price=signal["price"],
            size_usdc=min(signal["size_usdc"], user.max_position_usdc),
        )
        session.add(trade_signal)
        await session.flush()

        # Create copy trade record
        copy_trade = CopyTrade(
            user_id=user.id,
            signal_id=trade_signal.id,
            status=TradeStatus.EXECUTING,
            size_usdc=trade_signal.size_usdc,
        )
        session.add(copy_trade)
        await session.flush()

        # Build CLOB order via py-clob-client
        # The client uses the user's CLOB API key (derived from their wallet)
        clob = ClobClient(
            host=__import__("core.config", fromlist=["settings"]).settings.polymarket_clob_rest_url,
            chain_id=__import__("core.config", fromlist=["settings"]).settings.polymarket_chain_id,
            key=user.wallet_address,       # placeholder; real key from Privy
        )

        order = clob.create_order(
            {
                "token_id": signal["market_id"],
                "price": signal["price"],
                "side": BUY if signal["side"] == "YES" else "SELL",
                "size": trade_signal.size_usdc / signal["price"],
            }
        )

        # Sign + submit via Privy
        raw_tx = order.to_transaction()
        tx_hash = await privy_client.sign_and_send_transaction(
            privy_user_id=user.privy_user_id,
            wallet_address=user.wallet_address,
            tx=raw_tx,
        )

        copy_trade.tx_hash = tx_hash
        copy_trade.status = TradeStatus.CONFIRMED
        await session.commit()

        log.info("copy_trade_confirmed", user_id=user_id, tx=tx_hash, market=signal["market_id"])

        # Notify user immediately
        _notify_trade_executed(user.telegram_id, signal, tx_hash)

        return {"tx_hash": tx_hash, "user_id": user_id}


def _notify_trade_executed(telegram_id: int, signal: dict, tx_hash: str) -> None:
    from telegram import Bot

    from core.config import settings

    bot = Bot(token=settings.telegram_bot_token)
    msg = (
        f"Сделка исполнена\n"
        f"Рынок: {signal['market_id']}\n"
        f"Направление: {signal['side']} @ {signal['price']:.4f}\n"
        f"Донор: {signal['donor_label']} (ROI {signal.get('donor_roi', 0)*100:+.0f}%)\n"
        f"TX: {tx_hash[:10]}... "
        f"[Polygonscan](https://polygonscan.com/tx/{tx_hash})"
    )
    asyncio.get_event_loop().run_until_complete(
        bot.send_message(chat_id=telegram_id, text=msg, parse_mode="Markdown")
    )
