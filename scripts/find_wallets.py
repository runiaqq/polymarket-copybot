"""
Discover profitable, ACTIVE Polymarket wallets for the copy whitelist.

Uses Polymarket's OFFICIAL profit leaderboard (lb-api) as the truth for P&L,
cross-referenced with recent trade activity so we only pick wallets that are
both profitable AND trading frequently right now.

Run from repo root:
    python scripts/find_wallets.py
    python scripts/find_wallets.py 300 90   # leaderboard limit, candidates to check
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

LB_URL = "https://lb-api.polymarket.com/profit"
ACT_URL = "https://data-api.polymarket.com/activity"
H = {"User-Agent": "Mozilla/5.0 (PolyMind finder)"}


def leaderboard(window: str, limit: int) -> dict[str, float]:
    try:
        r = httpx.get(LB_URL, params={"window": window, "limit": limit}, headers=H, timeout=20)
        r.raise_for_status()
        return {row["proxyWallet"]: float(row.get("amount") or 0)
                for row in r.json() if row.get("proxyWallet")}
    except Exception as e:
        print(f"  leaderboard {window} failed: {e}")
        return {}


def recent_activity(addr: str) -> dict:
    """Count BUY trades in the last 24h / 48h and how many are on SHORT markets."""
    try:
        r = httpx.get(ACT_URL, params={"user": addr, "limit": 100}, headers=H, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            rows = rows.get("data", [])
    except Exception:
        return {"t24": 0, "t48": 0, "last_days": 999}
    now = time.time()
    t24 = t48 = 0
    last_ts = 0
    for a in rows:
        if a.get("type") != "TRADE":
            continue
        ts = int(a.get("timestamp") or 0)
        last_ts = max(last_ts, ts)
        age = now - ts
        if age <= 86400:
            t24 += 1
        if age <= 172800:
            t48 += 1
    return {"t24": t24, "t48": t48, "last_days": round((now - last_ts) / 86400, 1) if last_ts else 999}


def main() -> None:
    lb_limit = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    max_check = int(sys.argv[2]) if len(sys.argv) > 2 else 90

    print(f"Fetching profit leaderboards (7d & 30d, limit {lb_limit})…")
    p7 = leaderboard("7d", lb_limit)
    p30 = leaderboard("30d", lb_limit)
    print(f"  7d: {len(p7)} wallets · 30d: {len(p30)} wallets")

    # Candidates: profitable over the last 7 days (recent edge), ranked by 7d profit.
    cands = sorted([(w, pnl) for w, pnl in p7.items() if pnl > 0],
                   key=lambda kv: kv[1], reverse=True)[:max_check]
    print(f"  checking activity for top {len(cands)} by 7d profit…\n")

    results = []
    for w, pnl7 in cands:
        act = recent_activity(w)
        time.sleep(0.15)
        active = act["t24"] >= 3 or act["t48"] >= 6   # ~several trades/day
        results.append({
            "wallet": w, "p7": pnl7, "p30": p30.get(w, 0),
            "t24": act["t24"], "t48": act["t48"], "last": act["last_days"],
            "good": active and pnl7 > 0 and p30.get(w, 0) > 0,
        })

    results.sort(key=lambda r: (r["good"], r["t48"]), reverse=True)

    print("=" * 96)
    print(f"{'#':<3}{'WALLET':<44}{'7d P&L$':>11}{'30d P&L$':>11}{'24h':>5}{'48h':>5}{'last_d':>7} pick")
    print("=" * 96)
    for i, r in enumerate(results[:40], 1):
        pick = "✅" if r["good"] else "  "
        print(f"{i:<3}{r['wallet']:<44}{r['p7']:>11,.0f}{r['p30']:>11,.0f}"
              f"{r['t24']:>5}{r['t48']:>5}{r['last']:>7}  {pick}")

    picks = [r["wallet"] for r in results if r["good"]]
    print("\n✅ Recommended whitelist (profitable 7d+30d AND actively trading):")
    for w in picks[:30]:
        print(w)
    print(f"\nTotal picks: {len(picks)}")


if __name__ == "__main__":
    main()
