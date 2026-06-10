"""
Parallel AI analysis task — runs AFTER trade execution, never blocks it.
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

Score the risk of this copied trade from 1 (lowest risk) to 10 (highest risk).
Consider: price extremity (near 0 or 1 is risky), donor track record, position size.

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
        score = int(data.get("score", 5))
        reason = str(data.get("reason", "Unable to assess"))
        score = max(1, min(10, score))
    except (json.JSONDecodeError, KeyError, ValueError):
        log.warning("ai_parse_error", raw=raw)
        score, reason = 5, "AI parse error"

    return score, reason


def _score_to_emoji(score: int) -> str:
    if score <= 4:
        return "Low risk"
    if score <= 6:
        return "Medium risk"
    return "High risk — review manually"


@celery_app.task(
    name="worker.tasks.run_ai_analysis",
    queue="ai",
    max_retries=2,
)
def run_ai_analysis(signal: dict, user_ids: list[int]) -> dict:
    try:
        score, reason = _call_gpt(signal)
    except Exception:
        log.exception("ai_call_failed", market=signal.get("market_id"))
        score, reason = 5, "AI unavailable"

    log.info("ai_scored", market=signal["market_id"], score=score, reason=reason)

    # Persist score to DB
    asyncio.get_event_loop().run_until_complete(
        _save_score(signal["market_id"], score, reason)
    )

    # Build follow-up Telegram message
    risk_label = _score_to_emoji(score)
    msg = (
        f"AI оценка риска: {score}/10 — {risk_label}\n"
        f"Причина: {reason}"
    )

    if score >= settings.ai_risk_warn_threshold:
        msg += (
            "\n\nОсторожно: высокий риск.\n"
            "Рассмотри закрытие позиции через /positions"
        )

    _broadcast_to_users(user_ids, signal["market_id"], msg)
    return {"score": score, "reason": reason}


async def _save_score(market_id: str, score: int, reason: str) -> None:
    from sqlalchemy import update

    from core.db import AsyncSessionLocal
    from core.db.models import TradeSignal

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(TradeSignal)
            .where(TradeSignal.market_id == market_id)
            .values(ai_score=score, ai_reason=reason)
        )
        await session.commit()


def _broadcast_to_users(user_ids: list[int], market_id: str, message: str) -> None:
    asyncio.get_event_loop().run_until_complete(
        _async_broadcast(user_ids, market_id, message)
    )


async def _async_broadcast(user_ids: list[int], market_id: str, message: str) -> None:
    from sqlalchemy import select

    from core.config import settings
    from core.db import AsyncSessionLocal
    from core.db.models import User
    from telegram import Bot

    bot = Bot(token=settings.telegram_bot_token)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.id.in_(user_ids)))
        users = result.scalars().all()

    for user in users:
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=message,
                parse_mode="Markdown",
            )
        except Exception:
            log.warning("notify_failed", telegram_id=user.telegram_id)
