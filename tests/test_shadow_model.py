import pytest

from core.shadow_model import (
    EwmaVolatility,
    active_entry_variants,
    build_entry_variants,
    calibrated_probability,
    divergence_exceeds_ceiling,
    fee_usdc,
    maker_bid_price,
    maker_fill,
    maker_should_cancel,
    probability_up,
    stressed_sigma,
    walk_order_book,
)

MODEL_ARGS = {"sigma_floor": 1e-6, "z_cap": 8.0}


@pytest.mark.parametrize(
    ("slow", "fast", "expected"),
    [
        (None, None, None),
        (0.001, None, 0.001),
        (None, 0.002, 0.002),
        (0.001, 0.002, 0.002),
        (0.003, 0.002, 0.003),
    ],
)
def test_stressed_sigma_uses_highest_available_estimate(
    slow: float | None,
    fast: float | None,
    expected: float | None,
) -> None:
    assert stressed_sigma(slow, fast) == expected


@pytest.mark.parametrize(
    ("model_p", "market_price", "lam", "expected"),
    [
        (0.8, 0.6, 0.0, 0.6),
        (0.8, 0.6, 1.0, 0.8),
        (0.9, 0.7, 2.0, 1.0),
        (0.1, 0.3, 2.0, 0.0),
    ],
)
def test_calibrated_probability_shrinks_and_clamps(
    model_p: float,
    market_price: float,
    lam: float,
    expected: float,
) -> None:
    assert calibrated_probability(model_p, market_price, lam) == pytest.approx(expected)


def test_divergence_above_ceiling_is_blocked() -> None:
    assert divergence_exceeds_ceiling(0.83, 0.70, 0.12) is True
    assert divergence_exceeds_ceiling(0.82, 0.70, 0.12) is False


@pytest.mark.parametrize(
    ("best_bid", "best_ask", "tick", "expected"),
    [
        (None, 0.60, 0.01, None),
        (0.58, None, 0.01, None),
        (0.59, 0.60, 0.01, 0.59),
        (0.58, 0.60, 0.01, 0.59),
        (0.58, 0.59, 0.60, None),
    ],
)
def test_maker_bid_price_improves_without_crossing(
    best_bid: float | None,
    best_ask: float | None,
    tick: float,
    expected: float | None,
) -> None:
    result = maker_bid_price(best_bid, best_ask, tick)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_maker_fill_requires_ask_to_reach_bid() -> None:
    assert maker_fill(None, 0.60) is False
    assert maker_fill(0.61, 0.60) is False
    assert maker_fill(0.60, 0.60) is True
    assert maker_fill(0.59, 0.60) is True


def test_maker_cancel_is_strictly_below_threshold() -> None:
    assert maker_should_cancel(0.65, 0.60, 0.05) is False
    assert maker_should_cancel(0.649, 0.60, 0.05) is True


def test_build_entry_variants_creates_full_and_adjacent_buckets() -> None:
    variants = build_entry_variants(
        20.0,
        120.0,
        [20.0, 30.0, 60.0, 90.0, 120.0],
    )

    assert variants == [
        ("full", 20.0, 120.0),
        ("t20-30", 20.0, 30.0),
        ("t30-60", 30.0, 60.0),
        ("t60-90", 60.0, 90.0),
        ("t90-120", 90.0, 120.0),
    ]


@pytest.mark.parametrize(
    ("time_left", "expected_names"),
    [
        (19.999, []),
        (20.0, ["full", "t20-30"]),
        (30.0, ["full", "t20-30", "t30-60"]),
        (45.0, ["full", "t30-60"]),
        (60.0, ["full", "t30-60", "t60-90"]),
        (90.0, ["full", "t60-90", "t90-120"]),
        (120.0, ["full", "t90-120"]),
        (120.001, []),
    ],
)
def test_active_entry_variants_use_inclusive_boundaries(
    time_left: float,
    expected_names: list[str],
) -> None:
    variants = build_entry_variants(
        20.0,
        120.0,
        [20.0, 30.0, 60.0, 90.0, 120.0],
    )

    active = active_entry_variants(variants, time_left)

    assert [variant[0] for variant in active] == expected_names


def test_build_entry_variants_rejects_unsorted_edges() -> None:
    with pytest.raises(ValueError, match="strictly ascending"):
        build_entry_variants(20.0, 120.0, [20.0, 60.0, 30.0])


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
