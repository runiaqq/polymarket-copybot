"""
Parallel AI analysis — runs alongside auto-copy execution, never blocks it.
Sends the user a concise risk analysis of each copied whale entry.
"""

import asyncio
import json
import time

import structlog
from openai import OpenAI

from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)
openai_client = OpenAI(api_key=settings.openai_api_key)

# In-memory rate limiters (reset on worker restart — acceptable).
_market_notified: dict[str, float] = {}
_user_hourly: dict[int, list] = {}

MARKET_COOLDOWN_SEC = 900   # same market won't notify more than once / 15 min
USER_HOURLY_LIMIT = 8       # max AI analyses per user per hour

RISK_PROMPT = """\
Ты — опытный аналитик рынков предсказаний Polymarket внутри сервиса сигналов.
Крупный игрок («кит») только что купил исход — это потенциальный сигнал для входа.
Дай пользователю краткий профессиональный разбор — это ключевая ценность сервиса.

Данные сделки:
- Рынок: {title}
- Купленный исход: {outcome}
- Цена входа: {price} (рынок оценивает вероятность ~{prob}%)
- Размер ставки кита: ${size_usdc:.0f}
- До закрытия рынка: {hours}
- Комиссия рынка: {fee} bps

Оцени и верни СТРОГО JSON без лишнего текста:
{{"score": <целое 1-10, 1=низкий риск>, "verdict": "<Сильный сигнал|Умеренный|Рискованно>",
"reason": "<1-2 коротких предложения на русском с КОНКРЕТИКОЙ: учитывай уровень цены/вероятность,
запас времени до резолва и размер ставки кита. Без общих фраз и воды.>"}}
"""


def _call_gpt(signal: dict) -> tuple[int, str, str]:
    hours = signal.get("hours_to_resolve")
    price = float(signal.get("price", 0) or 0)
    prompt = RISK_PROMPT.format(
        title=(signal.get("title") or signal.get("market_id", "—"))[:90],
        outcome=signal.get("outcome") or "—",
        price=f"{price:.3f}",
        prob=f"{price*100:.0f}",
        size_usdc=signal.get("size_usdc", 0),
        hours=f"~{hours:.0f} ч" if hours else "неизвестно",
        fee=int(signal.get("fee_bps", 0) or 0),
    )
    response = openai_client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=160,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        data = json.loads(raw.strip())
        score = max(1, min(10, int(data.get("score", 5))))
        verdict = str(data.get("verdict", "Умеренный"))[:24]
        reason = str(data.get("reason", "Не удалось оценить"))
    except (json.JSONDecodeError, ValueError):
        score, verdict, reason = 5, "Умеренный", "Ошибка разбора ответа ИИ"
    return score, verdict, reason


@celery_app.task(name="worker.tasks.run_ai_analysis", queue="ai", max_retries=2)
def run_ai_analysis(signal: dict, user_ids: list[int]) -> dict:
    try:
        score, verdict, reason = _call_gpt(signal)
    except Exception:
        log.exception("ai_call_failed", market=signal.get("market_id"))
        score, verdict, reason = 5, "Умеренный", "ИИ временно недоступен"

    log.info("ai_scored", market=signal.get("market_id"), score=score, verdict=verdict)

    # Persist score on the signal row (by id when available).
    from core.db import get_supabase
    sb = get_supabase()
    try:
        update = {"ai_score": score, "ai_reason": reason}
        if signal.get("signal_id"):
            sb.table("trade_signals").update(update).eq("id", signal["signal_id"]).execute()
        else:
            sb.table("trade_signals").update(update).eq("market_id", signal["market_id"]).execute()
    except Exception:
        log.warning("ai_score_persist_failed", market=signal.get("market_id"))

    now = time.time()
    market_id = signal.get("market_id", "")
    if now - _market_notified.get(market_id, 0) < MARKET_COOLDOWN_SEC:
        return {"score": score, "reason": reason, "notified": False}
    _market_notified[market_id] = now

    filtered_users = []
    for uid in user_ids:
        recent = [t for t in _user_hourly.get(uid, []) if now - t < 3600]
        if len(recent) < USER_HOURLY_LIMIT:
            recent.append(now)
            _user_hourly[uid] = recent
            filtered_users.append(uid)

    if not filtered_users:
        return {"score": score, "reason": reason, "notified": False}

    from core.polymarket import event_url

    title = (signal.get("title") or signal.get("market_id", "—"))[:70]
    outcome = signal.get("outcome") or "—"
    size = signal.get("size_usdc", 0)
    price = float(signal.get("price", 0) or 0)
    hours = signal.get("hours_to_resolve")
    url = event_url(signal.get("event_slug"))

    risk_icon = "🟢" if score <= 4 else ("🟡" if score <= 6 else "🔴")
    title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
    hours_line = f" · ⏳ ~{hours:.0f} ч" if hours else ""
    link_line = f"\n🔗 <a href=\"{url}\">Открыть на Polymarket</a>" if url else ""

    # Signals mode: this is a signal to act on, not a copied trade.
    header = "🐳 <b>Сигнал по киту</b>" if not settings.auto_copy_enabled else "🧠 <b>ИИ-анализ сделки</b>"
    cta = "\n\n👉 Заходи на Polymarket и решай сам." if not settings.auto_copy_enabled else ""

    msg = (
        f"{header}\n\n"
        f"📌 {title_html}\n"
        f"🎯 Кит купил: <b>{outcome}</b> @ {price:.3f} (~{price*100:.0f}%)\n"
        f"🐳 Объём: <b>${size:.0f}</b>{hours_line}\n\n"
        f"{risk_icon} <b>{verdict}</b> · риск {score}/10\n"
        f"💬 {reason}"
        f"{link_line}"
        f"{cta}"
    )
    asyncio.get_event_loop().run_until_complete(_broadcast(filtered_users, msg))

    return {"score": score, "verdict": verdict, "reason": reason, "notified": True}


async def _broadcast(user_ids: list[int], message: str) -> None:
    from telegram import Bot
    from core.db import get_supabase

    sb = get_supabase()
    res = sb.table("users").select("telegram_id").in_("id", user_ids).execute()
    bot = Bot(token=settings.telegram_bot_token)

    for row in res.data:
        try:
            await bot.send_message(
                chat_id=row["telegram_id"], text=message,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception:
            log.warning("notify_failed", telegram_id=row.get("telegram_id"))
