"""BP28 audit part 2: donor-side EV, donor sell behaviour, live 5-min book depth."""
import time
from collections import Counter

import httpx

DONOR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
HDRS = {"User-Agent": "Mozilla/5.0"}

print("=" * 70)
print("A. DONOR ACTIVITY (last ~200 events): does he SELL / manage positions?")
print("=" * 70)
try:
    acts = httpx.get(
        "https://data-api.polymarket.com/activity",
        params={"user": DONOR, "limit": 200},
        headers=HDRS, timeout=20,
    ).json()
    types = Counter((a.get("type"), a.get("side")) for a in acts)
    print("  event types:", dict(types))
    sells = [a for a in acts if a.get("side") == "SELL"]
    print(f"  SELL count: {len(sells)}")
    for s in sells[:10]:
        print(f"    {s.get('timestamp')} {str(s.get('title'))[:50]} px={s.get('price')} usdc={s.get('usdcSize')}")
    buys = [a for a in acts if a.get("side") == "BUY" and a.get("type") == "TRADE"]
    if buys:
        pxs = [float(b.get("price") or 0) for b in buys]
        szs = [float(b.get("usdcSize") or 0) for b in buys]
        print(f"  BUYs: n={len(buys)} avg_px={sum(pxs)/len(pxs):.3f} avg_size=${sum(szs)/len(szs):.2f}")
        # rough donor EV at his own prices using redeem events
        redeems = [a for a in acts if a.get("type") == "REDEEM"]
        print(f"  REDEEM events: {len(redeems)}")
except Exception as e:
    print("  activity fetch failed:", e)

print()
print("=" * 70)
print("B. LIVE 5-MIN CRYPTO MARKETS: order book depth snapshot")
print("=" * 70)
try:
    # gamma text search for active 5-min up/down markets
    found = []
    for q in ("Bitcoin Up or Down", "Ethereum Up or Down", "Solana Up or Down", "XRP Up or Down"):
        try:
            ms = httpx.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false", "limit": 20,
                        "order": "endDate", "ascending": "true", "tag_id": "",
                        "liquidity_num_min": 1},
                headers=HDRS, timeout=20,
            ).json()
        except Exception:
            ms = []
        found.extend(m for m in ms if "up or down" in str(m.get("question", "")).lower())
        if found:
            break
    # fallback: events search
    if not found:
        evs = httpx.get(
            "https://gamma-api.polymarket.com/events",
            params={"active": "true", "closed": "false", "limit": 50,
                    "order": "endDate", "ascending": "true"},
            headers=HDRS, timeout=20,
        ).json()
        for e in evs:
            for m in (e.get("markets") or []):
                q = str(m.get("question", "")).lower()
                if "up or down" in q and ("bitcoin" in q or "ethereum" in q or "sol" in q or "xrp" in q):
                    found.append(m)
    print(f"  candidate markets: {len(found)}")
    import json as _json
    for m in found[:6]:
        q = m.get("question")
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            toks = _json.loads(toks)
        end = m.get("endDate")
        print(f"\n  {q}  end={end}")
        for tok in (toks or [])[:2]:
            try:
                book = httpx.get("https://clob.polymarket.com/book",
                                 params={"token_id": tok}, headers=HDRS, timeout=15).json()
                asks = book.get("asks") or []
                bids = book.get("bids") or []
                if not asks:
                    print(f"    token …{tok[-8:]}: empty ask side")
                    continue
                # CLOB /book returns asks sorted; normalize
                lv = [(float(a["price"]), float(a["size"])) for a in asks]
                lv.sort(key=lambda x: x[0])
                best = lv[0][0]
                for band in (0.02, 0.05, 0.10):
                    lim = best * (1 + band)
                    depth = sum(p * s for p, s in lv if p <= lim)
                    print(f"    token …{tok[-8:]}: best_ask={best:.2f} "
                          f"depth within +{band:.0%}: ${depth:,.0f}")
            except Exception as e:
                print(f"    token …{str(tok)[-8:]}: book error {e}")
except Exception as e:
    print("  live depth check failed:", e)

print()
print("=" * 70)
print("C. DONOR EV AT DONOR PRICES on our settled sample")
print("=" * 70)
from core.db.session import get_supabase
sb = get_supabase()
rows = (
    sb.table("copy_trades")
    .select("id,signal_id,result,size_usdc,entry_price,created_at")
    .eq("mode", "sniper").in_("result", ["win", "loss"])
    .order("created_at", desc=True).limit(150)
    .execute().data
)
# one row per signal (two users copy the same signal)
seen = {}
for r in rows:
    sid = r.get("signal_id")
    if sid and sid not in seen:
        seen[sid] = r
sig_rows = sb.table("trade_signals").select("id,price,size_usdc").in_(
    "id", list(seen.keys())).execute().data
sigmap = {s["id"]: s for s in sig_rows}
n = w = 0
pnl_donor = 0.0
staked = 0.0
for sid, r in seen.items():
    s = sigmap.get(sid)
    if not s:
        continue
    px = float(s.get("price") or 0)
    sz = float(s.get("size_usdc") or 0)
    if px <= 0 or sz <= 0:
        continue
    n += 1
    staked += sz
    if r["result"] == "win":
        w += 1
        pnl_donor += sz * (1.0 / px - 1.0)
    else:
        pnl_donor -= sz
print(f"  unique signals settled: {n}, donor winrate={w / n:.1%}" if n else "no data")
if n:
    avg_px = sum(float(sigmap[s]['price'] or 0) for s in sigmap) / len(sigmap)
    print(f"  donor avg entry px={avg_px:.3f} (breakeven wr={avg_px:.1%})")
    print(f"  hypothetical hold-to-resolution PnL at donor prices: ${pnl_donor:+.2f} on ${staked:.2f}")
