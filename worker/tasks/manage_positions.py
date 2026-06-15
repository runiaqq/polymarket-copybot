"""
Position management (Phase 3): real P&L sync, hybrid exits, resolution detection.

Hybrid exit policy:
  * markets with >= tp_sl_min_hours left  → dynamic take-profit / stop-loss
  * markets closer to resolution          → hold to settle
  * resolved (redeemable) positions        → notify user (on-chain redeem is gated
                                             behind AUTO_REDEEM_ENABLED, off by default,
                                             pending live V2/pUSD verification).
"""

import asyncio
import time
from datetime import datetime, timezone

import structlog

from core.cache import claim, notify_once
from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

# Best-effort in-process guard for in-flight closes (per worker child).
_closing: set[tuple] = set()
# When a position was first observed (resets on restart — that's fine: old
# positions are assumed mature and immediately eligible for TP/SL evaluation).
_first_seen: dict[str, float] = {}


def _notify_once(key: str) -> bool:
    return notify_once(key)


def _claim_settled(user_id: int, condition_id: str) -> None:
    claim(f"settle:{user_id}:{condition_id}")


def _hours_left(end_date: str | None) -> float | None:
    if not end_date:
        return None
    try:
        from dateutil.parser import parse as parse_dt

        dt = parse_dt(end_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


@celery_app.task(name="worker.tasks.sync_positions", queue="periodic")
def sync_positions() -> dict:
    from core.db import get_active_subscribers
    from core.polymarket import get_closed_positions, get_positions

    subscribers = get_active_subscribers()
    if not subscribers:
        return {"users": 0}

    now = time.time()
    actions = 0
    for user in subscribers:
        wallet = user.get("deposit_wallet_address")
        if not wallet:
            continue
        uid = user["id"]
        tg = user["telegram_id"]

        # ── Open positions: win-on-resolution (redeemable) + TP/SL ──────────────
        try:
            positions = get_positions(wallet)
        except Exception:
            log.warning("positions_fetch_failed", user_id=uid)
            positions = None

        for p in (positions or []):
            if p["shares"] <= 0:
                continue
            token_id = p["token_id"]
            condition_id = p["condition_id"]

            if p.get("redeemable"):
                if _notify_once(f"settle:{uid}:{condition_id}"):
                    _emit_win(tg, p.get("title"), p.get("outcome"), p.get("cash_pnl", 0),
                              claimable=True, event_slug=p.get("event_slug"))
                    actions += 1
                continue

            # ── Hold-to-resolution strategy ───────────────────────────────
            # Binary markets pay $1 (win) or $0 (loss) at resolution.
            # Selling early almost always hurts EV, so we hold.
            # Exception: "hard stop" when the MARKET itself prices this
            # outcome at near-zero — it's effectively dead, exit now.

            hours = _hours_left(p.get("end_date"))

            # When very close to resolution — let it settle, never hard-stop.
            if hours is not None and hours < settings.tp_sl_min_hours:
                continue

            # Enforce minimum hold time (API P&L data unreliable on fresh positions).
            fkey = f"{uid}:{token_id}"
            seen_at = _first_seen.setdefault(fkey, now)
            age_sec = now - seen_at
            if age_sec < settings.position_min_hold_sec:
                log.debug("exit_skipped_too_new",
                          user_id=uid, token=token_id[:14],
                          age_min=round(age_sec / 60, 1))
                continue

            ckey = (uid, token_id)
            if ckey in _closing:
                continue

            # Hard stop: market has priced this outcome as essentially dead.
            best_bid = p.get("cur_price", p.get("best_bid"))
            if best_bid is not None and float(best_bid) < settings.hard_stop_abs_price:
                log.info("hard_stop_triggered",
                         user_id=uid, token=token_id[:14],
                         cur_price=best_bid, threshold=settings.hard_stop_abs_price)
                _closing.add(ckey)
                close_position.delay(uid, token_id, "hard_stop")
                actions += 1

        # ── Closed positions: resolution win/loss notices ───────────────────────
        try:
            closed = get_closed_positions(wallet)
        except Exception:
            closed = []
        for c in closed:
            if now - c["timestamp"] > settings.settlement_lookback_sec:
                continue  # too old — avoid restart spam
            cur = c["cur_price"]
            resolved_win = cur >= 0.98
            resolved_loss = cur <= 0.02
            if not (resolved_win or resolved_loss):
                continue  # mid-market sell (TP/SL/manual) — already notified at close
            if not _notify_once(f"settle:{uid}:{c['condition_id']}"):
                continue
            if resolved_win:
                _emit_win(tg, c.get("title"), c.get("outcome"), c.get("realized_pnl", 0),
                          event_slug=c.get("event_slug"))
            else:
                _emit_loss(tg, c.get("title"), c.get("outcome"), c.get("realized_pnl", 0),
                           event_slug=c.get("event_slug"))
            actions += 1

    log.info("sync_positions_done", users=len(subscribers), actions=actions)
    return {"users": len(subscribers), "actions": actions}


@celery_app.task(
    bind=True, name="worker.tasks.close_position", queue="trades", max_retries=2,
    default_retry_delay=5,
)
def close_position(self, user_id: int, token_id: str, reason: str = "manual") -> dict:
    from core.clob import generate_api_creds, sell_position
    from core.db import get_supabase
    from core.polymarket import get_order_book, get_positions

    sb = get_supabase()
    res = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = res.data if res else None
    if not user or not user.get("wallet_private_key_enc"):
        return {"skipped": True, "reason": "no_wallet"}

    deposit_wallet = user.get("deposit_wallet_address")
    if not deposit_wallet:
        _closing.discard((user_id, token_id))
        return {"skipped": True, "reason": "not_registered"}

    # Find the live position to know how many shares to sell.
    position = next(
        (p for p in get_positions(deposit_wallet)
         if p["token_id"] == token_id and p["shares"] > 0),
        None,
    )
    if position is None:
        if reason == "manual":
            _notify(
                user["telegram_id"],
                "ℹ️ <b>Позиция не найдена</b>\n\n"
                "Возможно, она уже закрылась или разрезолвилась.\n"
                "Проверь баланс через /balance.",
            )
        _closing.discard((user_id, token_id))
        return {"skipped": True, "reason": "no_position"}

    condition_id = position.get("condition_id", "")
    title = (position.get("title") or "—")[:50]
    outcome = position.get("outcome") or "—"
    cur_price = float(position.get("cur_price") or 0)

    book = get_order_book(token_id)
    best_bid = book.get("best_bid") if book else None

    # At very high prices (>0.93) the order book is usually empty —
    # buyers won't offer 0.93+ for a 0.07 return. Tell the user to wait for resolution.
    if not best_bid or float(best_bid) < 0.01:
        if cur_price >= 0.90:
            _notify(
                user["telegram_id"],
                f"📊 <b>Стакан пустой — это нормально</b>\n\n"
                f"📌 {title} · <b>{outcome}</b>\n"
                f"💹 Текущая цена: <b>{cur_price:.3f} (~{cur_price*100:.0f}%)</b>\n\n"
                f"При цене {cur_price*100:.0f}% никто не хочет покупать — "
                f"слишком маленькая потенциальная прибыль для покупателя.\n\n"
                f"✅ <b>Ничего делать не нужно</b> — при резолве бот автоматически "
                f"зачислит выигрыш. Просто жди результата события.",
            )
        else:
            url = _event_link(position.get("event_slug"))
            _notify(
                user["telegram_id"],
                f"⚠️ <b>Нет покупателей для продажи</b>\n\n"
                f"📌 {title} · <b>{outcome}</b>\n\n"
                f"Стакан пустой — ордер не пройдёт. "
                f"Попробуй позже или дождись резолва события.{url}",
            )
        _closing.discard((user_id, token_id))
        return {"skipped": True, "reason": "no_bid"}

    api_creds = {
        "clob_api_key":    user.get("clob_api_key"),
        "clob_secret":     user.get("clob_secret"),
        "clob_passphrase": user.get("clob_passphrase"),
    }
    if not api_creds["clob_api_key"]:
        try:
            api_creds = generate_api_creds(user["wallet_private_key_enc"], funder=deposit_wallet)
            sb.table("users").update(api_creds).eq("id", user_id).execute()
        except Exception as exc:
            raise self.retry(exc=exc)

    try:
        import math
        # Truncate shares DOWN to 2 decimal places to avoid "not enough balance"
        # errors caused by the positions API rounding up vs actual on-chain token balance.
        shares_to_sell = math.floor(position["shares"] * 100) / 100
        if shares_to_sell <= 0:
            _closing.discard((user_id, token_id))
            return {"skipped": True, "reason": "zero_shares_after_truncation"}

        sell_position(
            private_key_enc=user["wallet_private_key_enc"],
            api_creds=api_creds,
            token_id=token_id,
            shares=shares_to_sell,
            price=float(best_bid),
            tick_size=str(book.get("tick_size", "0.01")),
            neg_risk=bool(book.get("neg_risk", position.get("neg_risk", False))),
            slippage_pct=settings.exit_slippage_pct,
            deposit_wallet=deposit_wallet,
        )
        # Claim the settlement key so the resolution scanner won't double-notify
        # if this exit fills near 0/1.
        if condition_id:
            _claim_settled(user_id, condition_id)
        _notify_closed(user["telegram_id"], position, reason)
        log.info("position_closed", user_id=user_id, token=token_id[:18], reason=reason)
        return {"closed": True, "reason": reason}
    except Exception as exc:
        log.exception("close_position_failed", user_id=user_id, token=token_id[:18])
        err_str = str(exc).lower()
        # After max retries, notify the user so they know what happened.
        if self.request.retries >= self.max_retries:
            if "geoblock" in err_str or "trading restricted" in err_str:
                msg = "⚠️ <b>Не удалось закрыть позицию</b>\n\nГео-блок на сервере. Обратись к администратору."
            elif "no_bid" in err_str or "liquidity" in err_str:
                msg = (
                    f"⚠️ <b>Не удалось закрыть позицию</b>\n\n"
                    f"Стакан пустой. Попробуй позже или дождись резолва события."
                )
            else:
                msg = f"⚠️ <b>Не удалось закрыть позицию</b>\n\n<code>{str(exc)[:200]}</code>"
            _notify(user["telegram_id"], msg)
        raise self.retry(exc=exc)
    finally:
        _closing.discard((user_id, token_id))


# ── Notifications ────────────────────────────────────────────────────────────

def _notify(telegram_id: int, text: str) -> None:
    from telegram import Bot
    from core.config import settings as s

    async def _send() -> None:
        await Bot(token=s.telegram_bot_token).send_message(
            chat_id=telegram_id, text=text, parse_mode="HTML"
        )

    try:
        asyncio.get_event_loop().run_until_complete(_send())
    except Exception:
        log.warning("notify_failed", telegram_id=telegram_id)


def _event_link(event_slug: str | None) -> str:
    from core.polymarket import event_url
    url = event_url(event_slug)
    return f"\n🔗 <a href=\"{url}\">Открыть на Polymarket</a>" if url else ""


def _notify_closed(telegram_id: int, position: dict, reason: str) -> None:
    labels = {
        "take_profit": "🎯 Тейк-профит",
        "stop_loss":   "🛑 Стоп-лосс",
        "hard_stop":   "🚨 Жёсткий стоп (цена ~0)",
        "manual":      "✋ Вручную",
    }
    pnl = position.get("cash_pnl", 0)
    pct = max(-1.0, min(10.0, position.get("percent_pnl", 0)))
    icon = "📈" if pnl >= 0 else "📉"
    outcome = position.get("outcome") or "—"
    title = (position.get("title") or "—")[:50]
    _notify(
        telegram_id,
        f"✅ <b>Позиция закрыта</b> ({labels.get(reason, reason)})\n\n"
        f"📌 {title}\n"
        f"🎯 Исход: <b>{outcome}</b>\n"
        f"{icon} P&L: <b>{pnl:+.2f}$</b> ({pct:+.0%})"
        f"{_event_link(position.get('event_slug'))}",
    )


def _emit_win(telegram_id: int, title: str | None, outcome: str | None,
              pnl: float, claimable: bool = False, event_slug: str | None = None) -> None:
    note = ""
    if claimable:
        note = (
            "\n\nСредства зачислятся автоматически после расчёта."
            if settings.auto_redeem_enabled
            else "\n\nЗабери выигрыш на Polymarket (Portfolio → Claim)."
        )
    _notify(
        telegram_id,
        f"🏆 <b>Событие выиграно!</b>\n\n"
        f"📌 {(title or '—')[:50]}\n"
        f"🎯 Исход: <b>{outcome or '—'}</b>\n"
        f"📈 Результат: <b>{pnl:+.2f}$</b>"
        f"{_event_link(event_slug)}{note}",
    )


def _emit_loss(telegram_id: int, title: str | None, outcome: str | None,
               pnl: float, event_slug: str | None = None) -> None:
    _notify(
        telegram_id,
        f"💔 <b>Событие проиграно</b>\n\n"
        f"📌 {(title or '—')[:50]}\n"
        f"🎯 Исход: <b>{outcome or '—'}</b>\n"
        f"📉 Результат: <b>{pnl:+.2f}$</b>"
        f"{_event_link(event_slug)}",
    )
