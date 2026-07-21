"""CLOB order-response fill accounting."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

FIXED_MATH_SCALE = Decimal("1000000")
NONE_FILL_THRESHOLD = Decimal("0.05")
FULL_FILL_THRESHOLD = Decimal("0.90")


@dataclass(frozen=True)
class BuyFill:
    filled_usdc: float
    shares: float
    fill_price: float
    status: str
    fee_usdc: float | None = None


def fill_status(filled_usdc: float, intended_usdc: float) -> str:
    """Classify a fill using the existing 5% and 90% thresholds."""
    if intended_usdc <= 0 or filled_usdc < float(NONE_FILL_THRESHOLD) * intended_usdc:
        return "none"
    if filled_usdc < float(FULL_FILL_THRESHOLD) * intended_usdc:
        return "partial"
    return "full"


def extract_buy_fill(response: dict[str, Any], intended_usdc: float) -> BuyFill | None:
    """Read exact BUY cost and shares from a CLOB V2 SendOrderResponse."""
    filled_usdc = _fixed_six(response.get("makingAmount") or response.get("making_amount"))
    shares = _fixed_six(response.get("takingAmount") or response.get("taking_amount"))
    if filled_usdc <= 0 or shares <= 0:
        return None

    fee_usdc = _optional_fee(response)
    return BuyFill(
        filled_usdc=filled_usdc,
        shares=shares,
        fill_price=filled_usdc / shares,
        status=fill_status(filled_usdc, intended_usdc),
        fee_usdc=fee_usdc,
    )


def _fixed_six(value: Any) -> float:
    """Decode CLOB fixed-math integer strings with six decimal places."""
    if value in (None, ""):
        return 0.0
    try:
        return float(Decimal(str(value)) / FIXED_MATH_SCALE)
    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def _optional_fee(response: dict[str, Any]) -> float | None:
    """Decode a future/extended response fee field when the API supplies one."""
    for key in ("feeAmount", "fee_amount", "feeUSDC", "fee_usdc"):
        if response.get(key) not in (None, ""):
            fee = _fixed_six(response[key])
            return fee if fee >= 0 else None
    return None
