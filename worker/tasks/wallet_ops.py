"""
Wallet operations as background tasks: wrap USDC.e -> pUSD, and withdraw
(unwrap pUSD -> USDC.e if needed, then transfer USDC.e out).
These send on-chain transactions, so they run off the request path.
"""

import asyncio

import structlog

from worker.celery_app import celery_app

log = structlog.get_logger(__name__)


def _notify(telegram_id: int, text: str) -> None:
    from telegram import Bot
    from core.config import settings

    async def _send() -> None:
        await Bot(token=settings.telegram_bot_token).send_message(
            chat_id=telegram_id, text=text, parse_mode="HTML"
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.warning("walletop_notify_failed", telegram_id=telegram_id)


@celery_app.task(name="worker.tasks.wrap_collateral", queue="trades")
def wrap_collateral(user_id: int) -> dict:
    from core.db import get_supabase
    from core.polygon import get_balances, wrap_usdce_to_pusd

    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None
    if not user or not user.get("wallet_private_key_enc"):
        return {"skipped": True, "reason": "no_wallet"}

    addr = user["wallet_address"]
    balances = get_balances(addr)
    if balances.get("usdc_e", 0) < 1.0:
        _notify(user["telegram_id"], "ℹ️ Нет USDC.e для конвертации (или уже в pUSD).")
        return {"skipped": True, "reason": "nothing_to_wrap"}
    if balances.get("matic", 0) < 0.01:
        _notify(user["telegram_id"], "⛽️ Недостаточно POL на газ для конвертации. Пополни ~0.05 POL.")
        return {"skipped": True, "reason": "no_gas"}

    try:
        amount = balances["usdc_e"]
        wrap_usdce_to_pusd(user["wallet_private_key_enc"], addr)
        _notify(
            user["telegram_id"],
            f"♻️ <b>Готово!</b> Конвертировано <b>${amount:.2f}</b> USDC.e → pUSD.\n"
            f"Средства готовы к торговле.",
        )
        return {"wrapped": amount}
    except Exception as exc:
        log.exception("wrap_collateral_failed", user_id=user_id)
        _notify(user["telegram_id"], f"❌ Ошибка конвертации: <code>{str(exc)[:200]}</code>")
        return {"error": str(exc)[:200]}


@celery_app.task(name="worker.tasks.withdraw_funds", queue="trades")
def withdraw_funds(user_id: int, to_address: str, amount_usdc: float) -> dict:
    from core.db import get_supabase
    from core.polygon import get_balances, transfer_usdc, unwrap_pusd_to_usdce

    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None
    if not user or not user.get("wallet_private_key_enc"):
        return {"skipped": True, "reason": "no_wallet"}

    addr = user["wallet_address"]
    key = user["wallet_private_key_enc"]

    try:
        balances = get_balances(addr)
        usdc_e = balances.get("usdc_e", 0.0)
        # Unwrap pUSD -> USDC.e if we don't have enough liquid USDC.e to send.
        if usdc_e < amount_usdc:
            need = amount_usdc - usdc_e
            if balances.get("pusd", 0.0) >= need:
                unwrap_pusd_to_usdce(key, addr, need)
            # else: transfer_usdc will raise on insufficient funds below.

        tx_hash = transfer_usdc(
            private_key_enc=key,
            wallet_address=addr,
            to_address=to_address,
            amount_usdc=amount_usdc,
        )
        _notify(
            user["telegram_id"],
            f"✅ <b>Вывод выполнен!</b>\n\n"
            f"💵 <b>${amount_usdc:.2f} USDC.e</b>\n"
            f"📬 На: <code>{to_address}</code>\n\n"
            f"🔗 <a href=\"https://polygonscan.com/tx/{tx_hash}\">Транзакция</a>",
        )
        return {"tx": tx_hash}
    except Exception as exc:
        log.exception("withdraw_failed", user_id=user_id)
        _notify(user["telegram_id"], f"❌ Ошибка вывода: <code>{str(exc)[:200]}</code>")
        return {"error": str(exc)[:200]}
