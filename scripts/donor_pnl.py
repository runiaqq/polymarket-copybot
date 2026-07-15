"""One-off: donor full-period PnL from Data-API activity (buys vs redeems+sells)."""
import collections
import time

import httpx

ADDR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

rows = []
for offset in range(0, 3000, 500):
    r = httpx.get("https://data-api.polymarket.com/activity",
                  params={"user": ADDR, "limit": 500, "offset": offset},
                  headers=H, timeout=20)
    r.raise_for_status()
    b = r.json()
    rows += b
    if len(b) < 500:
        break

per = collections.defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "redeem": 0.0, "ts": 0})
for t in rows:
    m = t.get("conditionId") or "?"
    u = float(t.get("usdcSize") or 0)
    per[m]["ts"] = max(per[m]["ts"], t.get("timestamp") or 0)
    if t.get("type") == "TRADE" and t.get("side") == "BUY":
        per[m]["buy"] += u
    elif t.get("type") == "TRADE" and t.get("side") == "SELL":
        per[m]["sell"] += u
    elif t.get("type") == "REDEEM":
        per[m]["redeem"] += u

now = time.time()
closed = {m: v for m, v in per.items() if now - v["ts"] > 1800 and v["buy"] > 0}
wins = sum(1 for v in closed.values() if v["redeem"] + v["sell"] > v["buy"])
returned = sum(v["redeem"] + v["sell"] for v in closed.values())
spent = sum(v["buy"] for v in closed.values())
pnl = returned - spent

print("closed markets:", len(closed), "| wins:", wins, f"({100*wins/len(closed):.1f}%)")
print("total spent:", round(spent, 2), "| returned:", round(returned, 2))
print(f"NET PnL over period: {pnl:+.2f} USDC | ROI: {100*pnl/spent:+.2f}%")

buckets = collections.defaultdict(lambda: [0.0, 0])
for m, v in closed.items():
    wk = int((now - v["ts"]) // (7 * 86400))
    buckets[wk][0] += v["redeem"] + v["sell"] - v["buy"]
    buckets[wk][1] += 1
for wk in sorted(buckets):
    print(f"  week -{wk}: pnl={buckets[wk][0]:+8.2f} over {buckets[wk][1]:3d} markets")
