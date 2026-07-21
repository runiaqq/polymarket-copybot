"""Pure entry-band and stake calculations for sniper trades."""

from typing import Literal

EntryDecision = Literal["enter", "wait"]


def entry_bounds(
    donor_price: float,
    max_below_pct: float,
    slippage_pct: float,
) -> tuple[float, float]:
    """Return the inclusive sniper entry band around the donor fill price."""
    return (
        donor_price * (1.0 - max_below_pct),
        donor_price * (1.0 + slippage_pct),
    )


def entry_decision(
    donor_price: float,
    best_ask: float,
    *,
    max_below_pct: float,
    slippage_pct: float,
    max_entry_price: float,
) -> EntryDecision:
    """Enter only while the current ask is inside every configured price bound."""
    if donor_price <= 0 or best_ask <= 0:
        return "wait"

    lower, upper = entry_bounds(donor_price, max_below_pct, slippage_pct)
    if best_ask < lower or best_ask > upper:
        return "wait"
    if best_ask > max_entry_price:
        return "wait"
    return "enter"


def calculate_sniper_stake(
    free_pusd: float,
    ask_depth_usdc: float,
    *,
    stake_frac: float,
    min_order_usdc: float,
    stake_cap_usdc: float,
    fee_headroom_pct: float,
) -> float:
    """Apply the sniper stake floor followed by all hard ceilings."""
    if free_pusd <= 0 or ask_depth_usdc <= 0:
        return 0.0

    balance_cap = free_pusd * max(0.0, 1.0 - fee_headroom_pct)
    requested = max(free_pusd * stake_frac, min_order_usdc)
    return max(0.0, min(requested, stake_cap_usdc, ask_depth_usdc, balance_cap))
