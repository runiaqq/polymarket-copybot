"""
Wallet track-record scoring for whale signals.

The real-time market WS feed is anonymous (no buyer address), so we resolve the
buyer of a whale BUY via the Data API `/trades` endpoint (matching the tx hash),
then score that wallet's realized P&L history. Used as an optional copy filter:

  * observe (default): resolve + score + log, never blocks — collects validation data
  * enforce:           only copy when the buyer's history passes the thresholds

This is where real edge likely lives: following *proven-profitable* wallets beats
following *any large* buy. We validate in observe mode before enforcing.
"""

import time

import structlog

from core.config import settings

log = structlog.get_logger(__name__)

_TRADES_URL = "https://data-api.polymarket.com/trades"
_HEADERS = {"User-Agent": "Mozilla/5.0 (PolyMind)"}

# address -> (fetched_at, score) — bounded by TTL, refreshed lazily.
_cache: dict[str, tuple[float, dict]] = {}


def resolve_buyer(condition_id: str, tx_hash: str) -> str | None:
    """Find the taker (buyer) proxyWallet for a whale BUY by matching the tx hash.
    Retries to absorb Data API indexing lag right after the WS print."""
    if not (condition_id and tx_hash):
        return None
    import httpx

    tx = tx_hash.lower()
    for _ in range(max(1, settings.wallet_resolve_retries)):
        try:
            r = httpx.get(
                _TRADES_URL,
                params={"market": condition_id, "limit": 200},
                timeout=8.0, headers=_HEADERS,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                data = data.get("data", [])
            for t in data or []:
                if (t.get("transactionHash", "") or "").lower() == tx and \
                        (t.get("side", "") or "").upper() == "BUY":
                    return t.get("proxyWallet") or None
        except Exception:
            log.warning("resolve_buyer_failed", market=condition_id[:10])
        time.sleep(settings.wallet_resolve_delay_sec)
    return None


def score_wallet(address: str) -> dict:
    """Realized P&L history for a wallet (cached). Higher realized_pnl + more
    resolved markets = more trustworthy track record."""
    cached = _cache.get(address)
    if cached and (time.time() - cached[0]) < settings.wallet_score_ttl_sec:
        return cached[1]

    from core.polymarket import get_closed_positions, get_positions

    closed = get_closed_positions(address)
    resolved = len(closed)
    realized = sum(c.get("realized_pnl", 0) for c in closed)
    wins = sum(1 for c in closed if c.get("realized_pnl", 0) > 0)
    winrate = (wins / resolved) if resolved else 0.0
    try:
        unrealized = sum(p.get("cash_pnl", 0) for p in get_positions(address))
    except Exception:
        unrealized = 0.0

    score = {
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "resolved_count": resolved,
        "winrate": round(winrate, 3),
    }
    _cache[address] = (time.time(), score)
    return score


def passes(score: dict | None) -> bool:
    """Whether a wallet's track record clears the enforce-mode thresholds."""
    if not score:
        return False
    if score.get("resolved_count", 0) < settings.wallet_min_resolved:
        return False
    return score.get("realized_pnl", 0) >= settings.wallet_min_realized_pnl


def evaluate(condition_id: str, tx_hash: str) -> tuple[str | None, dict | None, bool]:
    """Resolve the buyer and score them. Returns (address, score, passed)."""
    addr = resolve_buyer(condition_id, tx_hash)
    if not addr:
        return None, None, False
    score = score_wallet(addr)
    return addr, score, passes(score)
