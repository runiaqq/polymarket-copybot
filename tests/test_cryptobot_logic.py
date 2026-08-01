"""BP33: unit tests for the executor's pure decision helpers."""

import pytest

from core.shadow_model import ask_depth_usdc
from cryptobot.logic import (
    daily_loss_exceeded,
    entry_price_ok,
    pilot_stake,
    requote_price_ok,
    should_flag_stuck,
    signal_is_fresh,
)


class TestPilotStake:
    def test_preset_fits_free_balance(self):
        assert pilot_stake(10.0, 100.0, fee_headroom_pct=0.03, exchange_min_usdc=5.0) == 10.0

    def test_capped_by_free_balance_minus_headroom(self):
        # 8 pUSD free -> cap 7.76; preset 10 shrinks to the cap.
        assert pilot_stake(10.0, 8.0, fee_headroom_pct=0.03, exchange_min_usdc=5.0) == 7.76

    def test_below_exchange_minimum_returns_zero(self):
        assert pilot_stake(10.0, 4.0, fee_headroom_pct=0.03, exchange_min_usdc=5.0) == 0.0

    def test_zero_balance_returns_zero(self):
        assert pilot_stake(10.0, 0.0, fee_headroom_pct=0.03, exchange_min_usdc=5.0) == 0.0

    def test_zero_preset_returns_zero(self):
        assert pilot_stake(0.0, 100.0, fee_headroom_pct=0.03, exchange_min_usdc=5.0) == 0.0


class TestSignalIsFresh:
    def test_fresh_signal_passes(self):
        assert signal_is_fresh(100.0, 102.0, max_age_sec=4.0)

    def test_stale_signal_rejected(self):
        assert not signal_is_fresh(100.0, 105.0, max_age_sec=4.0)

    def test_future_timestamp_rejected(self):
        assert not signal_is_fresh(110.0, 100.0, max_age_sec=4.0)

    def test_boundary_age_passes(self):
        assert signal_is_fresh(100.0, 104.0, max_age_sec=4.0)


class TestEntryPriceOk:
    @pytest.mark.parametrize(
        ("ask", "expected"),
        [(0.80, True), (0.95, True), (0.951, False), (None, False), (0.0, False)],
    )
    def test_ceiling(self, ask, expected):
        assert entry_price_ok(ask, 0.95) is expected


class TestRequotePriceOk:
    """BP34: guard for the one-shot FAK re-quote (max_worse_pct=0.03, ceiling=0.95)."""

    @pytest.mark.parametrize(
        ("signal_ask", "fresh_ask", "expected"),
        [
            # Fresh ask equal to or better than the signal ask is always fine.
            (0.80, 0.80, True),
            (0.80, 0.78, True),
            # Exactly 3% worse is still allowed (inclusive ceiling)...
            (0.80, 0.824, True),
            # ...one tick beyond is not.
            (0.80, 0.83, False),
            # Hard entry ceiling wins even when within the 3% band.
            (0.94, 0.95, True),
            (0.94, 0.951, False),
            # Invalid fresh ask: empty book or degenerate price.
            (0.80, None, False),
            (0.80, 0.0, False),
            # Invalid signal ask: worseness can't be evaluated.
            (None, 0.80, False),
            (0.0, 0.80, False),
        ],
    )
    def test_guard(self, signal_ask, fresh_ask, expected):
        assert requote_price_ok(signal_ask, fresh_ask, 0.03, 0.95) is expected


class TestShouldFlagStuck:
    """BP35 watchdog threshold (threshold_sec=900, i.e. 15 minutes)."""

    @pytest.mark.parametrize(
        ("window_end_ts", "now_ts", "expected"),
        [
            # Window just ended — nothing wrong yet.
            (1000.0, 1000.0, False),
            # Under the threshold.
            (1000.0, 1899.0, False),
            # Exactly at the threshold: not yet "older than" 15 minutes.
            (1000.0, 1900.0, False),
            # One second past — stuck.
            (1000.0, 1901.0, True),
            # Way past (the incident case: 25+ minutes).
            (1000.0, 2600.0, True),
            # Window still in the future (clock skew) — never stuck.
            (2000.0, 1000.0, False),
        ],
    )
    def test_threshold(self, window_end_ts, now_ts, expected):
        assert should_flag_stuck(window_end_ts, now_ts, 900.0) is expected


class TestAskDepthUsdc:
    BOOK = [
        {"price": "0.85", "size": "10"},   # $8.50
        {"price": "0.86", "size": "20"},   # $17.20
        {"price": "0.90", "size": "50"},   # $45.00
        {"price": "bad", "size": "1"},     # malformed — ignored
        {"price": "0.87"},                 # missing size — ignored
    ]

    def test_best_level_only(self):
        assert ask_depth_usdc(self.BOOK, 0.85) == 8.5

    def test_band_includes_deeper_levels(self):
        assert ask_depth_usdc(self.BOOK, 0.86) == 25.7

    def test_wide_band(self):
        assert ask_depth_usdc(self.BOOK, 0.95) == 70.7

    def test_zero_ceiling(self):
        assert ask_depth_usdc(self.BOOK, 0.0) == 0.0

    def test_empty_book(self):
        assert ask_depth_usdc([], 0.9) == 0.0


class TestDailyLossExceeded:
    def test_under_limit(self):
        assert not daily_loss_exceeded(-14.9, 5.0, 3.0)

    def test_at_limit(self):
        assert daily_loss_exceeded(-15.0, 5.0, 3.0)

    def test_profit_never_trips(self):
        assert not daily_loss_exceeded(20.0, 5.0, 3.0)

    def test_disabled_when_mult_zero(self):
        assert not daily_loss_exceeded(-100.0, 5.0, 0.0)
