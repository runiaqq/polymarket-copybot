"""
Real-time whale detector over the Polymarket CLOB market WebSocket.

Maintains live order books for a rolling universe of fast markets, watches trade
prints, and when a buy is "large for that book" (dynamic, liquidity/volume-relative)
and passes the break-even guards, hands the signal to Celery for copy execution.

Runs as a daemon thread inside the Celery worker (see worker/signals.py).
"""

import asyncio
import json
import time

import structlog
import websockets

from core.config import settings
from core.detector import OrderBook, RecentTrades, evaluate_trade
from core.polymarket import get_watch_markets, normalize_book

log = structlog.get_logger(__name__)

_SEEN_TX_MAX = 20000


class WhaleListener:
    def __init__(self) -> None:
        self.token_map: dict[str, dict] = {}
        self.subscribed: set[str] = set()
        self.books: dict[str, OrderBook] = {}
        self.recents: dict[str, RecentTrades] = {}
        self.seen: set[str] = set()
        self.market_cooldown: dict[str, float] = {}
        self._last_refresh = 0.0

    # ── Market universe ─────────────────────────────────────────────────────────
    async def refresh_markets(self) -> list[str]:
        """Rebuild the watched-market map; return newly added token ids to subscribe."""
        token_map = await asyncio.to_thread(get_watch_markets)
        if not token_map:
            return []
        self.token_map = token_map
        # Prune state for markets that left the universe.
        for tok in list(self.books):
            if tok not in token_map:
                self.books.pop(tok, None)
                self.recents.pop(tok, None)
        new = [t for t in token_map if t not in self.subscribed]
        self._last_refresh = time.time()
        log.info("ws_markets_refreshed", watched=len(token_map), new=len(new))
        return new

    # ── Event handlers ──────────────────────────────────────────────────────────
    def _handle_book(self, msg: dict) -> None:
        token = msg.get("asset_id")
        if not token or token not in self.token_map:
            return
        book = self.books.setdefault(token, OrderBook())
        book.apply_snapshot(normalize_book(msg))

    def _handle_price_change(self, msg: dict) -> None:
        for ch in msg.get("price_changes", []):
            token = ch.get("asset_id")
            if not token or token not in self.token_map:
                continue
            book = self.books.setdefault(token, OrderBook())
            try:
                book.apply_change(float(ch["price"]), float(ch["size"]), ch.get("side", ""))
            except (KeyError, TypeError, ValueError):
                continue
            try:
                bb = float(ch["best_bid"]) if ch.get("best_bid") is not None else None
                ba = float(ch["best_ask"]) if ch.get("best_ask") is not None else None
                book.set_best(bb, ba)
            except (TypeError, ValueError):
                pass

    def _handle_trade(self, msg: dict) -> None:
        token = msg.get("asset_id")
        meta = self.token_map.get(token or "")
        if not meta:
            return
        try:
            price = float(msg.get("price") or 0)
            size = float(msg.get("size") or 0)
        except (TypeError, ValueError):
            return
        usdc = price * size
        side = (msg.get("side") or "").upper()
        tx = msg.get("transaction_hash", "")
        fee_bps = float(msg.get("fee_rate_bps") or 0)

        # Record every trade for volume stats (both sides).
        recent = self.recents.setdefault(token, RecentTrades(settings.recent_trade_window_sec))
        recent.add(usdc)

        if side != "BUY":
            return
        key = f"{tx}:{token}"
        if not tx or key in self.seen:
            return

        book = self.books.get(token)
        if book is None:
            return

        condition_id = meta.get("condition_id", "")
        now = time.time()
        if now - self.market_cooldown.get(condition_id, 0) < settings.market_signal_cooldown_sec:
            self.seen.add(key)
            return

        trade = {
            "price": price,
            "size": size,
            "side": side,
            "usdc": usdc,
            "tx": tx,
            "token_id": token,
            "whale_wallet": msg.get("maker", ""),
        }
        signal = evaluate_trade(book, recent, trade, meta, fee_bps)
        self.seen.add(key)
        if len(self.seen) > _SEEN_TX_MAX:
            self.seen.clear()

        if signal is None:
            return

        self.market_cooldown[condition_id] = now
        log.info("ws_whale_signal", market=condition_id[:18], usdc=round(usdc),
                 price=price, fee_bps=fee_bps, depth=round(signal.get("fillable_usdc", 0)))

        from worker.tasks import dispatch_signal
        dispatch_signal.delay(signal)

    def _dispatch_message(self, msg: dict) -> None:
        et = msg.get("event_type")
        if et == "book":
            self._handle_book(msg)
        elif et == "price_change":
            self._handle_price_change(msg)
        elif et == "last_trade_price":
            self._handle_trade(msg)

    # ── Connection loop ─────────────────────────────────────────────────────────
    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(settings.ws_heartbeat_sec)
            try:
                await ws.send("{}")
            except Exception:
                return

    async def _refresher(self, ws) -> None:
        while True:
            await asyncio.sleep(settings.ws_refresh_markets_sec)
            try:
                new = await self.refresh_markets()
                if new:
                    await ws.send(json.dumps({"operation": "subscribe", "assets_ids": new}))
                    self.subscribed.update(new)
            except Exception:
                log.warning("ws_refresh_failed")

    async def _session(self) -> None:
        new = await self.refresh_markets()
        if not new:
            log.warning("ws_no_markets_to_watch")
            await asyncio.sleep(30)
            return

        async with websockets.connect(settings.ws_market_url, ping_interval=None,
                                      open_timeout=20, max_size=None) as ws:
            await ws.send(json.dumps({"assets_ids": new, "type": "market"}))
            self.subscribed = set(new)
            log.info("ws_connected", subscribed=len(self.subscribed))

            hb = asyncio.create_task(self._heartbeat(ws))
            rf = asyncio.create_task(self._refresher(ws))
            try:
                async for raw in ws:
                    if raw == "{}":
                        continue
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    events = data if isinstance(data, list) else [data]
                    for ev in events:
                        if isinstance(ev, dict):
                            try:
                                self._dispatch_message(ev)
                            except Exception:
                                log.exception("ws_event_handler_failed")
            finally:
                hb.cancel()
                rf.cancel()

    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                await self._session()
                backoff = 1
            except Exception:
                log.exception("ws_session_error", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


def run_listener() -> None:
    """Entrypoint for the WS detector (blocking; run in its own thread/process)."""
    listener = WhaleListener()
    asyncio.run(listener.run())
