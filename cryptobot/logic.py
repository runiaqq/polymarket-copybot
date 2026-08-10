"""Pure decision helpers for the BP33 executor — no I/O, unit-testable."""

from __future__ import annotations


def pilot_stake(
    preset_usdc: float,
    free_pusd: float,
    *,
    fee_headroom_pct: float,
    exchange_min_usdc: float,
) -> float:
    """Stake for one trade: the user's preset, capped by free pUSD minus fee
    headroom. Returns 0.0 when even the exchange minimum is unaffordable."""
    if preset_usdc <= 0 or free_pusd <= 0:
        return 0.0
    cap = free_pusd * (1.0 - fee_headroom_pct)
    stake = min(preset_usdc, cap)
    if stake < exchange_min_usdc:
        return 0.0
    return round(stake, 2)


def signal_is_fresh(published_at: float, now: float, max_age_sec: float) -> bool:
    """Reject stale signals (Redis lag, executor restart, clock jitter)."""
    age = now - published_at
    return 0 <= age <= max_age_sec


def entry_price_ok(best_ask: float | None, max_entry_price: float) -> bool:
    """Skip entries whose ask is already above the hard price ceiling."""
    return best_ask is not None and 0 < best_ask <= max_entry_price


def price_collapsed(
    signal_ask: float | None,
    fresh_ask: float | None,
    max_drop_pct: float,
) -> bool:
    """BP38 collapse guard: a fresh ask far BELOW the signal ask means the
    market violently repriced after the signal snapshot (trade #176: signal
    0.81 -> fill 0.49) — the model probability is stale and the edge thesis
    is dead. Moderate dips (<= max_drop_pct) are liquidity noise and remain
    tradable. Fails open on a missing/invalid fresh ask: the guard targets a
    rare tail event and must not halt trading on book-fetch hiccups."""
    if not signal_ask or signal_ask <= 0 or not fresh_ask or fresh_ask <= 0:
        return False
    return fresh_ask < signal_ask * (1.0 - max_drop_pct)


def should_flag_stuck(window_end_ts: float, now_ts: float, threshold_sec: float) -> bool:
    """BP35 watchdog: an open trade whose window ended more than threshold_sec
    ago is stuck — the outcome is overdue and the user deserves a heartbeat."""
    return now_ts - window_end_ts > threshold_sec


def daily_loss_exceeded(realized_today_usdc: float, stake_usdc: float, mult: float) -> bool:
    """Stop trading for the day once realized losses reach mult × stake."""
    if mult <= 0 or stake_usdc <= 0:
        return False
    return realized_today_usdc <= -(mult * stake_usdc)


def wr_gate_blocks(outcomes: list[bool], lookback: int, min_wr: float) -> bool:
    """BP45 regime gate: block entries while the model is cold.

    `outcomes` — win/loss results of the most recent RESOLVED shadow trades
    that match the executor's own filter, newest first. Bad days cluster
    (07-10.08: shadow WR fell 86% -> 73% and every one of those days closed
    red), so a trailing window over the SHADOW stream — which keeps trading
    while the real bot sits out — both detects the cold streak and slides
    past it for auto-resume.

    Fails open until a full window exists: no data must never halt trading.
    """
    if lookback <= 0 or len(outcomes) < lookback:
        return False
    window = outcomes[:lookback]
    return sum(window) / lookback < min_wr
