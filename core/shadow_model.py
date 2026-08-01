"""Pure math and execution simulation for the BP30 shadow engine."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CRYPTO_FEE_RATE = 0.07
EntryVariant = tuple[str, float, float]


@dataclass(frozen=True)
class FillSimulation:
    requested_usdc: float
    filled_usdc: float
    shares: float
    effective_price: float
    best_ask: float | None
    fee_usdc: float
    complete: bool

    @property
    def fee_per_share(self) -> float:
        return self.fee_usdc / self.shares if self.shares > 0 else 0.0


class EwmaVolatility:
    """EWMA volatility of fixed-interval log returns, in units per sqrt(second)."""

    def __init__(self, *, alpha: float, sample_interval_sec: float) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        if sample_interval_sec <= 0:
            raise ValueError("sample_interval_sec must be positive")
        self.alpha = alpha
        self.sample_interval_sec = sample_interval_sec
        self._sample_price: float | None = None
        self._sample_timestamp: float | None = None
        self._variance_per_second: float | None = None
        self.samples = 0

    def update(self, price: float, timestamp_sec: float) -> float | None:
        if price <= 0 or not math.isfinite(price):
            return self.sigma
        if self._sample_timestamp is None:
            self._sample_price = price
            self._sample_timestamp = timestamp_sec
            return self.sigma
        elapsed = timestamp_sec - self._sample_timestamp
        if elapsed < self.sample_interval_sec:
            return self.sigma
        if elapsed <= 0 or self._sample_price is None:
            return self.sigma

        log_return = math.log(price / self._sample_price)
        variance_per_second = (log_return * log_return) / elapsed
        if self._variance_per_second is None:
            self._variance_per_second = variance_per_second
        else:
            self._variance_per_second = (
                self.alpha * variance_per_second + (1.0 - self.alpha) * self._variance_per_second
            )
        self._sample_price = price
        self._sample_timestamp = timestamp_sec
        self.samples += 1
        return self.sigma

    @property
    def sigma(self) -> float | None:
        if self._variance_per_second is None:
            return None
        return math.sqrt(max(self._variance_per_second, 0.0))


def stressed_sigma(slow: float | None, fast: float | None) -> float | None:
    """Return the highest available volatility estimate."""
    available = [sigma for sigma in (slow, fast) if sigma is not None]
    return max(available) if available else None


def calibrated_probability(model_p: float, market_price: float, lam: float) -> float:
    """Shrink model probability toward the execution price."""
    return max(0.0, min(1.0, market_price + lam * (model_p - market_price)))


def divergence_exceeds_ceiling(model_p: float, market_price: float, ceiling: float) -> bool:
    """Return whether model probability is too far above execution price."""
    return model_p - market_price > ceiling


def strike_distance_bp(spot: float, open_price: float) -> float | None:
    """Absolute log distance between spot and the window strike, in basis points."""
    if (
        spot <= 0
        or open_price <= 0
        or not math.isfinite(spot)
        or not math.isfinite(open_price)
    ):
        return None
    return abs(math.log(spot / open_price)) * 10_000


def passes_signal_filter(
    edge: float,
    spot: float,
    open_price: float,
    *,
    min_edge: float,
    min_strike_bp: float,
) -> bool:
    """BP30.4 hard signal filter.

    Collected data showed the only positive-expectancy slices are edge >= 0.07
    combined with spot at least ~3 bp away from the strike; entries hugging the
    strike are toxic (WR ~60% at price ~0.63). The engine keeps *collecting*
    unfiltered trades for research; this filter gates only what counts as a
    tradeable signal (Telegram notifications, filtered report section).
    """
    if not math.isfinite(edge) or edge < min_edge:
        return False
    distance = strike_distance_bp(spot, open_price)
    return distance is not None and distance >= min_strike_bp


def maker_bid_price(
    best_bid: float | None,
    best_ask: float | None,
    tick: float,
) -> float | None:
    """Improve the best bid by one tick without crossing the ask."""
    if best_bid is None or best_ask is None:
        return None
    values = (best_bid, best_ask, tick)
    if any(not math.isfinite(value) for value in values):
        return None
    if best_bid <= 0 or best_ask <= best_bid or best_ask > 1 or tick <= 0:
        return None
    bid = min(best_bid + tick, best_ask - tick)
    return bid if bid > 0 else None


def maker_fill(best_ask: float | None, bid: float) -> bool:
    """Conservatively fill only after the best ask reaches the maker bid."""
    return (
        best_ask is not None
        and math.isfinite(best_ask)
        and math.isfinite(bid)
        and best_ask > 0
        and bid > 0
        and best_ask <= bid
    )


def maker_should_cancel(p_side: float, bid: float, cancel_edge: float) -> bool:
    """Cancel only when edge falls strictly below the configured threshold."""
    return p_side - bid < cancel_edge


def build_entry_variants(
    entry_min_sec: float,
    entry_max_sec: float,
    variant_edges_sec: Sequence[float],
) -> list[EntryVariant]:
    """Build the canonical entry range and adjacent research buckets."""
    lower = float(entry_min_sec)
    upper = float(entry_max_sec)
    edges = [float(edge) for edge in variant_edges_sec]
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise ValueError("entry range must contain finite ascending bounds")
    if len(edges) < 2 or any(not math.isfinite(edge) for edge in edges):
        raise ValueError("variant edges must contain at least two finite values")
    if any(low >= high for low, high in zip(edges, edges[1:])):
        raise ValueError("variant edges must be strictly ascending")
    buckets = [(f"t{low:g}-{high:g}", low, high) for low, high in zip(edges, edges[1:])]
    return [("full", lower, upper), *buckets]


def active_entry_variants(
    variants: Iterable[EntryVariant],
    time_left_sec: float,
) -> list[EntryVariant]:
    """Return variants whose inclusive entry range contains ``time_left_sec``."""
    return [variant for variant in variants if variant[1] <= time_left_sec <= variant[2]]


def probability_up(
    spot: float,
    open_price: float,
    sigma_per_sqrt_second: float,
    time_left_sec: float,
    *,
    sigma_floor: float,
    z_cap: float,
) -> float:
    """Return Φ(log(S/S0) / (sigma*sqrt(tau)))."""
    if spot <= 0 or open_price <= 0:
        return 0.5
    log_delta = math.log(spot / open_price)
    if time_left_sec <= 0:
        if log_delta > 0:
            return 1.0
        if log_delta < 0:
            return 0.0
        return 0.5
    sigma = max(abs(sigma_per_sqrt_second), sigma_floor)
    denominator = sigma * math.sqrt(time_left_sec)
    if denominator <= 0:
        return 0.5
    z_score = max(-z_cap, min(z_cap, log_delta / denominator))
    return 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))


def fee_usdc(
    price: float,
    shares: float,
    *,
    fee_rate: float = CRYPTO_FEE_RATE,
    exponent: float = 1.0,
) -> float:
    """Polymarket taker fee: shares * rate * (price * (1-price))**exponent."""
    if not 0 < price < 1 or shares <= 0 or fee_rate <= 0:
        return 0.0
    raw = shares * fee_rate * (price * (1.0 - price)) ** exponent
    rounded = Decimal(str(raw)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
    return float(rounded)


def ask_depth_usdc(asks: Iterable[Mapping[str, object]], up_to_price: float) -> float:
    """BP36: dollars purchasable from ask levels priced <= up_to_price.

    Capacity telemetry for multi-account scaling: how much stake the book can
    absorb inside a given price band at signal time."""
    if up_to_price <= 0:
        return 0.0
    total = 0.0
    for level in asks:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0 and price <= up_to_price:
            total += price * size
    return round(total, 4)


def walk_order_book(
    asks: Iterable[Mapping[str, object]],
    stake_usdc: float,
    *,
    fee_rate: float,
    fee_exponent: float = 1.0,
    completion_epsilon_usdc: float = 0.00001,
) -> FillSimulation:
    """Spend up to ``stake_usdc`` across asks, cheapest first."""
    requested = max(float(stake_usdc), 0.0)
    remaining = requested
    filled = 0.0
    shares = 0.0
    fee = 0.0
    valid_levels: list[tuple[float, float]] = []
    for level in asks:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            valid_levels.append((price, size))
    valid_levels.sort(key=lambda item: item[0])

    for price, available_shares in valid_levels:
        if remaining <= completion_epsilon_usdc:
            break
        level_shares = min(available_shares, remaining / price)
        level_cost = level_shares * price
        shares += level_shares
        filled += level_cost
        fee += fee_usdc(
            price,
            level_shares,
            fee_rate=fee_rate,
            exponent=fee_exponent,
        )
        remaining -= level_cost

    complete = requested > 0 and remaining <= completion_epsilon_usdc
    effective_price = filled / shares if shares > 0 else 0.0
    return FillSimulation(
        requested_usdc=requested,
        filled_usdc=filled,
        shares=shares,
        effective_price=effective_price,
        best_ask=valid_levels[0][0] if valid_levels else None,
        fee_usdc=fee,
        complete=complete,
    )
