"""
Parallel AI analysis — runs AFTER trade execution, never blocks it.
Sends a follow-up Telegram message with risk score and reason.
"""

import asyncio
import json

import structlog
from openai import OpenAI

from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)
openai_client = OpenAI(api_key=settings.openai_api_key)

RISK_PROMPT = """\
You are a prediction market risk analyst.

Market ID: {market_id}
Direction copied: {side}
Price at execution: {price}
Size: ${size_usdc:.2f} USDC
Donor win rate (30d): {win_rate}
Donor ROI (30d): {roi}

Score the risk of this copied trade from 1 (lowest) to 10 (highest).
Reply with ONLY valid JSON, no extra text:
{{"score": <integer 1-10>, "reason": "<max 12 words>"}}
"""


def _call_gpt(signal: dict) -> tuple[int, str]:
    win_rate = signal.get("donor_win_rate")
    roi = signal.get("donor_roi")
    prompt = RISK_PROMPT.format(
        market_id=signal["market_id"],
        side=signal["side"],
        price=signal["price"],
        size_usdc=signal["size_usdc"],
        win_rate=f"{win_rate*100:.0f}%" if win_rate else "unknown",
        roi=f"{roi*100:+.0f}%" if roi else "unknown",
    )
    response = openai_client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=60,
    )
    raw = response.choices[0].message.content or "{}"
    try:
        data = json.loads(raw.strip())
        score = max(1, min(10, int(data.get("score", 5))))
        reason = str(data.get("reason", "Unable to assess"))
    except (json.JSONDecodeError, ValueError):
        score, reason = 5, "AI parse error"
    return score, reason


@celery_app.task(name="worker.tasks.run_ai_analysis", queue="ai", max_retries=2)
def run_ai_analysis(signal: dict, user_ids: list[int]) -> dict:
    try:
        score, reason = _call_gpt(signal)
    except Exception:
        log.exception("ai_call_failed", market=signal.get("market_id"))
        score, reason = 5, "AI unavailable"

    log.info("ai_scored", market=signal["market_id"], score=score, reason=reason)

    # Update signal in DB
    from core.db import get_supabase
    sb = get_supabase()
    sb.table("trade_signals").update({
        "ai_score": score,
        "ai_reason": reason,
    }).eq("market_id", signal["market_id"]).execute()

    # Build message
    if score <= 4:
        risk_label = "Низкий риск"
    elif score <= 6:
        risk_label = "Средний риск"
    else:
        risk_label = "Высокий риск"

    msg = f"AI оценка риска: {score}/10 — {risk_label}\nПричина: {reason}"

    if score >= settings.ai_risk_warn_threshold:
        msg += "\n\nОсторожно: высокий риск.\nРассмотри закрытие позиции через /positions"

    asyncio.get_event_loop().run_until_complete(_broadcast(user_ids, msg))
    return {"score": score, "reason": reason}


async def _broadcast(user_ids: list[int], message: str) -> None:
    from telegram import Bot
    from core.config import settings
    from core.db import get_supabase

    sb = get_supabase()
    res = sb.table("users").select("telegram_id").in_("id", user_ids).execute()
    bot = Bot(token=settings.telegram_bot_token)

    for row in res.data:
        try:
            await bot.send_message(chat_id=row["telegram_id"], text=message)
        except Exception:
            log.warning("notify_failed", telegram_id=row.get("telegram_id"))
