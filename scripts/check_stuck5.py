"""One-off: check the 5 stuck conditions on-chain + Gamma status."""
import httpx
from web3 import Web3

from core.clob import CONDITIONAL_TOKENS
from core.config import settings
from core.relayer import _CTF_ABI

CONDS = [
    ("1121", "0x5b95093aac811c9d52a7b56ada07cfdc667fc83c1201e07540b5d63e07b21529", True),
    ("1107/1108", "0xdbb604bc03f837d31cc7d476c5e7477fa241f8e311e42a805141cabb28ffcf64", True),
    ("1109/1110", "0x6f6f06d08dfc9615aa8e06e865c0e5a47838aeb81bd03925951cd678484e2314", False),
]
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
ctf = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=_CTF_ABI)

for ids, cond, nr in CONDS:
    cond_b = Web3.to_bytes(hexstr=cond)
    den = ctf.functions.payoutDenominator(cond_b).call()
    nums = [ctf.functions.payoutNumerators(cond_b, i).call() for i in (0, 1)] if den else None
    q, closed, end, outs = "?", "?", "?", "?"
    for extra in ({}, {"closed": "true"}):
        try:
            r = httpx.get("https://gamma-api.polymarket.com/markets",
                          params={"condition_ids": cond, **extra}, headers=H, timeout=10)
            ms = r.json()
            if isinstance(ms, list) and ms:
                m = ms[0]
                q = (m.get("question") or "")[:60]
                closed = m.get("closed")
                end = (m.get("endDate") or "")[:16]
                outs = m.get("outcomes")
                break
        except Exception as e:
            q = f"gamma err {e}"
    print(f"rows {ids}: neg_risk={nr} onchain_resolved={bool(den)} nums={nums}")
    print(f"   {q} | closed={closed} | end={end} | outcomes={outs}")
