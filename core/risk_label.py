"""
Blueprint 14.B — single source of truth for risk-score -> (emoji, label) mapping.

The LLM (worker.tasks.ai_filter) returns ONLY an integer risk_score (1-10). It never
emits an emoji or a verdict string. Both Telegram surfaces that show an AI risk verdict
(ai_filter._broadcast and execute_copy._notify) must call risk_label() instead of
deriving the emoji/verdict themselves, so the two can never disagree (the prod bug this
fixes: "🟢 Рискованно · риск 3/10" — emoji, verdict and number from three independent
sources, free to contradict each other).
"""

# (max_score_inclusive, emoji, verdict_label) — checked in ascending order.
_BANDS: list[tuple[int, str, str]] = [
    (2, "🟢", "Сильный сетап"),
    (4, "🟢", "Уверенный сигнал"),
    (6, "🟡", "Умеренный риск"),
    (8, "🟠", "Высокий риск"),
    (10, "🔴", "Опасная зона"),
]


def risk_label(score: int) -> tuple[str, str]:
    """Map a 1-10 risk_score to (emoji, verdict_label). Out-of-range scores clamp."""
    score = max(1, min(10, int(score)))
    for max_score, emoji, label in _BANDS:
        if score <= max_score:
            return emoji, label
    return _BANDS[-1][1], _BANDS[-1][2]
