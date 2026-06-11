"""
Polymarket CLOB REST polling — polls donor trades every 30 seconds.
Simpler and more reliable than WebSocket for wallet-level tracking.
"""

import httpx
import structlog

from core.config import settings

log = structlog.get_logger(__name__)

CLOB_TRADES_URL = "https://clob.polymarket.com/data/trades"


def fetch_donor_recent_trades(maker_address: str, limit: int = 10) -> list[dict]:
    """
    Fetch recent trades for a donor wallet via CLOB REST API.
    Returns list of trade dicts.
    """
    try:
        resp = httpx.get(
            CLOB_TRADES_URL,
            params={"maker_address": maker_address, "limit": limit},
            timeout=10.0,
            headers={"User-Agent": "polymarket-copybot/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception:
        log.exception("fetch_trades_failed", address=maker_address)
        return []
