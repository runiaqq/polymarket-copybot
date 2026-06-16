"""
Discover QUALITY directional traders for the copy whitelist.

Pulls the official profit leaderboard, scores each candidate's real track
record, and filters OUT arbitrage / hedging bots, keeping only genuine
directional bettors. Used by both the CLI seeder and the admin `/refresh`
command (weekly whitelist refresh).

Heuristics that separate directional traders from arbitrage/hedge bots:
  * profit/volume ratio — THE primary discriminator. Market makers/churners earn
    big absolute profit only because they trade enormous volume (ratio ~1-4%);
    directional bettors clear 10%+. Computed by cross-referencing the profit and
    volume leaderboards (no per-wallet scraping). Empirically separates skk1ch /
    swisstony (MMs, ~4%) from mintblade / fishalive / weatherman12 (14-68%).
  * avg profit per resolved market — arbitrageurs earn cents across thousands
    of markets; directional traders earn hundreds/thousands on conviction wins.
  * winrate — pure arbitrage/hedging sits near 0.95-1.0 (near-riskless);
    real directional edge lives around 0.5-0.9.
  * resolved_count — absurdly high counts signal industrial arbitrage.
  * recency — must be actively trading (last trade within a few days).
  * 30d consistency — must appear on the 30d profit board, not just a 7d spike.
"""

import time

import structlog

from core.config import settings

log = structlog.get_logger(__name__)

_ACT_URL = "https://data-api.polymarket.com/activity"
_H = {"User-Agent": "Mozilla/5.0 (PolyMind seeder)"}

# Quality thresholds (anti-arbitrage / anti-hedge / anti-market-maker).
MIN_REALIZED = 20_000      # meaningful realized P&L ($)
MIN_RESOLVED = 25          # real track record
MAX_RESOLVED = 2_000       # above this = industrial arbitrage
MIN_AVG_PER_MARKET = 120   # directional conviction, not cent-scalping ($)
WINRATE_LO = 0.45
WINRATE_HI = 0.90          # exclude near-riskless arb/hedge
MAX_LAST_DAYS = 5          # must be trading recently
MAX_REWARDS = 2            # liquidity rewards in feed => MM/LP (secondary signal)


def _profit_volume_ratio(pnl: float, volume: float | None) -> float | None:
    """Profit / traded-volume. None when we can't compute it (wallet not on the
    volume board, or no known profit) — caller then falls back to other signals
    rather than wrongly excluding a low-volume directional trader."""
    if not volume or volume <= 0:
        return None
    if pnl <= 0:
        return None
    return pnl / volume


def _activity_profile(addr: str) -> dict:
    """Recency + market-maker fingerprint from a wallet's activity feed.

    Liquidity providers / market makers receive MAKER_REBATE and REWARD payouts
    (liquidity rewards) — directional takers never do. That's the cleanest way
    to tell a copy-worthy bettor from an MM that just farms the spread.
    """
    import httpx
    try:
        r = httpx.get(_ACT_URL, params={"user": addr, "limit": 200}, headers=_H, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            rows = rows.get("data", [])
    except Exception:
        return {"last_days": 999.0, "is_mm": False, "trades": 0}
    now = time.time()
    last = 0
    maker_rebate = rewards = trades = 0
    for a in rows:
        t = a.get("type")
        if t == "TRADE":
            trades += 1
            last = max(last, int(a.get("timestamp") or 0))
        elif t == "MAKER_REBATE":
            maker_rebate += 1
        elif t == "REWARD":
            rewards += 1
    return {
        "last_days": round((now - last) / 86400, 1) if last else 999.0,
        "is_mm": maker_rebate > 0 or rewards > MAX_REWARDS,
        "trades": trades,
    }


def discover_quality(target: int = 20, add: bool = True) -> dict:
    """Scan leaderboard, filter to quality directional traders, optionally add
    them to the whitelist. Returns a summary dict for reporting.

    Blocking (network + DB) — call via asyncio.to_thread from async code.
    """
    from core.db import (
        add_tracked_wallet,
        list_tracked_wallets,
        remove_tracked_wallet,
    )
    from core.leaderboard import top_profit_wallets, volume_map
    from core.wallet_score import score_wallet

    min_ratio = settings.discovery_min_profit_volume_ratio
    c30 = top_profit_wallets("30d", 150)
    c7 = top_profit_wallets("7d", 100)
    vmap = volume_map(("7d", "30d"))  # {wallet: traded volume} for MM cross-ref
    # Require consistent 30d profitability — a wallet that spikes in 7d but is
    # absent from the 30d board is usually an MM/arb, not a durable edge.
    addrs30 = {w["wallet"].lower() for w in c30}
    # Best (max) leaderboard profit per wallet, for the profit/volume ratio.
    pnl_map: dict[str, float] = {}
    by_addr: dict[str, dict] = {}
    for w in c30 + c7:
        a = w["wallet"].lower()
        pnl_map[a] = max(pnl_map.get(a, 0.0), float(w.get("pnl") or 0))
        by_addr.setdefault(a, {"wallet": w["wallet"], "name": w["name"]})

    existing = {w["address"].lower() for w in list_tracked_wallets()}
    qualified: list[dict] = []

    for addr, meta in by_addr.items():
        if addr not in addrs30:
            continue
        # Primary MM filter: high profit on huge volume = churner, not a bettor.
        ratio = _profit_volume_ratio(pnl_map.get(addr, 0.0), vmap.get(addr))
        if ratio is not None and ratio < min_ratio:
            continue
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
        prof = _activity_profile(addr)
        time.sleep(0.05)
        if prof["is_mm"] or prof["last_days"] > MAX_LAST_DAYS:
            continue
        qualified.append({
            "wallet": meta["wallet"], "name": meta["name"],
            "realized": realized, "winrate": winrate,
            "ratio": ratio,
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

    # Hygiene: drop already-tracked wallets that turn out to be market makers /
    # churners — a low profit/volume ratio on the leaderboards is the giveaway
    # (e.g. skk1ch / swisstony at ~4%). Falls back to the activity feed when the
    # wallet isn't on the volume board.
    qualified_addrs = {p["wallet"].lower() for p in qualified}
    removed: list[dict] = []
    if add:
        for w in list_tracked_wallets():
            a = w["address"].lower()
            if a in qualified_addrs:
                continue
            ratio = _profit_volume_ratio(pnl_map.get(a, 0.0), vmap.get(a))
            is_mm = ratio is not None and ratio < min_ratio
            if not is_mm:
                prof = _activity_profile(a)
                time.sleep(0.05)
                is_mm = prof["is_mm"]
            if is_mm:
                try:
                    remove_tracked_wallet(w["address"])
                    removed.append({"wallet": w["address"], "name": w.get("name") or ""})
                except Exception:
                    log.warning("prune_remove_failed", wallet=w["address"])

    total = len(list_tracked_wallets()) if add else len(existing)
    return {
        "scanned": len(by_addr),
        "qualified": len(qualified),
        "added": added,
        "kept": kept,
        "removed": removed,
        "total": total,
    }
