"""
Blueprint 26.5 — real-time sniper feed over the Polymarket RTDS WebSocket.

Subscribes to the platform-wide `activity` / `orders_matched` stream
(wss://ws-live-data.polymarket.com) and filters fills client-side by the
sniper donors' proxy wallets, cutting donor→entry latency from the Data-API
polling path (5-20 s indexing lag) down to ~1-2 s after the on-chain match.

Protocol facts (docs.polymarket.com + Polymarket/real-time-data-client):
  * the `trades` subscription type is dead — only `orders_matched` delivers;
  * server-side filters support only event/market slug, NOT wallets, so we
    subscribe unfiltered and match `proxyWallet` locally (cheap dict lookup);
  * the server expects a literal "ping" text frame every ~5 s;
  * connections are known to die silently, so any prolonged full silence
    (the unfiltered stream is never quiet for long) forces a reconnect.

Runs as a daemon thread inside the BEAT container (worker/beat.py): exactly
one replica and a plain (non-gevent) interpreter, so the synchronous
`websockets` client is safe. The 3-second Data-API poller stays as a
fallback; the shared Redis once-key inside fire_sniper_signal() guarantees
at most one entry per market whichever path sees the fill first.
"""

import json
import time

import structlog

from core.config import settings

log = structlog.get_logger(__name__)

_SUBSCRIBE_MSG = json.dumps(
    {
        "action": "subscribe",
        "subscriptions": [{"topic": "activity", "type": "orders_matched"}],
    },
    separators=(",", ":"),  # RTDS matches filter strings byte-exactly; stay compact
)
_PING_EVERY_SEC = 5.0


class SniperFeed:
    def __init__(self) -> None:
        self.donors: dict[str, dict] = {}  # lower(address) -> tracked_wallets row
        self._donors_loaded_at = 0.0

    # ── Donor universe ──────────────────────────────────────────────────────
    def refresh_donors(self) -> None:
        from core.db import list_tracked_wallets

        rows = [w for w in list_tracked_wallets()
                if (w.get("mode") or "default") == "sniper"]
        self.donors = {
            (w.get("address") or "").lower(): w for w in rows if w.get("address")
        }
        self._donors_loaded_at = time.time()
        log.info("sniper_ws_donors_refreshed", donors=len(self.donors))

    def _maybe_refresh_donors(self) -> None:
        if time.time() - self._donors_loaded_at < settings.sniper_ws_refresh_donors_sec:
            return
        try:
            self.refresh_donors()
        except Exception:
            log.warning("sniper_ws_donor_refresh_failed")
            self._donors_loaded_at = time.time()  # don't hammer a dead DB

    # ── Fill handling ───────────────────────────────────────────────────────
    def handle_trade(self, t: dict) -> None:
        addr = (t.get("proxyWallet") or "").lower()
        w = self.donors.get(addr)
        if w is None:
            return
        if (t.get("side") or "").upper() != "BUY":
            return
        cond = t.get("conditionId") or ""
        token = str(t.get("asset") or "")
        if not cond or not token:
            return

        try:
            ts = int(t.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts > 10**12:  # defensive: milliseconds → seconds
            ts //= 1000
        lag = round(time.time() - ts, 2) if ts else None
        # Reconnect replays / clock skew guard — same freshness bar as the poller.
        if ts and (time.time() - ts) > settings.sniper_max_trade_age_sec:
            log.info("sniper_ws_stale_fill", wallet=addr[:10], lag_sec=lag)
            return

        try:
            price = float(t.get("price") or 0)
            shares = float(t.get("size") or 0)  # RTDS size is in SHARES (like Data-API)
        except (TypeError, ValueError):
            return
        if price <= 0 or shares <= 0:
            return

        allowed = [int(x) for x in (w.get("allowed_telegram_ids") or [])]
        if not allowed:
            return

        fill = {
            "id":            t.get("transactionHash") or "",
            "tx_hash":       t.get("transactionHash") or "",
            "condition_id":  cond,
            "token_id":      token,
            "side":          "BUY",
            "price":         price,
            "size_usdc":     round(shares * price, 4),
            "timestamp":     ts,
            "title":         t.get("title") or "",
            "outcome":       t.get("outcome") or "",
            "outcome_index": t.get("outcomeIndex"),
            "event_slug":    t.get("eventSlug") or t.get("slug") or "",
        }
        from worker.tasks.poll_sniper_wallets import fire_sniper_signal

        log.info("sniper_ws_donor_fill", wallet=addr[:10], market=cond[:14],
                 price=price, shares=shares, usdc=fill["size_usdc"], lag_sec=lag)
        fire_sniper_signal(addr, allowed, cond, token, [fill])

    def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        # Server replies to our text "ping" and sends other non-JSON frames.
        if "payload" not in raw:
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            if not isinstance(m, dict):
                continue
            if m.get("topic") != "activity" or m.get("type") != "orders_matched":
                continue
            payload = m.get("payload")
            trades = payload if isinstance(payload, list) else [payload]
            for t in trades:
                if isinstance(t, dict):
                    try:
                        self.handle_trade(t)
                    except Exception:
                        log.exception("sniper_ws_trade_handler_failed")

    # ── Connection loop ─────────────────────────────────────────────────────
    def _session(self) -> None:
        from websockets.sync.client import connect

        # ping_interval=None: RTDS doesn't play well with protocol-level
        # keepalive (silent drops) — we send the text "ping" the official
        # client uses and enforce liveness via the silence window instead.
        with connect(settings.sniper_ws_url, open_timeout=20,
                     close_timeout=5, max_size=None,
                     ping_interval=None) as ws:
            ws.send(_SUBSCRIBE_MSG)
            log.info("sniper_ws_connected", url=settings.sniper_ws_url)

            last_ping = 0.0
            last_rx = time.time()
            while True:
                now = time.time()
                if now - last_ping >= _PING_EVERY_SEC:
                    ws.send("ping")
                    last_ping = now
                if now - last_rx > settings.sniper_ws_silence_reconnect_sec:
                    raise ConnectionError(
                        f"no frames for {settings.sniper_ws_silence_reconnect_sec}s"
                    )
                self._maybe_refresh_donors()
                try:
                    raw = ws.recv(timeout=_PING_EVERY_SEC)
                except TimeoutError:
                    continue
                last_rx = time.time()
                try:
                    self._handle_raw(raw)
                except Exception:
                    log.exception("sniper_ws_frame_failed")

    def run(self) -> None:
        backoff = 1
        while True:
            if not (settings.auto_copy_enabled and settings.sniper_ws_enabled):
                time.sleep(60)
                continue
            try:
                self.refresh_donors()
                if not self.donors:
                    log.info("sniper_ws_idle_no_donors")
                    time.sleep(30)
                    continue
                self._session()
                backoff = 1
            except Exception as exc:
                log.warning("sniper_ws_session_error",
                            error=str(exc)[:200], backoff=backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


def run_listener() -> None:
    """Blocking entrypoint — run in a daemon thread (see worker/beat.py)."""
    SniperFeed().run()
