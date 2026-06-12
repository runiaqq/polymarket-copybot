from datetime import datetime, timezone

from core.db.session import get_supabase


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
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    res = (
        sb.table("users")
        .select("*")
        .neq("sub_tier", "free")
        .gt("sub_expires_at", now)
        .eq("copy_active", True)
        .not_.is_("wallet_address", "null")
        .execute()
    )
    return res.data


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
        .in_("status", ["confirmed", "executing", "failed"])
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
