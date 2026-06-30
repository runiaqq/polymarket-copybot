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
        withdrawable_usdc,
    )

    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None
    if not user or not user.get("wallet_private_key_enc"):
        return {"skipped": True, "reason": "no_wallet"}

    tg_id = user["telegram_id"]
    addr  = user["wallet_address"]
    key   = user["wallet_private_key_enc"]
    deposit_wallet = user.get("deposit_wallet_address")

    # ── Pre-flight balance gate (belt-and-suspenders with the FSM UI gate) ──────
    avail = withdrawable_usdc(user)
    if amount_usdc > avail:
        msg = (
            f"❌ Недостаточно средств.\n"
            f"Доступно: <b>${avail:.2f} USDC</b>, запрошено: <b>${amount_usdc:.2f} USDC</b>."
        )
        log.warning("withdraw_insufficient_funds", user_id=user_id,
                    avail=avail, requested=amount_usdc)
        _notify(tg_id, msg)
        return {"skipped": True, "reason": "insufficient_funds", "avail": avail}

    # ── POL gas pre-check on the EOA ────────────────────────────────────────────
    eoa_bal = get_balances(addr)
    if eoa_bal.get("matic", 0.0) < 0.02:
        _notify(tg_id, "⛽️ Недостаточно POL на газ. Пополни EOA ~0.05 POL и попробуй снова.")
        log.warning("withdraw_no_gas", user_id=user_id, matic=eoa_bal.get("matic"))
        return {"skipped": True, "reason": "no_gas"}

    try:
        # ── Step 1: pull pUSD from deposit wallet → EOA (gasless relayer) ───────
        if deposit_wallet:
            try:
                from core import relayer
                dw_pusd = get_balances(deposit_wallet).get("pusd", 0.0)
                pull = min(amount_usdc, dw_pusd)
                if pull > 0:
                    relayer.transfer_from_deposit_wallet(key, addr, int(round(pull * 1_000_000)))
                    log.info("withdraw_dw_pull_ok", user_id=user_id, pulled=pull)
            except Exception as pull_exc:
                # Fail loud — don't silently proceed if the pull failed
                raise RuntimeError(
                    f"не удалось вывести средства из торгового кошелька: {pull_exc}"
                ) from pull_exc

        # ── Step 2: convert EOA pUSD → USDC.e → native USDC ────────────────────
        b = get_balances(addr)
        if b.get("usdc", 0.0) < amount_usdc:
            need_usdce = amount_usdc - b.get("usdc", 0.0)
            if b.get("usdc_e", 0.0) < need_usdce and b.get("pusd", 0.0) > 0:
                try:
                    unwrap_pusd_to_usdce(key, addr, need_usdce - b.get("usdc_e", 0.0))
                    log.info("withdraw_unwrap_ok", user_id=user_id)
                except Exception as unwrap_exc:
                    raise RuntimeError(
                        f"конвертация pUSD → USDC.e не удалась: {unwrap_exc}"
                    ) from unwrap_exc
            b2 = get_balances(addr)
            swap_amt = min(b2.get("usdc_e", 0.0), need_usdce)
            if swap_amt > 0:
                try:
                    swap_usdce_to_usdc(key, addr, swap_amt)
                    log.info("withdraw_swap_ok", user_id=user_id, swapped=swap_amt)
                except Exception as swap_exc:
                    raise RuntimeError(
                        f"swap USDC.e → USDC не удался: {swap_exc}"
                    ) from swap_exc

        # ── Step 3: send native USDC + wait for on-chain receipt ────────────────
        tx_hash = transfer_usdc(
            private_key_enc=key,
            wallet_address=addr,
            to_address=to_address,
            amount_usdc=amount_usdc,
            use_bridged=False,
        )
        log.info("withdraw_ok", user_id=user_id, amount=amount_usdc, tx=tx_hash[:12])
        _notify(
            tg_id,
            f"✅ <b>Вывод успешно завершён!</b>\n\n"
            f"💵 <b>${amount_usdc:.2f} USDC</b>\n"
            f"📬 На: <code>{to_address}</code>\n\n"
            f'🔗 <a href="https://polygonscan.com/tx/{tx_hash}">Polygonscan</a>',
        )
        return {"tx": tx_hash}
    except Exception as exc:
        log.exception("withdraw_failed", user_id=user_id)
        _notify(tg_id, f"❌ Ошибка вывода: <code>{str(exc)[:300]}</code>\n\nПопробуй позже или обратись в поддержку.")
        return {"error": str(exc)[:300]}
