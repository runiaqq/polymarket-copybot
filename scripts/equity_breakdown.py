"""One-off: what the cost-basis equity is made of for users 1 and 2."""
from core.db import get_supabase, get_open_trades_cost
from core.polygon import get_balances
from core.polymarket import get_positions

c = get_supabase()
for uid in (1, 2):
    u = (c.table("users").select("username,deposit_wallet_address")
         .eq("id", uid).maybe_single().execute().data)
    dw = u["deposit_wallet_address"]
    free = get_balances(dw).get("pusd", 0.0)
    pos = get_positions(dw)
    ledger = get_open_trades_cost(uid)
    print(f"=== user {uid} {u['username']} free={free:.2f} ledger_open={sum(ledger.values()):.2f}")
    total = 0.0
    for p in pos:
        if p.get("shares", 0) <= 0:
            continue
        tid = p.get("token_id") or ""
        in_ledger = tid in ledger
        if in_ledger:
            cost = ledger[tid]
        elif p.get("shares") and p.get("avg_price"):
            cost = float(p["shares"]) * float(p["avg_price"])
        else:
            cost = float(p.get("current_value") or 0)
        total += cost
        title = str(p.get("title"))[:42]
        curval = float(p.get("current_value") or 0)
        print(f"  cost={cost:8.2f} curval={curval:8.2f} ledger={in_ledger} "
              f"redeemable={p.get('redeemable')} {title}")
    print(f"  open_cost_total={total:.2f} equity={free + total:.2f}")
