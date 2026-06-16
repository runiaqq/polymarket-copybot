"""
Seed the tracked_wallets whitelist with QUALITY directional traders.

Thin CLI wrapper around core.wallet_discovery.discover_quality (same logic the
admin /refresh command uses). Filters out arbitrage / hedging bots.

Run INSIDE the server container (has DB creds + non-geoblocked network):
    docker compose exec api python scripts/seed_quality.py
    docker compose exec api python scripts/seed_quality.py 15   # target count
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.wallet_discovery import discover_quality


def main() -> None:
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    print(f"Scanning leaderboard for quality directional traders (target {target})…\n")
    r = discover_quality(target=target, add=True)

    print(f"Scanned {r['scanned']} candidates · qualified {r['qualified']}\n")
    if r["added"]:
        print("Added:")
        for p in r["added"]:
            print(f"  + {p['wallet']}  {p['name'] or ''}  "
                  f"(${p['realized']:,.0f}, win {p['winrate']:.0%})")
    if r["kept"]:
        print("\nAlready present:")
        for p in r["kept"]:
            print(f"  = {p['wallet']}  {p['name'] or ''}")
    print(f"\nDone. Whitelist now has {r['total']} active wallets.")


if __name__ == "__main__":
    main()
