"""
Blueprint 42 — per-donor loss-streak circuit breaker.

Pure decision logic + small shared helpers. A donor whose last N unique copied
markets ALL resolved at a loss gets `tracked_wallets.paused_until` set; the
pollers (poll_tracked_wallets / poll_sniper_wallets) skip paused donors. The
evaluation loop lives in worker/tasks/donor_refresh.py (called from
sync_positions every 2 min).
"""

from datetime import datetime, timezone
from typing import Iterable

import structlog

log = structlog.get_logger(__name__)


def parse_ts(value: str | None) -> float | None:
    """ISO timestamp (Supabase format, any microsecond width) -> unix epoch."""
    if not value:
        return None
    try:
        raw = value.replace("Z", "+00:00")
        # Python <3.11 fromisoformat chokes on non-6-digit microseconds.
        if "." in raw:
            head, _, tail = raw.partition(".")
            frac = tail[:tail.find("+")] if "+" in tail else tail
            tz = tail[len(frac):]
            raw = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def donor_is_paused(paused_until: str | None, now_ts: float) -> bool:
    ts = parse_ts(paused_until)
    return ts is not None and now_ts < ts


def pause_decision(
    resolved: Iterable[tuple[str, float, float]],
    streak_len: int,
    paused_until_ts: float | None,
    now_ts: float,
) -> bool:
    """Should this donor be paused NOW?

    `resolved`: (condition_id, resolved_at_ts, realized_pnl) tuples, NEWEST
    FIRST, possibly with duplicate condition_ids (several users copied the same
    market — same outcome, so it counts as ONE result, not one per copier).

    Triggers when the `streak_len` most recent UNIQUE markets are all losses.
    Guards:
    - already paused -> no (don't extend/stack pauses);
    - newest loss resolved before the previous pause ended -> no (that streak
      was already punished; re-pausing requires at least one NEW post-resume
      loss on top of the still-intact streak).
    """
    if streak_len <= 0:
        return False
    seen: set[str] = set()
    streak: list[tuple[float, float]] = []
    for cond, ts, pnl in resolved:
        if cond in seen:
            continue
        seen.add(cond)
        streak.append((ts, pnl))
        if len(streak) == streak_len:
            break
    if len(streak) < streak_len or any(pnl >= 0 for _, pnl in streak):
        return False
    if paused_until_ts is not None:
        if now_ts < paused_until_ts:
            return False
        if streak[0][0] <= paused_until_ts:
            return False
    return True


def notify_admins(text: str) -> None:
    """Push to the ADMIN bot (falls back to the main bot token if the admin bot
    is not configured). Recipients: super-admin + active admins table."""
    import httpx

    from core.config import settings
    from core.db import list_admins

    token = settings.telegram_admin_bot_token or settings.telegram_bot_token
    chat_ids = {settings.admin_telegram_id}
    try:
        chat_ids |= {int(a["telegram_id"]) for a in list_admins()}
    except Exception:
        log.warning("notify_admins_list_failed")
    for chat_id in chat_ids:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10.0,
            )
        except Exception:
            log.warning("notify_admin_failed", chat_id=chat_id)
