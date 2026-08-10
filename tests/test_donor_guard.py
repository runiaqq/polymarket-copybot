"""BP42 — per-donor loss-streak circuit breaker (pure logic)."""

import pytest

from core.donor_guard import donor_is_paused, parse_ts, pause_decision

NOW = 1_700_000_000.0


def rows(*pnls: float, start_ts: float = NOW, cond_prefix: str = "c") -> list:
    """Newest-first resolved rows, one unique condition per pnl."""
    return [
        (f"{cond_prefix}{i}", start_ts - i * 60, pnl)
        for i, pnl in enumerate(pnls)
    ]


class TestPauseDecision:
    def test_five_straight_losses_pauses(self):
        assert pause_decision(rows(-1, -2, -3, -4, -5), 5, None, NOW) is True

    def test_win_inside_streak_blocks(self):
        assert pause_decision(rows(-1, -2, 3, -4, -5), 5, None, NOW) is False

    def test_fewer_than_streak_len_blocks(self):
        assert pause_decision(rows(-1, -2, -3, -4), 5, None, NOW) is False

    def test_older_win_beyond_window_ignored(self):
        # 5 newest are losses; a 6th (older) win must not rescue the donor.
        assert pause_decision(rows(-1, -2, -3, -4, -5, 6), 5, None, NOW) is True

    def test_duplicate_condition_counts_once(self):
        # 3 users copied the same losing market -> ONE loss, streak stays at 3
        # unique markets and must NOT trigger a 5-streak.
        data = [
            ("m1", NOW - 10, -5), ("m1", NOW - 11, -5), ("m1", NOW - 12, -5),
            ("m2", NOW - 20, -5), ("m2", NOW - 21, -5),
            ("m3", NOW - 30, -5),
        ]
        assert pause_decision(data, 5, None, NOW) is False

    def test_currently_paused_blocks(self):
        assert pause_decision(rows(-1, -2, -3, -4, -5), 5, NOW + 3600, NOW) is False

    def test_expired_pause_same_old_streak_blocks(self):
        # Pause ended, but no NEW loss resolved after it — same streak,
        # already punished.
        old = rows(-1, -2, -3, -4, -5, start_ts=NOW - 90_000)
        assert pause_decision(old, 5, NOW - 3600, NOW) is False

    def test_expired_pause_with_fresh_loss_repauses(self):
        # Newest loss resolved AFTER the previous pause ended -> re-arm.
        data = rows(-1, -2, -3, -4, -5)
        assert pause_decision(data, 5, NOW - 3600, NOW) is True

    def test_zero_pnl_counts_as_non_loss(self):
        assert pause_decision(rows(0, -2, -3, -4, -5), 5, None, NOW) is False

    def test_empty_history(self):
        assert pause_decision([], 5, None, NOW) is False


class TestDonorIsPaused:
    def test_none(self):
        assert donor_is_paused(None, NOW) is False

    def test_future_iso(self):
        assert donor_is_paused("2099-01-01T00:00:00+00:00", NOW) is True

    def test_past_iso(self):
        assert donor_is_paused("2020-01-01T00:00:00+00:00", NOW) is False

    def test_garbage(self):
        assert donor_is_paused("not-a-date", NOW) is False


class TestParseTs:
    @pytest.mark.parametrize("value", [
        "2026-07-21T18:53:01.76106+00:00",   # 5-digit microseconds (Supabase)
        "2026-07-21T18:53:01.761060+00:00",
        "2026-07-21T18:53:01+00:00",
        "2026-07-21T18:53:01Z",
        "2026-07-21T18:53:01",               # naive -> assume UTC
    ])
    def test_parses(self, value):
        ts = parse_ts(value)
        assert ts is not None and ts > 1_700_000_000

    def test_invalid(self):
        assert parse_ts("") is None
        assert parse_ts(None) is None
        assert parse_ts("garbage") is None
