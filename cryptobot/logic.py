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


def requote_price_ok(
    signal_ask: float | None,
    fresh_ask: float | None,
    max_worse_pct: float,
    max_entry_price: float,
) -> bool:
    """BP34 re-quote guard after a FAK kill: the fresh ask must be a valid
    price below the hard entry ceiling and not worse than the signal ask by
    more than max_worse_pct (paying above that breaks the edge thesis)."""
    if not signal_ask or signal_ask <= 0 or not fresh_ask or fresh_ask <= 0:
        return False
    if fresh_ask > max_entry_price:
        return False
    return fresh_ask <= signal_ask * (1.0 + max_worse_pct)


def daily_loss_exceeded(realized_today_usdc: float, stake_usdc: float, mult: float) -> bool:
    """Stop trading for the day once realized losses reach mult × stake."""
    if mult <= 0 or stake_usdc <= 0:
        return False
    return realized_today_usdc <= -(mult * stake_usdc)
