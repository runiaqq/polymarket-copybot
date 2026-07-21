"""One-off BP28 audit: sniper PnL sanity, entry-size chaos, donor stats, book depth."""
from datetime import datetime, timedelta, timezone

from core.db.session import get_supabase

sb = get_supabase()

print("=" * 70)
print("A. SNIPER LEDGER SANITY (shares vs size/entry_price, pnl vs expected)")
print("=" * 70)
rows = (
    sb.table("copy_trades")
    .select("id,user_id,size_usdc,entry_price,shares,result,realized_pnl,created_at")
    .eq("mode", "sniper").in_("result", ["win", "loss"])
    .order("created_at", desc=True).limit(150)
    .execute().data
)
print(f"{len(rows)} settled sniper trades")
bad = 0
for r in rows:
    sz = float(r["size_usdc"] or 0)
    ep = float(r["entry_price"] or 0)
    sh = float(r["shares"] or 0)
    pnl = float(r["realized_pnl"] or 0)
    exp_sh = sz / ep if ep else 0
    exp_pnl = (sh - sz) if r["result"] == "win" else -sz
    ok_sh = abs(sh - exp_sh) < max(1.0, 0.15 * exp_sh)
    ok_pnl = abs(pnl - exp_pnl) < max(0.5, 0.1 * abs(exp_pnl))
    flag = "" if (ok_sh and ok_pnl) else "  <<< MISMATCH"
    if flag:
        bad += 1
    print(f"{r['id']} u{r['user_id']} {r['created_at'][:16]} {r['result']:4} "
          f"sz={sz:7.2f} ep={ep:.2f} sh={sh:8.2f} (exp {exp_sh:7.2f}) "
          f"pnl={pnl:+8.2f} (exp {exp_pnl:+7.2f}){flag}")
print("mismatches:", bad)

print()
print("=" * 70)
print("B. ENTRY-SIZE CHAOS: our size vs donor size per signal")
print("=" * 70)
# join copy_trades -> trade_signals to compare our filled size with donor size
sn = (
    sb.table("copy_trades")
    .select("id,user_id,signal_id,size_usdc,entry_price,status,result,created_at")
    .eq("mode", "sniper").eq("status", "confirmed")
    .order("created_at", desc=True).limit(80)
    .execute().data
)
sig_ids = [r["signal_id"] for r in sn if r.get("signal_id")]
sigs = {}
if sig_ids:
    for s in (sb.table("trade_signals").select("id,size_usdc,price")
              .in_("id", sig_ids).execute().data):
        sigs[s["id"]] = s
ratios = []
for r in sn:
    s = sigs.get(r.get("signal_id"))
    if not s:
        continue
    donor = float(s.get("size_usdc") or 0)
    ours = float(r.get("size_usdc") or 0)
    if donor > 0:
        ratio = ours / donor
        ratios.append(ratio)
        print(f"{r['id']} u{r['user_id']} {r['created_at'][:16]} donor=${donor:7.2f} "
              f"ours=${ours:7.2f} ratio={ratio:5.2f} px_sig={s.get('price')} px_fill={r.get('entry_price')}")
if ratios:
    import statistics
    print(f"\nratio ours/donor: n={len(ratios)} mean={statistics.mean(ratios):.2f} "
          f"median={statistics.median(ratios):.2f} min={min(ratios):.2f} max={max(ratios):.2f}")
    full = sum(1 for x in ratios if x >= 0.9)
    print(f"full mirrors (>=0.9): {full}/{len(ratios)}")

print()
print("=" * 70)
print("C. DONOR/SNIPER STRATEGY STATS from our settled trades")
print("=" * 70)
wins = [r for r in rows if r["result"] == "win"]
losses = [r for r in rows if r["result"] == "loss"]
n = len(wins) + len(losses)
if n:
    wr = len(wins) / n
    avg_px_w = sum(float(r["entry_price"] or 0) for r in wins) / max(len(wins), 1)
    avg_px_l = sum(float(r["entry_price"] or 0) for r in losses) / max(len(losses), 1)
    tot_pnl = sum(float(r["realized_pnl"] or 0) for r in rows)
    tot_staked = sum(float(r["size_usdc"] or 0) for r in rows)
    print(f"n={n} winrate={wr:.1%} avg_entry_px win={avg_px_w:.2f} loss={avg_px_l:.2f}")
    print(f"total staked=${tot_staked:.2f} total pnl=${tot_pnl:+.2f} "
          f"ROI per trade={tot_pnl / tot_staked:+.1%}" if tot_staked else "")
    # breakeven winrate at avg entry price (ignoring fees)
    all_px = [float(r["entry_price"] or 0) for r in rows if r.get("entry_price")]
    avg_px = sum(all_px) / len(all_px)
    print(f"avg entry price={avg_px:.3f} -> breakeven winrate (no fees)={avg_px:.1%}, "
          f"with ~1.5% taker fee ≈ {avg_px * 1.015:.1%}")

print()
print("=" * 70)
print("D. DAILY PNL of sniper (worst day / drawdown estimate)")
print("=" * 70)
daily: dict = {}
for r in rows:
    d = r["created_at"][:10]
    daily.setdefault(d, {"pnl": 0.0, "n": 0, "staked": 0.0})
    daily[d]["pnl"] += float(r["realized_pnl"] or 0)
    daily[d]["n"] += 1
    daily[d]["staked"] += float(r["size_usdc"] or 0)
for d in sorted(daily):
    v = daily[d]
    print(f"  {d}: n={v['n']:3d} staked=${v['staked']:8.2f} pnl={v['pnl']:+8.2f}")
