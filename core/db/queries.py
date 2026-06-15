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


def get_active_subscribers() -> list[dict]:
    """Active paying subscribers.

    In signals mode (auto_copy disabled) the only requirement is a valid paid
    subscription. The copy_active / wallet_address filters are auto-copy
    (custodial) concerns and are applied only when auto-copy is enabled.
    """
    from core.config import settings

    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    q = (
        sb.table("users")
        .select("*")
        .neq("sub_tier", "free")
        .gt("sub_expires_at", now)
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
