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
        asyncio.run(_send())
    except Exception:
        log.warning("walletop_notify_failed", telegram_id=telegram_id)


@celery_app.task(name="worker.tasks.wrap_collateral", queue="trades")
def wrap_collateral(user_id: int) -> dict:
    """Fund the deposit wallet: convert EOA USDC -> pUSD and sweep it into the
    deposit wallet (the trading collateral)."""
    from core.db import get_supabase
    from core.polygon import fund_deposit_wallet, get_balances

    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None
    if not user or not user.get("wallet_private_key_enc"):
        return {"skipped": True, "reason": "no_wallet"}

    addr = user["wallet_address"]
    deposit_wallet = user.get("deposit_wallet_address")
    if not deposit_wallet:
        _notify(user["telegram_id"], "⚙️ Сначала разверни торговый кошелёк: /register")
        return {"skipped": True, "reason": "not_registered"}

    balances = get_balances(addr)
    on_eoa = balances.get("usdc", 0) + balances.get("usdc_e", 0) + balances.get("pusd", 0)
    if on_eoa < 1.0:
        _notify(user["telegram_id"], "ℹ️ На кошельке нет средств для перевода в торговый баланс.")
        return {"skipped": True, "reason": "nothing_to_fund"}
    if balances.get("matic", 0) < 0.02:
        _notify(user["telegram_id"], "⛽️ Недостаточно POL на газ. Пополни ~0.05 POL.")
        return {"skipped": True, "reason": "no_gas"}

    try:
        moved = fund_deposit_wallet(user["wallet_private_key_enc"], addr, deposit_wallet)
        if moved < 1.0:
            _notify(user["telegram_id"], "ℹ️ Нечего переводить.")
            return {"skipped": True, "reason": "nothing_moved"}
        _notify(
            user["telegram_id"],
            f"♻️ <b>Готово!</b> <b>${moved:.2f}</b> переведено на торговый кошелёк (pUSD).\n"
            f"Средства готовы к торговле.",
        )
        return {"moved": moved}
    except Exception as exc:
        log.exception("wrap_collateral_failed", user_id=user_id)
        _notify(user["telegram_id"], f"❌ Ошибка: <code>{str(exc)[:200]}</code>")
        return {"error": str(exc)[:200]}


@celery_app.task(name="worker.tasks.withdraw_funds", queue="trades")
def withdraw_funds(user_id: int, to_address: str, amount_usdc: float) -> dict:
    from core.db import get_supabase
    from core.polygon import (
        get_balances,
        swap_usdce_to_usdc,
        transfer_usdc,
        unwrap_pusd_to_usdce,
    )

    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None
    if not user or not user.get("wallet_private_key_enc"):
        return {"skipped": True, "reason": "no_wallet"}

    addr = user["wallet_address"]
    key = user["wallet_private_key_enc"]
    deposit_wallet = user.get("deposit_wallet_address")

    try:
        # In V2 the collateral lives in the deposit wallet — pull it back to the EOA
        # first (gasless via the relayer), then convert/transfer from the EOA.
        if deposit_wallet:
            try:
                from core import relayer
                dw_pusd = get_balances(deposit_wallet).get("pusd", 0.0)
                pull = min(amount_usdc, dw_pusd)
                if pull > 0:
                    relayer.transfer_from_deposit_wallet(key, addr, int(round(pull * 1_000_000)))
            except Exception:
                log.warning("withdraw_dw_pull_failed", user_id=user_id)

        # User receives native USDC. Build it up: unwrap pUSD -> USDC.e, then swap -> native.
        b = get_balances(addr)
        if b.get("usdc", 0.0) < amount_usdc:
            need_usdce = amount_usdc - b.get("usdc", 0.0)
            if b.get("usdc_e", 0.0) < need_usdce and b.get("pusd", 0.0) > 0:
                unwrap_pusd_to_usdce(key, addr, need_usdce - b.get("usdc_e", 0.0))
            b2 = get_balances(addr)
            swap_amt = min(b2.get("usdc_e", 0.0), need_usdce)
            if swap_amt > 0:
                swap_usdce_to_usdc(key, addr, swap_amt)

        tx_hash = transfer_usdc(
            private_key_enc=key,
            wallet_address=addr,
            to_address=to_address,
            amount_usdc=amount_usdc,
            use_bridged=False,   # send native USDC to the user
        )
        _notify(
            user["telegram_id"],
            f"✅ <b>Вывод выполнен!</b>\n\n"
            f"💵 <b>${amount_usdc:.2f} USDC</b>\n"
            f"📬 На: <code>{to_address}</code>\n\n"
            f"🔗 <a href=\"https://polygonscan.com/tx/{tx_hash}\">Транзакция</a>",
        )
        return {"tx": tx_hash}
    except Exception as exc:
        log.exception("withdraw_failed", user_id=user_id)
        _notify(user["telegram_id"], f"❌ Ошибка вывода: <code>{str(exc)[:200]}</code>")
        return {"error": str(exc)[:200]}
