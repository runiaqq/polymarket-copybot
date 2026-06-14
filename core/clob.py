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

# Polymarket V2 contract addresses on Polygon (source: docs.polymarket.com/resources/contracts)
CTF_EXCHANGE          = "0xE111180000d2663C0091e4f400237545B87B996B"  # CTF Exchange V2
NEG_RISK_CTF_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"  # Neg Risk CTF Exchange V2
NEG_RISK_ADAPTER      = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # Neg Risk Adapter
CONDITIONAL_TOKENS    = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # CTF outcome tokens (ERC1155)
PUSD_ADDRESS          = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # pUSD — V2 collateral
MAX_UINT              = 2**256 - 1

# Spenders that need approval for CLOB order matching + settlement.
_TRADING_SPENDERS = (CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, NEG_RISK_ADAPTER)

_ERC20_APPROVE_ABI = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]
_ERC1155_APPROVAL_ABI = [{"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]


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


def register_wallet(private_key_enc: str) -> dict:
    """
    One-time wallet registration for Polymarket CLOB V2 trading (EOA / signature type 0).
    Approves pUSD (collateral) and the CTF outcome tokens for the V2 exchanges so orders
    can be matched and settled on-chain, then wraps any existing USDC.e into pUSD.
    Requires POL (MATIC) for gas (~0.05 recommended for the full approval set).
    """
    from web3 import Web3
    from core.config import settings

    private_key = decrypt_key(private_key_enc)
    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    account = w3.eth.account.from_key(private_key)
    address = account.address

    matic_balance = w3.eth.get_balance(address)
    if matic_balance < w3.to_wei(0.02, "ether"):
        raise ValueError(
            f"Недостаточно POL для регистрации. "
            f"Нужно минимум ~0.05 POL, на балансе: "
            f"{w3.from_wei(matic_balance, 'ether'):.6f} POL"
        )

    gas_price = w3.eth.gas_price
    nonce = w3.eth.get_transaction_count(address)

    def _send_tx(contract_address: str, abi: list, fn_name: str, *args) -> None:
        nonlocal nonce
        contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)
        fn = getattr(contract.functions, fn_name)(*args)
        tx = fn.build_transaction({
            "from": address, "nonce": nonce, "gasPrice": gas_price, "gas": 120_000, "chainId": 137,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        nonce += 1
        log.info("approval_tx", fn=fn_name, contract=contract_address[:10], tx=tx_hash.hex()[:16])

    # Approve each V2 trading spender to move the wallet's pUSD and outcome tokens.
    for spender in _TRADING_SPENDERS:
        _send_tx(PUSD_ADDRESS, _ERC20_APPROVE_ABI, "approve",
                 Web3.to_checksum_address(spender), MAX_UINT)
        _send_tx(CONDITIONAL_TOKENS, _ERC1155_APPROVAL_ABI, "setApprovalForAll",
                 Web3.to_checksum_address(spender), True)

    # Wrap any existing USDC.e into pUSD so the wallet has tradeable collateral.
    wrapped = None
    try:
        from core.polygon import wrap_usdce_to_pusd
        wrapped = wrap_usdce_to_pusd(private_key_enc, address)
    except Exception:
        log.warning("register_wrap_failed", address=address[:10])

    log.info("wallet_registered", address=address, wrapped=bool(wrapped))
    return {"registered": True, "address": address, "wrapped_tx": wrapped}


def generate_api_creds(private_key_enc: str) -> dict:
    """
    Generate Polymarket CLOB API credentials via L1 (EIP-712) auth.
    Called once per user wallet. Runs wallet registration first if needed.
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


def _tick_decimals(tick_size: str) -> int:
    """Number of price decimals implied by the tick size string ('0.01' -> 2)."""
    if "." in tick_size:
        return len(tick_size.split(".", 1)[1].rstrip("0")) or 1
    return 2


def _worst_buy_price(price: float, tick_size: str, slippage_pct: float) -> float:
    """Compute a slippage-protected worst acceptable BUY price, clamped to the tick grid."""
    decimals = _tick_decimals(tick_size)
    tick = float(tick_size)
    worst = price * (1.0 + slippage_pct)
    # Price must stay strictly below 1.0 (max is 1 - tick).
    worst = min(worst, 1.0 - tick)
    worst = max(worst, tick)
    return round(worst, decimals)


def _worst_sell_price(price: float, tick_size: str, slippage_pct: float) -> float:
    """Compute a slippage-protected worst (lowest) acceptable SELL price, clamped to tick."""
    decimals = _tick_decimals(tick_size)
    tick = float(tick_size)
    worst = price * (1.0 - slippage_pct)
    worst = max(worst, tick)
    worst = min(worst, 1.0 - tick)
    return round(worst, decimals)


def sell_position(
    private_key_enc: str,
    api_creds: dict,
    token_id: str,
    shares: float,
    price: float,
    tick_size: str = "0.01",
    neg_risk: bool = False,
    slippage_pct: float = 0.03,
) -> dict:
    """
    Exit a position with a marketable SELL (FAK) of `shares` outcome tokens.
    `price` is the reference (best bid); we accept down to a slippage-protected floor.
    """
    from py_clob_client_v2 import (
        MarketOrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )

    private_key = decrypt_key(private_key_enc)
    client = _make_client(private_key, api_creds)
    worst_price = _worst_sell_price(price, tick_size, slippage_pct)

    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=round(shares, 2),   # SELL market order amount = number of shares
        side=Side.SELL,
        price=worst_price,
        order_type=OrderType.FAK,
    )
    try:
        resp = client.create_and_post_market_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.FAK,
        )
        log.info("sell_placed", token=token_id[:20], shares=shares,
                 ref_price=price, worst_price=worst_price, neg_risk=neg_risk)
        return resp if isinstance(resp, dict) else {"status": "ok", "raw": str(resp)}
    except Exception:
        log.exception("sell_order_failed", token_id=token_id[:20])
        raise


def place_order(
    private_key_enc: str,
    api_creds: dict,
    token_id: str,
    side: str,
    price: float,
    size_usdc: float,
    tick_size: str = "0.01",
    neg_risk: bool = False,
    slippage_pct: float = 0.02,
) -> dict:
    """
    Place a copy-trade BUY as a marketable order (FAK) with slippage protection.

    size_usdc: dollar amount to spend.
    The worst-price limit caps how much worse than the donor/whale price we'll pay.
    Returns the CLOB order response dict.
    """
    from py_clob_client_v2 import (
        MarketOrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )

    if side.upper() not in ("BUY", "YES"):
        raise ValueError(f"only BUY copy is supported, got side={side}")

    private_key = decrypt_key(private_key_enc)
    client = _make_client(private_key, api_creds)

    worst_price = _worst_buy_price(price, tick_size, slippage_pct)

    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=round(size_usdc, 2),
        side=Side.BUY,
        price=worst_price,
        order_type=OrderType.FAK,
    )

    try:
        resp = client.create_and_post_market_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.FAK,
        )
        log.info(
            "order_placed",
            token=token_id[:20],
            side=side,
            ref_price=price,
            worst_price=worst_price,
            size_usdc=size_usdc,
            neg_risk=neg_risk,
        )
        return resp if isinstance(resp, dict) else {"status": "ok", "raw": str(resp)}
    except Exception:
        log.exception("place_order_failed", token_id=token_id[:20], side=side)
        raise
