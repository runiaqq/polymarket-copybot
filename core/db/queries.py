from datetime import datetime, timezone

from core.db.session import get_supabase

# Single subscription tier — any non-"free" value activates copying.
ACTIVE_TIER = "active"


def get_active_donor_addresses() -> set[str]:
    sb = get_supabase()
    res = sb.table("donor_wallets").select("address").eq("active", True).execute()
    return {row["address"].lower() for row in res.data}


def get_donor_by_address(address: str) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("donor_wallets")
        .select("*")
        .eq("address", address.lower())
        .maybe_single()
        .execute()
    )
    return res.data if res else None


def list_tracked_wallets(active_only: bool = True) -> list[dict]:
    """Curated whitelist of profitable wallets to copy (Model B)."""
    sb = get_supabase()
    q = sb.table("tracked_wallets").select("*")
    if active_only:
        q = q.eq("active", True)
    return q.execute().data or []


def add_tracked_wallet(address: str, label: str | None = None) -> dict:
    sb = get_supabase()
    addr = address.strip().lower()
    existing = sb.table("tracked_wallets").select("id").eq("address", addr).maybe_single().execute()
    if existing and existing.data:
        sb.table("tracked_wallets").update({"active": True, "label": label}).eq("address", addr).execute()
        return {"address": addr, "updated": True}
    sb.table("tracked_wallets").insert({"address": addr, "label": label, "active": True}).execute()
    return {"address": addr, "added": True}


def remove_tracked_wallet(address: str) -> bool:
    sb = get_supabase()
    addr = address.strip().lower()
    sb.table("tracked_wallets").update({"active": False}).eq("address", addr).execute()
    return True


def get_active_subscribers() -> list[dict]:
    """Active paying subscribers whose copying is not currently paused.

    In signals mode (auto_copy disabled) the only requirement is a valid paid
    subscription. The copy_active / wallet_address filters are auto-copy
    (custodial) concerns and are applied only when auto-copy is enabled.
    Blueprint 4: subscribers with copy_paused_until > now() are excluded so
    the fan-out never dispatches doomed trades during a risk pause.
    """
    from core.config import settings

    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    q = (
        sb.table("users")
        .select("*")
        .neq("sub_tier", "free")
        .gt("sub_expires_at", now)
        # Honor drawdown / daily-loss pause (null = not paused, past date = expired pause).
        .or_(f"copy_paused_until.is.null,copy_paused_until.lt.{now}")
    )
    if settings.auto_copy_enabled:
        q = q.eq("copy_active", True).not_.is_("wallet_address", "null")
    return q.execute().data


def get_user_by_telegram_id(telegram_id: int) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("users")
        .select("*")
        .eq("telegram_id", telegram_id)
        .maybe_single()
        .execute()
    )
    return res.data if res else None


def get_user_by_username(username: str) -> dict | None:
    """Case-insensitive lookup by Telegram username (with or without leading @)."""
    uname = (username or "").lstrip("@").strip()
    if not uname:
        return None
    sb = get_supabase()
    res = (
        sb.table("users")
        .select("*")
        .ilike("username", uname)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def upsert_user(telegram_id: int) -> dict:
    sb = get_supabase()
    existing = get_user_by_telegram_id(telegram_id)
    if existing:
        return existing
    res = (
        sb.table("users")
        .insert({"telegram_id": telegram_id})
        .execute()
    )
    return res.data[0]


def update_user(telegram_id: int, data: dict) -> dict:
    sb = get_supabase()
    res = (
        sb.table("users")
        .update(data)
        .eq("telegram_id", telegram_id)
        .execute()
    )
    return res.data[0]


def set_subscription(telegram_id: int, days: int) -> dict:
    """Manually grant/extend the subscription by `days` (admin action)."""
    from datetime import timedelta

    sb = get_supabase()
    existing = get_user_by_telegram_id(telegram_id)
    if not existing:
        upsert_user(telegram_id)

    now = datetime.now(timezone.utc)
    current_exp = None
    if existing and existing.get("sub_expires_at"):
        try:
            from dateutil.parser import parse as parse_dt

            current_exp = parse_dt(existing["sub_expires_at"])
            if current_exp.tzinfo is None:
                current_exp = current_exp.replace(tzinfo=timezone.utc)
        except Exception:
            current_exp = None

    base = current_exp if (current_exp and current_exp > now) else now
    new_exp = base + timedelta(days=days)

    res = (
        sb.table("users")
        .update({
            "sub_tier": ACTIVE_TIER,
            "sub_expires_at": new_exp.isoformat(),
            "copy_active": True,
        })
        .eq("telegram_id", telegram_id)
        .execute()
    )
    return res.data[0]


def create_access_code(days: int = 30, note: str | None = None) -> str:
    """Generate and store a one-time access code. Returns the code string."""
    import secrets

    sb = get_supabase()
    code = secrets.token_urlsafe(9)
    sb.table("access_codes").insert({
        "code": code, "tier": ACTIVE_TIER, "days": days, "note": note,
    }).execute()
    return code


def redeem_access_code(code: str, telegram_id: int) -> dict:
    """
    Atomically redeem a one-time code and activate the subscription.
    Returns {ok: bool, reason?, days?, expires_at?}.
    """
    sb = get_supabase()
    code = (code or "").strip()
    if not code:
        return {"ok": False, "reason": "empty"}

    row = (
        sb.table("access_codes").select("*").eq("code", code).maybe_single().execute()
    )
    data = row.data if row else None
    if not data:
        return {"ok": False, "reason": "invalid"}
    if data.get("used_by"):
        return {"ok": False, "reason": "used"}

    # Atomic claim: only succeeds while still unused (guards double-redeem races).
    claim = (
        sb.table("access_codes")
        .update({"used_by": telegram_id, "used_at": datetime.now(timezone.utc).isoformat()})
        .eq("code", code)
        .is_("used_by", "null")
        .execute()
    )
    if not claim.data:
        return {"ok": False, "reason": "used"}

    days = int(data["days"])
    user = set_subscription(telegram_id, days)
    return {"ok": True, "days": days, "expires_at": user.get("sub_expires_at")}


# ── Admin bot: admins registry + invite codes ────────────────────────────────

def is_admin(telegram_id: int) -> bool:
    """Super-admin (env) is always allowed; others must have an active admins row."""
    from core.config import settings

    if telegram_id == settings.admin_telegram_id:
        return True
    sb = get_supabase()
    res = (
        sb.table("admins").select("telegram_id,active")
        .eq("telegram_id", telegram_id).eq("active", True).limit(1).execute()
    )
    return bool(res.data)


def is_super_admin(telegram_id: int) -> bool:
    from core.config import settings
    return telegram_id == settings.admin_telegram_id


def add_admin(telegram_id: int, username: str | None, added_by: int | None) -> dict:
    sb = get_supabase()
    existing = sb.table("admins").select("telegram_id").eq("telegram_id", telegram_id).limit(1).execute()
    payload = {"username": username, "active": True}
    if existing.data:
        res = sb.table("admins").update(payload).eq("telegram_id", telegram_id).execute()
    else:
        res = sb.table("admins").insert({
            "telegram_id": telegram_id, "added_by": added_by, **payload,
        }).execute()
    return res.data[0]


def remove_admin(telegram_id: int) -> bool:
    sb = get_supabase()
    res = sb.table("admins").update({"active": False}).eq("telegram_id", telegram_id).execute()
    return bool(res.data)


def list_admins() -> list[dict]:
    sb = get_supabase()
    res = sb.table("admins").select("*").eq("active", True).order("created_at").execute()
    return res.data or []


def create_admin_code(note: str | None = None) -> str:
    import secrets

    sb = get_supabase()
    code = secrets.token_urlsafe(9)
    sb.table("admin_codes").insert({"code": code, "note": note}).execute()
    return code


def redeem_admin_code(code: str, telegram_id: int, username: str | None) -> bool:
    """Atomically consume a one-time admin invite code and register the admin."""
    sb = get_supabase()
    code = (code or "").strip()
    if not code:
        return False
    claim = (
        sb.table("admin_codes")
        .update({"used_by": telegram_id, "used_at": datetime.now(timezone.utc).isoformat()})
        .eq("code", code)
        .is_("used_by", "null")
        .execute()
    )
    if not claim.data:
        return False
    add_admin(telegram_id, username, added_by=None)
    return True


def list_active_subscribers_detail() -> list[dict]:
    """All currently-active subscribers with the fields admins care about."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    res = (
        sb.table("users")
        .select("telegram_id,username,sub_expires_at,balance_usdc,max_position_usdc,"
                "copy_active,wallet_address,wallet_registered")
        .neq("sub_tier", "free")
        .gt("sub_expires_at", now)
        .order("sub_expires_at")
        .execute()
    )
    return res.data or []


def get_subscription_status(telegram_id: int) -> dict:
    """Return {active, tier, expires_at} for a user."""
    user = get_user_by_telegram_id(telegram_id)
    if not user:
        return {"active": False, "tier": "free", "expires_at": None}

    tier = user.get("sub_tier", "free")
    expires_at = user.get("sub_expires_at")
    active = False
    if tier and tier != "free" and expires_at:
        try:
            from dateutil.parser import parse as parse_dt

            exp = parse_dt(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            active = exp > datetime.now(timezone.utc)
        except Exception:
            active = False
    return {"active": active, "tier": tier, "expires_at": expires_at}


def insert_trade_signal(signal: dict) -> dict:
    sb = get_supabase()
    res = sb.table("trade_signals").insert(signal).execute()
    return res.data[0]


def insert_copy_trade(trade: dict) -> dict:
    sb = get_supabase()
    res = sb.table("copy_trades").insert(trade).execute()
    return res.data[0]


def update_copy_trade(trade_id: int, data: dict) -> None:
    sb = get_supabase()
    sb.table("copy_trades").update(data).eq("id", trade_id).execute()


def get_user_open_positions(user_id: int) -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("*, trade_signals(*)")
        .eq("user_id", user_id)
        .in_("status", ["confirmed", "executing"])
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )
    return res.data


def get_user_pnl_stats(user_id: int) -> dict:
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("size_usdc, pnl_usdc")
        .eq("user_id", user_id)
        .eq("status", "confirmed")
        .execute()
    )
    rows = res.data
    total = len(rows)
    volume = sum(r["size_usdc"] or 0 for r in rows)
    pnl = sum(r["pnl_usdc"] or 0 for r in rows)
    return {"total": total, "volume": volume, "pnl": pnl}


# ── Blueprint 1: on-chain settlement reconciler helpers ──────────────────────

def get_outstanding_copy_trades() -> list[dict]:
    """
    All confirmed trades that still need on-chain settlement reconciliation
    (status='confirmed', redeemed_at IS NULL, condition_id IS NOT NULL).
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("id, user_id, condition_id, token_id, outcome_index, neg_risk,"
                " entry_price, shares, size_usdc")
        .eq("status", "confirmed")
        .is_("redeemed_at", "null")
        .not_.is_("condition_id", "null")
        .execute()
    )
    return res.data or []


def mark_trade_settled(trade_id: int, result: str, realized_pnl: float,
                       redeem_tx: str | None = None) -> None:
    """Mark a copy_trade row as resolved on-chain (win or loss)."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    payload: dict = {
        "result": result,
        "realized_pnl": round(realized_pnl, 4),
        "resolved_at": now,
        "redeemed_at": now,
    }
    if redeem_tx:
        payload["redeem_tx"] = redeem_tx
    sb.table("copy_trades").update(payload).eq("id", trade_id).execute()


# ── Blueprint 4: HWM + copy-pause helpers ────────────────────────────────────

def get_user_equity_hwm(user_id: int) -> float:
    sb = get_supabase()
    res = sb.table("users").select("equity_hwm").eq("id", user_id).maybe_single().execute()
    return float((res.data or {}).get("equity_hwm") or 0.0)


def update_user_equity_hwm(user_id: int, hwm: float) -> None:
    sb = get_supabase()
    sb.table("users").update({"equity_hwm": round(hwm, 4)}).eq("id", user_id).execute()


def pause_user_copying(user_id: int, until_iso: str) -> None:
    sb = get_supabase()
    sb.table("users").update({"copy_paused_until": until_iso}).eq("id", user_id).execute()


def resume_user_copying(user_id: int) -> None:
    sb = get_supabase()
    sb.table("users").update({"copy_paused_until": None}).eq("id", user_id).execute()


def get_daily_trade_count(user_id: int) -> int:
    """Number of copy_trades this user entered since 00:00 UTC today.

    Counts rows that reached order placement (status != 'failed').  Rows are only
    inserted in execute_copy_trade once we commit to placing an order, so skipped
    signals never consume a slot.

    Known race (accepted, soft cap): two concurrent signals can both read count=N-1
    and both proceed, overshooting by one.  This is a risk knob, not a financial
    invariant; an atomic counter is out of scope.
    """
    sb = get_supabase()
    since = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    res = (
        sb.table("copy_trades")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .neq("status", "failed")
        .gte("created_at", since)
        .execute()
    )
    return int(res.count or 0)


def get_daily_realized_pnl(user_id: int) -> float:
    """Sum of realized_pnl for trades settled in the trailing 24 h."""
    sb = get_supabase()
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    res = (
        sb.table("copy_trades")
        .select("realized_pnl")
        .eq("user_id", user_id)
        .not_.is_("realized_pnl", "null")
        .not_.is_("resolved_at", "null")
        .gte("resolved_at", since)
        .execute()
    )
    return sum(float(r["realized_pnl"] or 0) for r in (res.data or []))


# ── Blueprint 6: position state machine helpers ───────────────────────────────

def get_open_trade_by_token(user_id: int, token_id: str) -> dict | None:
    """Find the newest confirmed-and-unredeemed copy_trade for a token.

    Used by close_position to obtain the trade_id + size_usdc for P&L booking.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("id, size_usdc, condition_id, signal_id")
        .eq("user_id", user_id)
        .eq("token_id", token_id)
        .eq("status", "confirmed")
        .is_("redeemed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def mark_trade_closed(trade_id: int, realized_pnl: float,
                      exit_tx: str | None = None) -> None:
    """Mark a copy_trade row as closed by a token-sale exit (terminal state).

    Sets status='closed', result='closed', realized_pnl, resolved_at=now,
    redeemed_at=now.  Setting redeemed_at removes the row from
    get_outstanding_copy_trades (which filters redeemed_at IS NULL) permanently —
    independent of any Redis TTL.
    """
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    payload: dict = {
        "status":       "closed",
        "result":       "closed",
        "realized_pnl": round(realized_pnl, 4),
        "resolved_at":  now,
        "redeemed_at":  now,
    }
    if exit_tx:
        payload["exit_tx"] = exit_tx
    sb.table("copy_trades").update(payload).eq("id", trade_id).execute()


# ── Blueprint 8: risk state machine + manual override helpers ────────────────

def get_risk_state(user_id: int) -> str:
    """Return the current risk_state for a user (default 'active')."""
    sb = get_supabase()
    res = sb.table("users").select("risk_state").eq("id", user_id).maybe_single().execute()
    return str((res.data or {}).get("risk_state") or "active")


def set_risk_state(user_id: int, state: str) -> None:
    """Set risk_state column. Valid values: active | paused_drawdown | paused_daily_loss."""
    sb = get_supabase()
    sb.table("users").update({"risk_state": state}).eq("id", user_id).execute()


def reset_risk_baseline(user_id: int, equity: float) -> None:
    """Reset both equity_hwm and realized_baseline to the current equity value.

    Called on manual override — current equity becomes the new drawdown reference.
    """
    sb = get_supabase()
    sb.table("users").update({
        "equity_hwm":        round(equity, 4),
        "realized_baseline": round(equity, 4),
    }).eq("id", user_id).execute()


def record_risk_override(user_id: int) -> None:
    """Increment risk_override_count and stamp risk_override_at (consent audit trail)."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    # Fetch current count first (Supabase JS SDK doesn't support atomic increment).
    res = sb.table("users").select("risk_override_count").eq("id", user_id).maybe_single().execute()
    current = int((res.data or {}).get("risk_override_count") or 0)
    sb.table("users").update({
        "risk_override_at":    now,
        "risk_override_count": current + 1,
    }).eq("id", user_id).execute()


def get_open_trades_cost(user_id: int) -> dict:
    """Return {token_id: size_usdc} for all open (confirmed, unredeemed) copy_trades.

    Used by total_equity() in cost-basis mode to price open positions at entry cost
    rather than the depressed live mark.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("token_id, size_usdc")
        .eq("user_id", user_id)
        .in_("status", ["confirmed", "executing"])
        .is_("redeemed_at", "null")
        .execute()
    )
    result: dict = {}
    for row in (res.data or []):
        tid = row.get("token_id")
        cost = float(row.get("size_usdc") or 0)
        if tid and cost > 0:
            # If multiple rows for the same token (e.g. partial fills), sum them.
            result[tid] = result.get(tid, 0.0) + cost
    return result


def get_realized_baseline(user_id: int) -> float | None:
    """Return the realized_baseline stored for a user (None if not yet set)."""
    sb = get_supabase()
    res = sb.table("users").select("realized_baseline").eq("id", user_id).maybe_single().execute()
    val = (res.data or {}).get("realized_baseline")
    return float(val) if val is not None else None


def has_terminal_trade(user_id: int, condition_id: str) -> bool:
    """Return True when any copy_trade for (user, condition) is in a terminal state.

    Terminal = status='closed' OR redeemed_at IS NOT NULL.
    Used as defense-in-depth in backfill_legacy_redemptions to prevent re-claiming
    positions that were already exited via close_position or reconcile_settlements.
    """
    sb = get_supabase()
    # Check status='closed' first (token-sale exit).
    res = (
        sb.table("copy_trades")
        .select("id")
        .eq("user_id", user_id)
        .eq("condition_id", condition_id)
        .eq("status", "closed")
        .limit(1)
        .execute()
    )
    if res.data:
        return True
    # Check redeemed_at IS NOT NULL (resolved on-chain).
    res2 = (
        sb.table("copy_trades")
        .select("id")
        .eq("user_id", user_id)
        .eq("condition_id", condition_id)
        .not_.is_("redeemed_at", "null")
        .limit(1)
        .execute()
    )
    return bool(res2.data)
