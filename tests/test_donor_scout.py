"""BP48 donor scout: pure decision logic tests."""

import pytest

from core.donor_scout import (
    candidate_qualifies,
    laplace_score,
    probation_pnl,
    tally_outcomes,
)

THRESHOLDS = dict(
    min_directionality=0.5,
    max_trades_per_day=20.0,
    min_avg_trade_size=300.0,
    max_event_outcomes=3,
)


def _profile(**overrides) -> dict:
    base = {
        "is_mm": False,
        "directionality": 0.9,
        "trades": 40,
        "trades_per_day": 5.0,
        "avg_size": 800.0,
        "max_event_outcomes": 1,
        "last_days": 1.0,
    }
    base.update(overrides)
    return base


class TestLaplaceScore:
    def test_smoothing_ranks_track_record_over_luck(self):
        # 2/2 lucky streak must not outrank a proven 9/10.
        assert laplace_score(9, 10) > laplace_score(2, 2)

    def test_no_resolutions_gives_neutral_prior(self):
        assert laplace_score(0, 0) == 0.5

    def test_invalid_inputs_zero(self):
        assert laplace_score(5, 3) == 0.0
        assert laplace_score(-1, 3) == 0.0

    def test_all_losses_below_prior(self):
        assert laplace_score(0, 10) < 0.5


class TestCandidateQualifies:
    def test_clean_directional_trader_passes(self):
        ok, reason = candidate_qualifies(_profile(), **THRESHOLDS)
        assert ok and reason == ""

    @pytest.mark.parametrize("overrides, expected_reason", [
        ({"is_mm": True}, "mm_rewards"),
        ({"directionality": 0.2}, "directionality"),
        ({"trades_per_day": 50.0}, "density"),
        ({"avg_size": 50.0}, "size"),
        ({"max_event_outcomes": 4}, "scattershot"),
    ])
    def test_hard_filters(self, overrides, expected_reason):
        ok, reason = candidate_qualifies(_profile(**overrides), **THRESHOLDS)
        assert not ok and reason == expected_reason

    def test_density_and_size_only_with_enough_trades(self):
        # A wallet with 3 observed trades can't be judged on cadence/size.
        ok, _ = candidate_qualifies(
            _profile(trades=3, trades_per_day=99.0, avg_size=10.0), **THRESHOLDS)
        assert ok

    def test_missing_directionality_passes(self):
        # None = no BUY data in the feed window; other filters decide.
        ok, _ = candidate_qualifies(
            _profile(directionality=None), **THRESHOLDS)
        assert ok


class TestTallyOutcomes:
    WINNERS = {"0xaaa": "yes", "0xbbb": "no"}

    def test_counts_wins_and_losses(self):
        entries = [
            {"condition_id": "0xaaa", "outcome": "Yes"},   # win (case-insensitive)
            {"condition_id": "0xbbb", "outcome": "Yes"},   # loss
            {"condition_id": "0xccc", "outcome": "Yes"},   # unresolved — skipped
            {"condition_id": "0xaaa", "outcome": ""},      # no outcome — skipped
        ]
        assert tally_outcomes(entries, self.WINNERS) == (2, 1)

    def test_empty(self):
        assert tally_outcomes([], self.WINNERS) == (0, 0)


class TestProbationPnl:
    WINNERS = {"0xaaa": "yes", "0xbbb": "no"}

    def test_would_be_pnl_at_fixed_stake(self):
        signals = [
            {"market_id": "0xaaa", "outcome": "Yes", "price": 0.5},   # +15
            {"market_id": "0xbbb", "outcome": "Yes", "price": 0.8},   # -15
            {"market_id": "0xccc", "outcome": "Yes", "price": 0.6},   # unresolved
        ]
        st = probation_pnl(signals, self.WINNERS, stake=15.0)
        assert st == {"signals": 3, "resolved": 2, "wins": 1, "pnl": 0.0}

    def test_invalid_price_not_counted(self):
        signals = [{"market_id": "0xaaa", "outcome": "Yes", "price": 0.0}]
        st = probation_pnl(signals, self.WINNERS, stake=15.0)
        assert st["resolved"] == 0 and st["pnl"] == 0.0
