import pytest

from core.order_fill import extract_buy_fill


def test_extract_buy_fill_decodes_fixed_six_buy_amounts() -> None:
    fill = extract_buy_fill(
        {
            "status": "matched",
            "makingAmount": "6210000",
            "takingAmount": "7961538",
        },
        intended_usdc=6.21,
    )

    assert fill is not None
    assert fill.filled_usdc == pytest.approx(6.21)
    assert fill.shares == pytest.approx(7.961538)
    assert fill.fill_price == pytest.approx(0.78, rel=1e-5)
    assert fill.status == "full"


def test_extract_buy_fill_decodes_human_unit_decimal_strings() -> None:
    fill = extract_buy_fill(
        {
            "status": "matched",
            "makingAmount": "9.999999",
            "takingAmount": "14.925372",
        },
        intended_usdc=10.0,
    )

    assert fill is not None
    assert fill.filled_usdc == pytest.approx(9.999999)
    assert fill.shares == pytest.approx(14.925372)
    assert fill.fill_price == pytest.approx(0.67, rel=1e-4)


def test_extract_buy_fill_treats_small_integer_as_human_units() -> None:
    fill = extract_buy_fill(
        {
            "status": "matched",
            "makingAmount": "10",
            "takingAmount": "12.5",
        },
        intended_usdc=10.0,
    )

    assert fill is not None
    assert fill.filled_usdc == pytest.approx(10.0)
    assert fill.shares == pytest.approx(12.5)


@pytest.mark.parametrize(
    ("making_amount", "expected"),
    [
        ("400000", "none"),
        ("500000", "partial"),
        ("8999999", "partial"),
        ("9000000", "full"),
    ],
)
def test_extract_buy_fill_uses_existing_fill_thresholds(
    making_amount: str,
    expected: str,
) -> None:
    fill = extract_buy_fill(
        {
            "status": "matched",
            "makingAmount": making_amount,
            "takingAmount": "10000000",
        },
        intended_usdc=10.0,
    )

    assert fill is not None
    assert fill.status == expected


def test_extract_buy_fill_returns_none_without_both_amounts() -> None:
    assert extract_buy_fill(
        {"status": "matched", "makingAmount": "5000000"},
        5.0,
    ) is None


def test_extract_buy_fill_returns_none_for_non_matched_status() -> None:
    assert extract_buy_fill(
        {
            "status": "unmatched",
            "makingAmount": "10",
            "takingAmount": "12.5",
        },
        10.0,
    ) is None
