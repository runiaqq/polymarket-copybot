"""BP30: isolated, read-only signal engine with virtual execution and settlement."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog
import websockets

from core.cache import clear_once, notify_once
from core.config import settings
from core.db.session import get_supabase
from core.shadow_model import (
    EntryVariant,
    EwmaVolatility,
    active_entry_variants,
    ask_depth_usdc,
    build_entry_variants,
    calibrated_probability,
    divergence_exceeds_ceiling,
    maker_bid_price,
    maker_fill,
    maker_should_cancel,
    passes_signal_filter,
    probability_up,
    stressed_sigma,
    walk_order_book,
)

log = structlog.get_logger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_MARKETS_URL = "https://clob.polymarket.com/clob-markets"
SUPPORTED_ASSETS = {"btc", "eth", "sol", "xrp"}


@dataclass
class SpotState:
    volatility: EwmaVolatility
    fast_volatility: EwmaVolatility
    eligible_from_window: int
    price: float | None = None
    timestamp_sec: float | None = None
    window_opens: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketWindow:
    asset: str
    condition_id: str
    window_start: int
    window_end: int
    tokens: dict[str, str]
    fee_rate: float
    fee_exponent: float
    tick_size: float


@dataclass
class MakerOrder:
    market: MarketWindow
    side: str
    bid: float
    edge: float
    placed_at: float
    placement_payload: dict[str, Any]
    terminal_status: str | None = None
    note: str | None = None
    entered_at: float | None = None
    terminal_time_left: float | None = None


@dataclass
class WindowObservation:
    asset: str
    window_start: int
    condition_id: str | None = None
    best_edge: float | None = None
    model_p: float | None = None
    ask: float | None = None
    entered: bool = False
    entered_variants: set[str] = field(default_factory=set)
    reason: str = "not_evaluated"


class ShadowEngine:
    def __init__(self) -> None:
        now = int(time.time())
        current_window = self._window_start(now)
        eligible_from = current_window + settings.shadow_window_sec
        assets = [asset.lower() for asset in settings.shadow_assets]
        self.assets = [asset for asset in assets if asset in SUPPORTED_ASSETS]
        self.entry_variants: list[EntryVariant] = build_entry_variants(
            settings.shadow_entry_min_sec,
            settings.shadow_entry_max_sec,
            settings.shadow_variant_edges_sec,
        )
        self.spots = {
            asset: SpotState(
                volatility=EwmaVolatility(
                    alpha=settings.shadow_ewma_alpha,
                    sample_interval_sec=settings.shadow_vol_sample_sec,
                ),
                fast_volatility=EwmaVolatility(
                    alpha=settings.shadow_ewma_fast_alpha,
                    sample_interval_sec=settings.shadow_vol_sample_sec,
                ),
                eligible_from_window=eligible_from,
            )
            for asset in self.assets
        }
        self.markets: dict[tuple[str, int], MarketWindow] = {}
        self.observations: dict[tuple[str, int], WindowObservation] = {
            (asset, current_window): WindowObservation(
                asset=asset,
                window_start=current_window,
                reason="restart_mid_window",
            )
            for asset in self.assets
        }
        self.logged_windows: set[tuple[str, int]] = {
            (asset, current_window - settings.shadow_window_sec) for asset in self.assets
        }
        self.entered_conditions: set[tuple[str, str]] = set()
        self.maker_orders: dict[tuple[str, int], MakerOrder] = {}
        self.maker_attempted_conditions: set[str] = set()
        # BP49: conditions already handed to the real-money executor (one
        # publish per condition; in-memory — the executor's DB dedup is the
        # durable backstop across restarts).
        self.published_conditions: set[str] = set()
        self.db_retry_after = 0.0
        self.last_spot_rx_monotonic = time.monotonic()
        self._signal_tasks: set[asyncio.Task] = set()
        self._redis = None  # lazy redis.asyncio client for BP33 signal publishing

    async def _broadcast_signal(self, text: str) -> None:
        for chat_id in settings.shadow_signal_telegram_ids:
            try:
                await _telegram_send(chat_id, text)
            except Exception:
                log.exception("shadow_signal_send_failed", chat_id=chat_id)

    def _spawn_signal(self, text: str) -> None:
        """Send in the background so the 1s evaluation loop is never blocked."""
        if not settings.shadow_signal_telegram_ids:
            return
        task = asyncio.create_task(self._broadcast_signal(text))
        self._signal_tasks.add(task)
        task.add_done_callback(self._signal_tasks.discard)

    # ── BP33: real-money executor feed ────────────────────────────────────────

    async def _publish_execution_signal(self, payload: dict[str, Any]) -> None:
        """Fire-and-forget pub/sub: no subscribers means the message vanishes,
        which is exactly what we want (no stale-signal queue to drain)."""
        try:
            if self._redis is None:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
            await self._redis.publish(
                settings.crypto_signals_channel,
                json.dumps(payload, separators=(",", ":")),
            )
        except Exception as exc:
            log.warning("crypto_signal_publish_failed", error=str(exc)[:200])
            self._redis = None

    def _spawn_publish(self, payload: dict[str, Any]) -> None:
        task = asyncio.create_task(self._publish_execution_signal(payload))
        self._signal_tasks.add(task)
        task.add_done_callback(self._signal_tasks.discard)

    @staticmethod
    def _window_start(timestamp_sec: int | float) -> int:
        window = settings.shadow_window_sec
        return int(timestamp_sec) // window * window

    def _record_spot(self, asset: str, price: float, timestamp_sec: float) -> None:
        state = self.spots.get(asset)
        if state is None or price <= 0:
            return
        state.price = price
        state.timestamp_sec = timestamp_sec
        self.last_spot_rx_monotonic = time.monotonic()
        state.volatility.update(price, timestamp_sec)
        state.fast_volatility.update(price, timestamp_sec)
        window_start = self._window_start(timestamp_sec)
        if window_start >= state.eligible_from_window:
            state.window_opens.setdefault(window_start, price)
        oldest = window_start - settings.shadow_window_sec
        state.window_opens = {
            start: value for start, value in state.window_opens.items() if start >= oldest
        }

    async def _handle_rtds_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        messages = decoded if isinstance(decoded, list) else [decoded]
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("topic") not in {"crypto_prices_chainlink", "crypto_prices"}:
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            symbol = str(payload.get("symbol") or "").lower()
            if "/" not in symbol:
                continue
            asset = symbol.split("/", 1)[0]
            snapshot = payload.get("data")
            if isinstance(snapshot, list):
                for point in snapshot:
                    if isinstance(point, dict):
                        self._record_spot_point(asset, point)
                continue
            self._record_spot_point(asset, payload)

    def _record_spot_point(self, asset: str, point: dict[str, Any]) -> None:
        try:
            price = float(point["value"])
            timestamp_sec = float(point["timestamp"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            return
        self._record_spot(asset, price, timestamp_sec)

    async def spot_feed_loop(self) -> None:
        subscriptions = [
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": json.dumps({"symbol": f"{asset}/usd"}, separators=(",", ":")),
            }
            for asset in self.assets
        ]
        subscribe = json.dumps(
            {"action": "subscribe", "subscriptions": subscriptions},
            separators=(",", ":"),
        )
        backoff = settings.shadow_reconnect_initial_sec
        while True:
            try:
                async with websockets.connect(
                    settings.shadow_rtds_url,
                    open_timeout=settings.shadow_http_timeout_sec,
                    close_timeout=settings.shadow_http_timeout_sec,
                    ping_interval=None,
                    max_size=None,
                ) as websocket:
                    await websocket.send(subscribe)
                    log.info("shadow_spot_connected", assets=self.assets)
                    backoff = settings.shadow_reconnect_initial_sec
                    last_rx = time.monotonic()
                    last_ping = 0.0
                    self.last_spot_rx_monotonic = time.monotonic()
                    while True:
                        now = time.monotonic()
                        if now - last_ping >= settings.shadow_rtds_ping_sec:
                            await websocket.send("PING")
                            last_ping = now
                        if now - last_rx > settings.shadow_rtds_silence_sec:
                            raise ConnectionError("Chainlink RTDS stream went silent")
                        if now - self.last_spot_rx_monotonic > settings.shadow_rtds_silence_sec:
                            raise ConnectionError("Chainlink RTDS has no price updates")
                        try:
                            raw = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=settings.shadow_rtds_ping_sec,
                            )
                        except TimeoutError:
                            continue
                        last_rx = time.monotonic()
                        await self._handle_rtds_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "shadow_spot_session_error",
                    error=str(exc)[:300],
                    backoff_sec=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, settings.shadow_reconnect_max_sec)

    async def _fetch_market(self, client: httpx.AsyncClient, asset: str, start: int) -> None:
        slug = f"{asset}-updown-5m-{start}"
        response = await client.get(GAMMA_EVENTS_URL, params={"slug": slug})
        response.raise_for_status()
        events = response.json()
        if not isinstance(events, list) or not events:
            return
        event = events[0]
        markets = event.get("markets") or []
        if not markets:
            return
        market = markets[0]
        if not market.get("acceptingOrders"):
            return
        resolution_source = str(
            market.get("resolutionSource") or event.get("resolutionSource") or ""
        ).lower()
        if "data.chain.link/streams/" not in resolution_source:
            log.error(
                "shadow_market_wrong_resolution_source",
                asset=asset,
                slug=slug,
                source=resolution_source,
            )
            return
        try:
            start_value = (
                market.get("eventStartTime")
                or event.get("startTime")
                or event.get("eventStartTime")
            )
            actual_start = _parse_timestamp(start_value)
            actual_end = _parse_timestamp(market["endDate"])
        except (KeyError, TypeError, ValueError):
            return
        tolerance = settings.shadow_market_time_tolerance_sec
        if abs(actual_start - start) > tolerance:
            log.error(
                "shadow_market_start_mismatch",
                asset=asset,
                slug=slug,
                expected=start,
                actual=actual_start,
            )
            return
        expected_end = start + settings.shadow_window_sec
        if abs(actual_end - expected_end) > tolerance:
            log.error(
                "shadow_market_end_mismatch",
                asset=asset,
                slug=slug,
                expected=expected_end,
                actual=actual_end,
            )
            return
        tokens = _market_tokens(market)
        if set(tokens) != {"up", "down"}:
            log.error("shadow_market_bad_tokens", asset=asset, slug=slug, outcomes=list(tokens))
            return
        condition_id = str(market["conditionId"])
        clob_response = await client.get(f"{CLOB_MARKETS_URL}/{condition_id}")
        clob_response.raise_for_status()
        clob_market = clob_response.json()
        if not clob_market.get("ao"):
            return
        clob_tokens = _clob_market_tokens(clob_market)
        if clob_tokens != tokens:
            log.error(
                "shadow_market_token_mismatch",
                asset=asset,
                slug=slug,
                gamma_tokens=tokens,
                clob_tokens=clob_tokens,
            )
            return
        fee_details = clob_market.get("fd") or {}
        if fee_details and fee_details.get("to") is not True:
            log.error("shadow_market_fee_not_taker_only", asset=asset, slug=slug)
            return
        fee_schedule = market.get("feeSchedule") or {}
        fee_rate = float(
            fee_details.get("r") or fee_schedule.get("rate") or settings.shadow_fee_rate
        )
        fee_exponent = float(
            fee_details.get("e") or fee_schedule.get("exponent") or settings.shadow_fee_exponent
        )
        try:
            tick_size = float(market.get("orderPriceMinTickSize") or 0.01)
        except (TypeError, ValueError):
            tick_size = 0.01
        if not 0 < tick_size < 1:
            tick_size = 0.01
        discovered = MarketWindow(
            asset=asset,
            condition_id=condition_id,
            window_start=start,
            window_end=expected_end,
            tokens=tokens,
            fee_rate=fee_rate,
            fee_exponent=fee_exponent,
            tick_size=tick_size,
        )
        self.markets[(asset, start)] = discovered

    async def discovery_loop(self) -> None:
        async with httpx.AsyncClient(timeout=settings.shadow_http_timeout_sec) as client:
            while True:
                now = int(time.time())
                current = self._window_start(now)
                starts = (current, current + settings.shadow_window_sec)
                try:
                    await asyncio.gather(
                        *(
                            self._fetch_market(client, asset, start)
                            for asset in self.assets
                            for start in starts
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("shadow_market_discovery_failed")
                await asyncio.sleep(settings.shadow_market_refresh_sec)

    def _observation(self, asset: str, start: int) -> WindowObservation:
        key = (asset, start)
        observation = self.observations.get(key)
        if observation is None:
            observation = WindowObservation(asset=asset, window_start=start)
            self.observations[key] = observation
        market = self.markets.get(key)
        if market is not None:
            observation.condition_id = market.condition_id
        return observation

    @staticmethod
    def _complete_maker_order(
        order: MakerOrder,
        *,
        status: str,
        note: str,
        entered_at: float,
        time_left: float,
    ) -> None:
        order.terminal_status = status
        order.note = note
        order.entered_at = entered_at
        order.terminal_time_left = time_left

    async def _persist_maker_order(
        self,
        key: tuple[str, int],
        order: MakerOrder,
    ) -> bool:
        if order.terminal_status is None or order.note is None or order.entered_at is None:
            return False
        if time.monotonic() < self.db_retry_after:
            return False
        payload = {
            **order.placement_payload,
            "variant": "maker",
            "status": order.terminal_status,
            "note": order.note,
            "entered_at": _iso(order.entered_at),
        }
        result = await asyncio.to_thread(self._insert_trade, payload)
        if result == "error":
            self.db_retry_after = time.monotonic() + settings.shadow_db_retry_sec
            return False
        if self.maker_orders.get(key) is order:
            del self.maker_orders[key]
        event = {
            "filled": "shadow_maker_filled",
            "cancelled_edge_lost": "shadow_maker_cancelled",
            "expired": "shadow_maker_expired",
        }[order.note]
        log.info(
            event,
            asset=order.market.asset,
            condition_id=order.market.condition_id,
            side=order.side,
            bid=round(order.bid, 6),
            edge=round(order.edge, 6),
            time_left=round(order.terminal_time_left or 0.0, 3),
        )
        return True

    async def _expire_maker_orders(self, asset: str, now: float) -> None:
        for key, order in list(self.maker_orders.items()):
            if order.market.asset != asset:
                continue
            if order.terminal_status is None and now >= order.market.window_end:
                self._complete_maker_order(
                    order,
                    status="unfilled",
                    note="expired",
                    entered_at=order.placed_at,
                    time_left=order.market.window_end - now,
                )
            if order.terminal_status is not None:
                await self._persist_maker_order(key, order)

    async def _evaluate_asset(self, asset: str, now: float) -> None:
        await self._expire_maker_orders(asset, now)
        start = self._window_start(now)
        observation = self._observation(asset, start)
        market = self.markets.get((asset, start))
        if market is None:
            observation.reason = "market_not_discovered"
            return
        state = self.spots[asset]
        open_price = state.window_opens.get(start)
        if open_price is None:
            observation.reason = "no_window_open"
            return
        if state.price is None or state.timestamp_sec is None:
            observation.reason = "no_spot"
            return
        if now - state.timestamp_sec > settings.shadow_spot_stale_sec:
            observation.reason = "stale_spot"
            return
        slow_sigma = state.volatility.sigma
        if slow_sigma is None or state.volatility.samples < settings.shadow_vol_min_samples:
            observation.reason = "vol_not_warm"
            return
        sigma_fast = (
            state.fast_volatility.sigma
            if state.fast_volatility.samples >= settings.shadow_vol_fast_min_samples
            else None
        )
        sigma = stressed_sigma(slow_sigma, sigma_fast)
        if sigma is None:
            observation.reason = "vol_not_warm"
            return
        time_left = market.window_end - now
        active_variants = active_entry_variants(self.entry_variants, time_left)
        pending_variants = [
            variant
            for variant in active_variants
            if (market.condition_id, variant[0]) not in self.entered_conditions
        ]
        maker_key = (asset, start)
        maker_order = self.maker_orders.get(maker_key)
        maker_can_place = (
            settings.shadow_maker_enabled
            and maker_order is None
            and market.condition_id not in self.maker_attempted_conditions
            and settings.shadow_entry_min_sec <= time_left <= settings.shadow_entry_max_sec
        )
        if not pending_variants and maker_order is None and not maker_can_place:
            observation.reason = "already_entered" if active_variants else "outside_entry_window"
            return
        p_up = probability_up(
            state.price,
            open_price,
            sigma,
            time_left,
            sigma_floor=settings.shadow_sigma_floor,
            z_cap=settings.shadow_model_z_cap,
        )
        side = "up" if p_up >= 0.5 else "down"
        model_p = p_up if side == "up" else 1.0 - p_up
        from core.polymarket import get_order_book

        books: dict[str, dict | None] = {}

        async def load_book(book_side: str) -> dict | None:
            token = market.tokens[book_side]
            if token not in books:
                books[token] = await asyncio.to_thread(get_order_book, token)
            return books[token]

        if maker_order is not None and maker_order.terminal_status is None:
            maker_book = await load_book(maker_order.side)
            best_ask = maker_book.get("best_ask") if maker_book else None
            p_side = p_up if maker_order.side == "up" else 1.0 - p_up
            if maker_fill(best_ask, maker_order.bid):
                self._complete_maker_order(
                    maker_order,
                    status="open",
                    note="filled",
                    entered_at=now,
                    time_left=time_left,
                )
            elif maker_should_cancel(
                p_side,
                maker_order.bid,
                settings.shadow_maker_cancel_edge,
            ):
                self._complete_maker_order(
                    maker_order,
                    status="unfilled",
                    note="cancelled_edge_lost",
                    entered_at=maker_order.placed_at,
                    time_left=time_left,
                )
            if maker_order.terminal_status is not None:
                await self._persist_maker_order(maker_key, maker_order)

        if maker_can_place:
            placement_book = await load_book(side)
            best_bid = placement_book.get("best_bid") if placement_book else None
            best_ask = placement_book.get("best_ask") if placement_book else None
            bid = maker_bid_price(best_bid, best_ask, market.tick_size)
            if bid is not None:
                maker_edge = model_p - bid
                if (
                    maker_edge >= settings.shadow_maker_min_edge
                    and not divergence_exceeds_ceiling(
                        model_p,
                        bid,
                        settings.shadow_max_model_divergence,
                    )
                    and bid <= settings.shadow_max_price
                ):
                    placement_payload = {
                        "asset": asset,
                        "condition_id": market.condition_id,
                        "token_id": market.tokens[side],
                        "side": side,
                        "window_start": _iso(start),
                        "window_end": _iso(market.window_end),
                        "placed_at": _iso(now),
                        "time_left_sec": round(time_left, 3),
                        "model_p": round(model_p, 8),
                        "spot": state.price,
                        "open_price": open_price,
                        "sigma": sigma,
                        "sigma_fast": sigma_fast,
                        "q_cal": calibrated_probability(
                            model_p,
                            bid,
                            settings.shadow_calibration_lambda,
                        ),
                        "book_best_ask": best_ask,
                        "sim_fill_price": bid,
                        "sim_shares": settings.shadow_stake_usdc / bid,
                        "stake_usdc": settings.shadow_stake_usdc,
                        "fee_usdc": 0.0,
                        "edge": maker_edge,
                    }
                    maker_order = MakerOrder(
                        market=market,
                        side=side,
                        bid=bid,
                        edge=maker_edge,
                        placed_at=now,
                        placement_payload=placement_payload,
                    )
                    self.maker_orders[maker_key] = maker_order
                    self.maker_attempted_conditions.add(market.condition_id)
                    log.info(
                        "shadow_maker_placed",
                        asset=asset,
                        condition_id=market.condition_id,
                        side=side,
                        bid=round(bid, 6),
                        edge=round(maker_edge, 6),
                        time_left=round(time_left, 3),
                    )

        if not pending_variants:
            observation.reason = "already_entered"
            return

        token_id = market.tokens[side]
        book = await load_book(side)
        if not book:
            observation.reason = "book_unavailable"
            return
        fill = walk_order_book(
            book.get("asks") or [],
            settings.shadow_stake_usdc,
            fee_rate=market.fee_rate,
            fee_exponent=market.fee_exponent,
            completion_epsilon_usdc=settings.shadow_fill_epsilon_usdc,
        )
        if fill.shares <= 0:
            observation.reason = "no_ask_depth"
            return
        edge = model_p - fill.effective_price - fill.fee_per_share
        if observation.best_edge is None or edge > observation.best_edge:
            observation.best_edge = edge
            observation.model_p = model_p
            observation.ask = fill.best_ask
        if divergence_exceeds_ceiling(
            model_p,
            fill.effective_price,
            settings.shadow_max_model_divergence,
        ):
            observation.reason = "model_divergence_ceiling"
            return
        if fill.effective_price > settings.shadow_max_price:
            observation.reason = "price_above_ceiling"
            return
        # BP50: alts record at a lower bar purely to grow the dataset — at the
        # BTC bar they produced ~0.7 rows/day, unusable for model work.
        min_edge = (
            settings.shadow_min_edge if asset == "btc" else settings.shadow_alt_min_edge
        )
        if edge < min_edge:
            observation.reason = "edge_below_threshold"
            return

        payload = {
            "asset": asset,
            "condition_id": market.condition_id,
            "token_id": token_id,
            "side": side,
            "window_start": _iso(start),
            "window_end": _iso(market.window_end),
            "entered_at": _iso(now),
            "time_left_sec": round(time_left, 3),
            "model_p": round(model_p, 8),
            "spot": state.price,
            "open_price": open_price,
            "sigma": sigma,
            "sigma_fast": sigma_fast,
            "q_cal": calibrated_probability(
                model_p,
                fill.effective_price,
                settings.shadow_calibration_lambda,
            ),
            "book_best_ask": fill.best_ask,
            "sim_fill_price": fill.effective_price,
            "sim_shares": fill.shares,
            "stake_usdc": fill.filled_usdc,
            "fee_usdc": fill.fee_usdc,
            "edge": edge,
            "status": "open",
        }
        if time.monotonic() < self.db_retry_after:
            observation.reason = "db_retry_backoff"
            return
        is_signal = passes_signal_filter(
            edge,
            state.price,
            open_price,
            min_edge=settings.shadow_filter_min_edge,
            min_strike_bp=settings.shadow_filter_min_strike_bp,
        )
        inserted_any = False
        saw_duplicate = False
        for variant_name, _, _ in pending_variants:
            variant_payload = {**payload, "variant": variant_name}
            insert_result = await asyncio.to_thread(self._insert_trade, variant_payload)
            condition_variant = (market.condition_id, variant_name)
            if insert_result == "error":
                self.db_retry_after = time.monotonic() + settings.shadow_db_retry_sec
                observation.reason = "db_unavailable"
                return
            self.entered_conditions.add(condition_variant)
            if insert_result == "duplicate":
                saw_duplicate = True
                observation.entered = True
                observation.entered_variants.add(variant_name)
                continue

            inserted_any = True
            observation.entered = True
            observation.entered_variants.add(variant_name)
            # BP49: the executor feed fires only inside the profitable entry
            # window (60-90s to close; every earlier/later bucket is negative
            # over the whole real-money era — see CURSOR.md BP49). Collection
            # is untouched: all variants keep recording, so the wide 'full'
            # stream still feeds the BP45 WR gate and future re-analysis.
            publish_window = (
                settings.crypto_signal_time_left_min_sec
                <= time_left
                <= settings.crypto_signal_time_left_max_sec
            )
            # BP50: only whitelisted assets reach the real-money executor —
            # alt entries are data collection, never execution signals.
            if (
                is_signal
                and publish_window
                and asset in settings.crypto_signal_assets
                and market.condition_id not in self.published_conditions
            ):
                if len(self.published_conditions) > 4000:
                    self.published_conditions.clear()
                self.published_conditions.add(market.condition_id)
                # BP33: hand the filter-passing signal to the real-money executor.
                # BP36: attach book-depth bands (capacity telemetry for scaling).
                asks = book.get("asks") or []
                best = fill.best_ask or 0.0
                self._spawn_publish(
                    {
                        "asset": asset,
                        "condition_id": market.condition_id,
                        "token_id": token_id,
                        "side": side,
                        "price": fill.effective_price,
                        "best_ask": fill.best_ask,
                        "tick_size": market.tick_size,
                        "fee_rate": market.fee_rate,
                        "fee_exponent": market.fee_exponent,
                        "window_end": market.window_end,
                        "model_p": model_p,
                        "edge": edge,
                        "depth_best_usdc": ask_depth_usdc(asks, best),
                        "depth_150bp_usdc": ask_depth_usdc(asks, best * 1.015),
                        "depth_300bp_usdc": ask_depth_usdc(asks, best * 1.03),
                        "published_at": time.time(),
                    }
                )
                side_label = "Up ⬆️" if side == "up" else "Down ⬇️"
                window_end_utc = datetime.fromtimestamp(
                    market.window_end,
                    tz=timezone.utc,  # noqa: UP017 - production currently runs Python 3.10.
                )
                self._spawn_signal(
                    f"🎯 Сигнал: {asset.upper()} {side_label}\n"
                    f"Вход: {fill.effective_price:.3f} | Ставка: ${fill.filled_usdc:.2f}\n"
                    f"Модель: {model_p:.0%} | Edge: {edge:+.1%}\n"
                    f"Окно закрывается в {window_end_utc:%H:%M:%S} UTC"
                )
            log.info(
                "shadow_virtual_entry",
                asset=asset,
                condition_id=market.condition_id,
                variant=variant_name,
                side=side,
                time_left_sec=round(time_left, 3),
                model_p=round(model_p, 6),
                fill_price=round(fill.effective_price, 6),
                filled_usdc=round(fill.filled_usdc, 6),
                requested_usdc=fill.requested_usdc,
                depth_complete=fill.complete,
                fee_usdc=fill.fee_usdc,
                edge=round(edge, 6),
                signal=is_signal,
            )
        observation.reason = (
            "entered" if inserted_any else ("db_duplicate" if saw_duplicate else "already_entered")
        )

    @staticmethod
    def _insert_trade(payload: dict[str, Any]) -> str:
        try:
            sb = get_supabase()
            existing = (
                sb.table("shadow_trades")
                .select("id")
                .eq("condition_id", payload["condition_id"])
                .eq("variant", payload["variant"])
                .limit(1)
                .execute()
                .data
                or []
            )
            if existing:
                return "duplicate"
            sb.table("shadow_trades").insert(payload).execute()
            return "inserted"
        except Exception as exc:
            log.error("shadow_trade_insert_failed", error=str(exc)[:500])
            return "error"

    def _log_completed_windows(self, now: float) -> None:
        current = self._window_start(now)
        for asset in self.assets:
            previous = current - settings.shadow_window_sec
            key = (asset, previous)
            if key in self.logged_windows or now < previous + settings.shadow_window_sec:
                continue
            observation = self._observation(asset, previous)
            log.info(
                "shadow_window",
                asset=asset,
                window_start=_iso(previous),
                window_end=_iso(previous + settings.shadow_window_sec),
                condition_id=observation.condition_id,
                entered=observation.entered,
                entered_variants=sorted(observation.entered_variants),
                best_edge=observation.best_edge,
                model_p=observation.model_p,
                ask=observation.ask,
                reason=observation.reason,
            )
            self.logged_windows.add(key)
        oldest = current - 2 * settings.shadow_window_sec
        self.observations = {
            key: value for key, value in self.observations.items() if key[1] >= oldest
        }

    async def evaluation_loop(self) -> None:
        while True:
            now = time.time()
            try:
                await asyncio.gather(*(self._evaluate_asset(asset, now) for asset in self.assets))
                self._log_completed_windows(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("shadow_evaluation_failed")
            await asyncio.sleep(settings.shadow_evaluation_interval_sec)

    @staticmethod
    def _open_trades() -> list[dict[str, Any]]:
        return (
            get_supabase()
            .table("shadow_trades")
            .select(
                "id,condition_id,token_id,window_end,sim_shares,"
                "stake_usdc,fee_usdc,asset,side,sim_fill_price,variant,"
                "edge,spot,open_price"
            )
            .eq("status", "open")
            .order("window_end")
            .execute()
            .data
            or []
        )

    @staticmethod
    def _row_is_signal(row: dict[str, Any]) -> bool:
        """Settlement notifications must mirror the entry-side signal set.

        BP49/BP50: entries are published from the execution-window bucket
        (t60-90 by default) for whitelisted assets only — win/loss notices
        key on the same variant and asset, or subscribers would get results
        for trades they never saw open (and vice versa)."""
        exec_variant = (
            f"t{settings.crypto_signal_time_left_min_sec:g}"
            f"-{settings.crypto_signal_time_left_max_sec:g}"
        )
        return (
            row.get("variant") == exec_variant
            and str(row.get("asset") or "") in settings.crypto_signal_assets
            and passes_signal_filter(
                float(row.get("edge") or 0),
                float(row.get("spot") or 0),
                float(row.get("open_price") or 0),
                min_edge=settings.shadow_filter_min_edge,
                min_strike_bp=settings.shadow_filter_min_strike_bp,
            )
        )

    async def _resolve_trade(self, row: dict[str, Any], now: datetime) -> None:
        window_end = datetime.fromisoformat(str(row["window_end"]).replace("Z", "+00:00"))
        if now < window_end:
            return
        condition_id = str(row["condition_id"])
        token_id = str(row["token_id"])
        from core.relayer import (
            detect_outcome_index,
            get_payout_numerator,
            is_condition_resolved,
        )

        resolved = await asyncio.to_thread(is_condition_resolved, condition_id)
        if not resolved:
            age = (now - window_end).total_seconds()
            if age >= settings.shadow_resolution_void_after_sec:
                await asyncio.to_thread(
                    self._settle_row,
                    int(row["id"]),
                    "void",
                    0.0,
                    now,
                )
                if self._row_is_signal(row):
                    await self._broadcast_signal(
                        f"⚠️ {str(row.get('asset') or '').upper()} "
                        f"{str(row.get('side') or '').capitalize()}: "
                        "рынок не резолвился за 24 часа, сделка аннулирована"
                    )
            return
        outcome_index = await asyncio.to_thread(
            detect_outcome_index,
            condition_id,
            token_id,
        )
        if outcome_index is None:
            log.warning("shadow_resolution_index_unknown", condition_id=condition_id)
            return
        held_payout = await asyncio.to_thread(
            get_payout_numerator,
            condition_id,
            outcome_index,
        )
        other_payout = await asyncio.to_thread(
            get_payout_numerator,
            condition_id,
            1 - outcome_index,
        )
        if held_payout <= 0 and other_payout <= 0:
            log.warning("shadow_resolution_payout_unavailable", condition_id=condition_id)
            return
        won = held_payout > 0
        stake = float(row["stake_usdc"])
        fee = float(row["fee_usdc"])
        shares = float(row["sim_shares"])
        pnl = shares - stake - fee if won else -stake - fee
        await asyncio.to_thread(
            self._settle_row,
            int(row["id"]),
            "win" if won else "loss",
            pnl,
            now,
        )
        cost = stake + fee
        roi = pnl / cost if cost > 0 else 0.0
        header = "✅ Победа" if won else "❌ Проигрыш"
        if self._row_is_signal(row):
            self._spawn_signal(
                f"{header}: {str(row.get('asset') or '').upper()} "
                f"{str(row.get('side') or '').capitalize()}\n"
                f"Вход: {float(row.get('sim_fill_price') or 0):.3f} | "
                f"Ставка: ${stake:.2f}\n"
                f"PnL: ${pnl:+.2f} ({roi:+.1%})"
            )

    @staticmethod
    def _settle_row(row_id: int, status: str, pnl: float, now: datetime) -> None:
        get_supabase().table("shadow_trades").update(
            {
                "status": status,
                "resolved_at": now.isoformat(),
                "pnl_usdc": round(pnl, 6),
            }
        ).eq("id", row_id).eq("status", "open").execute()
        log.info("shadow_virtual_settled", trade_id=row_id, status=status, pnl_usdc=pnl)

    async def resolution_loop(self) -> None:
        while True:
            try:
                rows = await asyncio.to_thread(self._open_trades)
                now = datetime.now(
                    timezone.utc  # noqa: UP017 - production currently runs Python 3.10.
                )
                for row in rows:
                    await self._resolve_trade(row, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "shadow_resolution_cycle_failed",
                    error=str(exc)[:500],
                    retry_sec=settings.shadow_db_retry_sec,
                )
                await asyncio.sleep(settings.shadow_db_retry_sec)
                continue
            await asyncio.sleep(settings.shadow_resolution_poll_sec)

    @staticmethod
    def _digest_stats(start: datetime, end: datetime) -> tuple[list[dict], list[dict]]:
        sb = get_supabase()
        yesterday = (
            sb.table("shadow_trades")
            .select("status,pnl_usdc,stake_usdc")
            .gte("entered_at", start.isoformat())
            .lt("entered_at", end.isoformat())
            .in_("status", ["win", "loss"])
            .eq("variant", "full")
            .execute()
            .data
            or []
        )
        total = (
            sb.table("shadow_trades")
            .select("status,pnl_usdc,stake_usdc")
            .in_("status", ["win", "loss"])
            .eq("variant", "full")
            .execute()
            .data
            or []
        )
        return yesterday, total

    async def digest_loop(self) -> None:
        if not settings.shadow_digest_telegram_ids:
            return
        while True:
            now = datetime.now(
                timezone.utc  # noqa: UP017 - production currently runs Python 3.10.
            )
            send_after = now.replace(
                hour=settings.shadow_digest_hour_utc,
                minute=settings.shadow_digest_minute_utc,
                second=0,
                microsecond=0,
            )
            if now < send_after:
                await asyncio.sleep(
                    min(
                        (send_after - now).total_seconds(),
                        settings.shadow_digest_poll_sec,
                    )
                )
                continue
            day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_start = day_end - timedelta(days=1)
            try:
                yesterday, total = await asyncio.to_thread(
                    self._digest_stats,
                    day_start,
                    day_end,
                )
                text = _digest_text(day_start, yesterday, total)
                for chat_id in settings.shadow_digest_telegram_ids:
                    key = f"shadow-digest:{day_start.date()}:{chat_id}"
                    if not notify_once(key, ttl=settings.shadow_digest_throttle_sec):
                        continue
                    try:
                        await _telegram_send(chat_id, text)
                    except Exception:
                        clear_once(key)
                        log.exception("shadow_digest_send_failed", chat_id=chat_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("shadow_digest_failed")
            await asyncio.sleep(settings.shadow_digest_poll_sec)

    async def run(self) -> None:
        if not self.assets:
            raise RuntimeError("SHADOW_ASSETS contains no supported assets")
        log.info(
            "shadow_engine_started",
            assets=self.assets,
            stake_usdc=settings.shadow_stake_usdc,
            entry_min_sec=settings.shadow_entry_min_sec,
            entry_max_sec=settings.shadow_entry_max_sec,
            entry_variants=self.entry_variants,
            min_edge=settings.shadow_min_edge,
        )
        await asyncio.gather(
            self.spot_feed_loop(),
            self.discovery_loop(),
            self.evaluation_loop(),
            self.resolution_loop(),
            self.digest_loop(),
        )


def _market_tokens(market: dict[str, Any]) -> dict[str, str]:
    outcomes = market.get("outcomes") or []
    token_ids = market.get("clobTokenIds") or []
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return {}
    return {str(outcome).lower(): str(token_id) for outcome, token_id in zip(outcomes, token_ids)}


def _clob_market_tokens(market: dict[str, Any]) -> dict[str, str]:
    tokens = market.get("t") or []
    if not isinstance(tokens, list):
        return {}
    return {
        str(token.get("o") or "").lower(): str(token.get("t") or "")
        for token in tokens
        if isinstance(token, dict) and token.get("o") and token.get("t")
    }


def _parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _iso(timestamp_sec: int | float) -> str:
    return datetime.fromtimestamp(
        timestamp_sec,
        tz=timezone.utc,  # noqa: UP017 - production currently runs Python 3.10.
    ).isoformat()


def _summarize(rows: list[dict]) -> tuple[int, int, float, float]:
    count = len(rows)
    wins = sum(1 for row in rows if row.get("status") == "win")
    pnl = sum(float(row.get("pnl_usdc") or 0) for row in rows)
    stake = sum(float(row.get("stake_usdc") or 0) for row in rows)
    return count, wins, pnl, stake


def _digest_text(day: datetime, yesterday: list[dict], total: list[dict]) -> str:
    day_count, day_wins, day_pnl, day_stake = _summarize(yesterday)
    total_count, total_wins, total_pnl, total_stake = _summarize(total)
    day_winrate = day_wins / day_count if day_count else 0.0
    total_winrate = total_wins / total_count if total_count else 0.0
    day_roi = day_pnl / day_stake if day_stake else 0.0
    total_roi = total_pnl / total_stake if total_stake else 0.0
    return (
        f"Shadow за {day:%d.%m}: {day_count} сделок\n"
        f"Winrate: {day_winrate:.1%}\n"
        f"Net PnL: ${day_pnl:+.2f} ({day_roi:+.1%})\n"
        f"Всего: {total_count} сделок\n"
        f"Общий winrate: {total_winrate:.1%}\n"
        f"Общий net PnL: ${total_pnl:+.2f} ({total_roi:+.1%})"
    )


async def _telegram_send(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=settings.shadow_http_timeout_sec) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        response.raise_for_status()


async def main() -> None:
    if not settings.shadow_enabled:
        log.warning("shadow_engine_disabled")
        while True:
            await asyncio.sleep(settings.shadow_db_retry_sec)
    await ShadowEngine().run()


if __name__ == "__main__":
    asyncio.run(main())
