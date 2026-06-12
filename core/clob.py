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

# Polymarket contract addresses on Polygon
CTF_EXCHANGE        = "0x4bFb41d5B3570DeFd03C39a9A4D8DE6BD8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER    = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_ADDRESS        = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e
MAX_UINT            = 2**256 - 1

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
    One-time wallet registration for Polymarket CLOB trading.
    Approves USDC and CTF contracts so orders can be matched on-chain.
    Requires a small amount of MATIC for gas (~0.01 MATIC).
    """
    from web3 import Web3
    from core.config import settings

    private_key = decrypt_key(private_key_enc)
    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    account = w3.eth.account.from_key(private_key)
    address = account.address

    matic_balance = w3.eth.get_balance(address)
    if matic_balance < w3.to_wei(0.005, "ether"):
        raise ValueError(
            f"Недостаточно MATIC для регистрации. "
            f"Нужно минимум 0.005 MATIC, на балансе: "
            f"{w3.from_wei(matic_balance, 'ether'):.6f} MATIC"
        )

    gas_price = w3.eth.gas_price
    nonce = w3.eth.get_transaction_count(address)
    receipts = []

    def _send_tx(contract_address: str, abi: list, fn_name: str, *args):
        nonlocal nonce
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address), abi=abi
        )
        fn = getattr(contract.functions, fn_name)(*args)
        tx = fn.build_transaction({
            "from": address,
            "nonce": nonce,
            "gasPrice": gas_price,
            "gas": 100_000,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        nonce += 1
        log.info("approval_tx", fn=fn_name, contract=contract_address[:10], tx=tx_hash.hex()[:16])
        return receipt

    # 1. Approve USDC for CTF Exchange
    _send_tx(USDC_ADDRESS, _ERC20_APPROVE_ABI, "approve", Web3.to_checksum_address(CTF_EXCHANGE), MAX_UINT)
    # 2. Approve USDC for Neg Risk CTF Exchange
    _send_tx(USDC_ADDRESS, _ERC20_APPROVE_ABI, "approve", Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE), MAX_UINT)
    # 3. CTF Exchange → Neg Risk Adapter approval
    _send_tx(CTF_EXCHANGE, _ERC1155_APPROVAL_ABI, "setApprovalForAll", Web3.to_checksum_address(NEG_RISK_ADAPTER), True)

    log.info("wallet_registered", address=address)
    return {"registered": True, "address": address}


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
