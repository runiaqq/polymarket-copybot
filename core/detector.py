"""
Whale-detection core: order-book state, rolling trade stats, and the
dynamic (liquidity/volume-relative) significance filter with break-even guards.

Pure-ish and unit-testable — no network, no Celery. The WebSocket listener feeds
it book snapshots, price-change deltas and trade prints; it returns a signal dict
(ready for copy execution) or None with a reason.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.config import settings


def _hours_fresh(market: dict) -> float | None:
    """Compute hours to resolution fresh from the stored ISO end date.

    Falls back to the cached `hours_to_resolve` value if the date can't be parsed.
    This avoids showing stale values from when the market cache was last built.
    """
    end_iso = market.get("end_date_iso")
    if end_iso:
        try:
            from dateutil.parser import parse as _dp
            dt = _dp(end_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            diff = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if diff > 0:
                return round(diff, 1)
        except Exception:
            pass
    return market.get("hours_to_resolve")


class OrderBook:
    """In-memory order book maintained from WS `book` snapshots + `price_change` deltas."""

    __slots__ = ("bids", "asks", "best_bid_hint", "best_ask_hint")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}   # price -> size
        self.asks: dict[float, float] = {}
        self.best_bid_hint: float | None = None
        self.best_ask_hint: float | None = None

    def apply_snapshot(self, normalized: dict) -> None:
        self.bids = {lvl["price"]: lvl["size"] for lvl in normalized.get("bids", [])}
        self.asks = {lvl["price"]: lvl["size"] for lvl in normalized.get("asks", [])}
        self.best_bid_hint = normalized.get("best_bid")
        self.best_ask_hint = normalized.get("best_ask")

    def apply_change(self, price: float, size: float, side: str) -> None:
        """A price_change entry: absolute resting `size` now at `price` on `side`."""
        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def set_best(self, best_bid: float | None, best_ask: float | None) -> None:
        if best_bid is not None:
            self.best_bid_hint = best_bid
        if best_ask is not None:
            self.best_ask_hint = best_ask

    def best_ask(self) -> float | None:
        live = min((p for p, s in self.asks.items() if s > 0), default=None)
        return live if live is not None else self.best_ask_hint

    def best_bid(self) -> float | None:
        live = max((p for p, s in self.bids.items() if s > 0), default=None)
        return live if live is not None else self.best_bid_hint

    def ask_fillable_usdc(self, max_price: float) -> float:
        """USDC required to clear all ask levels priced <= max_price (i.e. fillable depth)."""
        return sum(p * s for p, s in self.asks.items() if 0 < p <= max_price and s > 0)


@dataclass
class RecentTrades:
    """Rolling window of recent trade USDC sizes for one market."""

    window_sec: int
    _samples: deque = field(default_factory=deque)

    def add(self, usdc: float, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        self._samples.append((ts, usdc))
        self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def count(self) -> int:
        self._evict(time.time())
        return len(self._samples)

    def p90(self) -> float:
        self._evict(time.time())
        vals = sorted(v for _, v in self._samples)
        if not vals:
            return 0.0
        idx = max(0, int(round(0.9 * (len(vals) - 1))))
        return vals[idx]


def evaluate_trade(
    book: OrderBook,
    recent: RecentTrades,
    trade: dict,
    market: dict,
    fee_bps: float,
) -> dict | None:
    """
    Decide whether a trade print is a copy-worthy whale BUY.

    `trade`: {price, size(shares), side, usdc, tx, timestamp}
    `market`: meta from get_watch_markets() (token_id, tick_size, neg_risk, min_size, ...)
    Returns a signal dict (with sizing hints) or None if it fails any rule/guard.
    """
    if (trade.get("side") or "").upper() != "BUY":
        return None

    usdc = float(trade.get("usdc") or 0)
    if usdc < settings.dyn_abs_floor_usdc:
        return None

    best_ask = book.best_ask() or float(trade.get("price") or 0)
    best_bid = book.best_bid()
    if best_ask <= 0:
        return None

    # ── Break-even guards ──────────────────────────────────────────────────────
    if best_ask > settings.max_entry_price or best_ask < settings.min_entry_price:
        return None
    if fee_bps > settings.fee_bps_max:
        return None
    if best_bid and best_ask:
        mid = (best_bid + best_ask) / 2
        if mid > 0 and (best_ask - best_bid) / mid > settings.max_spread_pct:
            return None

    # ── Fillable depth within the slippage band ─────────────────────────────────
    band = best_ask * (1.0 + settings.order_slippage_pct)
    fillable_usdc = book.ask_fillable_usdc(band)
    if fillable_usdc < settings.dyn_min_book_depth_usdc:
        return None

    # ── Dynamic "large for this book" filter (hybrid) ───────────────────────────
    if usdc < settings.dyn_rel_depth * fillable_usdc:
        return None
    if recent.count() >= settings.recent_trade_min_samples:
        if usdc < settings.dyn_rel_vol * recent.p90():
            return None

    # ── Copy sizing hint (capped by book depth) ─────────────────────────────────
    max_copy_usdc = max(0.0, fillable_usdc * settings.book_safe_frac)

    # Recompute hours_to_resolve fresh from the stored ISO end date so the signal
    # always reflects the current distance to resolution, not a cached value that
    # may have been computed minutes (or hours) ago.
    hours_to_resolve = _hours_fresh(market)

    return {
        "market_id":        market.get("condition_id", ""),
        "token_id":         market.get("token_id") or trade.get("token_id"),
        "title":            market.get("title", ""),
        "outcome":          market.get("outcome", ""),   # e.g. "Yes", "No", "Over", "Under"
        "side":             "BUY",
        "price":            best_ask,
        "size_usdc":        usdc,                 # whale size (informational)
        "max_copy_usdc":    max_copy_usdc,        # cap from book depth
        "fillable_usdc":    fillable_usdc,
        "tick_size":        market.get("tick_size", "0.01"),
        "neg_risk":         bool(market.get("neg_risk", False)),
        "min_size":         market.get("min_size", 5),
        "hours_to_resolve": hours_to_resolve,
        "event_slug":       market.get("event_slug"),
        "fee_bps":          fee_bps,
        "best_bid":         best_bid,
        "best_ask":         best_ask,
        "source_tx_hash":   trade.get("tx", ""),
        "whale_wallet":     trade.get("whale_wallet", ""),
    }
