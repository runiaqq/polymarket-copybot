"""
Polymarket CLOB WebSocket listener — fast-path core.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from core.config import settings

log = structlog.get_logger(__name__)

DONOR_SET: set[str] = set()
REFRESH_INTERVAL_SEC = 300


@dataclass
class TradeSignalRaw:
    market_id: str
    side: str
    price: float
    size_usdc: float
    donor_address: str
    timestamp: datetime


def _parse_ws_message(raw: str) -> TradeSignalRaw | None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(msg, list):
        return None

    for event in msg:
        if event.get("event_type") != "trade":
            continue

        maker: str = event.get("maker_address", "").lower()
        if maker not in DONOR_SET:
            continue

        size = float(event.get("size", 0))
        price = float(event.get("price", 0))
        size_usdc = size * price

        if size_usdc < settings.min_trade_size_usdc:
            continue

        outcome = event.get("outcome", "YES").upper()

        return TradeSignalRaw(
            market_id=event["market"],
            side=outcome,
            price=price,
            size_usdc=size_usdc,
            donor_address=maker,
            timestamp=datetime.now(timezone.utc),
        )

    return None


async def _refresh_donor_set() -> None:
    from core.db import get_active_donor_addresses

    while True:
        try:
            addresses = get_active_donor_addresses()
            DONOR_SET.clear()
            DONOR_SET.update(addresses)
            log.info("donor_set_refreshed", count=len(DONOR_SET))
        except Exception:
            log.exception("donor_set_refresh_failed")
        await asyncio.sleep(REFRESH_INTERVAL_SEC)


async def _dispatch_signal(signal: TradeSignalRaw) -> None:
    from core.db import get_active_subscribers, get_donor_by_address, get_supabase
    from worker.tasks import execute_copy_trade, run_ai_analysis

    donor = get_donor_by_address(signal.donor_address)
    if donor is None:
        return

    subscribers = get_active_subscribers()
    if not subscribers:
        log.debug("no_active_subscribers")
        return

    signal_payload = {
        "market_id": signal.market_id,
        "side": signal.side,
        "price": signal.price,
        "size_usdc": signal.size_usdc,
        "donor_address": signal.donor_address,
        "donor_db_id": donor.get("id", 1),
        "donor_label": donor.get("label") or signal.donor_address[:8],
        "donor_win_rate": donor.get("win_rate_30d"),
        "donor_roi": donor.get("roi_30d"),
        "timestamp": signal.timestamp.isoformat(),
    }

    user_ids = [u["id"] for u in subscribers]

    log.info("signal_dispatched", market=signal.market_id, side=signal.side,
             size=signal.size_usdc, subscribers=len(user_ids))

    # Fast path: copy immediately
    for user in subscribers:
        execute_copy_trade.delay(user["id"], signal_payload)

    # Parallel: AI analysis
    run_ai_analysis.delay(signal_payload, user_ids)


async def run_ws_listener() -> None:
    asyncio.create_task(_refresh_donor_set())

    backoff = 1
    while True:
        try:
            log.info("ws_connecting", url=settings.polymarket_clob_ws_url)
            async with websockets.connect(
                settings.polymarket_clob_ws_url,
                ping_interval=20,
                ping_timeout=30,
            ) as ws:
                backoff = 1
                log.info("ws_connected")
                async for raw_msg in ws:
                    signal = _parse_ws_message(str(raw_msg))
                    if signal:
                        asyncio.create_task(_dispatch_signal(signal))

        except ConnectionClosed as e:
            log.warning("ws_disconnected", code=e.code, backoff=backoff)
        except Exception:
            log.exception("ws_error", backoff=backoff)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)
