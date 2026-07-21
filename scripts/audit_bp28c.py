"""BP28 audit part 3: signal funnel — how many sniper signals never became entries."""
from datetime import datetime, timedelta, timezone

from core.db.session import get_supabase

sb = get_supabase()
DONOR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

sigs = (
    sb.table("trade_signals").select("id,created_at,price,size_usdc")
    .eq("source_wallet", DONOR).gte("created_at", since)
    .order("created_at").execute().data
)
print(f"sniper signals 7d: {len(sigs)}")

sig_ids = [s["id"] for s in sigs]
trades = []
for i in range(0, len(sig_ids), 100):
    trades += (
        sb.table("copy_trades").select("signal_id,user_id,status")
        .in_("signal_id", sig_ids[i:i + 100]).execute().data
    )
by_sig: dict = {}
for t in trades:
    by_sig.setdefault(t["signal_id"], []).append(t)

no_attempt = attempted_fail = confirmed = 0
missed_px = []
for s in sigs:
    rows = by_sig.get(s["id"], [])
    conf = [r for r in rows if r["status"] == "confirmed"]
    if conf:
        confirmed += 1
    elif rows:
        attempted_fail += 1
        missed_px.append(float(s.get("price") or 0))
    else:
        no_attempt += 1
        missed_px.append(float(s.get("price") or 0))
print(f"signals with >=1 confirmed entry: {confirmed}")
print(f"signals attempted but all failed:  {attempted_fail}")
print(f"signals with NO copy_trade row (skipped pre-insert: drift/timeout/etc): {no_attempt}")
if missed_px:
    print(f"avg donor px of missed signals: {sum(missed_px)/len(missed_px):.3f} "
          f"(n={len(missed_px)}, >=0.85: {sum(1 for p in missed_px if p >= 0.85)})")
