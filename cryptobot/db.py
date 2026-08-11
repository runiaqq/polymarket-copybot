"""Supabase access for the BP33 pilot tables (crypto_users / crypto_trades).

All functions are synchronous (supabase-py is blocking) — callers run them in
threads via asyncio.to_thread.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from core.db.session import get_supabase

log = structlog.get_logger(__name__)


# ── crypto_users ──────────────────────────────────────────────────────────────

def get_user(telegram_id: int) -> dict | None:
    res = (
        get_supabase()
        .table("crypto_users")
        .select("*")
        .eq("telegram_id", telegram_id)
        .maybe_single()
        .execute()
    )
    return res.data if res else None


def upsert_user(telegram_id: int, username: str | None) -> dict:
    sb = get_supabase()
    existing = get_user(telegram_id)
    if existing:
        if username and existing.get("username") != username:
            sb.table("crypto_users").update({"username": username}).eq(
                "id", existing["id"]
            ).execute()
        return existing
    res = (
        sb.table("crypto_users")
        .insert({"telegram_id": telegram_id, "username": username})
        .execute()
    )
    return res.data[0]


def update_user(user_id: int, fields: dict[str, Any]) -> None:
    get_supabase().table("crypto_users").update(fields).eq("id", user_id).execute()


def active_traders() -> list[dict]:
    """Users the executor may trade for: registered wallet + trading switched on."""
    res = (
        get_supabase()
        .table("crypto_users")
        .select("*")
        .eq("trading_on", True)
        .eq("wallet_registered", True)
        .execute()
    )
    return res.data or []


def wallet_users() -> list[dict]:
    """Users with a generated wallet — the funding loop's working set."""
    res = (
        get_supabase()
        .table("crypto_users")
        .select("*")
        .not_.is_("wallet_address", "null")
        .execute()
    )
    return res.data or []


# ── crypto_trades ─────────────────────────────────────────────────────────────

def has_trade(user_id: int, condition_id: str) -> bool:
    res = (
        get_supabase()
        .table("crypto_trades")
        .select("id")
        .eq("user_id", user_id)
        .eq("condition_id", condition_id)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def insert_trade(payload: dict[str, Any]) -> bool:
    """Insert a trade row. Returns False on the unique (user, condition)
    violation — the executor's double-entry backstop."""
    try:
        get_supabase().table("crypto_trades").insert(payload).execute()
        return True
    except Exception as exc:
        if "uq_crypto_trades_user_condition" in str(exc) or "duplicate" in str(exc).lower():
            return False
        raise


def open_trades() -> list[dict]:
    """Open trades joined with the owner's signing context for redemption."""
    res = (
        get_supabase()
        .table("crypto_trades")
        .select(
            "*, crypto_users(telegram_id, wallet_private_key_enc, deposit_wallet_address)"
        )
        .eq("status", "open")
        .order("window_end")
        .execute()
    )
    return res.data or []


def settle_trade(
    trade_id: int,
    status: str,
    pnl_usdc: float,
    redeem_tx: str | None,
) -> None:
    get_supabase().table("crypto_trades").update(
        {
            "status": status,
            "pnl_usdc": round(pnl_usdc, 6),
            "redeem_tx": redeem_tx,
            "resolved_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        }
    ).eq("id", trade_id).eq("status", "open").execute()


def unredeemed_wins() -> list[dict]:
    """BP35: settled wins whose redemption failed (bounded to the last 7 days),
    joined with the owner's signing context — the retry sweep's working set."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)  # noqa: UP017
    res = (
        get_supabase()
        .table("crypto_trades")
        .select(
            "*, crypto_users(telegram_id, wallet_private_key_enc, deposit_wallet_address)"
        )
        .eq("status", "win")
        .is_("redeem_tx", "null")
        .gte("resolved_at", cutoff.isoformat())
        .order("resolved_at")
        .execute()
    )
    return res.data or []


def set_redeem_tx(trade_id: int, redeem_tx: str) -> None:
    """BP35: record the money move separately from settlement."""
    get_supabase().table("crypto_trades").update({"redeem_tx": redeem_tx}).eq(
        "id", trade_id
    ).execute()


def realized_pnl_today(user_id: int) -> float:
    """Sum of realized PnL since UTC midnight (daily-loss gate input)."""
    day_start = datetime.now(timezone.utc).replace(  # noqa: UP017
        hour=0, minute=0, second=0, microsecond=0
    )
    res = (
        get_supabase()
        .table("crypto_trades")
        .select("pnl_usdc")
        .eq("user_id", user_id)
        .in_("status", ["win", "loss"])
        .gte("resolved_at", day_start.isoformat())
        .execute()
    )
    return sum(float(row.get("pnl_usdc") or 0) for row in (res.data or []))


def recent_shadow_outcomes(lookback: int) -> list[bool]:
    """BP45: win/loss outcomes of the latest resolved shadow trades that match
    the executor's own tradable set (the regime gate's input), newest first.

    Filter mirrors what the real bot would have traded — btc / full variant /
    min-edge / strike-distance / entry ceiling. Verified proxy: over
    30.07–10.08 it reproduces the real bot's WR (86% before 07.08, 73% after
    vs 70% real). The min-edge cut is pushed into the query; strike/price
    need spot/open_price math PostgREST can't express, so they filter
    client-side over a deep fetch. The fetch must comfortably out-span the
    window: seen live 08-11, a 120-row fetch yielded only 9/15 outcomes in a
    low-flow night and the gate FAILED OPEN exactly when the flow-toxicity
    made it most needed."""
    from core.config import settings

    res = (
        get_supabase()
        .table("shadow_trades")
        .select("status, edge, sim_fill_price, variant, asset, spot, open_price")
        .in_("status", ["win", "loss"])
        .eq("asset", "btc")
        .eq("variant", "full")
        .gte("edge", settings.shadow_filter_min_edge)
        .not_.is_("resolved_at", "null")
        .order("resolved_at", desc=True)
        .limit(1000)
        .execute()
    )
    outcomes: list[bool] = []
    for r in res.data or []:
        spot, op = r.get("spot") or 0, r.get("open_price") or 0
        if not spot or not op:
            continue
        if abs(spot - op) / op * 10000 < settings.shadow_filter_min_strike_bp:
            continue
        if (r.get("sim_fill_price") or 0) > settings.crypto_max_entry_price:
            continue
        outcomes.append(r["status"] == "win")
        if len(outcomes) >= lookback:
            break
    return outcomes


def user_stats(user_id: int) -> dict:
    """Aggregates for the «Статистика» screen: today and all-time."""
    rows = (
        get_supabase()
        .table("crypto_trades")
        .select("status, pnl_usdc, filled_usdc, created_at")
        .eq("user_id", user_id)
        .in_("status", ["open", "win", "loss", "void"])
        .execute()
        .data
        or []
    )
    day_start = datetime.now(timezone.utc).replace(  # noqa: UP017
        hour=0, minute=0, second=0, microsecond=0
    )

    def _bucket(subset: list[dict]) -> dict:
        settled = [r for r in subset if r["status"] in ("win", "loss")]
        wins = sum(1 for r in settled if r["status"] == "win")
        pnl = sum(float(r.get("pnl_usdc") or 0) for r in settled)
        return {
            "trades": len(subset),
            "open": sum(1 for r in subset if r["status"] == "open"),
            "settled": len(settled),
            "wins": wins,
            "pnl": pnl,
        }

    today = [
        r
        for r in rows
        if datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00")) >= day_start
    ]
    return {"today": _bucket(today), "total": _bucket(rows)}
