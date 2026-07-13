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

# BP25: pUSD-native collateral adapters — the relayer-permitted route for pUSD-native
# split/merge/redeem.  Direct NegRiskAdapter redeemPositions from a deposit wallet is
# rejected by the relayer allowlist ("call blocked: calls to 0xd91E…35296 are not
# permitted"); these thin adapters burn the ERC1155 via CTF and return pUSD.
CTF_COLLATERAL_ADAPTER          = "0xAdA100Db00Ca00073811820692005400218FcE1f"  # CtfCollateralAdapter
NEG_RISK_CTF_COLLATERAL_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"  # NegRiskCtfCollateralAdapter

# Spenders that need approval for CLOB order matching + settlement.
_TRADING_SPENDERS = (CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, NEG_RISK_ADAPTER)
# Spenders that must be approved so the pUSD-native adapters can burn/move the deposit
# wallet's tokens during split/merge/redeem (setApprovalForAll on CTF + pUSD approve).
_ADAPTER_SPENDERS = (CTF_COLLATERAL_ADAPTER, NEG_RISK_CTF_COLLATERAL_ADAPTER)

_ERC20_APPROVE_ABI = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]
_ERC1155_APPROVAL_ABI = [{"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]


def _make_client(private_key: str, api_creds: dict | None = None, funder: str | None = None):
    """Build a CLOB v2 client.

    When `funder` (the user's deposit wallet) is given, the client signs orders as
    POLY_1271 with the deposit wallet as collateral source — the only path V2 accepts
    (plain EOA makers are rejected with "maker address not allowed").
    """
    from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2

    creds = None
    if api_creds and api_creds.get("clob_api_key"):
        creds = ApiCreds(
            api_key=api_creds["clob_api_key"],
            api_secret=api_creds["clob_secret"],
            api_passphrase=api_creds["clob_passphrase"],
        )

    kwargs = dict(host=CLOB_HOST, chain_id=CHAIN_ID, key=private_key, creds=creds)
    if funder:
        kwargs["signature_type"] = SignatureTypeV2.POLY_1271
        kwargs["funder"] = funder
    return ClobClient(**kwargs)


def register_deposit_wallet(private_key_enc: str) -> dict:
    """
    One-time per-user setup for Polymarket V2 trading via a deposit wallet.

    Gasless (relayer-paid): derive the deterministic deposit wallet, deploy it,
    set pUSD + CTF approvals on it, then derive CLOB API creds bound to that
    deposit wallet (POLY_1271). No POL needed from the user for any of this.
    Funding (moving pUSD into the deposit wallet) happens separately on deposit.
    """
    import time

    from core import relayer

    deposit_wallet = relayer.derive_deposit_wallet(private_key_enc)
    relayer.deploy_deposit_wallet(private_key_enc)

    # The relayer's owner/wallet registry is indexed ASYNCHRONOUSLY after the
    # deploy tx mines.  On a brand-new wallet the approvals batch (which calls the
    # relayer's get_nonce / execute) can race ahead of that indexing and fail with
    # "wallet registry validation failed: <eoa> is not registered".  Existing users
    # never hit this because their deposit wallet was deployed long ago (deploy is
    # skipped as already-deployed).  Retry until the registry catches up.  deploy is
    # idempotent, so re-invoking it between attempts is safe.
    last_exc: Exception | None = None
    for attempt in range(8):
        try:
            relayer.set_trading_approvals(private_key_enc)
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 — inspect message, re-raise if unrelated
            msg = str(exc).lower()
            if "not registered" in msg or "registry" in msg:
                last_exc = exc
                log.warning("approvals_registry_lag_retry",
                            attempt=attempt + 1, dw=deposit_wallet[:12])
                time.sleep(5)
                try:
                    relayer.deploy_deposit_wallet(private_key_enc)  # idempotent
                except Exception:
                    pass
                continue
            raise
    if last_exc is not None:
        raise RuntimeError(
            f"trading approvals failed after registry-lag retries: {last_exc}")

    creds = generate_api_creds(private_key_enc, funder=deposit_wallet)

    log.info("deposit_wallet_registered", dw=deposit_wallet[:12])
    return {"registered": True, "deposit_wallet": deposit_wallet, "creds": creds}


def generate_api_creds(private_key_enc: str, funder: str | None = None) -> dict:
    """
    Generate/derive Polymarket CLOB API credentials via L1 (EIP-712) auth.
    `funder` is the user's deposit wallet (POLY_1271) so the creds are bound to it.
    """
    private_key = decrypt_key(private_key_enc)
    client = _make_client(private_key, funder=funder)
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
    deposit_wallet: str | None = None,
) -> dict:
    """
    Exit a position with a marketable SELL (FAK) of `shares` outcome tokens.
    `price` is the reference (best bid); we accept down to a slippage-protected floor.
    `deposit_wallet` is the POLY_1271 funder that holds the position.
    """
    from py_clob_client_v2 import (
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
        SignatureTypeV2,
    )

    private_key = decrypt_key(private_key_enc)
    client = _make_client(private_key, api_creds, funder=deposit_wallet)
    if deposit_wallet:
        try:
            client.update_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=SignatureTypeV2.POLY_1271,
            ))
        except Exception:
            log.warning("sell_allowance_update_failed", token=token_id[:18])
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
    deposit_wallet: str | None = None,
) -> dict:
    """
    Place a copy-trade BUY as a marketable order (FAK) with slippage protection.

    size_usdc: dollar amount to spend.
    The worst-price limit caps how much worse than the donor/whale price we'll pay.
    `deposit_wallet` is the POLY_1271 funder (holds pUSD collateral).
    Returns the CLOB order response dict.
    """
    from py_clob_client_v2 import (
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
        SignatureTypeV2,
    )

    if side.upper() not in ("BUY", "YES"):
        raise ValueError(f"only BUY copy is supported, got side={side}")

    private_key = decrypt_key(private_key_enc)
    client = _make_client(private_key, api_creds, funder=deposit_wallet)
    if deposit_wallet:
        try:
            client.update_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=SignatureTypeV2.POLY_1271,
            ))
        except Exception:
            log.warning("buy_allowance_update_failed", token=token_id[:18])

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
