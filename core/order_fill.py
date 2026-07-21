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
    if str(response.get("status") or "").lower() != "matched":
        return None

    making_raw = _decimal(response.get("makingAmount") or response.get("making_amount"))
    taking_raw = _decimal(response.get("takingAmount") or response.get("taking_amount"))
    if making_raw <= 0 or taking_raw <= 0:
        return None

    # py-clob-client-v2 1.0.1 production responses use human-unit decimal
    # strings (for example "9.999999"). The current OpenAPI examples show
    # fixed-6 integer strings. Detect the representation from the BUY cost,
    # which cannot legitimately exceed the requested amount by orders of magnitude.
    human_limit = Decimal(str(max(intended_usdc * 2.0, 1.0)))
    scale = Decimal(1) if making_raw <= human_limit else FIXED_MATH_SCALE
    filled_usdc = float(making_raw / scale)
    shares = float(taking_raw / scale)
    if filled_usdc <= 0 or shares <= 0:
        return None

    fee_usdc = _optional_fee(response, scale)
    return BuyFill(
        filled_usdc=filled_usdc,
        shares=shares,
        fill_price=filled_usdc / shares,
        status=fill_status(filled_usdc, intended_usdc),
        fee_usdc=fee_usdc,
    )


def _decimal(value: Any) -> Decimal:
    """Parse an order-response amount without assuming its wire representation."""
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _optional_fee(response: dict[str, Any], scale: Decimal) -> float | None:
    """Decode a future/extended response fee field when the API supplies one."""
    for key in ("feeAmount", "fee_amount", "feeUSDC", "fee_usdc"):
        if response.get(key) not in (None, ""):
            fee = float(_decimal(response[key]) / scale)
            return fee if fee >= 0 else None
    return None
