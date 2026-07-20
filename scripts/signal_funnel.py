"""One-off: last-24h signal funnel — signals inserted vs copy_trades created."""
import json

from core.db import get_supabase

c = get_supabase()

s = (c.table("trade_signals")
     .select("id,created_at,source_wallet,title,price,size_usdc,consensus")
     .gte("created_at", "2026-07-19T08:00:00Z")
     .order("created_at", desc=True).limit(50).execute())
print(f"signals since 2026-07-19 08:00 UTC: {len(s.data)}")
for r in s.data:
    print(r["created_at"][:16], (r.get("source_wallet") or "")[:10],
          f"px={r.get('price')}", f"${r.get('size_usdc')}",
          str(r.get("title"))[:50])

t = (c.table("copy_trades")
     .select("id,user_id,status,mode,size_usdc,created_at,result,error_msg")
     .gte("created_at", "2026-07-18T00:00:00Z")
     .order("created_at", desc=True).limit(20).execute())
print(f"\ncopy_trades since 2026-07-18: {len(t.data)}")
for r in t.data:
    print(r["created_at"][:16], "id", r["id"], r["status"], r.get("mode"),
          r["size_usdc"], r.get("result"), (r.get("error_msg") or "")[:60])

u = (c.table("users")
     .select("id,telegram_id,copy_enabled,signal_only,copy_paused_until,pause_reason")
     .execute())
print("\nusers:")
for r in u.data:
    print(json.dumps(r, ensure_ascii=False))
