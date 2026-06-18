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

# Quality thresholds.
#
# The leaderboard ranks by TOTAL P&L (largely unrealized — open positions), so
# the closed-positions track record is the WRONG gate: the best directional
# whales (mintblade +$9.2M, fishalive +$9.1M) hold their edge in open positions
# and show realized≈0 / few resolved markets. Gating on realized P&L or a 0.90
# winrate cap wiped them out (only 1/79 survived). The reliable signals are:
#   * leaderboard profit (proven edge),
#   * profit/volume ratio (separates directional bettors from MMs/arb/churn),
#   * 30d consistency + recent activity.
MIN_LB_PNL = 50_000        # minimum leaderboard profit to bother copying ($)
MIN_RESOLVED_LIGHT = 3     # light anti-luck floor for wallets we can't ratio-check
MAX_LAST_DAYS = 7          # must be trading recently
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

    Also computes trade density (trades/day) and average trade size from the
    `usdcSize` field: MMs place 30-200 tiny orders/day, real whales place a
    handful of large conviction bets.
    """
    import httpx
    try:
        r = httpx.get(_ACT_URL, params={"user": addr, "limit": 200}, headers=_H, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            rows = rows.get("data", [])
    except Exception:
        return {"last_days": 999.0, "is_mm": False, "trades": 0,
                "trades_per_day": 0.0, "avg_size": 0.0,
                "directionality": None, "max_event_outcomes": 0}

    now = time.time()
    first_ts = last_ts = 0
    maker_rebate = rewards = trades = 0
    total_size = 0.0
    # Directionality: track BUY volume split by YES/NO per market.
    # D = |V_yes - V_no| / (V_yes + V_no) per market, averaged across markets.
    # MMs score near 0 (equal YES/NO); directional traders score near 1.
    mkt_yes: dict[str, float] = {}
    mkt_no: dict[str, float] = {}
    # Scattershot/hedge detection: distinct markets bought per event (slug).
    # A wallet betting many mutually-exclusive outcomes of one event (e.g. every
    # exact football score) is gambling/hedging — low EV, not worth copying.
    evt_markets: dict[str, set[str]] = {}

    for a in rows:
        t = a.get("type")
        if t == "TRADE":
            trades += 1
            ts = int(a.get("timestamp") or 0)
            if ts > 0:
                if first_ts == 0 or ts < first_ts:
                    first_ts = ts
                if ts > last_ts:
                    last_ts = ts
            size = float(a.get("usdcSize") or 0)
            total_size += size
            # Only BUY orders reveal directional intent; sells are exits.
            if size > 0 and str(a.get("side", "")).upper() == "BUY":
                mkt = a.get("conditionId") or ""
                outcome = str(a.get("outcome") or "").upper()
                if mkt:
                    if "YES" in outcome:
                        mkt_yes[mkt] = mkt_yes.get(mkt, 0.0) + size
                    elif "NO" in outcome:
                        mkt_no[mkt] = mkt_no.get(mkt, 0.0) + size
                    # Group markets under their parent event for hedge detection.
                    slug = a.get("eventSlug") or a.get("slug") or ""
                    if slug:
                        evt_markets.setdefault(slug, set()).add(mkt)
        elif t == "MAKER_REBATE":
            maker_rebate += 1
        elif t == "REWARD":
            rewards += 1

    # Trade density: how many trades per day over the observed window.
    if first_ts and last_ts and last_ts > first_ts:
        days_span = max((last_ts - first_ts) / 86400, 0.5)
        trades_per_day = trades / days_span
    else:
        trades_per_day = float(trades)

    avg_size = (total_size / trades) if trades > 0 else 0.0

    # Directionality score (average D across all markets traded).
    all_mkts = set(mkt_yes) | set(mkt_no)
    d_scores = []
    for mkt in all_mkts:
        v_yes = mkt_yes.get(mkt, 0.0)
        v_no = mkt_no.get(mkt, 0.0)
        total = v_yes + v_no
        if total > 0:
            d_scores.append(abs(v_yes - v_no) / total)
    directionality = round(sum(d_scores) / len(d_scores), 3) if d_scores else None

    # Most distinct markets the wallet bought within a single event.
    max_event_outcomes = max((len(s) for s in evt_markets.values()), default=0)

    return {
        "last_days": round((now - last_ts) / 86400, 1) if last_ts else 999.0,
        "is_mm": maker_rebate > 0 or rewards > MAX_REWARDS,
        "trades": trades,
        "trades_per_day": round(trades_per_day, 1),
        "avg_size": round(avg_size, 1),
        "directionality": directionality,
        "max_event_outcomes": max_event_outcomes,
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
        pnl = pnl_map.get(addr, 0.0)
        if pnl < MIN_LB_PNL:
            continue
        # Primary MM/arb filter: big profit on huge volume = churner, not a bettor.
        ratio = _profit_volume_ratio(pnl, vmap.get(addr))
        if ratio is not None and ratio < min_ratio:
            continue
        if ratio is None:
            # Not on the volume board (modest volume) — can't compute the ratio,
            # so require a light resolved track record to rule out one-shot luck.
            s = score_wallet(addr)
            time.sleep(0.05)
            if s["resolved_count"] < MIN_RESOLVED_LIGHT:
                continue
        # Must be actively trading; drop obvious MM/LP (liquidity-reward earners).
        prof = _activity_profile(addr)
        time.sleep(0.05)
        if prof["is_mm"] or prof["last_days"] > MAX_LAST_DAYS:
            continue
        # Trade density filter: >20 trades/day → automated MM.
        max_tpd = settings.discovery_max_trades_per_day
        if prof["trades"] >= 10 and prof["trades_per_day"] > max_tpd:
            log.debug("discovery_skip_density",
                      addr=addr[:10], tpd=prof["trades_per_day"])
            continue
        # Average trade size filter: MMs scalp with tiny orders;
        # directional whales bet hundreds+ per order.
        min_sz = settings.discovery_min_avg_trade_size
        if prof["trades"] >= 10 and prof["avg_size"] < min_sz:
            log.debug("discovery_skip_size",
                      addr=addr[:10], avg_size=prof["avg_size"])
            continue
        # Directionality filter: MMs buy both YES and NO on the same market
        # (D ≈ 0); directional traders consistently bet one side (D ≈ 1).
        min_dir = settings.discovery_min_directionality
        d = prof["directionality"]
        if d is not None and d < min_dir:
            log.debug("discovery_skip_directionality",
                      addr=addr[:10], directionality=d)
            continue
        # Scattershot/hedge filter: betting many mutually-exclusive outcomes of
        # one event (e.g. every exact football score) is a low-EV gamble.
        max_evt = settings.discovery_max_event_outcomes
        if prof["max_event_outcomes"] >= max_evt:
            log.debug("discovery_skip_scattershot",
                      addr=addr[:10], event_outcomes=prof["max_event_outcomes"])
            continue
        qualified.append({
            "wallet": meta["wallet"], "name": meta["name"],
            "realized": pnl, "winrate": 0.0,
            "ratio": ratio,
            "directionality": d,
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
    # churners (low profit/volume ratio, e.g. skk1ch / swisstony at ~4%) or
    # scattershot/hedge bettors (many outcomes in one event). Falls back to the
    # activity feed when the wallet isn't on the volume board.
    qualified_addrs = {p["wallet"].lower() for p in qualified}
    max_evt = settings.discovery_max_event_outcomes
    removed: list[dict] = []
    if add:
        for w in list_tracked_wallets():
            a = w["address"].lower()
            if a in qualified_addrs:
                continue
            ratio = _profit_volume_ratio(pnl_map.get(a, 0.0), vmap.get(a))
            bad = ratio is not None and ratio < min_ratio
            reason = "low_ratio" if bad else ""
            if not bad:
                prof = _activity_profile(a)
                time.sleep(0.05)
                if prof["is_mm"]:
                    bad, reason = True, "mm"
                elif prof["max_event_outcomes"] >= max_evt:
                    bad, reason = True, "scattershot"
            if bad:
                try:
                    remove_tracked_wallet(w["address"])
                    removed.append({"wallet": w["address"], "name": w.get("name") or "",
                                    "reason": reason})
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
