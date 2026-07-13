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
    rows = q.execute().data or []
    if not settings.auto_copy_enabled:
        return rows
    # Auto-copy mode: custodial copiers need copy_active + a wallet to be dispatched.
    # Signal-only users trade manually (off-platform) so they are included
    # regardless of copy_active / wallet — execute_copy_trade short-circuits them
    # into a notification before any on-chain path.
    return [
        u for u in rows
        if u.get("is_signal_only")
        or (u.get("copy_active") and u.get("wallet_address"))
    ]


def get_users_for_monitoring() -> list[dict]:
    """Blueprint 21 + 24: every wallet whose OPEN positions must keep being
    monitored, stop-lossed and redeemed — INDEPENDENT of any risk pause /
    copy_active state.

    Unlike get_active_subscribers (the ENTRY / fan-out gate, which excludes
    copy_paused_until users), this deliberately does NOT filter on
    copy_paused_until / risk_state / copy_active, so a drawdown or daily-loss
    pause blocks only *new entries* (still enforced in execute_copy_trade and by
    get_active_subscribers), never the exit/redeem path on positions the user
    already holds.  A paused account must still get its stops fired and its wins
    claimed.

    BP24 (multi-wallet): returns ONE flattened entry PER wallet, not per user.
    Each entry is the users row with its wallet-scoped fields overridden by the
    specific wallet (deposit_wallet_address / wallet_private_key_enc / clob_*),
    plus `wallet_id`.  So sync_positions monitors ALL of a user's wallets — new
    trades only ever land on the active wallet, but a wallet the user switched
    away from must still have its open positions stopped and redeemed.  For a
    single-wallet user this yields exactly one entry, identical to the old
    behaviour.  Legacy accounts with no user_wallets rows fall back to the users
    row itself (wallet_id=None → resolve_signing_wallet uses the active wallet).
    """
    from core.db.wallets import _WALLET_FIELDS

    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    users = (
        sb.table("users")
        .select("*")
        .neq("sub_tier", "free")
        .gt("sub_expires_at", now)
        .execute()
    ).data or []
    if not users:
        return []

    uid_list = [u["id"] for u in users]
    wrows = (
        sb.table("user_wallets")
        .select("*")
        .in_("user_id", uid_list)
        .execute()
    ).data or []
    by_user: dict = {}
    for w in wrows:
        by_user.setdefault(w["user_id"], []).append(w)

    out: list[dict] = []
    for u in users:
        wallets = by_user.get(u["id"])
        if wallets:
            for w in wallets:
                if not w.get("deposit_wallet_address"):
                    continue
                entry = dict(u)
                for f in _WALLET_FIELDS:
                    entry[f] = w.get(f)
                entry["wallet_id"] = w["id"]
                entry["wallet_name"] = w.get("name")
                out.append(entry)
        elif u.get("deposit_wallet_address"):
            # Legacy fallback: no wallet rows yet — monitor the users-row wallet.
            entry = dict(u)
            entry["wallet_id"] = u.get("active_wallet_id")
            out.append(entry)
    return out


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
            # Renewal re-arms the expiry alert for the next cycle.
            "subscription_notified_expired": False,
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


def is_subscription_active(user: dict) -> bool:
    """Pure check (no side-effects): True when the user holds a non-free tier
    whose ``sub_expires_at`` is still in the future."""
    tier = user.get("sub_tier", "free")
    if not tier or tier == "free":
        return False
    exp_raw = user.get("sub_expires_at")
    if not exp_raw:
        return False
    try:
        from dateutil.parser import parse as parse_dt

        exp = parse_dt(exp_raw)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)
    except Exception:
        return False


def set_subscription_notified_expired(user_id: int, value: bool) -> None:
    """Flip the 'expiry alert already sent' flag (migration 014)."""
    sb = get_supabase()
    sb.table("users").update(
        {"subscription_notified_expired": bool(value)}
    ).eq("id", user_id).execute()


def set_signal_only(telegram_id: int, value: bool) -> dict:
    """Toggle Signal-Only Mode for a user (migration 014).

    When True the bot delivers signals but never places on-chain orders; only
    new-trade ENTRY is affected — open positions keep being managed."""
    sb = get_supabase()
    res = (
        sb.table("users")
        .update({"is_signal_only": bool(value)})
        .eq("telegram_id", telegram_id)
        .execute()
    )
    return res.data[0]


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
        .select("id, user_id, wallet_id, condition_id, token_id, outcome_index, neg_risk,"
                " entry_price, shares, size_usdc")
        .eq("status", "confirmed")
        .is_("redeemed_at", "null")
        .not_.is_("condition_id", "null")
        # BP22.7: FIFO — oldest rows first, so the backlog drains oldest-first
        # now that reconcile dispatches at most one redeem per user per cycle.
        .order("id")
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

    Returns id, size_usdc, condition_id, signal_id, created_at, entry_bid.
    Blueprint 17: created_at is used for hold-time anchor (survives worker
    restarts); entry_bid enables the Layer-3 bid-vs-bid drop comparison.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("id, size_usdc, condition_id, signal_id, created_at, entry_bid")
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


def get_open_trade_by_condition(user_id: int, condition_id: str) -> dict | None:
    """Find the newest confirmed-and-unredeemed copy_trade for a condition.

    Blueprint 20 Fix A2: called by sync_positions before dispatching
    redeem_position so trade_id and entry_cost are always passed through.
    Returns id, entry_price, shares, size_usdc, signal_id — all fields needed
    for the BP19 cost-basis resolver and the BP20.B outcome fallback.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("id, entry_price, shares, size_usdc, signal_id")
        .eq("user_id", user_id)
        .eq("condition_id", condition_id)
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


def set_risk_override_until(user_id: int, until_ts: str) -> None:
    """Set risk_override_until to suppress both breakers until that timestamp.

    Blueprint 17.B: called by the unlock_drawdown handler with the next 00:00 UTC
    so the daily-loss (and drawdown) monitor gate is bypassed for the rest of the
    UTC day.  The monitor reads this column at the top of every cycle.
    """
    sb = get_supabase()
    sb.table("users").update({"risk_override_until": until_ts}).eq("id", user_id).execute()


def get_risk_override_until(user_id: int) -> str | None:
    """Return the risk_override_until timestamp string for a user, or None."""
    sb = get_supabase()
    res = sb.table("users").select("risk_override_until").eq("id", user_id).maybe_single().execute()
    return (res.data or {}).get("risk_override_until")


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


def get_entry_prices_by_token(user_id: int) -> dict:
    """Return {token_id: entry_price} for open (confirmed/executing, unredeemed) copy_trades.

    Blueprint 16: local cost-basis fallback for the /positions view. The Polymarket
    Data API returns avgPrice=0 for freshly-opened positions on our POLY_1271 proxy
    wallets (indexing lag / proxy attribution), so the on-chain read alone renders
    "@ 0.000" and "+0%". Our copy_trades row already stores the real entry_price
    (Blueprint 1, migration 008); this helper surfaces it keyed by token.

    Only non-zero entry prices are returned. When multiple rows exist for a token
    (partial fills / add-ons), the newest non-zero entry wins.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("token_id, entry_price, created_at")
        .eq("user_id", user_id)
        .in_("status", ["confirmed", "executing"])
        .is_("redeemed_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    out: dict = {}
    for row in (res.data or []):
        tid = row.get("token_id")
        ep = float(row.get("entry_price") or 0)
        if tid and ep > 0 and tid not in out:  # first (newest) non-zero wins
            out[tid] = ep
    return out


def get_signal_price(signal_id: int) -> float | None:
    """Return trade_signals.price for a given signal_id.

    Blueprint 19 Tier-4 cost-basis fallback: when copy_trades.entry_price is
    NULL/0 and the Data-API avg_price is 0, the VWAP entry price stored on the
    originating signal is the last deterministic source before the hard floor.
    Returns None when the signal doesn't exist or its price is 0.
    """
    if not signal_id:
        return None
    sb = get_supabase()
    res = (
        sb.table("trade_signals")
        .select("price")
        .eq("id", signal_id)
        .maybe_single()
        .execute()
    )
    px = float((res.data or {}).get("price") or 0)
    return px if px > 0 else None


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


# ── Blueprint 18: Admin dashboard helpers ─────────────────────────────────────

def _parse_ts(value: str | None) -> "datetime | None":
    """Parse an ISO-8601 timestamp string from Supabase into a UTC-aware datetime."""
    if not value:
        return None
    try:
        from dateutil.parser import parse as _dp
        d = _dp(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def count_open_positions(user_id: int) -> int:
    """Count of the user's currently-open copy_trades (confirmed/executing, unredeemed).

    Authoritative, DB-sourced position count for the admin dashboard — mirrors the
    predicate the copy-engine uses for the max_open_positions guard (§2.2). Avoids the
    unreliable on-chain Data-API call against the EOA that returned a fake 0.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .in_("status", ["confirmed", "executing"])
        .is_("redeemed_at", "null")
        .execute()
    )
    return int(res.count or 0)


def get_pnl_summary(user_id: int) -> dict:
    """Realized-PnL rollup for the admin dashboard: today (UTC calendar day),
    trailing 7 days, and all-time — plus the settled-trade count.

    Filter: redeemed_at IS NOT NULL (most inclusive terminal predicate — covers both
    modern rows with resolved_at set and legacy rows settled before migration 008 which
    only have redeemed_at). PnL value: COALESCE(realized_pnl, pnl_usdc, 0) so pre-008
    rows that only populated pnl_usdc are still counted correctly.

    Timestamp bucketing uses resolved_at when available, falling back to redeemed_at
    (both are set to the same moment by mark_trade_settled; older rows may only have
    redeemed_at).
    """
    from datetime import timedelta
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select("realized_pnl, pnl_usdc, resolved_at, redeemed_at")
        .eq("user_id", user_id)
        .not_.is_("redeemed_at", "null")
        .execute()
    )
    now = datetime.now(timezone.utc)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = now - timedelta(days=7)
    today = week = all_time = 0.0
    n = 0
    for r in (res.data or []):
        # Use realized_pnl when present; fall back to pnl_usdc for pre-008 rows.
        pnl = float(r.get("realized_pnl") or r.get("pnl_usdc") or 0)
        # Use resolved_at for bucketing; fall back to redeemed_at for legacy rows.
        ts = _parse_ts(r.get("resolved_at") or r.get("redeemed_at"))
        all_time += pnl
        n += 1
        if ts and ts >= day0:
            today += pnl
        if ts and ts >= week0:
            week += pnl
    return {"today": today, "week": week, "all_time": all_time, "settled": n}


def get_user_trade_history(user_id: int, limit: int = 5, offset: int = 0) -> list[dict]:
    """Most-recent settled/closed copy_trades for the admin history view, newest first.

    Two-step query: fetch trade rows first, then batch-lookup signal titles by signal_id.
    Avoids relying on Supabase FK-embed syntax which requires an explicit schema relationship.

    BP22: the signal lookup must select only columns guaranteed to exist
    (id, title, outcome — migration 017). A previous version selected event_slug
    which was absent from the live schema; PostgREST rejected the whole query and
    the bare except swallowed it, so every history row rendered title '—'.
    A lookup failure is now logged loudly instead of silently degrading.
    """
    sb = get_supabase()
    res = (
        sb.table("copy_trades")
        .select(
            "id, signal_id, entry_price, shares, size_usdc, result, realized_pnl, "
            "pnl_usdc, outcome_index, resolved_at, redeemed_at, created_at, status"
        )
        .eq("user_id", user_id)
        .not_.is_("redeemed_at", "null")
        .order("redeemed_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return rows

    # Batch-fetch signal metadata and embed into each row (same shape _fmt_history_row expects).
    signal_ids = [r["signal_id"] for r in rows if r.get("signal_id")]
    sig_map: dict = {}
    if signal_ids:
        try:
            sig_res = (
                sb.table("trade_signals")
                .select("id, title, outcome")
                .in_("id", signal_ids)
                .execute()
            )
            sig_map = {s["id"]: s for s in (sig_res.data or [])}
        except Exception:
            # Degrade to titleless rows but leave a trace (BP22: never fail silent).
            import structlog
            structlog.get_logger(__name__).warning(
                "trade_history_signal_lookup_failed", user_id=user_id,
                signal_ids=signal_ids[:10],
            )

    for r in rows:
        r["trade_signals"] = sig_map.get(r.get("signal_id")) or {}

    return rows
