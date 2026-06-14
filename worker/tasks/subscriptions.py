"""
Subscription expiry reminders.

Runs periodically and notifies users:
  * when their subscription is about to expire (5 days and 1 day left),
  * once when it has just expired (copying is gated off by then).
Deduplicated per (user, expiry timestamp, threshold) via Redis, so renewing
resets the cycle and there's no spam on restart.
"""

import asyncio
import math
from datetime import datetime, timezone

import structlog

from core.cache import notify_once
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

_REM_TTL = 40 * 86400


def _parse(s: str) -> datetime:
    from dateutil.parser import parse as p

    d = p(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@celery_app.task(name="worker.tasks.check_subscription_expiry", queue="periodic")
def check_subscription_expiry() -> dict:
    from core.db import get_supabase

    sb = get_supabase()
    res = (
        sb.table("users")
        .select("telegram_id,sub_tier,sub_expires_at")
        .neq("sub_tier", "free")
        .not_.is_("sub_expires_at", "null")
        .execute()
    )
    now = datetime.now(timezone.utc)
    notified = 0

    for u in res.data or []:
        exp_raw = u.get("sub_expires_at")
        if not exp_raw:
            continue
        try:
            exp = _parse(exp_raw)
        except Exception:
            continue
        tg = u["telegram_id"]
        expkey = str(exp_raw)[:19]
        days_left = (exp - now).total_seconds() / 86400

        if days_left > 0:
            # Remind once at 1 day, once at 5 days (most urgent first).
            for n in (1, 5):
                if days_left <= n and notify_once(f"subrem:{tg}:{expkey}:{n}", ttl=_REM_TTL):
                    _notify_expiring(tg, max(1, math.ceil(days_left)))
                    notified += 1
                    break
        elif -1.5 <= days_left <= 0:
            if notify_once(f"subexp:{tg}:{expkey}", ttl=_REM_TTL):
                _notify_expired(tg)
                notified += 1

    log.info("subscription_check_done", checked=len(res.data or []), notified=notified)
    return {"checked": len(res.data or []), "notified": notified}


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
        log.warning("sub_notify_failed", telegram_id=telegram_id)


def _notify_expiring(telegram_id: int, days_left: int) -> None:
    word = "день" if days_left == 1 else ("дня" if 2 <= days_left <= 4 else "дней")
    _notify(
        telegram_id,
        f"⏳ <b>Подписка скоро закончится</b>\n\n"
        f"Осталось <b>{days_left} {word}</b>.\n\n"
        f"Чтобы не прерывать копирование сделок, продли подписку — "
        f"обратись к администратору. Статус: /subscription",
    )


def _notify_expired(telegram_id: int) -> None:
    _notify(
        telegram_id,
        "⛔️ <b>Подписка закончилась</b>\n\n"
        "Бот приостановил копирование сделок.\n"
        "Уже открытые позиции остаются без изменений.\n\n"
        "Для возобновления — продли подписку у администратора.",
    )
