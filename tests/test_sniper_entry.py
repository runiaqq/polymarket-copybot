from types import SimpleNamespace

import pytest

from core.sniper_entry import calculate_sniper_stake, entry_decision


@pytest.fixture
def cfg() -> SimpleNamespace:
    return SimpleNamespace(
        max_below_pct=0.04,
        slippage_pct=0.02,
        max_entry_price=0.97,
        stake_frac=0.10,
        min_order_usdc=5.0,
        stake_cap_usdc=50.0,
        fee_headroom_pct=0.03,
    )


@pytest.mark.parametrize("ask", [0.768, 0.784, 0.816])
def test_entry_decision_enters_on_inclusive_band(ask: float, cfg: SimpleNamespace) -> None:
    assert entry_decision(
        0.80,
        ask,
        max_below_pct=cfg.max_below_pct,
        slippage_pct=cfg.slippage_pct,
        max_entry_price=cfg.max_entry_price,
    ) == "enter"


@pytest.mark.parametrize("ask", [0.7679, 0.8161])
def test_entry_decision_waits_outside_donor_band(ask: float, cfg: SimpleNamespace) -> None:
    assert entry_decision(
        0.80,
        ask,
        max_below_pct=cfg.max_below_pct,
        slippage_pct=cfg.slippage_pct,
        max_entry_price=cfg.max_entry_price,
    ) == "wait"


def test_entry_decision_waits_above_absolute_ceiling(cfg: SimpleNamespace) -> None:
    assert entry_decision(
        0.96,
        0.975,
        max_below_pct=cfg.max_below_pct,
        slippage_pct=cfg.slippage_pct,
        max_entry_price=cfg.max_entry_price,
    ) == "wait"


def test_sniper_stake_uses_bankroll_fraction(cfg: SimpleNamespace) -> None:
    stake = calculate_sniper_stake(
        200.0,
        100.0,
        stake_frac=cfg.stake_frac,
        min_order_usdc=cfg.min_order_usdc,
        stake_cap_usdc=cfg.stake_cap_usdc,
        fee_headroom_pct=cfg.fee_headroom_pct,
    )
    assert stake == pytest.approx(20.0)


def test_sniper_stake_applies_exchange_floor(cfg: SimpleNamespace) -> None:
    stake = calculate_sniper_stake(
        30.0,
        100.0,
        stake_frac=cfg.stake_frac,
        min_order_usdc=cfg.min_order_usdc,
        stake_cap_usdc=cfg.stake_cap_usdc,
        fee_headroom_pct=cfg.fee_headroom_pct,
    )
    assert stake == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("free_pusd", "depth", "expected"),
    [
        (1000.0, 1000.0, 50.0),
        (200.0, 12.5, 12.5),
        (5.0, 100.0, 4.85),
    ],
)
def test_sniper_stake_applies_each_ceiling(
    free_pusd: float,
    depth: float,
    expected: float,
    cfg: SimpleNamespace,
) -> None:
    stake = calculate_sniper_stake(
        free_pusd,
        depth,
        stake_frac=cfg.stake_frac,
        min_order_usdc=cfg.min_order_usdc,
        stake_cap_usdc=cfg.stake_cap_usdc,
        fee_headroom_pct=cfg.fee_headroom_pct,
    )
    assert stake == pytest.approx(expected)
