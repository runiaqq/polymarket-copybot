"""
Polymarket CLOB WebSocket listener — the fast-path core.

Connects to the official trade stream, filters by donor addresses,
and dispatches Celery tasks immediately upon matching trades.
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

# In-memory set for O(1) donor lookup — reloaded every REFRESH_INTERVAL_SEC
DONOR_SET: set[str] = set()
REFRESH_INTERVAL_SEC = 300  # 5 minutes


@dataclass
class TradeSignalRaw:
    market_id: str
    side: str          # "YES" | "NO"
    price: float
    size_usdc: float
    donor_address: str
    timestamp: datetime


def _parse_ws_message(raw: str) -> TradeSignalRaw | None:
    """
    Parse a raw WebSocket message from the Polymarket CLOB trade stream.
    Returns None if the message is not a relevant trade.
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # The CLOB WS sends lists of trade events
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
    """Periodically reload donor addresses from DB into the in-memory set."""
    from core.db import AsyncSessionLocal, get_active_donor_addresses

    while True:
        try:
            async with AsyncSessionLocal() as session:
                addresses = await get_active_donor_addresses(session)
                DONOR_SET.clear()
                DONOR_SET.update(addresses)
                log.info("donor_set_refreshed", count=len(DONOR_SET))
        except Exception:
            log.exception("donor_set_refresh_failed")
        await asyncio.sleep(REFRESH_INTERVAL_SEC)


async def _dispatch_signal(signal: TradeSignalRaw) -> None:
    """
    Dispatch both Celery tasks simultaneously:
    - execute_copy_trade: fast path, no AI gate
    - run_ai_analysis: parallel, sends follow-up alert after execution
    """
    # Import here to avoid circular imports at module load
    from core.db import AsyncSessionLocal, get_active_subscribers, get_donor_by_address
    from worker.tasks import execute_copy_trade, run_ai_analysis

    async with AsyncSessionLocal() as session:
        donor = await get_donor_by_address(session, signal.donor_address)
        if donor is None:
            return

        subscribers = await get_active_subscribers(session)
        if not subscribers:
            log.debug("no_active_subscribers")
            return

        signal_payload = {
            "market_id": signal.market_id,
            "side": signal.side,
            "price": signal.price,
            "size_usdc": signal.size_usdc,
            "donor_address": signal.donor_address,
            "donor_label": donor.label or signal.donor_address[:8],
            "donor_win_rate": donor.win_rate_30d,
            "donor_roi": donor.roi_30d,
            "timestamp": signal.timestamp.isoformat(),
        }

        user_ids = [u.id for u in subscribers]

        log.info(
            "signal_dispatched",
            market=signal.market_id,
            side=signal.side,
            size=signal.size_usdc,
            subscribers=len(user_ids),
        )

        # ── FAST PATH: copy immediately, one task per user ──────────────────
        for user in subscribers:
            execute_copy_trade.delay(user.id, signal_payload)

        # ── PARALLEL PATH: AI analysis (non-blocking) ───────────────────────
        run_ai_analysis.delay(signal_payload, user_ids)


async def run_ws_listener() -> None:
    """
    Main entry point. Run this as a long-lived asyncio task in the worker process.
    Reconnects automatically on disconnect.
    """
    # Start the donor set refresh loop
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
                backoff = 1  # reset on successful connect
                log.info("ws_connected")

                async for raw_msg in ws:
                    signal = _parse_ws_message(str(raw_msg))
                    if signal:
                        asyncio.create_task(_dispatch_signal(signal))

        except ConnectionClosed as e:
            log.warning("ws_disconnected", code=e.code, reason=e.reason, backoff=backoff)
        except Exception:
            log.exception("ws_error", backoff=backoff)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)
