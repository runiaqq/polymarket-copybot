import pytest

from core.shadow_model import (
    EwmaVolatility,
    fee_usdc,
    probability_up,
    walk_order_book,
)


MODEL_ARGS = {"sigma_floor": 1e-6, "z_cap": 8.0}


def test_probability_is_symmetric_at_open_price() -> None:
    assert probability_up(100.0, 100.0, 0.001, 60.0, **MODEL_ARGS) == pytest.approx(0.5)


def test_probability_is_monotonic_in_spot_delta() -> None:
    down = probability_up(99.0, 100.0, 0.001, 60.0, **MODEL_ARGS)
    flat = probability_up(100.0, 100.0, 0.001, 60.0, **MODEL_ARGS)
    up = probability_up(101.0, 100.0, 0.001, 60.0, **MODEL_ARGS)
    assert down < flat < up


def test_probability_strengthens_as_time_runs_out() -> None:
    early = probability_up(100.5, 100.0, 0.001, 120.0, **MODEL_ARGS)
    late = probability_up(100.5, 100.0, 0.001, 20.0, **MODEL_ARGS)
    assert late > early > 0.5


@pytest.mark.parametrize(
    ("spot", "expected"),
    [(101.0, 1.0), (99.0, 0.0), (100.0, 0.5)],
)
def test_probability_handles_zero_sigma(spot: float, expected: float) -> None:
    result = probability_up(spot, 100.0, 0.0, 60.0, **MODEL_ARGS)
    assert result == pytest.approx(expected, abs=0.001)


def test_probability_handles_invalid_prices() -> None:
    assert probability_up(0.0, 100.0, 0.001, 60.0, **MODEL_ARGS) == 0.5


def test_ewma_volatility_uses_fixed_interval_log_returns() -> None:
    volatility = EwmaVolatility(alpha=1.0, sample_interval_sec=1.0)
    assert volatility.update(100.0, 0.0) is None
    sigma = volatility.update(101.0, 1.0)
    assert sigma == pytest.approx(0.00995033085)
    assert volatility.samples == 1


def test_fee_curve_matches_crypto_v2_formula() -> None:
    # $15 cost at p=.80 is 18.75 shares; fee is 1.4% of cost.
    shares = 15.0 / 0.8
    assert fee_usdc(0.8, shares) == pytest.approx(0.21)


def test_fee_curve_is_symmetric_per_share() -> None:
    low = fee_usdc(0.2, 10.0, fee_rate=0.07)
    high = fee_usdc(0.8, 10.0, fee_rate=0.07)
    assert low == high


def test_walk_order_book_fills_cheapest_asks_first() -> None:
    fill = walk_order_book(
        [
            {"price": "0.80", "size": "10"},
            {"price": "0.70", "size": "10"},
        ],
        10.0,
        fee_rate=0.07,
    )
    assert fill.complete is True
    assert fill.filled_usdc == pytest.approx(10.0)
    assert fill.shares == pytest.approx(13.75)
    assert fill.effective_price == pytest.approx(10.0 / 13.75)
    assert fill.best_ask == 0.70
    assert fill.fee_usdc == pytest.approx(0.147 + 0.042)


def test_walk_order_book_records_partial_depth() -> None:
    fill = walk_order_book(
        [{"price": 0.75, "size": 4.0}],
        15.0,
        fee_rate=0.07,
    )
    assert fill.complete is False
    assert fill.filled_usdc == pytest.approx(3.0)
    assert fill.shares == pytest.approx(4.0)
    assert fill.effective_price == pytest.approx(0.75)
