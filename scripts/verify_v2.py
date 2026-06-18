"""
V2 deposit-wallet verification harness.

Goal: confirm whether programmatic order placement works on Polymarket V2 via the
deposit-wallet (POLY_1271) flow BEFORE reworking the whole bot. Run staged so you
spend at most ~$1.

Prereqs in .env: BUILDER_API_KEY / BUILDER_SECRET / BUILDER_PASSPHRASE (Builder Program),
RELAYER_URL, plus the existing SUPABASE_* / ENCRYPTION_KEY / POLYGON_RPC_URL.
(`derive` works without builder creds — it only needs the signer.)

Run from repo root:
    python scripts/verify_v2.py derive
    python scripts/verify_v2.py deploy
    python scripts/verify_v2.py fund 2
    python scripts/verify_v2.py approve
    python scripts/verify_v2.py creds
    python scripts/verify_v2.py trade <token_id> <price>
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_abi import encode
from web3 import Web3

from core.config import settings
from core.clob import (
    CONDITIONAL_TOKENS,
    CTF_EXCHANGE,
    NEG_RISK_ADAPTER,
    NEG_RISK_CTF_EXCHANGE,
    PUSD_ADDRESS as CLOB_PUSD,
)
from core.db import get_supabase
from core.polygon import MAX_UINT, PUSD_ADDRESS, _ERC20_ABI, get_balances
from core.wallet import decrypt_key

CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"


def _w3():
    return Web3(Web3.HTTPProvider(settings.polygon_rpc_url))


def _load_signer() -> tuple[str, str]:
    sb = get_supabase()
    res = (sb.table("users").select("wallet_address,wallet_private_key_enc")
           .eq("telegram_id", settings.admin_telegram_id).maybe_single().execute())
    row = res.data
    if not row or not row.get("wallet_private_key_enc"):
        raise SystemExit("No wallet for admin user in DB.")
    return decrypt_key(row["wallet_private_key_enc"]), row["wallet_address"]


_HTTP_RELAYER_CLS = None


def _http_relayer_cls():
    """Subclass of RelayClient that authenticates with the 2-part Relayer API Key
    (RELAYER_API_KEY + RELAYER_API_KEY_ADDRESS headers) instead of 3-part builder HMAC.
    Reuses all of the SDK's EIP-712 payload building; only swaps the auth headers."""
    global _HTTP_RELAYER_CLS
    if _HTTP_RELAYER_CLS is not None:
        return _HTTP_RELAYER_CLS
    from py_builder_relayer_client.client import RelayClient

    class _RelayerHttpClient(RelayClient):
        def __init__(self, url, chain_id, private_key, api_key, api_key_address, rpc_url=None):
            super().__init__(url, chain_id, private_key, builder_config=None, rpc_url=rpc_url)
            self._rk = api_key
            self._rka = api_key_address

        def assert_builder_creds_needed(self):  # auth is via relayer-key headers
            return

        def _generate_builder_headers(self, method, request_path, body=None):
            return {"RELAYER_API_KEY": self._rk, "RELAYER_API_KEY_ADDRESS": self._rka}

    _HTTP_RELAYER_CLS = _RelayerHttpClient
    return _HTTP_RELAYER_CLS


def _relayer(private_key: str, need_builder: bool = True):
    from py_builder_relayer_client.client import RelayClient

    rpc = settings.polygon_rpc_url

    # Preferred: 3-part Builder creds (SDK-native HMAC auth).
    if settings.builder_api_key and settings.builder_secret and settings.builder_passphrase:
        from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
        cfg = BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
            key=settings.builder_api_key, secret=settings.builder_secret,
            passphrase=settings.builder_passphrase))
        return RelayClient(settings.relayer_url, CHAIN_ID, private_key, cfg, rpc_url=rpc)

    # Fallback: 2-part Relayer API Key via HTTP headers (what we have today).
    if need_builder:
        if not settings.relayer_api_key:
            raise SystemExit("Neither BUILDER_* nor RELAYER_API_KEY set in .env.")
        cls = _http_relayer_cls()
        return cls(settings.relayer_url, CHAIN_ID, private_key,
                   settings.relayer_api_key, settings.relayer_api_key_address, rpc_url=rpc)

    # Read-only (derive): no auth needed.
    return RelayClient(settings.relayer_url, CHAIN_ID, private_key, None, rpc_url=rpc)


def _approve_data(spender: str) -> str:
    sel = Web3.keccak(text="approve(address,uint256)")[:4]
    return "0x" + (sel + encode(["address", "uint256"],
                                [Web3.to_checksum_address(spender), MAX_UINT])).hex()


def _set_approval_data(spender: str) -> str:
    sel = Web3.keccak(text="setApprovalForAll(address,bool)")[:4]
    return "0x" + (sel + encode(["address", "bool"],
                                [Web3.to_checksum_address(spender), True])).hex()


def derive() -> None:
    pk, eoa = _load_signer()
    dw = _relayer(pk, need_builder=False).get_expected_deposit_wallet()
    w3 = _w3()
    code = w3.eth.get_code(Web3.to_checksum_address(dw))
    print("EOA (signer):       ", eoa)
    print("Deposit wallet:     ", dw)
    print("DW deployed (code): ", len(code) > 2)
    print("EOA balances:       ", get_balances(eoa))
    print("DW balances:        ", get_balances(dw))


def deploy() -> None:
    pk, _ = _load_signer()
    r = _relayer(pk)
    dw = r.get_expected_deposit_wallet()
    w3 = _w3()
    if len(w3.eth.get_code(Web3.to_checksum_address(dw))) > 2:
        print("Already deployed:", dw)
        return
    print("Deploying deposit wallet…", dw)
    print("result:", r.deploy_deposit_wallet().wait())
    print("deployed now:", len(w3.eth.get_code(Web3.to_checksum_address(dw))) > 2)


def fund(amount: float) -> None:
    pk, eoa = _load_signer()
    dw = _relayer(pk, need_builder=False).get_expected_deposit_wallet()
    w3 = _w3()
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS), abi=_ERC20_ABI)
    raw = int(amount * 1_000_000)
    tx = pusd.functions.transfer(Web3.to_checksum_address(dw), raw).build_transaction({
        "from": Web3.to_checksum_address(eoa),
        "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(eoa), "pending"),
        "gasPrice": w3.eth.gas_price, "gas": 120_000, "chainId": CHAIN_ID})
    signed = w3.eth.account.sign_transaction(tx, pk)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    print("transfer pUSD -> DW status:", rcpt.get("status"), "tx:", h.hex())
    print("DW balances:", get_balances(dw))


def approve() -> None:
    from py_builder_relayer_client.models import DepositWalletCall, TransactionType

    pk, _ = _load_signer()
    r = _relayer(pk)
    dw = r.get_expected_deposit_wallet()
    calls = []
    for sp in (CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, NEG_RISK_ADAPTER):
        calls.append(DepositWalletCall(target=Web3.to_checksum_address(CLOB_PUSD),
                                       value="0", data=_approve_data(sp)))
        calls.append(DepositWalletCall(target=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                                       value="0", data=_set_approval_data(sp)))
    nonce = str(r.get_nonce(r.signer.address(), TransactionType.WALLET.value)["nonce"])
    print("WALLET nonce:", nonce, "calls:", len(calls))
    print("approve batch result:", r.execute_deposit_wallet_batch(
        calls=calls, wallet_address=dw, nonce=nonce, deadline=str(int(time.time()) + 600)).wait())


def _clob_with_creds(pk: str, dw: str):
    from py_clob_client_v2 import ClobClient, SignatureTypeV2
    bootstrap = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=pk,
                           signature_type=SignatureTypeV2.POLY_1271, funder=dw)
    creds = bootstrap.create_or_derive_api_key()
    client = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=pk, creds=creds,
                        signature_type=SignatureTypeV2.POLY_1271, funder=dw)
    return client, creds


def creds() -> None:
    pk, _ = _load_signer()
    dw = _relayer(pk, need_builder=False).get_expected_deposit_wallet()
    _, c = _clob_with_creds(pk, dw)
    print("Deposit wallet:", dw)
    print("API key:", getattr(c, "api_key", None))
    print("If CLOB later rejects with 'signer address has to be the address of the API KEY' -> upstream bug #70.")


def trade(token_id: str, price: float) -> None:
    import httpx
    from py_clob_client_v2 import (AssetType, BalanceAllowanceParams, MarketOrderArgs,
                                   OrderType, PartialCreateOrderOptions, Side, SignatureTypeV2)

    pk, _ = _load_signer()
    dw = _relayer(pk, need_builder=False).get_expected_deposit_wallet()
    book = httpx.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=15).json()
    tick = str(book.get("tick_size", "0.01"))
    neg_risk = bool(book.get("neg_risk", False))
    decimals = len(tick.split(".")[1])
    worst = round(min(price * 1.02, 1 - float(tick)), decimals)
    client, _ = _clob_with_creds(pk, dw)
    client.update_balance_allowance(BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL, signature_type=SignatureTypeV2.POLY_1271))
    print(f"Placing $1 BUY token={token_id[:16]} worst={worst} tick={tick} negRisk={neg_risk} funder={dw}")
    resp = client.create_and_post_market_order(
        order_args=MarketOrderArgs(token_id=token_id, amount=1.0, side=Side.BUY,
                                   price=worst, order_type=OrderType.FAK),
        options=PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk),
        order_type=OrderType.FAK)
    print("ORDER RESPONSE:", resp)


def inspectpos() -> None:
    """Dump raw position data + on-chain ERC-1155 balances for redeemable wins.
    Diagnostic: figure out where the neg-risk outcome tokens actually live."""
    import httpx
    from core.clob import CONDITIONAL_TOKENS

    sb = get_supabase()
    res = (sb.table("users").select("wallet_address,deposit_wallet_address")
           .eq("telegram_id", settings.admin_telegram_id).maybe_single().execute())
    row = res.data
    dw = row["deposit_wallet_address"]
    eoa = row["wallet_address"]
    print("EOA:", eoa, "| Deposit wallet:", dw)

    raw = httpx.get("https://data-api.polymarket.com/positions",
                    params={"user": dw, "limit": 100}, timeout=15).json()
    if not isinstance(raw, list):
        raw = raw.get("data", []) if isinstance(raw, dict) else []

    w3 = _w3()
    bal_abi = [{"inputs": [{"name": "account", "type": "address"},
                           {"name": "id", "type": "uint256"}],
                "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view", "type": "function"}]
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=bal_abi)

    for p in raw:
        if float(p.get("size") or 0) <= 0:
            continue
        asset = str(p.get("asset", ""))
        print("\n---", p.get("title", "")[:50], "·", p.get("outcome"))
        print("  conditionId :", p.get("conditionId"))
        print("  asset(token):", asset)
        print("  size/cur/redeemable:", p.get("size"), p.get("curPrice"), p.get("redeemable"))
        print("  negativeRisk:", p.get("negativeRisk"))
        for label, who in (("DW", dw), ("EOA", eoa)):
            try:
                b = ctf.functions.balanceOf(Web3.to_checksum_address(who), int(asset)).call()
                print(f"  CTF.balanceOf({label}) = {b}")
            except Exception as exc:
                print(f"  CTF.balanceOf({label}) FAILED: {exc}")
        # full raw keys so we can spot any alternate token field
        print("  raw keys:", sorted(p.keys()))


def redeem() -> None:
    """Redeem all resolved winning positions for the admin user into pUSD."""
    from core.polymarket import get_positions
    from core.relayer import redeem_winnings

    sb = get_supabase()
    res = (sb.table("users").select("wallet_private_key_enc,deposit_wallet_address")
           .eq("telegram_id", settings.admin_telegram_id).maybe_single().execute())
    row = res.data
    if not row or not row.get("deposit_wallet_address"):
        raise SystemExit("No deposit wallet for admin user.")
    dw = row["deposit_wallet_address"]
    pk_enc = row["wallet_private_key_enc"]

    print("Deposit wallet:", dw)
    print("pUSD before:", get_balances(dw).get("pusd"))
    positions = [p for p in get_positions(dw) if p["shares"] > 0 and p.get("redeemable")]
    wins = [p for p in positions if float(p.get("cur_price") or 0) >= 0.5]
    if not wins:
        print("No redeemable winning positions.")
        return
    for p in wins:
        outcome = p.get("outcome") or ""
        idx = 0 if outcome.strip().lower().startswith("yes") else 1
        print(f"Redeeming: {p.get('title','')[:40]} · {outcome} · "
              f"neg_risk={p.get('neg_risk')} shares={p['shares']}")
        try:
            r = redeem_winnings(pk_enc, p["condition_id"], bool(p.get("neg_risk")),
                                idx, p["token_id"])
            print("  ->", r)
        except Exception as exc:
            print("  FAILED:", exc)
    time.sleep(5)
    print("pUSD after:", get_balances(dw).get("pusd"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "derive"
    {"derive": derive, "deploy": deploy, "approve": approve, "creds": creds,
     "redeem": redeem, "inspectpos": inspectpos}.get(cmd, lambda: None)() \
        if cmd in ("derive", "deploy", "approve", "creds", "redeem", "inspectpos") else (
            fund(float(sys.argv[2]) if len(sys.argv) > 2 else 2.0) if cmd == "fund"
            else trade(sys.argv[2], float(sys.argv[3])) if cmd == "trade"
            else print("usage: derive | deploy | fund <amt> | approve | creds | redeem | trade <token_id> <price>"))
