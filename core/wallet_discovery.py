"""
Discover QUALITY directional traders for the copy whitelist.

Pulls the official profit leaderboard, scores each candidate's real track
record, and filters OUT arbitrage / hedging bots, keeping only genuine
directional bettors. Used by both the CLI seeder and the admin `/refresh`
command (weekly whitelist refresh).

Heuristics that separate directional traders from arbitrage/hedge bots:
  * avg profit per resolved market — arbitrageurs earn cents across thousands
    of markets; directional traders earn hundreds/thousands on conviction wins.
  * winrate — pure arbitrage/hedging sits near 0.95-1.0 (near-riskless);
    real directional edge lives around 0.5-0.9.
  * resolved_count — absurdly high counts signal industrial arbitrage.
  * recency — must be actively trading (last trade within a few days).
"""

import time

import structlog

log = structlog.get_logger(__name__)

_ACT_URL = "https://data-api.polymarket.com/activity"
_H = {"User-Agent": "Mozilla/5.0 (PolyMind seeder)"}

# Quality thresholds (anti-arbitrage / anti-hedge).
MIN_REALIZED = 20_000      # meaningful realized P&L ($)
MIN_RESOLVED = 25          # real track record
MAX_RESOLVED = 2_000       # above this = industrial arbitrage
MIN_AVG_PER_MARKET = 120   # directional conviction, not cent-scalping ($)
WINRATE_LO = 0.45
WINRATE_HI = 0.90          # exclude near-riskless arb/hedge
MAX_LAST_DAYS = 5          # must be trading recently


def _last_trade_days(addr: str) -> float:
    import httpx
    try:
        r = httpx.get(_ACT_URL, params={"user": addr, "limit": 50}, headers=_H, timeout=15)
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


def discover_quality(target: int = 20, add: bool = True) -> dict:
    """Scan leaderboard, filter to quality directional traders, optionally add
    them to the whitelist. Returns a summary dict for reporting.

    Blocking (network + DB) — call via asyncio.to_thread from async code.
    """
    from core.db import add_tracked_wallet, list_tracked_wallets
    from core.leaderboard import top_profit_wallets
    from core.wallet_score import score_wallet

    c30 = top_profit_wallets("30d", 150)
    c7 = top_profit_wallets("7d", 100)
    by_addr: dict[str, dict] = {}
    for w in c30 + c7:
        by_addr.setdefault(w["wallet"].lower(), {"wallet": w["wallet"], "name": w["name"]})

    existing = {w["address"].lower() for w in list_tracked_wallets()}
    qualified: list[dict] = []

    for addr, meta in by_addr.items():
        s = score_wallet(addr)
        time.sleep(0.05)
        realized = s["realized_pnl"]
        resolved = s["resolved_count"]
        winrate = s["winrate"]
        avg = realized / resolved if resolved else 0

        if not (realized >= MIN_REALIZED
                and MIN_RESOLVED <= resolved <= MAX_RESOLVED
                and avg >= MIN_AVG_PER_MARKET
                and WINRATE_LO <= winrate <= WINRATE_HI):
            continue
        if _last_trade_days(addr) > MAX_LAST_DAYS:
            continue
        time.sleep(0.05)
        qualified.append({
            "wallet": meta["wallet"], "name": meta["name"],
            "realized": realized, "winrate": winrate,
            "existing": addr in existing,
        })

    qualified.sort(key=lambda p: p["realized"], reverse=True)
    qualified = qualified[:target]

    added, kept = [], []
    for p in qualified:
        if p["existing"]:
            kept.append(p)
            continue
        if add:
            try:
                add_tracked_wallet(p["wallet"], (p["name"] or "")[:30] or None)
            except Exception:
                log.warning("seed_add_failed", wallet=p["wallet"])
                continue
        added.append(p)

    total = len(list_tracked_wallets()) if add else len(existing)
    return {
        "scanned": len(by_addr),
        "qualified": len(qualified),
        "added": added,
        "kept": kept,
        "total": total,
    }
