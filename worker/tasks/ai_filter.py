"""
Parallel AI analysis — runs alongside auto-copy execution, never blocks it.
Sends the user a concise risk analysis of each copied whale entry.

Blueprint 14.B: the LLM returns ONLY risk_score + structured analysis fields via a
strict json_schema response_format. It never emits an emoji or a verdict label —
those are derived deterministically from risk_score by core.risk_label.risk_label(),
the single source of truth shared with worker.tasks.execute_copy._notify. This fixes
the prod bug where emoji/verdict/score disagreed (e.g. "🟢 Рискованно · риск 3/10").
"""

import asyncio
import json
import time

import structlog
from openai import OpenAI

from core.config import settings
from core.risk_label import risk_label
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)
openai_client = OpenAI(api_key=settings.openai_api_key)

# In-memory rate limiters (reset on worker restart — acceptable).
_market_notified: dict[str, float] = {}
_user_hourly: dict[int, list] = {}

MARKET_COOLDOWN_SEC = 900   # same market won't notify more than once / 15 min
USER_HOURLY_LIMIT = 8       # max AI analyses per user per hour

SIGNAL_TYPES = (
    "penny_collecting", "value_bet", "momentum",
    "longshot_size", "consensus_stack", "coin_flip",
)

SYSTEM_PROMPT = """\
Ты — старший аналитик хедж-фонда, специализация — рынки предсказаний (Polymarket).
Тебе дают сделку проверенного прибыльного кита из белого списка топ-трейдеров. Твоя
работа — оценить КАЧЕСТВО СДЕЛКИ как инвестиционный кейс, а не описывать вводные.

ЗАПРЕЩЕНО:
- Пересказывать цифры («цена 0.94, до закрытия 3 часа») — пользователь видит их сам.
- Общие фразы («высокая вероятность, но есть риск») — это мусор, не анализ.
- Эмодзи, вердикты или слово «риск N/10» — это проставит система, не ты.

ТРЕБУЕТСЯ распознать СТРУКТУРУ сделки и дать инсайт уровня деска:
- Дорогой фаворит (цена 0.90+) на крупный размер → «сбор копеек»: малый апсайд,
  жирный tail-risk одного чёрного лебедя. Оцени, оправдан ли риск.
- Низкая вероятность (<0.30) + агрессивный размер кита → возможный инсайд или
  асимметричная ставка: кит видит то, чего не видит рынок. Подсвети это.
- Консенсус нескольких китов в одном исходе → усиление сигнала, но проверь,
  не скученность ли это (один и тот же источник информации).
- Тонкий запас времени до резолва → нет места для разворота, риск выше.
- Цена около 0.50 → монетка; нужна причина, почему это не шум.

Верни строго JSON по схеме:
- risk_score: целое 1-10 (1 = низкий риск сделки, 10 = высокий риск).
- signal_type: один тег структуры сделки.
- thesis: главный тезис — почему сделка хороша или плоха (1 предложение, конкретика).
- caution: главный риск этой конкретной сделки (1 предложение, конкретика).

Тон — профессиональный аналитик, не маркетолог. Русский язык. Без воды.
"""

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "trade_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["risk_score", "signal_type", "thesis", "caution"],
            "properties": {
                "risk_score": {"type": "integer", "minimum": 1, "maximum": 10},
                "signal_type": {"type": "string", "enum": list(SIGNAL_TYPES)},
                "thesis": {"type": "string", "maxLength": 180},
                "caution": {"type": "string", "maxLength": 120},
            },
        },
    },
}

USER_PROMPT = """\
Данные сделки:
- Рынок: {title}
- Купленный исход: {outcome}
- Цена входа: {price} (рынок оценивает вероятность ~{prob}%)
- Размер ставки трейдера: ${size_usdc:.0f}
- Консенсус: {consensus} проверенных трейдер(ов) в этом же исходе
- До закрытия рынка: {hours}
"""


def _call_gpt(signal: dict) -> tuple[int, str, str, str]:
    """Returns (risk_score, signal_type, thesis, caution). Never raises — falls back
    to a neutral score on any parse/schema failure so the caller never crashes."""
    from core.polymarket import format_time_left
    price = float(signal.get("price", 0) or 0)
    # BP5: compute time-left fresh so the AI always sees the correct remaining window.
    time_left = format_time_left(signal.get("resolution_iso"))
    user_prompt = USER_PROMPT.format(
        title=(signal.get("title") or signal.get("market_id", "—"))[:90],
        outcome=signal.get("outcome") or "—",
        price=f"{price:.3f}",
        prob=f"{price*100:.0f}",
        size_usdc=signal.get("size_usdc", 0),
        consensus=int(signal.get("consensus") or 1),
        hours=time_left,
    )
    response = openai_client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=220,
        response_format=RESPONSE_SCHEMA,
    )
    raw = response.choices[0].message.content or "{}"
    try:
        data = json.loads(raw.strip())
        score = max(1, min(10, int(data["risk_score"])))
        signal_type = str(data.get("signal_type", "coin_flip"))
        if signal_type not in SIGNAL_TYPES:
            signal_type = "coin_flip"
        thesis = str(data.get("thesis", "Не удалось оценить"))[:180]
        caution = str(data.get("caution", ""))[:120]
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        score, signal_type, thesis, caution = 5, "coin_flip", "ИИ временно недоступен", ""
    return score, signal_type, thesis, caution


@celery_app.task(name="worker.tasks.run_ai_analysis", queue="ai", max_retries=2)
def run_ai_analysis(signal: dict, user_ids: list[int]) -> dict:
    try:
        score, signal_type, thesis, caution = _call_gpt(signal)
    except Exception:
        log.exception("ai_call_failed", market=signal.get("market_id"))
        score, signal_type, thesis, caution = 5, "coin_flip", "ИИ временно недоступен", ""

    emoji, verdict = risk_label(score)
    log.info("ai_scored", market=signal.get("market_id"), score=score,
              signal_type=signal_type, verdict=verdict)

    # Persist score on the signal row (by id when available).
    from core.db import get_supabase
    sb = get_supabase()
    try:
        update = {"ai_score": score, "ai_reason": thesis, "ai_signal_type": signal_type}
        if signal.get("signal_id"):
            sb.table("trade_signals").update(update).eq("id", signal["signal_id"]).execute()
        else:
            sb.table("trade_signals").update(update).eq("market_id", signal["market_id"]).execute()
    except Exception:
        log.warning("ai_score_persist_failed", market=signal.get("market_id"))

    now = time.time()
    market_id = signal.get("market_id", "")
    if now - _market_notified.get(market_id, 0) < MARKET_COOLDOWN_SEC:
        return {"score": score, "thesis": thesis, "notified": False}
    _market_notified[market_id] = now

    filtered_users = []
    for uid in user_ids:
        recent = [t for t in _user_hourly.get(uid, []) if now - t < 3600]
        if len(recent) < USER_HOURLY_LIMIT:
            recent.append(now)
            _user_hourly[uid] = recent
            filtered_users.append(uid)

    if not filtered_users:
        return {"score": score, "thesis": thesis, "notified": False}

    from core.polymarket import event_url, format_time_left

    title = (signal.get("title") or signal.get("market_id", "—"))[:70]
    outcome = signal.get("outcome") or "—"
    size = signal.get("size_usdc", 0)
    price = float(signal.get("price", 0) or 0)
    url = event_url(signal.get("event_slug"))

    title_html = f"<a href=\"{url}\">{title}</a>" if url else f"<b>{title}</b>"
    # BP5: compute time-left fresh at the moment of broadcast.
    time_left = format_time_left(signal.get("resolution_iso"))
    hours_line = f" · ⏳ {time_left}"
    link_line = f"\n🔗 <a href=\"{url}\">Открыть на Polymarket</a>" if url else ""
    caution_line = f"\n⚠️ {caution}" if caution else ""

    # Signals mode: this is a signal to act on, not a copied trade.
    header = "🐳 <b>Сигнал по киту</b>" if not settings.auto_copy_enabled else "🧠 <b>ИИ-анализ сделки</b>"
    cta = "\n\n👉 Заходи на Polymarket и решай сам." if not settings.auto_copy_enabled else ""

    msg = (
        f"{header}\n\n"
        f"📌 {title_html}\n"
        f"🎯 Кит купил: <b>{outcome}</b> @ {price:.3f} (~{price*100:.0f}%)\n"
        f"🐳 Объём: <b>${size:.0f}</b>{hours_line}\n\n"
        f"{emoji} <b>{verdict}</b> · риск {score}/10\n"
        f"💬 {thesis}"
        f"{caution_line}"
        f"{link_line}"
        f"{cta}"
    )
    asyncio.run(_broadcast(filtered_users, msg))

    return {"score": score, "signal_type": signal_type, "thesis": thesis, "notified": True}


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
