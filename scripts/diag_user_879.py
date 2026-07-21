"""One-off: diagnose user tg=879714159 (uid=2) — stuck resolution + missing sniper entries."""
import json
from datetime import datetime, timedelta, timezone

import httpx

from core.db.session import get_supabase

sb = get_supabase()
UID = 2

print("=== OPEN (confirmed) TRADES ===")
rows = (
    sb.table("copy_trades")
    .select("id,status,mode,condition_id,token_id,outcome_index,size_usdc,entry_price,"
            "result,resolved_at,redeemed_at,created_at,neg_risk,shares")
    .eq("user_id", UID).eq("status", "confirmed")
    .order("created_at", desc=True).limit(300)
    .execute().data
)
print(f"  confirmed rows: {len(rows)}")
for r in rows[:40]:
    print(f"  id={r['id']} {r['created_at'][:16]} mode={r.get('mode')} "
          f"res={r.get('result')} resolved={bool(r.get('resolved_at'))} "
          f"redeemed={bool(r.get('redeemed_at'))} ${r.get('size_usdc'):.2f} px={r.get('entry_price')} "
          f"cond={r['condition_id'][:14]}…")

print("\n=== WON BUT NOT REDEEMED (any status, 30d) ===")
since30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
wins = (
    sb.table("copy_trades")
    .select("id,status,mode,condition_id,token_id,outcome_index,size_usdc,result,"
            "resolved_at,redeemed_at,created_at,error_msg")
    .eq("user_id", UID).eq("result", "won").is_("redeemed_at", "null")
    .gte("created_at", since30).order("created_at", desc=True).limit(50)
    .execute().data
)
for r in wins:
    print(f"  id={r['id']} st={r['status']} {r['created_at'][:16]} ${r.get('size_usdc'):.2f} "
          f"cond={r['condition_id'][:16]}… err={(r.get('error_msg') or '')[:70]}")

print("\n=== GAMMA + ON-CHAIN for open conditions ===")
conds = sorted({r["condition_id"] for r in rows if not r.get("resolved_at") and r.get("condition_id")}
               | {r["condition_id"] for r in wins if r.get("condition_id")})
from web3 import Web3
from core.config import settings
w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
CTF = w3.eth.contract(
    address=Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
    abi=json.loads('[{"inputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"name":"payoutDenominator","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"bytes32","name":"","type":"bytes32"},{"internalType":"uint256","name":"","type":"uint256"}],"name":"payoutNumerators","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'),
)
for cond in conds:
    line = f"  {cond[:16]}…"
    try:
        den = CTF.functions.payoutDenominator(bytes.fromhex(cond[2:])).call()
        nums = [CTF.functions.payoutNumerators(bytes.fromhex(cond[2:]), i).call()
                for i in (0, 1)] if den else None
        line += f" onchain_den={den} nums={nums}"
    except Exception as e:
        line += f" onchain_err={e}"
    try:
        g = httpx.get("https://gamma-api.polymarket.com/markets",
                      params={"condition_ids": cond, "closed": "true"},
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
        if not g:
            g = httpx.get("https://gamma-api.polymarket.com/markets",
                          params={"condition_ids": cond},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
        if g:
            m = g[0]
            line += (f" | gamma closed={m.get('closed')} uma={m.get('umaResolutionStatus')} "
                     f"prices={m.get('outcomePrices')} q={str(m.get('question'))[:45]}")
        else:
            line += " | gamma NOT FOUND"
    except Exception as e:
        line += f" | gamma_err={e}"
    print(line)

print("\n=== BALANCES ===")
try:
    from core.polygon import get_balances
    print("  EOA   :", get_balances("0x34f29c90597A6eE525d95227a6B398A193aBb57b"))
    print("  DEPOSIT:", get_balances("0xCC77339253Da0EAcE7d79D73271aaBcF83faA20e"))
except Exception as e:
    print("  balance err:", e)

print("\n=== SNIPER TRADES 3d (uid=2) ===")
since3 = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
sn = (
    sb.table("copy_trades")
    .select("id,status,size_usdc,entry_price,result,created_at,error_msg")
    .eq("user_id", UID).eq("mode", "sniper")
    .gte("created_at", since3).order("created_at", desc=True).limit(60)
    .execute().data
)
print(f"  rows: {len(sn)}")
for r in sn:
    print(f"  {r['created_at'][:16]} st={r['status']} res={r.get('result')} "
          f"${(r.get('size_usdc') or 0):.2f} px={r.get('entry_price')} "
          f"err={(r.get('error_msg') or '')[:80]}")
