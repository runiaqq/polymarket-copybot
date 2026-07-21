"""BP28 audit part 4: does filling BELOW the donor's price predict losses?"""
from core.db.session import get_supabase

sb = get_supabase()
rows = (
    sb.table("copy_trades")
    .select("id,signal_id,result,entry_price,size_usdc,realized_pnl")
    .eq("mode", "sniper").in_("result", ["win", "loss"])
    .order("created_at", desc=True).limit(150)
    .execute().data
)
sids = list({r["signal_id"] for r in rows if r.get("signal_id")})
sigs = {}
for i in range(0, len(sids), 100):
    for s in (sb.table("trade_signals").select("id,price")
              .in_("id", sids[i:i + 100]).execute().data):
        sigs[s["id"]] = float(s.get("price") or 0)

buckets = {"below_-5%": [], "-5..-2%": [], "near(+-2%)": [], "above_+2%": []}
for r in rows:
    sp = sigs.get(r.get("signal_id"), 0)
    fp = float(r.get("entry_price") or 0)
    if sp <= 0 or fp <= 0:
        continue
    d = (fp - sp) / sp
    if d < -0.05:
        b = "below_-5%"
    elif d < -0.02:
        b = "-5..-2%"
    elif d <= 0.02:
        b = "near(+-2%)"
    else:
        b = "above_+2%"
    buckets[b].append(r)

for b, rs in buckets.items():
    if not rs:
        print(f"{b:12}: n=0")
        continue
    w = sum(1 for r in rs if r["result"] == "win")
    pnl = sum(float(r["realized_pnl"] or 0) for r in rs)
    staked = sum(float(r["size_usdc"] or 0) for r in rs)
    print(f"{b:12}: n={len(rs):3d} winrate={w/len(rs):5.1%} pnl={pnl:+8.2f} staked={staked:8.2f} roi={pnl/staked:+.1%}")
