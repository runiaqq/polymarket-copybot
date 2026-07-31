"""BP33 executor: consumes shadow-engine signals from Redis and trades them
with real money for pilot users, then settles positions on-chain.

Three independent loops:
  * signal_loop     — Redis pub/sub -> place FAK BUY orders per active user
  * resolution_loop — settle finished windows (redeem winners via relayer)
  * funding_loop    — sweep EOA deposits into the deposit wallet (pUSD)
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from core.cache import notify_once
from core.config import settings
from core.order_fill import extract_buy_fill
from core.shadow_model import fee_usdc as estimate_fee_usdc
from cryptobot import db
from cryptobot.logic import (
    daily_loss_exceeded,
    entry_price_ok,
    pilot_stake,
    requote_price_ok,
    should_flag_stuck,
    signal_is_fresh,
)

log = structlog.get_logger(__name__)

# Don't fire an order when the window is about to close: the fill would get
# almost no time value and resolution risk is pure noise.
MIN_TIME_LEFT_SEC = 8.0
LOW_BALANCE_THROTTLE_SEC = 6 * 3600
DAILY_LIMIT_THROTTLE_SEC = 12 * 3600
# BP35: an open row whose window ended this long ago is flagged as stuck.
STUCK_THRESHOLD_SEC = 15 * 60
STUCK_THROTTLE_SEC = 3600


async def notify(chat_id: int, text: str) -> None:
    """Direct Bot-API send with the crypto bot token (executor-side messages)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.crypto_bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            response.raise_for_status()
    except Exception:
        log.warning("crypto_notify_failed", chat_id=chat_id)


class CryptoExecutor:
    # ── signal path ───────────────────────────────────────────────────────────

    async def signal_loop(self) -> None:
        import redis.asyncio as aioredis

        while True:
            try:
                client = aioredis.from_url(settings.redis_url, decode_responses=True)
                pubsub = client.pubsub()
                await pubsub.subscribe(settings.crypto_signals_channel)
                log.info("crypto_signals_subscribed", channel=settings.crypto_signals_channel)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        signal = json.loads(message["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    try:
                        await self._handle_signal(signal)
                    except Exception:
                        log.exception("crypto_signal_handling_failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("crypto_signal_stream_error", error=str(exc)[:200])
                await asyncio.sleep(5)

    async def _handle_signal(self, signal: dict[str, Any]) -> None:
        now = time.time()
        condition_id = str(signal.get("condition_id") or "")
        if not condition_id or not signal.get("token_id"):
            return
        if not settings.crypto_trading_enabled:
            log.info("crypto_signal_skipped", reason="kill_switch", cond=condition_id[:14])
            return
        if not signal_is_fresh(
            float(signal.get("published_at") or 0), now, settings.crypto_signal_max_age_sec
        ):
            log.warning("crypto_signal_skipped", reason="stale", cond=condition_id[:14])
            return
        if float(signal.get("window_end") or 0) - now < MIN_TIME_LEFT_SEC:
            log.warning("crypto_signal_skipped", reason="window_closing", cond=condition_id[:14])
            return
        if not entry_price_ok(signal.get("best_ask"), settings.crypto_max_entry_price):
            log.info("crypto_signal_skipped", reason="price_ceiling", cond=condition_id[:14])
            return

        users = await asyncio.to_thread(db.active_traders)
        whitelist = set(settings.crypto_whitelist_telegram_ids)
        users = [u for u in users if u["telegram_id"] in whitelist]
        if not users:
            return
        # Fair-order dispatch: nobody consistently eats the book first.
        random.shuffle(users)
        await asyncio.gather(*(self._trade_for_user(user, signal) for user in users))

    async def _trade_for_user(self, user: dict, signal: dict[str, Any]) -> None:
        try:
            result = await asyncio.to_thread(self._place_trade, user, signal)
        except Exception:
            log.exception("crypto_trade_failed", user_id=user["id"])
            return
        if result.get("notify"):
            await notify(user["telegram_id"], result["notify"])

    def _place_trade(self, user: dict, signal: dict[str, Any]) -> dict:
        """Blocking order placement for one user. Returns {'notify': str|None}."""
        from core.clob import place_order
        from core.polygon import get_balances

        user_id = int(user["id"])
        condition_id = str(signal["condition_id"])
        if db.has_trade(user_id, condition_id):
            return {}

        realized_today = db.realized_pnl_today(user_id)
        preset = float(user.get("stake_usdc") or settings.crypto_default_stake_usdc)
        if daily_loss_exceeded(realized_today, preset, settings.crypto_daily_loss_mult):
            if notify_once(f"crypto-daily-limit:{user_id}", ttl=DAILY_LIMIT_THROTTLE_SEC):
                return {
                    "notify": (
                        "🛑 <b>Дневной лимит потерь достигнут</b>\n\n"
                        f"Сегодняшний результат: <b>${realized_today:+.2f}</b>.\n"
                        "Бот не открывает новые сделки до конца дня (UTC)."
                    )
                }
            return {}

        free_pusd = get_balances(user["deposit_wallet_address"]).get("pusd", 0.0)
        stake = pilot_stake(
            preset,
            free_pusd,
            fee_headroom_pct=settings.crypto_fee_headroom_pct,
            exchange_min_usdc=settings.exchange_min_order_usdc,
        )
        if stake <= 0:
            if notify_once(f"crypto-low-balance:{user_id}", ttl=LOW_BALANCE_THROTTLE_SEC):
                return {
                    "notify": (
                        "⚠️ <b>Недостаточно средств для сделки</b>\n\n"
                        f"Торговый баланс: <b>${free_pusd:.2f} pUSD</b>, "
                        f"ставка: <b>${preset:.2f}</b>.\n"
                        "Пополните кошелёк — сигналы сейчас пропускаются."
                    )
                }
            return {}

        api_creds = {
            "clob_api_key": user.get("clob_api_key"),
            "clob_secret": user.get("clob_secret"),
            "clob_passphrase": user.get("clob_passphrase"),
        }
        tick = signal.get("tick_size") or 0.01
        base = {
            "user_id": user_id,
            "asset": str(signal.get("asset") or ""),
            "condition_id": condition_id,
            "token_id": str(signal["token_id"]),
            "side": str(signal.get("side") or ""),
            "window_end": _iso(float(signal["window_end"])),
            "signal_price": signal.get("price"),
            "model_p": signal.get("model_p"),
            "edge": signal.get("edge"),
            "intended_usdc": stake,
        }
        requoted = False
        try:
            response = place_order(
                private_key_enc=user["wallet_private_key_enc"],
                api_creds=api_creds,
                token_id=str(signal["token_id"]),
                side="BUY",
                price=float(signal["best_ask"]),
                size_usdc=stake,
                tick_size=f"{float(tick):g}",
                neg_risk=False,
                slippage_pct=settings.crypto_entry_slippage_pct,
                deposit_wallet=user["deposit_wallet_address"],
            )
        except Exception as exc:
            error_text = str(exc)
            response = None
            skip_reason = f"order_error: {error_text[:180]}"
            # BP34: a FAK killed by the exchange (book moved between the shadow
            # snapshot and our order) gets exactly ONE re-quote at the fresh ask.
            if "no orders found to match" in error_text.lower():
                response, skip_reason = self._requote_once(user, signal, api_creds, stake, tick)
                requoted = response is not None
            if response is None:
                db.insert_trade({**base, "status": "skipped", "skip_reason": skip_reason})
                log.warning("crypto_order_error", user_id=user_id, error=error_text[:200])
                return {}

        fill = extract_buy_fill(response, stake)
        if fill is None or fill.status == "none":
            skip_reason = "no_fill_after_requote" if requoted else "no_fill"
            db.insert_trade({**base, "status": "skipped", "skip_reason": skip_reason})
            log.info("crypto_no_fill", user_id=user_id, cond=condition_id[:14])
            return {}

        fee = fill.fee_usdc
        if fee is None:
            fee = estimate_fee_usdc(
                fill.fill_price,
                fill.shares,
                fee_rate=float(signal.get("fee_rate") or 0.0),
                exponent=float(signal.get("fee_exponent") or 1.0),
            )
        inserted = db.insert_trade(
            {
                **base,
                "status": "open",
                "filled_usdc": round(fill.filled_usdc, 6),
                "shares": round(fill.shares, 6),
                "fill_price": fill.fill_price,
                "fee_usdc": round(fee, 6),
            }
        )
        if not inserted:
            # The unique index caught a race — the money IS spent; log loudly.
            log.error("crypto_trade_duplicate_after_fill", user_id=user_id, cond=condition_id)
            return {}

        side_label = "Up ⬆️" if signal.get("side") == "up" else "Down ⬇️"
        window_end_utc = datetime.fromtimestamp(
            float(signal["window_end"]),
            tz=timezone.utc,  # noqa: UP017
        )
        log.info(
            "crypto_trade_opened",
            user_id=user_id,
            cond=condition_id[:14],
            side=signal.get("side"),
            filled=round(fill.filled_usdc, 4),
            price=round(fill.fill_price, 4),
        )
        return {
            "notify": (
                f"🎯 <b>Сделка открыта: BTC {side_label}</b>\n\n"
                f"Вход: <b>{fill.fill_price:.3f}</b> | Ставка: <b>${fill.filled_usdc:.2f}</b>\n"
                f"Модель: {float(signal.get('model_p') or 0):.0%} | "
                f"Edge: {float(signal.get('edge') or 0):+.1%}\n"
                f"Окно закроется в {window_end_utc:%H:%M:%S} UTC"
            )
        }

    def _requote_once(
        self,
        user: dict,
        signal: dict[str, Any],
        api_creds: dict,
        stake: float,
        tick: Any,
    ) -> tuple[Any | None, str]:
        """BP34: one re-quote after a FAK kill. Re-fetches the live book and,
        if the entry guards still hold, re-places the same FAK at the fresh
        best ask. Returns (response, "") on success or (None, skip_reason)."""
        from core.clob import place_order
        from core.polymarket import get_order_book

        user_id = int(user["id"])
        condition_id = str(signal["condition_id"])

        if float(signal.get("window_end") or 0) - time.time() < MIN_TIME_LEFT_SEC:
            return None, "requote_window_closing"

        book = get_order_book(str(signal["token_id"]))
        fresh_ask = (book or {}).get("best_ask")
        signal_ask = signal.get("best_ask")
        if not requote_price_ok(
            signal_ask,
            fresh_ask,
            settings.crypto_requote_max_worse_pct,
            settings.crypto_max_entry_price,
        ):
            if not fresh_ask:
                return None, "requote_no_book"
            if float(fresh_ask) > settings.crypto_max_entry_price:
                return None, "requote_price_ceiling"
            return None, "requote_price_too_worse"

        try:
            response = place_order(
                private_key_enc=user["wallet_private_key_enc"],
                api_creds=api_creds,
                token_id=str(signal["token_id"]),
                side="BUY",
                price=float(fresh_ask),
                size_usdc=stake,
                tick_size=f"{float(tick):g}",
                neg_risk=False,
                slippage_pct=settings.crypto_entry_slippage_pct,
                deposit_wallet=user["deposit_wallet_address"],
            )
        except Exception as exc:
            if "no orders found to match" in str(exc).lower():
                return None, "no_fill_after_requote"
            return None, f"order_error_after_requote: {str(exc)[:150]}"

        log.info(
            "crypto_requote_placed",
            user_id=user_id,
            cond=condition_id[:14],
            old_ask=signal_ask,
            new_ask=fresh_ask,
        )
        return response, ""

    # ── settlement path ───────────────────────────────────────────────────────

    async def resolution_loop(self) -> None:
        while True:
            try:
                rows = await asyncio.to_thread(db.open_trades)
                now = datetime.now(timezone.utc)  # noqa: UP017
                for row in rows:
                    await self._resolve_trade(row, now)
                await self._redeem_sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("crypto_resolution_cycle_failed", error=str(exc)[:300])
            await asyncio.sleep(settings.crypto_resolution_poll_sec)

    async def _resolve_trade(self, row: dict[str, Any], now: datetime) -> None:
        window_end = datetime.fromisoformat(str(row["window_end"]).replace("Z", "+00:00"))
        if now < window_end:
            return
        owner = row.get("crypto_users") or {}
        chat_id = owner.get("telegram_id")
        condition_id = str(row["condition_id"])
        token_id = str(row["token_id"])
        from core.relayer import (
            detect_outcome_index,
            get_payout_numerator,
            is_condition_resolved,
        )

        resolved = await asyncio.to_thread(is_condition_resolved, condition_id)
        if not resolved:
            age = (now - window_end).total_seconds()
            if age >= settings.crypto_resolution_void_after_sec:
                await asyncio.to_thread(db.settle_trade, int(row["id"]), "void", 0.0, None)
                if chat_id:
                    await notify(
                        chat_id,
                        "⚠️ Рынок не резолвился за 24 часа — сделка аннулирована. "
                        "Средства вернутся после ручного разбора.",
                    )
                return
            await self._flag_stuck(row, chat_id, window_end, now)
            return

        outcome_index = await asyncio.to_thread(detect_outcome_index, condition_id, token_id)
        if outcome_index is None:
            log.warning("crypto_resolution_index_unknown", cond=condition_id[:14])
            await self._flag_stuck(row, chat_id, window_end, now)
            return
        held_payout = await asyncio.to_thread(get_payout_numerator, condition_id, outcome_index)
        other_payout = await asyncio.to_thread(
            get_payout_numerator, condition_id, 1 - outcome_index
        )
        if held_payout <= 0 and other_payout <= 0:
            log.warning("crypto_resolution_payout_unavailable", cond=condition_id[:14])
            await self._flag_stuck(row, chat_id, window_end, now)
            return

        won = held_payout > 0
        cost = float(row.get("filled_usdc") or 0)
        fee = float(row.get("fee_usdc") or 0)
        shares = float(row.get("shares") or 0)

        # BP35 settle-first: the outcome is known — settle and tell the user NOW.
        # The money move (redemption) happens after and never blocks the result.
        pnl = shares - cost - fee if won else -cost - fee
        await asyncio.to_thread(
            db.settle_trade, int(row["id"]), "win" if won else "loss", pnl, None
        )
        log.info(
            "crypto_trade_settled",
            trade_id=row["id"],
            status="win" if won else "loss",
            pnl=round(pnl, 4),
        )
        if chat_id:
            roi = pnl / (cost + fee) if cost + fee > 0 else 0.0
            header = "✅ <b>Победа</b>" if won else "❌ <b>Проигрыш</b>"
            side_label = "Up ⬆️" if row.get("side") == "up" else "Down ⬇️"
            await notify(
                chat_id,
                f"{header}: BTC {side_label}\n"
                f"Вход: {float(row.get('fill_price') or 0):.3f} | "
                f"Ставка: ${cost:.2f}\n"
                f"PnL: <b>${pnl:+.2f}</b> ({roi:+.1%})",
            )

        if won:
            redeem_tx = await self._try_redeem(row, outcome_index)
            if redeem_tx:
                await asyncio.to_thread(db.set_redeem_tx, int(row["id"]), redeem_tx)

    async def _try_redeem(self, row: dict[str, Any], outcome_index: int) -> str | None:
        """One redemption attempt for a settled win. Returns the redeem_tx value
        to persist ('recovered_externally' when the tokens are already burned)
        or None — the sweep retries later (redeem_winnings is idempotent)."""
        from core.relayer import redeem_winnings

        owner = row.get("crypto_users") or {}
        condition_id = str(row["condition_id"])
        try:
            redeem = await asyncio.to_thread(
                redeem_winnings,
                owner["wallet_private_key_enc"],
                condition_id,
                False,
                outcome_index,
                str(row["token_id"]),
            )
        except Exception as exc:
            log.error("crypto_redeem_failed", cond=condition_id[:14], error=str(exc)[:200])
            return None
        if redeem.get("skipped"):
            log.warning(
                "crypto_redeem_skipped",
                cond=condition_id[:14],
                reason=redeem.get("reason"),
            )
            if redeem.get("reason") == "no_token_balance":
                return "recovered_externally"
            return None
        return redeem.get("tx")

    async def _redeem_sweep(self) -> None:
        """BP35: retry redemption for wins settled without a money move.
        Silent for the user — they already got the result."""
        from core.relayer import detect_outcome_index

        rows = await asyncio.to_thread(db.unredeemed_wins)
        for row in rows:
            outcome_index = await asyncio.to_thread(
                detect_outcome_index, str(row["condition_id"]), str(row["token_id"])
            )
            if outcome_index is None:
                continue
            redeem_tx = await self._try_redeem(row, outcome_index)
            if redeem_tx:
                await asyncio.to_thread(db.set_redeem_tx, int(row["id"]), redeem_tx)
                log.info("crypto_redeem_recovered", trade_id=row["id"], tx=redeem_tx)

    async def _flag_stuck(
        self,
        row: dict[str, Any],
        chat_id: int | None,
        window_end: datetime,
        now: datetime,
    ) -> None:
        """BP35 watchdog: an open row long past window_end gets a throttled log
        and a single reassurance to the user (per throttle window)."""
        if not should_flag_stuck(window_end.timestamp(), now.timestamp(), STUCK_THRESHOLD_SEC):
            return
        if not notify_once(f"crypto-stuck:{row['id']}", ttl=STUCK_THROTTLE_SEC):
            return
        log.warning(
            "crypto_resolution_stuck",
            trade_id=row["id"],
            cond=str(row["condition_id"])[:14],
            age_sec=int((now - window_end).total_seconds()),
        )
        if chat_id:
            await notify(
                chat_id,
                "⏳ Рынок ещё не рассчитан — результат придёт автоматически, "
                "средства в безопасности.",
            )

    # ── funding path ──────────────────────────────────────────────────────────

    async def funding_loop(self) -> None:
        while True:
            try:
                users = await asyncio.to_thread(db.wallet_users)
                for user in users:
                    result = await asyncio.to_thread(self._fund_user, user)
                    if result.get("notify"):
                        await notify(user["telegram_id"], result["notify"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("crypto_funding_cycle_failed", error=str(exc)[:300])
            await asyncio.sleep(settings.crypto_funding_poll_sec)

    def _fund_user(self, user: dict) -> dict:
        """Detect EOA deposits and sweep them into tradeable pUSD (same
        mechanics as the copytrade deposit monitor, scoped to crypto_users)."""
        from core.polygon import fund_deposit_wallet, get_balances

        addr = user["wallet_address"]
        dw = user.get("deposit_wallet_address")
        try:
            balances = get_balances(addr)
        except Exception:
            log.warning("crypto_balance_check_failed", user_id=user["id"])
            return {}

        eoa_stable = round(balances.get("usdc", 0.0) + balances.get("usdc_e", 0.0), 4)
        baseline = float(user.get("eoa_stable_baseline") or 0.0)
        is_new_deposit = eoa_stable >= 0.5 and eoa_stable > baseline + 0.5
        has_pol = balances.get("matic", 0.0) >= 0.02

        moved = 0.0
        if dw and eoa_stable >= 1.0 and has_pol and user.get("wallet_private_key_enc"):
            try:
                moved = fund_deposit_wallet(user["wallet_private_key_enc"], addr, dw)
            except Exception:
                log.warning("crypto_auto_fund_failed", user_id=user["id"])

        new_baseline = 0.0 if moved >= 1.0 else eoa_stable
        if abs(new_baseline - baseline) > 0.01:
            db.update_user(user["id"], {"eoa_stable_baseline": new_baseline})

        # Self-heal stranded USDC.e on the deposit wallet (redeemed winnings).
        if dw and user.get("wallet_private_key_enc"):
            try:
                dw_balances = get_balances(dw)
                if dw_balances.get("usdc_e", 0.0) >= 0.10:
                    from core.relayer import convert_dw_usdce_to_pusd

                    convert_dw_usdce_to_pusd(user["wallet_private_key_enc"])
            except Exception:
                log.warning("crypto_dw_wrap_failed", user_id=user["id"])

        if moved >= 1.0:
            return {
                "notify": (
                    "💚 <b>Средства зачислены на торговый баланс</b>\n\n"
                    f"<b>${moved:.2f}</b> сконвертированы в pUSD и готовы к торговле.\n"
                    "Включите торговлю в меню, если ещё не включена."
                )
            }
        if is_new_deposit and not has_pol:
            return {
                "notify": (
                    "💚 <b>Пополнение получено</b> "
                    f"(+${eoa_stable - baseline:.2f} USDC)\n\n"
                    "⛽️ Для перевода на торговый баланс нужен <b>POL</b> на газ.\n"
                    "Отправьте <b>~0.1 POL</b> на тот же адрес — дальше всё произойдёт "
                    "автоматически."
                )
            }
        if is_new_deposit:
            return {
                "notify": (
                    "💚 <b>Пополнение получено</b> "
                    f"(+${eoa_stable - baseline:.2f} USDC)\n\n"
                    "Бот переведёт средства на торговый баланс (pUSD) в течение пары минут."
                )
            }
        return {}

    async def run(self) -> None:
        log.info(
            "crypto_executor_started",
            channel=settings.crypto_signals_channel,
            whitelist=settings.crypto_whitelist_telegram_ids,
            trading_enabled=settings.crypto_trading_enabled,
        )
        await asyncio.gather(
            self.signal_loop(),
            self.resolution_loop(),
            self.funding_loop(),
        )


def _iso(timestamp_sec: float) -> str:
    return datetime.fromtimestamp(timestamp_sec, tz=timezone.utc).isoformat()  # noqa: UP017
