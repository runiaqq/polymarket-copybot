"""
Polymarket profit leaderboard helpers (for the admin "top traders" browser).

Pulls the official lb-api profit leaderboard with a short in-memory cache so
paginating through the menu doesn't refetch on every click.
"""

import time

import structlog

log = structlog.get_logger(__name__)

LB_URL = "https://lb-api.polymarket.com/profit"
_H = {"User-Agent": "Mozilla/5.0 (PolyMind)"}
PROFILE_URL = "https://polymarket.com/profile/{addr}"

_cache: dict[str, tuple[float, list]] = {}
_TTL = 300  # 5 min


def top_profit_wallets(window: str = "7d", limit: int = 50) -> list[dict]:
    """[{wallet, pnl, name}] sorted by profit desc. Cached for 5 min per window."""
    cached = _cache.get(window)
    if cached and (time.time() - cached[0]) < _TTL:
        return cached[1]
    import httpx
    out: list[dict] = []
    try:
        r = httpx.get(LB_URL, params={"window": window, "limit": limit}, headers=_H, timeout=15)
        r.raise_for_status()
        for row in r.json():
            w = row.get("proxyWallet")
            if not w:
                continue
            out.append({
                "wallet": w,
                "pnl": float(row.get("amount") or 0),
                "name": row.get("name") or row.get("pseudonym") or "",
            })
    except Exception:
        log.warning("leaderboard_fetch_failed", window=window)
        return cached[1] if cached else []
    _cache[window] = (time.time(), out)
    return out


def wallet_profit(addr: str, window: str = "7d") -> float | None:
    """Profit for a wallet from the cached leaderboard (None if outside top-N)."""
    a = addr.lower()
    for w in top_profit_wallets(window):
        if w["wallet"].lower() == a:
            return w["pnl"]
    return None


def wallet_recent_trades(addr: str, limit: int = 6) -> list[dict]:
    from core.polymarket import fetch_donor_recent_trades
    try:
        return fetch_donor_recent_trades(addr, limit=limit)
    except Exception:
        return []


def profile_url(addr: str) -> str:
    return PROFILE_URL.format(addr=addr)


def fmt_money(v: float) -> str:
    """Compact money: 1.8M, 271K, 940."""
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{v / 1_000:.0f}K"
    return f"{v:.0f}"
