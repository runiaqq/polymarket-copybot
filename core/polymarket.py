"""
Polymarket data-api polling — polls donor activity every 30 seconds.
Uses data-api.polymarket.com/activity?user= which correctly filters by wallet.
"""

import httpx
import structlog

log = structlog.get_logger(__name__)

DATA_API_URL = "https://data-api.polymarket.com/activity"


def fetch_donor_recent_trades(maker_address: str, limit: int = 10) -> list[dict]:
    """
    Fetch recent trades for a donor wallet via Polymarket data API.
    Returns normalised list of trade dicts ready for poll_donors to consume.
    """
    try:
        resp = httpx.get(
            DATA_API_URL,
            params={"user": maker_address, "limit": limit},
            timeout=10.0,
            headers={"User-Agent": "polymarket-copybot/1.0"},
        )
        resp.raise_for_status()
        raw: list[dict] = resp.json()
        if not isinstance(raw, list):
            raw = raw.get("data", [])

        # Normalise field names so poll_donors works without change
        normalised = []
        for t in raw:
            if t.get("type") != "TRADE":
                continue
            normalised.append({
                "id":          t.get("transactionHash", ""),
                "trade_id":    t.get("transactionHash", ""),
                "market":      t.get("conditionId", ""),
                "condition_id": t.get("conditionId", ""),
                "asset_id":    t.get("asset", ""),
                "token_id":    t.get("asset", ""),
                "side":        t.get("side", "BUY"),
                "price":       float(t.get("price") or 0),
                "size":        float(t.get("usdcSize") or 0),  # already in USDC
                "size_usdc":   float(t.get("usdcSize") or 0),
                "timestamp":   t.get("timestamp", 0),
                "title":       t.get("title", ""),
            })
        return normalised
    except Exception:
        log.exception("fetch_trades_failed", address=maker_address)
        return []
