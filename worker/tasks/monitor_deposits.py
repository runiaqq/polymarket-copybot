"""
Celery task: monitor user wallets for USDC balance changes.
Sends Telegram notifications on deposits and manual withdrawals.
Runs every 2 minutes via beat scheduler.
"""

import asyncio

import structlog

from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

DEPOSIT_MIN = 0.50    # notify if balance increased by at least $0.50
WITHDRAW_MIN = 0.50   # notify if balance decreased by at least $0.50


@celery_app.task(name="worker.tasks.monitor_deposits", queue="periodic")
def monitor_deposits() -> dict:
    from core.db import get_supabase
    from core.polygon import get_balances

    sb = get_supabase()

    # Only users with wallets and active subscriptions
    res = sb.table("users").select(
        "id, telegram_id, wallet_address, wallet_private_key_enc, balance_usdc, deposit_wallet_address"
    ).not_.is_("wallet_address", "null").execute()

    users = res.data or []
    notified = 0

    for user in users:
        addr = user.get("wallet_address")
        if not addr:
            continue

        try:
            balances = get_balances(addr)
            dw = user.get("deposit_wallet_address")
            dw_pusd = get_balances(dw).get("pusd", 0.0) if dw else 0.0
            # Use TOTAL across EOA + deposit wallet so internal sweeps don't
            # look like withdrawals (EOA drops but DW rises → net = 0).
            eoa_total = balances.get("total_usdc", 0.0)
            new_balance = round(eoa_total + dw_pusd, 4)
            old_balance = float(user.get("balance_usdc") or 0.0)
            diff = new_balance - old_balance

            # Auto-fund the deposit wallet: convert any deposited USDC (native or
            # bridged) on the EOA into pUSD, then sweep it into the deposit wallet
            # (the trading collateral). Needs POL on the EOA for gas.
            dw = user.get("deposit_wallet_address")
            on_eoa = balances.get("usdc", 0.0) + balances.get("usdc_e", 0.0) + balances.get("pusd", 0.0)
            if dw and on_eoa >= 1.0 and balances.get("matic", 0.0) >= 0.02 and user.get("wallet_private_key_enc"):
                try:
                    from core.polygon import fund_deposit_wallet
                    moved = fund_deposit_wallet(user["wallet_private_key_enc"], addr, dw)
                    if moved >= 1.0:
                        _notify_wrapped(user["telegram_id"], moved)
                        notified += 1
                except Exception:
                    log.warning("auto_fund_failed", user_id=user["id"])

            # Update stored balance
            sb.table("users").update({"balance_usdc": new_balance}).eq("id", user["id"]).execute()

            if diff >= DEPOSIT_MIN:
                _notify_deposit(user["telegram_id"], new_balance, diff)
                notified += 1
            elif diff <= -WITHDRAW_MIN:
                _notify_withdrawal(user["telegram_id"], new_balance, abs(diff))
                notified += 1

        except Exception:
            log.exception("balance_check_failed", user_id=user["id"])

    log.info("deposit_monitor_done", users=len(users), notified=notified)
    return {"users": len(users), "notified": notified}


def _notify_wrapped(telegram_id: int, amount: float) -> None:
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"♻️ <b>Средства готовы к торговле</b>\n\n"
                f"<b>${amount:.2f}</b> сконвертированы в pUSD и переведены на торговый кошелёк.\n\n"
                f"▶️ PolyMind копирует сделки!"
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("wrapped_notify_failed", telegram_id=telegram_id)


def _notify_deposit(telegram_id: int, new_balance: float, amount: float) -> None:
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"💚 <b>Пополнение получено!</b>\n\n"
                f"➕ <b>+${amount:.2f} USDC</b>\n"
                f"💼 Текущий баланс: <b>${new_balance:.2f} USDC</b>\n\n"
                f"▶️ PolyMind готов копировать сделки!"
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("deposit_notify_failed", telegram_id=telegram_id)


def _notify_withdrawal(telegram_id: int, new_balance: float, amount: float) -> None:
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🔴 <b>Вывод выполнен</b>\n\n"
                f"➖ <b>-${amount:.2f} USDC</b>\n"
                f"💼 Остаток: <b>${new_balance:.2f} USDC</b>"
            ),
            parse_mode="HTML",
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.exception("withdraw_notify_failed", telegram_id=telegram_id)
