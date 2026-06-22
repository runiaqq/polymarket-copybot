"""
Polymarket V2 relayer wrapper — gasless deposit-wallet lifecycle.

Each user trades through their own deposit wallet (an ERC-1967 proxy owned by the
user's EOA). The relayer (authenticated with our Builder API key) pays gas for:
  - deploying the deposit wallet (WALLET-CREATE)
  - setting pUSD + CTF approvals on it (WALLET batch)

The user's EOA only signs the EIP-712 payloads; it never needs POL for these ops.
The flow here mirrors scripts/verify_v2.py, which is proven end-to-end on Polygon.
"""

import time

import structlog
from eth_abi import encode
from web3 import Web3

from core.config import settings
from core.clob import (
    CONDITIONAL_TOKENS,
    CTF_EXCHANGE,
    MAX_UINT,
    NEG_RISK_ADAPTER,
    NEG_RISK_CTF_EXCHANGE,
    PUSD_ADDRESS,
)
from core.wallet import decrypt_key

log = structlog.get_logger(__name__)

CHAIN_ID = 137
_TRADING_SPENDERS = (CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, NEG_RISK_ADAPTER)


def builder_configured() -> bool:
    return bool(settings.builder_api_key and settings.builder_secret and settings.builder_passphrase)


def _client(private_key: str):
    """RelayClient authenticated with our Builder API key (HMAC).

    The Builder key authorizes gasless relaying for ANY owner EOA — that's what
    makes the multi-user (per-subscriber) model work.
    """
    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

    if not builder_configured():
        raise RuntimeError("BUILDER_API_KEY/SECRET/PASSPHRASE not configured")

    cfg = BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
        key=settings.builder_api_key,
        secret=settings.builder_secret,
        passphrase=settings.builder_passphrase,
    ))
    return RelayClient(settings.relayer_url, CHAIN_ID, private_key, cfg,
                       rpc_url=settings.polygon_rpc_url)


def _approve_data(spender: str) -> str:
    sel = Web3.keccak(text="approve(address,uint256)")[:4]
    return "0x" + (sel + encode(["address", "uint256"],
                                [Web3.to_checksum_address(spender), MAX_UINT])).hex()


def _set_approval_data(spender: str) -> str:
    sel = Web3.keccak(text="setApprovalForAll(address,bool)")[:4]
    return "0x" + (sel + encode(["address", "bool"],
                                [Web3.to_checksum_address(spender), True])).hex()


def derive_deposit_wallet(private_key_enc: str) -> str:
    """Deterministic deposit-wallet address for a user's EOA (no on-chain call to deploy)."""
    pk = decrypt_key(private_key_enc)
    return _client(pk).get_expected_deposit_wallet()


def is_deployed(deposit_wallet: str) -> bool:
    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    return len(w3.eth.get_code(Web3.to_checksum_address(deposit_wallet))) > 2


def deploy_deposit_wallet(private_key_enc: str) -> dict:
    """Deploy the user's deposit wallet via the relayer (gasless). Idempotent."""
    pk = decrypt_key(private_key_enc)
    c = _client(pk)
    dw = c.get_expected_deposit_wallet()
    if is_deployed(dw):
        return {"deposit_wallet": dw, "deployed": True, "already": True}

    resp = c.deploy_deposit_wallet().wait()
    if not resp or resp.get("state") not in ("STATE_MINED", "STATE_CONFIRMED"):
        raise RuntimeError(f"deploy_deposit_wallet did not confirm: {resp}")
    log.info("deposit_wallet_deployed", dw=dw[:12], tx=(resp.get("transactionHash") or "")[:14])
    return {"deposit_wallet": dw, "deployed": True, "tx": resp.get("transactionHash")}


def set_trading_approvals(private_key_enc: str) -> dict:
    """Approve pUSD + CTF outcome tokens for the V2 exchanges from the deposit wallet
    via a single gasless relayer batch."""
    from py_builder_relayer_client.models import DepositWalletCall, TransactionType

    pk = decrypt_key(private_key_enc)
    c = _client(pk)
    dw = c.get_expected_deposit_wallet()

    calls = []
    for sp in _TRADING_SPENDERS:
        calls.append(DepositWalletCall(target=Web3.to_checksum_address(PUSD_ADDRESS),
                                       value="0", data=_approve_data(sp)))
        calls.append(DepositWalletCall(target=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                                       value="0", data=_set_approval_data(sp)))

    nonce = str(c.get_nonce(c.signer.address(), TransactionType.WALLET.value)["nonce"])
    resp = c.execute_deposit_wallet_batch(
        calls=calls, wallet_address=dw, nonce=nonce,
        deadline=str(int(time.time()) + 600),
    ).wait()
    if not resp or resp.get("state") not in ("STATE_MINED", "STATE_CONFIRMED"):
        raise RuntimeError(f"approval batch did not confirm: {resp}")
    log.info("deposit_wallet_approved", dw=dw[:12], tx=(resp.get("transactionHash") or "")[:14])
    return {"deposit_wallet": dw, "approved": True, "tx": resp.get("transactionHash")}


_CTF_ABI = [
    {"inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSet", "type": "uint256"}],
     "name": "getCollectionId", "outputs": [{"name": "", "type": "bytes32"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "collateralToken", "type": "address"},
                {"name": "collectionId", "type": "bytes32"}],
     "name": "getPositionId", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    # Resolution state — Blueprint 1: on-chain settlement source of truth
    {"inputs": [{"name": "conditionId", "type": "bytes32"}],
     "name": "payoutDenominator",
     "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "conditionId", "type": "bytes32"}, {"name": "index", "type": "uint256"}],
     "name": "payoutNumerators",
     "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

# Binary CTF markets can be collateralized by different stables across versions.
# We auto-detect which one matches the held token id.
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"   # USDC.e
USDC_NATIVE  = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # native USDC

_wcol_cache: str | None = None


def _ctf():
    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    return w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=_CTF_ABI)


def _wrapped_collateral() -> str | None:
    """WrappedCollateral address the NegRiskAdapter uses for neg-risk positions."""
    global _wcol_cache
    if _wcol_cache is not None:
        return _wcol_cache
    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    for fn in ("wcol", "getWrappedCollateral", "wrappedCollateral"):
        try:
            ad = w3.eth.contract(
                address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                abi=[{"inputs": [], "name": fn, "outputs": [{"name": "", "type": "address"}],
                      "stateMutability": "view", "type": "function"}])
            _wcol_cache = getattr(ad.functions, fn)().call()
            return _wcol_cache
        except Exception:
            continue
    return None


def ctf_token_balance(deposit_wallet: str, token_id: str) -> int:
    """Raw ERC-1155 outcome-token balance held by the deposit wallet."""
    return int(_ctf().functions.balanceOf(
        Web3.to_checksum_address(deposit_wallet), int(token_id)).call())


def _cond_bytes(condition_id: str) -> bytes:
    cond = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    return Web3.to_bytes(hexstr=cond)


def is_condition_resolved(condition_id: str) -> bool:
    """Return True when the CTF condition has been resolved on-chain (payoutDenominator > 0)."""
    try:
        return int(_ctf().functions.payoutDenominator(_cond_bytes(condition_id)).call()) > 0
    except Exception:
        log.warning("is_condition_resolved_failed", cond=condition_id[:14])
        return False


def get_payout_numerator(condition_id: str, outcome_index: int) -> int:
    """Return payoutNumerators[outcome_index]. > 0 means this outcome won."""
    try:
        return int(_ctf().functions.payoutNumerators(
            _cond_bytes(condition_id), int(outcome_index)).call())
    except Exception:
        log.warning("get_payout_numerator_failed", cond=condition_id[:14], idx=outcome_index)
        return 0


def redeem_winnings(private_key_enc: str, condition_id: str, neg_risk: bool,
                    outcome_index: int, token_id: str) -> dict:
    """Redeem a resolved winning position into pUSD on the deposit wallet (gasless).

    Auto-detects how the held outcome token is collateralized by matching the
    on-chain positionId against candidate collaterals — this is bulletproof and
    does NOT rely on the (often missing) `negativeRisk` API flag:
      * WrappedCollateral match → neg-risk → NegRiskAdapter.redeemPositions
      * stable (pUSD/USDC.e/USDC) match → binary → CTF.redeemPositions

    Payout lands in the deposit wallet (msg.sender). Idempotent: once the tokens
    are burned a repeat call redeems nothing.
    """
    from py_builder_relayer_client.models import DepositWalletCall, TransactionType

    pk = decrypt_key(private_key_enc)
    c = _client(pk)
    dw = c.get_expected_deposit_wallet()
    dw_cs = Web3.to_checksum_address(dw)

    cond = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    cond_b = Web3.to_bytes(hexstr=cond)
    asset_int = int(token_id)
    idx = int(outcome_index)

    ctf = _ctf()
    coll_id = ctf.functions.getCollectionId(b"\x00" * 32, cond_b, 1 << idx).call()
    wcol = _wrapped_collateral()

    # 1) neg-risk: token is WrappedCollateral-collateralized.
    if wcol and int(ctf.functions.getPositionId(
            Web3.to_checksum_address(wcol), coll_id).call()) == asset_int:
        bal = int(ctf.functions.balanceOf(dw_cs, asset_int).call())
        if bal <= 0:
            return {"skipped": True, "reason": "no_token_balance", "deposit_wallet": dw}
        amounts = [0, 0]
        amounts[idx] = bal
        sel = Web3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
        data = "0x" + (sel + encode(["bytes32", "uint256[]"], [cond_b, amounts])).hex()
        target = NEG_RISK_ADAPTER
        mode = "neg_risk"
    else:
        # 2) binary: find the stable collateral whose positionId matches the token.
        matched = None
        for coll in (PUSD_ADDRESS, USDC_BRIDGED, USDC_NATIVE):
            pid = int(ctf.functions.getPositionId(
                Web3.to_checksum_address(coll), coll_id).call())
            if pid == asset_int:
                matched = coll
                break
        if not matched:
            return {"skipped": True, "reason": "collateral_unmatched",
                    "deposit_wallet": dw}
        if int(ctf.functions.balanceOf(dw_cs, asset_int).call()) <= 0:
            return {"skipped": True, "reason": "no_token_balance", "deposit_wallet": dw}
        sel = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        data = "0x" + (sel + encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [Web3.to_checksum_address(matched), b"\x00" * 32, cond_b, [1, 2]],
        )).hex()
        target = CONDITIONAL_TOKENS
        mode = "binary"

    call = DepositWalletCall(target=Web3.to_checksum_address(target), value="0", data=data)
    nonce = str(c.get_nonce(c.signer.address(), TransactionType.WALLET.value)["nonce"])
    resp = c.execute_deposit_wallet_batch(
        calls=[call], wallet_address=dw, nonce=nonce,
        deadline=str(int(time.time()) + 600),
    ).wait()
    if not resp or resp.get("state") not in ("STATE_MINED", "STATE_CONFIRMED"):
        raise RuntimeError(f"redeem did not confirm: {resp}")
    log.info("redeemed", dw=dw[:12], mode=mode,
             tx=(resp.get("transactionHash") or "")[:14])
    return {"deposit_wallet": dw, "tx": resp.get("transactionHash"),
            "redeemed": True, "mode": mode}


def convert_dw_usdce_to_pusd(private_key_enc: str) -> dict:
    """Wrap any USDC.e sitting in the deposit wallet into tradeable pUSD (gasless).

    Redeemed winnings arrive as USDC.e (binary markets and neg-risk WrappedCollateral
    both settle to USDC.e). This makes them usable as trading collateral (pUSD).
    """
    from py_builder_relayer_client.models import DepositWalletCall, TransactionType
    from core.polygon import COLLATERAL_ONRAMP

    usdce = USDC_BRIDGED
    pk = decrypt_key(private_key_enc)
    c = _client(pk)
    dw = c.get_expected_deposit_wallet()
    dw_cs = Web3.to_checksum_address(dw)

    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    erc20 = w3.eth.contract(address=Web3.to_checksum_address(usdce), abi=[{
        "inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"}])
    bal = int(erc20.functions.balanceOf(dw_cs).call())
    if bal <= 0:
        return {"skipped": True, "reason": "no_usdce", "deposit_wallet": dw}

    approve = DepositWalletCall(target=Web3.to_checksum_address(usdce), value="0",
                               data=_approve_data(COLLATERAL_ONRAMP))
    sel = Web3.keccak(text="wrap(address,address,uint256)")[:4]
    wrap_data = "0x" + (sel + encode(
        ["address", "address", "uint256"],
        [Web3.to_checksum_address(usdce), dw_cs, bal])).hex()
    wrap = DepositWalletCall(target=Web3.to_checksum_address(COLLATERAL_ONRAMP),
                             value="0", data=wrap_data)

    nonce = str(c.get_nonce(c.signer.address(), TransactionType.WALLET.value)["nonce"])
    resp = c.execute_deposit_wallet_batch(
        calls=[approve, wrap], wallet_address=dw, nonce=nonce,
        deadline=str(int(time.time()) + 600),
    ).wait()
    if not resp or resp.get("state") not in ("STATE_MINED", "STATE_CONFIRMED"):
        raise RuntimeError(f"usdce->pusd wrap did not confirm: {resp}")
    log.info("dw_usdce_wrapped", dw=dw[:12], amount=round(bal / 1e6, 4),
             tx=(resp.get("transactionHash") or "")[:14])
    return {"wrapped": bal / 1e6, "tx": resp.get("transactionHash"), "deposit_wallet": dw}


def transfer_from_deposit_wallet(private_key_enc: str, to_address: str, raw_amount: int) -> dict:
    """Move pUSD out of the deposit wallet (e.g. for withdrawals) via a gasless batch."""
    from py_builder_relayer_client.models import DepositWalletCall, TransactionType

    pk = decrypt_key(private_key_enc)
    c = _client(pk)
    dw = c.get_expected_deposit_wallet()

    sel = Web3.keccak(text="transfer(address,uint256)")[:4]
    data = "0x" + (sel + encode(["address", "uint256"],
                                [Web3.to_checksum_address(to_address), int(raw_amount)])).hex()
    call = DepositWalletCall(target=Web3.to_checksum_address(PUSD_ADDRESS), value="0", data=data)

    nonce = str(c.get_nonce(c.signer.address(), TransactionType.WALLET.value)["nonce"])
    resp = c.execute_deposit_wallet_batch(
        calls=[call], wallet_address=dw, nonce=nonce,
        deadline=str(int(time.time()) + 600),
    ).wait()
    if not resp or resp.get("state") not in ("STATE_MINED", "STATE_CONFIRMED"):
        raise RuntimeError(f"deposit-wallet transfer did not confirm: {resp}")
    return {"deposit_wallet": dw, "tx": resp.get("transactionHash")}
