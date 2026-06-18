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


_CTF_BALANCE_ABI = [{
    "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view", "type": "function",
}]


def ctf_token_balance(deposit_wallet: str, token_id: str) -> int:
    """Raw ERC-1155 outcome-token balance held by the deposit wallet."""
    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                          abi=_CTF_BALANCE_ABI)
    return int(ctf.functions.balanceOf(
        Web3.to_checksum_address(deposit_wallet), int(token_id)).call())


def redeem_winnings(private_key_enc: str, condition_id: str, neg_risk: bool,
                    outcome_index: int, token_id: str) -> dict:
    """Redeem a resolved winning position into pUSD on the deposit wallet (gasless).

    * neg-risk markets  → NegRiskAdapter.redeemPositions(conditionId, amounts)
    * binary markets    → ConditionalTokens.redeemPositions(pUSD, 0x0, conditionId, [1,2])

    Payout (pUSD) is sent to the deposit wallet (msg.sender). Idempotent: a second
    call after the tokens are already burned simply redeems nothing.
    """
    from py_builder_relayer_client.models import DepositWalletCall, TransactionType

    pk = decrypt_key(private_key_enc)
    c = _client(pk)
    dw = c.get_expected_deposit_wallet()

    cond = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    cond_b = Web3.to_bytes(hexstr=cond)

    if neg_risk:
        bal = ctf_token_balance(dw, token_id)
        if bal <= 0:
            return {"skipped": True, "reason": "no_token_balance", "deposit_wallet": dw}
        amounts = [0, 0]
        amounts[outcome_index] = bal
        sel = Web3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
        data = "0x" + (sel + encode(["bytes32", "uint256[]"], [cond_b, amounts])).hex()
        target = NEG_RISK_ADAPTER
    else:
        sel = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        data = "0x" + (sel + encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [Web3.to_checksum_address(PUSD_ADDRESS), b"\x00" * 32, cond_b, [1, 2]],
        )).hex()
        target = CONDITIONAL_TOKENS

    call = DepositWalletCall(target=Web3.to_checksum_address(target), value="0", data=data)
    nonce = str(c.get_nonce(c.signer.address(), TransactionType.WALLET.value)["nonce"])
    resp = c.execute_deposit_wallet_batch(
        calls=[call], wallet_address=dw, nonce=nonce,
        deadline=str(int(time.time()) + 600),
    ).wait()
    if not resp or resp.get("state") not in ("STATE_MINED", "STATE_CONFIRMED"):
        raise RuntimeError(f"redeem did not confirm: {resp}")
    log.info("redeemed", dw=dw[:12], neg_risk=neg_risk,
             tx=(resp.get("transactionHash") or "")[:14])
    return {"deposit_wallet": dw, "tx": resp.get("transactionHash"), "redeemed": True}


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
