from scripts.repair_sniper_ledger import _match_trades


def _trade(
    order_id: str,
    match_time: int,
    *,
    trader_side: str = "TAKER",
    status: str = "CONFIRMED",
) -> dict:
    return {
        "side": "BUY",
        "trader_side": trader_side,
        "status": status,
        "taker_order_id": order_id,
        "match_time": str(match_time),
    }


def test_match_trades_accepts_one_confirmed_taker_order_near_row() -> None:
    matched, reason = _match_trades(
        {"created_at": 1_000},
        [_trade("order-1", 1_030), _trade("order-1", 1_031)],
    )

    assert reason is None
    assert len(matched) == 2


def test_match_trades_rejects_sole_order_outside_time_window() -> None:
    matched, reason = _match_trades(
        {"created_at": 1_000},
        [_trade("order-1", 2_000)],
    )

    assert matched == []
    assert reason == "no CLOB order within 120s of ledger row"


def test_match_trades_ignores_maker_and_unconfirmed_records() -> None:
    matched, reason = _match_trades(
        {"created_at": 1_000},
        [
            _trade("maker", 1_010, trader_side="MAKER"),
            _trade("pending", 1_010, status="MATCHED"),
        ],
    )

    assert matched == []
    assert reason == "no confirmed taker BUY history for token"


def test_match_trades_rejects_two_nearby_taker_orders() -> None:
    matched, reason = _match_trades(
        {"created_at": 1_000},
        [_trade("order-1", 1_010), _trade("order-2", 1_020)],
    )

    assert matched == []
    assert reason == "ambiguous CLOB history (2 nearby taker orders)"
