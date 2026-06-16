"""
Seed the tracked_wallets whitelist with QUALITY directional traders.

Pulls the official profit leaderboard, then scores each candidate's real
track record and filters OUT arbitrage / hedging bots, keeping only genuine
directional bettors. Heuristics that separate the two:

  * avg profit per resolved market — arbitrageurs earn cents across thousands
    of markets; directional traders earn hundreds/thousands on conviction wins.
  * winrate — pure arbitrage/hedging sits at ~0.95-1.0 (near-riskless); real
    directional edge lives around 0.5-0.9.
  * resolved_count — absurdly high counts (>2000) signal industrial arbitrage.
  * recency — must be actively trading (last trade within a few days).

Run INSIDE the server container (has DB creds + non-geoblocked network):
    docker compose exec api python -m scripts.seed_quality
    docker compose exec api python -m scripts.seed_quality 20   # target count
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from core.db import add_tracked_wallet, list_tracked_wallets
from core.leaderboard import top_profit_wallets
from core.wallet_score import score_wallet

ACT_URL = "https://data-api.polymarket.com/activity"
H = {"User-Agent": "Mozilla/5.0 (PolyMind seeder)"}

# Quality thresholds (anti-arbitrage / anti-hedge).
MIN_REALIZED = 20_000      # meaningful realized P&L ($)
MIN_RESOLVED = 25          # real track record
MAX_RESOLVED = 2_000       # above this = industrial arbitrage
MIN_AVG_PER_MARKET = 120   # directional conviction, not cent-scalping ($)
WINRATE_LO = 0.45
WINRATE_HI = 0.90          # exclude near-riskless arb/hedge
MAX_LAST_DAYS = 5          # must be trading recently


def last_trade_days(addr: str) -> float:
    try:
        r = httpx.get(ACT_URL, params={"user": addr, "limit": 50}, headers=H, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            rows = rows.get("data", [])
    except Exception:
        return 999.0
    now = time.time()
    last = 0
    for a in rows:
        if a.get("type") == "TRADE":
            last = max(last, int(a.get("timestamp") or 0))
    return round((now - last) / 86400, 1) if last else 999.0


def main() -> None:
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    print("Fetching profit leaderboards (30d + 7d)…")
    c30 = top_profit_wallets("30d", 150)
    c7 = top_profit_wallets("7d", 100)
    by_addr: dict[str, dict] = {}
    for w in c30 + c7:
        by_addr.setdefault(w["wallet"].lower(), {"wallet": w["wallet"], "name": w["name"]})
    print(f"  {len(by_addr)} unique candidates\n")

    already = {w["address"].lower() for w in list_tracked_wallets()}
    picks: list[dict] = []

    print(f"{'WALLET':<44}{'realP&L$':>11}{'res':>6}{'win':>6}{'avg$':>8}{'lastd':>7}  ok")
    print("=" * 92)
    for addr, meta in by_addr.items():
        if addr in already:
            continue
        s = score_wallet(addr)
        time.sleep(0.1)
        realized = s["realized_pnl"]
        resolved = s["resolved_count"]
        winrate = s["winrate"]
        avg = realized / resolved if resolved else 0

        ok = (
            realized >= MIN_REALIZED
            and MIN_RESOLVED <= resolved <= MAX_RESOLVED
            and avg >= MIN_AVG_PER_MARKET
            and WINRATE_LO <= winrate <= WINRATE_HI
        )
        last_d = 999.0
        if ok:
            last_d = last_trade_days(addr)
            time.sleep(0.1)
            ok = last_d <= MAX_LAST_DAYS

        flag = "✅" if ok else "  "
        print(f"{addr:<44}{realized:>11,.0f}{resolved:>6}{winrate:>6.2f}"
              f"{avg:>8,.0f}{last_d:>7}  {flag}")
        if ok:
            picks.append({"wallet": meta["wallet"], "name": meta["name"],
                          "realized": realized, "winrate": winrate})

    picks.sort(key=lambda p: p["realized"], reverse=True)
    picks = picks[:target]

    print("\n" + "=" * 60)
    print(f"Adding {len(picks)} quality wallets to the whitelist:\n")
    for p in picks:
        label = (p["name"] or "")[:30] or None
        add_tracked_wallet(p["wallet"], label)
        print(f"  + {p['wallet']}  {p['name'] or ''}  "
              f"(${p['realized']:,.0f}, win {p['winrate']:.0%})")

    print(f"\nDone. Whitelist now has {len(list_tracked_wallets())} active wallets.")


if __name__ == "__main__":
    main()
