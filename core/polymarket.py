"""
Polymarket data access for the whale-tracking strategy.

Two data sources are used:
  * Gamma API  (gamma-api.polymarket.com/markets)  — list of "fast" markets that
    resolve soon, plus per-market trading metadata (tick size, neg-risk, min size).
  * Data API   (data-api.polymarket.com/trades)     — a GLOBAL feed of recent trades
    with server-side cash filtering, used to detect large ("whale") buys.

The legacy per-donor poller (`fetch_donor_recent_trades`) is kept for the optional
donor-copy mode but is no longer the primary signal source.
"""

import json
import time
from datetime import datetime, timezone

import httpx
import structlog

from core.config import settings

log = structlog.get_logger(__name__)

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
DATA_API_TRADES_URL = "https://data-api.polymarket.com/trades"
DATA_API_ACTIVITY_URL = "https://data-api.polymarket.com/activity"
DATA_API_POSITIONS_URL = "https://data-api.polymarket.com/positions"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
CLOB_FEE_RATE_URL = "https://clob.polymarket.com/fee-rate"
EVENT_URL_BASE = "https://polymarket.com/event/"

_HEADERS = {"User-Agent": "polymarket-copybot/1.0"}


def event_url(event_slug: str | None) -> str | None:
    """Public Polymarket event page URL for a given event slug."""
    return f"{EVENT_URL_BASE}{event_slug}" if event_slug else None


def smart_truncate(text: str | None, limit: int = 50) -> str:
    """Truncate *text* to at most *limit* characters at the last word boundary.

    Blueprint 20.C: replaces scattered [:N] hard-slices throughout the
    notification templates.  Rules:
    - Returns '—' for None / empty.
    - Returns the full string when it fits within *limit*.
    - Cuts at the last space at or before *limit* and appends '…' (U+2026).
    - Falls back to a hard cut at *limit* when no space is found (e.g. one
      very long token).
    """
    if not text:
        return "—"
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip() + "…"


def resolve_outcome_name(
    outcome: str | None,
    outcome_index: int | None = None,
    condition_id: str | None = None,
    signal_id: int | None = None,
) -> str:
    """Central outcome resolver with a five-tier fallback chain.

    Blueprint 20.B: fixes 'Исход: —' notifications on grouped / scalar
    markets where the Data API returns an empty ``outcome`` field.

    Tier 1 — API outcome (direct, already normalised at call site).
    Tier 2 — trade_signals.outcome stored at copy-entry time.
    Tier 3 — Gamma market outcomes[outcome_index].
    Tier 4 — Gamma groupItemTitle (e.g. "Any Other Score").
    Tier 5 — binary default from outcome_index (0=Yes, 1=No).
    Last resort — '—'.
    """
    # Tier 1
    if outcome and outcome.strip():
        return outcome.strip()

    # Tier 2 — trade_signals.outcome via signal_id
    if signal_id:
        try:
            from core.db.session import get_supabase
            sb = get_supabase()
            sig = (
                sb.table("trade_signals")
                .select("outcome")
                .eq("id", signal_id)
                .maybe_single()
                .execute()
            )
            sig_out = ((sig.data or {}).get("outcome") or "").strip()
            if sig_out:
                return sig_out
        except Exception:
            pass

    # Tier 3 & 4 — Gamma market data
    if condition_id:
        try:
            resp = httpx.get(
                GAMMA_MARKETS_URL,
                params={"conditionId": condition_id},
                timeout=8.0,
                headers=_HEADERS,
            )
            resp.raise_for_status()
            markets = resp.json()
            if isinstance(markets, list) and markets:
                m = markets[0]
                # Tier 4 — groupItemTitle (grouped/scalar markets like "Any Other Score")
                group_title = (m.get("groupItemTitle") or "").strip()
                if group_title:
                    return group_title
                # Tier 3 — outcomes[outcome_index]
                if outcome_index is not None:
                    raw_out = m.get("outcomes") or "[]"
                    try:
                        outs = (
                            json.loads(raw_out)
                            if isinstance(raw_out, str)
                            else (raw_out or [])
                        )
                        if outcome_index < len(outs) and str(outs[outcome_index]).strip():
                            return str(outs[outcome_index]).strip()
                    except Exception:
                        pass
        except Exception:
            log.warning("resolve_outcome_gamma_failed", condition_id=(condition_id or "")[:14])

    # Tier 5 — binary default
    if outcome_index is not None:
        return "Yes" if int(outcome_index) == 0 else "No"

    return "—"

# ── Fast-markets cache ──────────────────────────────────────────────────────────
# condition_id -> market metadata dict
_fast_markets: dict[str, dict] = {}
_fast_markets_ts: float = 0.0
_GAMMA_PAGE = 100
_GAMMA_MAX_MARKETS = 1000


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from dateutil.parser import parse as parse_dt

        dt = parse_dt(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _hours_until(end_iso: str | None) -> float | None:
    end = _parse_iso(end_iso)
    if end is None:
        return None
    return (end - datetime.now(timezone.utc)).total_seconds() / 3600


def resolution_dt(obj: dict) -> datetime | None:
    """
    Select the authoritative resolution datetime from a Gamma market dict.

    Candidate ISO strings (in authority order, most reliable first):
      1. market endDateIso
      2. events[0].endDate  — event boundary, correct for grouped/sports markets
      3. market endDate     — often an understated placeholder; used as fallback

    Parsing rules:
      - Strings with timezone info → kept as-is.
      - Naive strings (no tz) → assumed UTC.
      - Date-only "YYYY-MM-DD" → 23:59:59Z of that day (never midnight, which
        would understate the deadline by up to a full day).

    Selection: return the LATEST candidate that is still in the future; if none
    are in the future (market resolving/already resolved), return the latest
    candidate overall so callers can detect the resolution window.

    Note: gameStartTime is a match START, never a resolution — it must not be
    used as the deadline.
    """
    now = datetime.now(timezone.utc)

    def _parse_candidate(value: str | None) -> datetime | None:
        if not value:
            return None
        stripped = value.strip()
        # Date-only "YYYY-MM-DD" → treat as end of day so we never understate.
        if len(stripped) == 10 and stripped.count("-") == 2 and "T" not in stripped:
            try:
                y, m, d = (int(x) for x in stripped.split("-"))
                return datetime(y, m, d, 23, 59, 59, tzinfo=timezone.utc)
            except Exception:
                pass
        return _parse_iso(value)

    events = obj.get("events") or []
    event_end_iso = (events[0] or {}).get("endDate") if events else None

    raw_candidates = [
        obj.get("endDateIso"),
        event_end_iso,
        obj.get("endDate"),
    ]

    candidates: list[datetime] = []
    for val in raw_candidates:
        dt = _parse_candidate(val)
        if dt is not None:
            candidates.append(dt)

    if not candidates:
        return None

    future = [c for c in candidates if c > now]
    return max(future) if future else max(candidates)


def format_time_left(resolution_iso: str | None,
                     now: datetime | None = None) -> str:
    """
    Human-readable time until resolution, computed fresh at call time.

    This is the canonical display formatter for every notification site;
    it must NEVER read a cached/frozen scalar — always compute from the ISO string.
    """
    from core.config import settings as _s

    dt = _parse_iso(resolution_iso)
    now = now or datetime.now(timezone.utc)
    if dt is None:
        return "время уточняется"
    delta = (dt - now).total_seconds()
    if delta <= 0:
        return "резолв скоро"
    if delta < 3600:
        return "<1 ч"
    hours = delta / 3600
    if delta < 86400:
        if _s.show_resolution_in_et:
            try:
                from zoneinfo import ZoneInfo
                et = dt.astimezone(ZoneInfo("America/New_York"))
                return f"~{hours:.0f} ч (до {et.strftime('%H:%M')} ET)"
            except Exception:
                pass
        return f"~{hours:.0f} ч"
    d, rem_h = divmod(int(hours), 24)
    return f"{d}д {rem_h}ч"


def _parse_tokens(m: dict) -> list[dict]:
    """Return list of {token_id, outcome} for a market."""
    raw_ids = m.get("clobTokenIds")
    if not raw_ids:
        return []
    try:
        ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        ids = [str(t) for t in ids if t]
    except Exception:
        return []

    raw_out = m.get("outcomes")
    try:
        outcomes = json.loads(raw_out) if isinstance(raw_out, str) else (raw_out or [])
    except Exception:
        outcomes = []

    return [
        {"token_id": tid, "outcome": str(outcomes[i]) if i < len(outcomes) else ""}
        for i, tid in enumerate(ids)
    ]


def _build_market_meta(m: dict) -> dict | None:
    """Normalise a Gamma market into the metadata we need, or None if untradeable."""
    if not m.get("acceptingOrders") or not m.get("enableOrderBook"):
        return None
    if m.get("closed") or not m.get("active"):
        return None

    condition_id = m.get("conditionId")
    if not condition_id:
        return None

    # Blueprint 5: use resolution_dt so the event boundary (events[0].endDate)
    # takes priority over the per-market endDate, which is often an understated
    # placeholder for grouped/sports markets.
    res_dt = resolution_dt(m)
    if res_dt is None:
        return None

    now = datetime.now(timezone.utc)
    hours_left = (res_dt - now).total_seconds() / 3600
    if hours_left <= 0:
        return None
    if hours_left > settings.market_max_hours_to_resolve:
        return None
    # Skip ultra-fast markets (thin books, HFT-dominated) per strategy config.
    if hours_left < settings.market_min_hours_to_resolve:
        return None

    liquidity = float(m.get("liquidityNum") or 0)
    if settings.market_min_liquidity_usdc > 0 and liquidity < settings.market_min_liquidity_usdc:
        return None

    token_list = _parse_tokens(m)
    if not token_list:
        return None

    events = m.get("events") or []
    event_slug = (events[0].get("slug") if events else None) or m.get("slug")

    res_iso = res_dt.isoformat()
    return {
        "condition_id": condition_id,
        "title": m.get("question", ""),
        "end_date_iso": res_iso,
        "resolution_iso": res_iso,          # BP5: carry on the signal for fresh formatting
        "hours_to_resolve": round(hours_left, 2),  # kept for internal window filter only
        "tick_size": str(m.get("orderPriceMinTickSize") or "0.01"),
        "min_size": float(m.get("orderMinSize") or 5),
        "neg_risk": bool(m.get("negRisk", False)),
        "liquidity": liquidity,
        "tokens": [t["token_id"] for t in token_list],   # back-compat list of IDs
        "token_outcomes": {t["token_id"]: t["outcome"] for t in token_list},
        "event_slug": event_slug,
    }


def refresh_fast_markets() -> dict[str, dict]:
    """Rebuild the fast-markets cache from Gamma. Returns condition_id -> meta."""
    now = datetime.now(timezone.utc)
    end_max = now.timestamp() + settings.market_max_hours_to_resolve * 3600
    params_base = {
        "closed": "false",
        "active": "true",
        "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": datetime.fromtimestamp(end_max, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "order": "volume24hr",
        "ascending": "false",
        "limit": _GAMMA_PAGE,
    }

    markets: dict[str, dict] = {}
    offset = 0
    try:
        with httpx.Client(timeout=15.0, headers=_HEADERS) as client:
            while offset < _GAMMA_MAX_MARKETS:
                params = dict(params_base, offset=offset)
                resp = client.get(GAMMA_MARKETS_URL, params=params)
                resp.raise_for_status()
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                for m in batch:
                    meta = _build_market_meta(m)
                    if meta:
                        markets[meta["condition_id"]] = meta
                if len(batch) < _GAMMA_PAGE:
                    break
                offset += _GAMMA_PAGE
    except Exception:
        log.exception("refresh_fast_markets_failed")
        # Keep the previous cache on failure
        return _fast_markets

    log.info("fast_markets_refreshed", count=len(markets))
    return markets


def get_fast_markets(force: bool = False) -> dict[str, dict]:
    """Return the cached fast-markets map, refreshing it if stale."""
    global _fast_markets, _fast_markets_ts
    now = time.time()
    if force or not _fast_markets or (now - _fast_markets_ts) > settings.fast_markets_refresh_sec:
        _fast_markets = refresh_fast_markets()
        _fast_markets_ts = now
    return _fast_markets


def get_watch_markets() -> dict[str, dict]:
    """
    Build a token_id -> market-meta map for every tradeable fast market in range.
    Used by the WebSocket listener to know what to subscribe to and how to size/guard.
    Capped at watch_max_markets (markets ordered by Gamma volume24hr).
    """
    markets = list(get_fast_markets().values())
    # Already ordered by volume from Gamma; cap the universe.
    markets = markets[: settings.watch_max_markets]

    token_map: dict[str, dict] = {}
    for meta in markets:
        outcomes_by_id = meta.get("token_outcomes", {})
        for token_id in meta.get("tokens", []):
            token_map[token_id] = {
                **meta,
                "token_id": token_id,
                "outcome": outcomes_by_id.get(token_id, ""),
            }
    return token_map


def normalize_book(raw: dict) -> dict:
    """
    Normalise a CLOB /book (or WS book) payload to sorted bids/asks (best first)
    plus best_bid / best_ask. Sizes/prices are floats.
    """
    def _levels(side: list, *, descending: bool) -> list[dict]:
        out = []
        for lvl in side or []:
            try:
                out.append({"price": float(lvl["price"]), "size": float(lvl["size"])})
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x["price"], reverse=descending)
        return out

    bids = _levels(raw.get("bids", []), descending=True)   # best (highest) first
    asks = _levels(raw.get("asks", []), descending=False)  # best (lowest) first
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": bids[0]["price"] if bids else None,
        "best_ask": asks[0]["price"] if asks else None,
        "tick_size": str(raw.get("tick_size") or "0.01"),
        "neg_risk": bool(raw.get("neg_risk", False)),
        "min_order_size": float(raw.get("min_order_size") or 1),
    }


def get_order_book(token_id: str) -> dict | None:
    """Fetch and normalise the live order book for a token via CLOB REST."""
    try:
        resp = httpx.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=10.0,
                         headers=_HEADERS)
        resp.raise_for_status()
        return normalize_book(resp.json())
    except Exception:
        log.exception("get_order_book_failed", token=token_id[:18])
        return None


def get_fee_rate(token_id: str) -> float:
    """Return the market's base fee (basis points). Falls back to 0 on error."""
    try:
        resp = httpx.get(CLOB_FEE_RATE_URL, params={"token_id": token_id}, timeout=10.0,
                         headers=_HEADERS)
        resp.raise_for_status()
        return float(resp.json().get("base_fee", 0) or 0)
    except Exception:
        return 0.0


def fetch_whale_trades(min_usdc: float | None = None, limit: int | None = None) -> list[dict]:
    """
    Fetch the most recent large BUY trades across ALL markets via the global feed.
    Server-side filtered by cash size (filterType=CASH). Returns normalised trades.
    """
    min_usdc = min_usdc if min_usdc is not None else settings.whale_min_usdc
    limit = limit if limit is not None else settings.scan_trades_limit
    try:
        resp = httpx.get(
            DATA_API_TRADES_URL,
            params={
                "takerOnly": "true",
                "filterType": "CASH",
                "filterAmount": min_usdc,
                "limit": limit,
            },
            timeout=15.0,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            raw = raw.get("data", []) if isinstance(raw, dict) else []

        trades: list[dict] = []
        for t in raw:
            price = float(t.get("price") or 0)
            shares = float(t.get("size") or 0)
            usdc_size = round(shares * price, 2)
            trades.append({
                "tx_hash": t.get("transactionHash", ""),
                "condition_id": t.get("conditionId", ""),
                "token_id": t.get("asset", ""),
                "side": (t.get("side") or "BUY").upper(),
                "outcome": t.get("outcome", ""),
                "price": price,
                "shares": shares,
                "size_usdc": usdc_size,
                "title": t.get("title", ""),
                "whale_wallet": t.get("proxyWallet", ""),
                "whale_name": t.get("name") or t.get("pseudonym") or "",
                "timestamp": t.get("timestamp", 0),
            })
        return trades
    except Exception:
        log.exception("fetch_whale_trades_failed")
        return []


def get_positions(wallet_address: str) -> list[dict]:
    """
    Fetch the wallet's open positions with live P&L from the Polymarket data API.
    Returns normalised dicts (size in shares, prices/PnL in USDC terms).
    """
    try:
        resp = httpx.get(
            DATA_API_POSITIONS_URL,
            params={"user": wallet_address, "limit": 100},
            timeout=15.0,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            raw = raw.get("data", []) if isinstance(raw, dict) else []

        out = []
        for p in raw:
            out.append({
                "token_id":     str(p.get("asset", "")),
                "condition_id": p.get("conditionId", ""),
                "title":        p.get("title", ""),
                "outcome":      p.get("outcome", ""),
                "shares":       float(p.get("size") or 0),
                "avg_price":    float(p.get("avgPrice") or 0),
                "cur_price":    float(p.get("curPrice") or 0),
                "current_value": float(p.get("currentValue") or 0),
                "cash_pnl":     float(p.get("cashPnl") or 0),
                # Polymarket API returns percentPnl as a percentage value (e.g. -3.96
                # meaning -3.96%), NOT as a fraction (0.0396). Divide by 100 so the
                # rest of the codebase works with standard fraction form (1.0 = 100%).
                "percent_pnl":  float(p.get("percentPnl") or 0) / 100.0,
                "realized_pnl": float(p.get("realizedPnl") or 0),
                "redeemable":   bool(p.get("redeemable", False)),
                "neg_risk":     bool(p.get("negativeRisk", False)),
                "outcome_index": p.get("outcomeIndex"),
                "end_date":     p.get("endDate"),
                "event_slug":   p.get("eventSlug") or p.get("slug"),
            })
        return out
    except Exception:
        log.exception("get_positions_failed", wallet=wallet_address[:10])
        return []


def get_closed_positions(wallet_address: str) -> list[dict]:
    """
    Fetch settled/flat positions (resolved or fully sold) with realized P&L.
    cur_price ~1 = won, ~0 = lost; values in between = sold before resolution.
    `timestamp` is the settlement/close unix time.
    """
    try:
        resp = httpx.get(
            "https://data-api.polymarket.com/closed-positions",
            params={"user": wallet_address, "limit": 100},
            timeout=15.0,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            raw = raw.get("data", []) if isinstance(raw, dict) else []

        out = []
        for p in raw:
            out.append({
                "token_id":     str(p.get("asset", "")),
                "condition_id": p.get("conditionId", ""),
                "title":        p.get("title", ""),
                "outcome":      p.get("outcome", ""),
                "realized_pnl": float(p.get("realizedPnl") or 0),
                "cur_price":    float(p.get("curPrice") or 0),
                "timestamp":    int(p.get("timestamp") or 0),
                "event_slug":   p.get("eventSlug") or p.get("slug"),
            })
        return out
    except Exception:
        log.exception("get_closed_positions_failed", wallet=wallet_address[:10])
        return []


def fetch_donor_recent_trades(maker_address: str, limit: int = 10) -> list[dict]:
    """
    Legacy: fetch recent trades for a specific donor wallet (optional donor-copy mode).
    """
    try:
        resp = httpx.get(
            DATA_API_ACTIVITY_URL,
            params={"user": maker_address, "limit": limit},
            timeout=10.0,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            raw = raw.get("data", []) if isinstance(raw, dict) else []

        normalised = []
        for t in raw:
            if t.get("type") != "TRADE":
                continue
            normalised.append({
                "id":           t.get("transactionHash", ""),
                "trade_id":     t.get("transactionHash", ""),
                "market":       t.get("conditionId", ""),
                "condition_id": t.get("conditionId", ""),
                "asset_id":     t.get("asset", ""),
                "token_id":     t.get("asset", ""),
                "side":         t.get("side", "BUY"),
                "price":        float(t.get("price") or 0),
                "size":         float(t.get("usdcSize") or 0),
                "size_usdc":    float(t.get("usdcSize") or 0),
                "timestamp":    t.get("timestamp", 0),
                "title":        t.get("title", ""),
                "outcome":      t.get("outcome", ""),
                "event_slug":   t.get("eventSlug") or t.get("slug") or "",
                "outcome_index": t.get("outcomeIndex"),
            })
        return normalised
    except Exception:
        log.exception("fetch_trades_failed", address=maker_address)
        return []
