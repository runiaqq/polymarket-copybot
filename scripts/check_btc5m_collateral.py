"""One-off: figure out which collateral the 5-min BTC market tokens are bound to.

Replicates redeem_winnings' positionId detection for a recent donor market to
explain the collateral_unmatched redeem skips.
"""
import httpx
from web3 import Web3

from core.config import settings
from core.clob import CONDITIONAL_TOKENS, NEG_RISK_ADAPTER, PUSD_ADDRESS
from core.relayer import _CTF_ABI, USDC_BRIDGED, USDC_NATIVE, _wrapped_collateral

ADDR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

r = httpx.get("https://data-api.polymarket.com/activity",
              params={"user": ADDR, "limit": 60}, headers=H, timeout=20)
r.raise_for_status()
trades = [t for t in r.json()
          if t.get("type") == "TRADE" and "btc-updown" in (t.get("eventSlug") or "")]
t = trades[0]
cond = t["conditionId"]
asset = int(t["asset"])
idx = int(t.get("outcomeIndex") or 0)
print("market:", t.get("eventSlug"), "| outcome:", t.get("outcome"), "idx:", idx)
print("cond:", cond)
print("asset:", str(asset)[:40], "...")

w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
ctf = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=_CTF_ABI)
cond_b = Web3.to_bytes(hexstr=cond)

candidates = {
    "pUSD": PUSD_ADDRESS,
    "USDC.e": USDC_BRIDGED,
    "USDC_native": USDC_NATIVE,
}
wcol = _wrapped_collateral()
if wcol:
    candidates["WCOL(negrisk)"] = wcol

matched = None
for both_idx in (idx, 1 - idx):
    coll_id = ctf.functions.getCollectionId(b"\x00" * 32, cond_b, 1 << both_idx).call()
    for name, coll in candidates.items():
        pid = int(ctf.functions.getPositionId(Web3.to_checksum_address(coll), coll_id).call())
        hit = "  <-- MATCH" if pid == asset else ""
        if pid == asset:
            matched = (name, both_idx)
        print(f"idx={both_idx} {name:14s} positionId={str(pid)[:24]}...{hit}")

print()
print("payoutDenominator:", ctf.functions.payoutDenominator(cond_b).call())
print("RESULT:", matched or "NO MATCH — this is the collateral_unmatched cause")
