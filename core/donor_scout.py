"""
Blueprint 48 — donor scout: pure decision logic + Gamma resolution helper.

The scout inverts the old discovery funnel: instead of asking the global
leaderboard "who is rich", it watches OUR fast-market tape for wallets whose
entries we could have copied, scores them on resolution results in OUR
universe, and lets them prove themselves in shadow probation before any real
money follows them. Celery wiring lives in worker/tasks/donor_scout.py.
"""

import structlog

log = structlog.get_logger(__name__)

CLOB_MARKETS_URL = "https://clob.polymarket.com/markets"
_H = {"User-Agent": "Mozilla/5.0 (PolyMind donor scout)"}


def laplace_score(wins: int, resolved: int) -> float:
    """Laplace-smoothed win rate (wins+1)/(resolved+2): ranks candidates without
    letting a lucky 2/2 outrank a proven 8/10 (0.75 vs 0.75 — tie; 3/3 → 0.8
    still below 9/10 → 0.83). Score of an unresolved wallet is the 0.5 prior."""
    if resolved < 0 or wins < 0 or wins > resolved:
        return 0.0
    return (wins + 1) / (resolved + 2)


def candidate_qualifies(
    profile: dict,
    *,
    min_directionality: float,
    max_trades_per_day: float,
    min_avg_trade_size: float,
    max_event_outcomes: int,
) -> tuple[bool, str]:
    """Hard anti-MM/anti-gambler filters over an activity-feed profile
    (see wallet_discovery._activity_profile). Returns (ok, reject_reason).

    Same fingerprints the leaderboard funnel used — they are good at
    RECOGNIZING an MM, the old pipeline's sin was where it looked for
    candidates, not how it rejected them."""
    if profile.get("is_mm"):
        return False, "mm_rewards"
    d = profile.get("directionality")
    if d is not None and d < min_directionality:
        return False, "directionality"
    if profile.get("trades", 0) >= 10:
        if profile.get("trades_per_day", 0.0) > max_trades_per_day:
            return False, "density"
        if profile.get("avg_size", 0.0) < min_avg_trade_size:
            return False, "size"
    if profile.get("max_event_outcomes", 0) >= max_event_outcomes:
        return False, "scattershot"
    return True, ""


def tally_outcomes(
    entries: list[dict], winners: dict[str, str]
) -> tuple[int, int]:
    """(resolved, wins) for a list of entries [{condition_id, outcome}] against
    a {condition_id: winning_outcome} map. Entries on unresolved / unknown
    markets or with a missing outcome name are not counted."""
    resolved = wins = 0
    for e in entries:
        win_outcome = winners.get(e.get("condition_id") or "")
        outcome = (e.get("outcome") or "").strip().lower()
        if not win_outcome or not outcome:
            continue
        resolved += 1
        if outcome == win_outcome:
            wins += 1
    return resolved, wins


def probation_pnl(
    signals: list[dict], winners: dict[str, str], stake: float
) -> dict:
    """Would-be result of copying a candidate's probation signals at a nominal
    fixed stake: win pays stake*(1/price - 1), loss burns the stake. Uses OUR
    vwap at OUR signal time (trade_signals.price), i.e. exactly what a live
    user would have gotten — the whole point of shadow probation."""
    total = len(signals)
    resolved = wins = 0
    pnl = 0.0
    for s in signals:
        win_outcome = winners.get(s.get("market_id") or "")
        outcome = (s.get("outcome") or "").strip().lower()
        price = float(s.get("price") or 0)
        if not win_outcome or not outcome or price <= 0 or price >= 1:
            continue
        resolved += 1
        if outcome == win_outcome:
            wins += 1
            pnl += stake * (1 / price - 1)
        else:
            pnl -= stake
    return {"signals": total, "resolved": resolved, "wins": wins,
            "pnl": round(pnl, 2)}


def retro_score(buys: list[dict], winners: dict[str, str], stake: float) -> dict:
    """BP53: would-be result of having copied a wallet's own BUY fills at a
    nominal fixed stake — the instant replacement for a week of live probation.
    Uses the wallet's fill price (close to what a fast copy would get on these
    fast markets). Also reports the median entry price: a 0.90+ median is the
    favorite-buyer fingerprint (scout 0xd4fa: 86% WR, negative PnL) that a
    win-rate ranking rewards and a PnL ranking correctly punishes."""
    trades = len(buys)
    resolved = wins = 0
    pnl = 0.0
    prices: list[float] = []
    for b in buys:
        price = float(b.get("price") or 0)
        if 0 < price < 1:
            prices.append(price)
        win_outcome = winners.get(b.get("condition_id") or "")
        outcome = (b.get("outcome") or "").strip().lower()
        if not win_outcome or not outcome or not 0 < price < 1:
            continue
        resolved += 1
        if outcome == win_outcome:
            wins += 1
            pnl += stake * (1 / price - 1)
        else:
            pnl -= stake
    prices.sort()
    median_price = prices[len(prices) // 2] if prices else None
    return {
        "trades": trades,
        "resolved": resolved,
        "wins": wins,
        "pnl": round(pnl, 2),
        "median_price": median_price,
    }


def fetch_wallet_buys(
    addr: str, *, days: float, max_trades: int
) -> list[dict]:
    """BP53: a wallet's recent BUY fills from the public Data API — its whole
    tradable history, not just the slice that crossed our tape. Paginated;
    returns [{condition_id, outcome, price, size_usdc, timestamp}]. Empty on
    any error: the scout treats an unreadable wallet as unscoreable, never
    crashes."""
    import time as _time

    import httpx

    cutoff = _time.time() - days * 86400
    out: list[dict] = []
    offset = 0
    try:
        with httpx.Client(headers=_H, timeout=15.0) as client:
            while offset < max_trades:
                r = client.get(
                    "https://data-api.polymarket.com/trades",
                    params={"user": addr, "limit": 500, "offset": offset},
                )
                r.raise_for_status()
                raw = r.json()
                if not isinstance(raw, list):
                    raw = raw.get("data", []) if isinstance(raw, dict) else []
                if not raw:
                    break
                stop = False
                for t in raw:
                    ts = int(t.get("timestamp") or 0)
                    if ts and ts < cutoff:
                        stop = True
                        break
                    if str(t.get("side") or "").upper() != "BUY":
                        continue
                    price = float(t.get("price") or 0)
                    out.append({
                        "condition_id": t.get("conditionId") or "",
                        "outcome": t.get("outcome") or "",
                        "price": price,
                        "size_usdc": round(float(t.get("size") or 0) * price, 2),
                        "timestamp": ts,
                    })
                if stop or len(raw) < 500:
                    break
                offset += 500
                _time.sleep(0.1)
    except Exception:
        log.warning("retro_history_fetch_failed", wallet=addr[:10])
        return []
    return out


def resolve_winning_outcomes(condition_ids: list[str]) -> dict[str, str]:
    """Resolve markets via CLOB /markets/{condition_id}: {condition_id:
    winning outcome name (lowercase)}. Unresolved / unknown markets are
    simply absent from the map.

    Seen live 08-21: Gamma's markets endpoint silently IGNORES unknown query
    params and its condition_ids filter returned zero rows for real (esports)
    conditions — every candidate scored resolved_count=0 and probation never
    enrolled. The CLOB endpoint is authoritative (closed + per-token `winner`
    flag); one call per condition with a politeness delay, tolerating
    individual failures (scout must never crash on a market lookup)."""
    import time as _time

    import httpx

    winners: dict[str, str] = {}
    ids = [c for c in dict.fromkeys(condition_ids) if c]
    with httpx.Client(headers=_H, timeout=15.0) as client:
        for cond in ids:
            try:
                r = client.get(f"{CLOB_MARKETS_URL}/{cond}")
                if r.status_code != 200:
                    continue
                m = r.json()
                if not m.get("closed"):
                    continue
                for tok in m.get("tokens") or []:
                    if tok.get("winner"):
                        name = str(tok.get("outcome") or "").strip().lower()
                        if name:
                            winners[cond] = name
                        break
            except Exception:
                log.warning("clob_resolve_failed", cond=cond[:16])
            _time.sleep(0.05)
    return winners
