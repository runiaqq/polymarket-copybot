"""
Polymarket CLOB v2 client wrapper.
Handles API credential generation and order placement per user.
"""

import structlog
import httpx
from core.wallet import decrypt_key

log = structlog.get_logger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def _make_client(private_key: str, api_creds: dict | None = None):
    from py_clob_client_v2 import ApiCreds, ClobClient

    creds = None
    if api_creds and api_creds.get("clob_api_key"):
        creds = ApiCreds(
            api_key=api_creds["clob_api_key"],
            api_secret=api_creds["clob_secret"],
            api_passphrase=api_creds["clob_passphrase"],
        )

    return ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=private_key,
        creds=creds,
    )


def generate_api_creds(private_key_enc: str) -> dict:
    """
    Generate Polymarket CLOB API credentials via L1 (EIP-712) auth.
    Called once per user wallet.
    """
    private_key = decrypt_key(private_key_enc)
    client = _make_client(private_key)
    raw = client.create_or_derive_api_key()
    return {
        "clob_api_key": raw.api_key,
        "clob_secret": raw.api_secret,
        "clob_passphrase": raw.api_passphrase,
    }


def get_market_token_id(condition_id: str, outcome: str) -> str | None:
    """
    Fetch token_id for a market outcome (YES/NO) from the CLOB REST API.
    outcome: 'YES' or 'NO'
    """
    try:
        resp = httpx.get(
            f"{CLOB_HOST}/markets/{condition_id}",
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        target = "Yes" if outcome.upper() == "YES" else "No"
        for token in data.get("tokens", []):
            if token.get("outcome", "").lower() == target.lower():
                return token["token_id"]
        log.warning("token_id_not_found", condition_id=condition_id, outcome=outcome)
    except Exception:
        log.exception("get_market_token_failed", condition_id=condition_id)
    return None


def place_order(
    private_key_enc: str,
    api_creds: dict,
    token_id: str,
    side: str,
    price: float,
    size_usdc: float,
) -> dict:
    """
    Place a copy-trade order on Polymarket CLOB.

    side: 'BUY' / 'YES' → BUY outcome token
          'SELL' / 'NO'  → SELL outcome token (only if user holds it)
    size_usdc: dollar amount to spend (for BUY) or receive (for SELL)
    Returns CLOB order response dict.
    """
    from py_clob_client_v2 import OrderArgs, OrderType, Side, PartialCreateOrderOptions

    private_key = decrypt_key(private_key_enc)
    clob_side = Side.BUY if side.upper() in ("BUY", "YES") else Side.SELL

    # Convert USDC amount → number of shares
    if clob_side == Side.BUY:
        shares = round(size_usdc / price, 2) if price > 0 else size_usdc
    else:
        shares = round(size_usdc, 2)

    client = _make_client(private_key, api_creds)

    order_args = OrderArgs(
        token_id=token_id,
        price=round(price, 4),
        size=shares,
        side=clob_side,
    )

    try:
        # Fetch tick size for the market
        tick = "0.01"
        resp = client.create_and_post_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size=tick),
            order_type=OrderType.GTC,
        )
        log.info(
            "order_placed",
            token=token_id[:20],
            side=side,
            price=price,
            size_usdc=size_usdc,
            shares=shares,
        )
        return resp if isinstance(resp, dict) else {"status": "ok", "raw": str(resp)}
    except Exception:
        log.exception("place_order_failed", token_id=token_id[:20], side=side)
        raise
