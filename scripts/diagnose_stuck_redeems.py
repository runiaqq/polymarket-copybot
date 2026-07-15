"""One-off: for every stuck (confirmed, unredeemed) copy_trade, determine
on-chain which (collateral, outcome_index) actually matches the held token —
explains the collateral_unmatched redeem skips and any wrong stored index."""
from web3 import Web3

from core.clob import CONDITIONAL_TOKENS, PUSD_ADDRESS
from core.config import settings
from core.db import get_supabase
from core.relayer import _CTF_ABI, USDC_BRIDGED, USDC_NATIVE, _wrapped_collateral

w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
ctf = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=_CTF_ABI)

CANDS = {"pUSD": PUSD_ADDRESS, "USDC.e": USDC_BRIDGED, "USDCn": USDC_NATIVE}
wcol = _wrapped_collateral()
if wcol:
    CANDS["WCOL"] = wcol
print("wcol resolved:", wcol)

sb = get_supabase()
rows = (sb.table("copy_trades")
        .select("id,user_id,condition_id,token_id,outcome_index,size_usdc,mode,created_at")
        .eq("status", "confirmed").is_("redeemed_at", "null")
        .order("created_at").execute()).data or []
print("stuck rows:", len(rows))

for t in rows:
    cond_b = Web3.to_bytes(hexstr=t["condition_id"])
    asset = int(t["token_id"])
    stored_idx = t["outcome_index"]
    try:
        den = ctf.functions.payoutDenominator(cond_b).call()
        nums = [ctf.functions.payoutNumerators(cond_b, i).call() for i in (0, 1)] if den else None
    except Exception as e:
        print(f"id={t['id']:5d} RPC error: {e}")
        continue
    match = None
    for idx in (0, 1):
        coll_id = ctf.functions.getCollectionId(b"\x00" * 32, cond_b, 1 << idx).call()
        for name, coll in CANDS.items():
            pid = int(ctf.functions.getPositionId(Web3.to_checksum_address(coll), coll_id).call())
            if pid == asset:
                match = (name, idx)
                break
        if match:
            break
    won_stored = nums and nums[stored_idx] > 0
    won_real = nums and match and nums[match[1]] > 0
    print(f"id={t['id']:5d} u={t['user_id']} idx_stored={stored_idx} "
          f"match={match} resolved={bool(den)} nums={nums} "
          f"won_by_stored_idx={won_stored} won_by_real_idx={won_real} "
          f"${t['size_usdc']} {t['created_at'][:16]}")
