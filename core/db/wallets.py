"""
Blueprint 24 — multi-wallet support.

Model: a Telegram user owns 1..N named wallets (`user_wallets`).  Exactly one is
ACTIVE at a time.  The wallet-related columns on `users` (wallet_address,
wallet_private_key_enc, deposit_wallet_address, deposit_wallet_deployed,
wallet_registered, clob_api_key/secret/passphrase, balance_usdc) are kept as a
live MIRROR of the ACTIVE wallet, so the entire entry / balance / deposit / UI
path keeps reading `users.*` unchanged and always operates on the active wallet.

The exit/redeem money-path uses `copy_trades.wallet_id` + `resolve_signing_wallet`
so a trade opened on wallet A is always closed/redeemed with wallet A's key, even
after the user switches their active wallet.
"""

from core.db.session import get_supabase

# The wallet-scoped fields mirrored between user_wallets and the users row.
_WALLET_FIELDS = (
    "wallet_address",
    "wallet_private_key_enc",
    "deposit_wallet_address",
    "deposit_wallet_deployed",
    "wallet_registered",
    "clob_api_key",
    "clob_secret",
    "clob_passphrase",
    "balance_usdc",
)

# Hard cap on wallets per user — abuse / UI guard.
MAX_WALLETS_PER_USER = 5


def list_wallets(user_id: int) -> list[dict]:
    """All wallets for a user, oldest first (creation order)."""
    sb = get_supabase()
    res = (
        sb.table("user_wallets")
        .select("*")
        .eq("user_id", user_id)
        .order("id")
        .execute()
    )
    return res.data or []


def count_wallets(user_id: int) -> int:
    sb = get_supabase()
    res = (
        sb.table("user_wallets")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .execute()
    )
    return int(res.count or 0)


def get_wallet(wallet_id: int) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("user_wallets")
        .select("*")
        .eq("id", wallet_id)
        .maybe_single()
        .execute()
    )
    return res.data if res else None


def get_active_wallet(user_id: int) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("user_wallets")
        .select("*")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def create_wallet(user_id: int, name: str, wallet_fields: dict,
                  make_active: bool = False) -> dict:
    """Insert a new named wallet row for a user.

    `wallet_fields` supplies wallet_address / wallet_private_key_enc and any of the
    deposit-wallet / CLOB-cred columns already available. `is_active` is left False
    here — call set_active_wallet() to activate (keeps mirror + pointer in sync).
    """
    sb = get_supabase()
    payload = {"user_id": user_id, "name": name, "is_active": False}
    for f in _WALLET_FIELDS:
        if f in wallet_fields:
            payload[f] = wallet_fields[f]
    row = sb.table("user_wallets").insert(payload).execute().data[0]
    if make_active:
        set_active_wallet(user_id, row["id"])
        row = get_wallet(row["id"]) or row
    return row


def update_wallet(wallet_id: int, data: dict) -> None:
    sb = get_supabase()
    sb.table("user_wallets").update(data).eq("id", wallet_id).execute()


def rename_wallet(wallet_id: int, name: str) -> None:
    update_wallet(wallet_id, {"name": name})


def _mirror_wallet_to_user(user_id: int, wallet: dict) -> None:
    """Copy the active wallet's fields onto the users row so the entry / balance /
    deposit / UI path keeps working unchanged on the active wallet."""
    sb = get_supabase()
    payload = {f: wallet.get(f) for f in _WALLET_FIELDS}
    payload["active_wallet_id"] = wallet["id"]
    sb.table("users").update(payload).eq("id", user_id).execute()


def set_active_wallet(user_id: int, wallet_id: int) -> dict | None:
    """Make `wallet_id` the user's active wallet and mirror it onto users.*.

    The partial unique index guarantees a single active wallet, so we clear the
    current active first, then set the target, then mirror.
    """
    sb = get_supabase()
    target = get_wallet(wallet_id)
    if not target or target.get("user_id") != user_id:
        return None
    # Clear any currently-active wallet for this user.
    sb.table("user_wallets").update({"is_active": False}) \
        .eq("user_id", user_id).eq("is_active", True).execute()
    # Activate the target.
    sb.table("user_wallets").update({"is_active": True}).eq("id", wallet_id).execute()
    target["is_active"] = True
    _mirror_wallet_to_user(user_id, target)
    return target


def resolve_signing_wallet(user_id: int, wallet_id: int | None = None) -> dict | None:
    """Return a users-row-shaped dict whose wallet fields belong to the requested
    wallet (or the active wallet when wallet_id is None), merged with the user's
    identity/subscription fields.

    Money-path callers (close_position / redeem_position) read
    wallet_private_key_enc / deposit_wallet_address / clob_* / telegram_id off this
    dict, so a trade always signs with the wallet that opened it.  Falls back to the
    raw users row for legacy accounts that have no user_wallets rows yet.
    """
    sb = get_supabase()
    ures = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    user = ures.data if ures else None
    if not user:
        return None

    wallet = None
    if wallet_id is not None:
        wallet = get_wallet(wallet_id)
        # Guard against a wallet_id that belongs to a different user.
        if wallet and wallet.get("user_id") != user_id:
            wallet = None
    if wallet is None:
        wallet = get_active_wallet(user_id)

    if wallet is None:
        # Legacy: no user_wallets rows — the users row already holds the wallet.
        merged = dict(user)
        merged["wallet_id"] = user.get("active_wallet_id")
        return merged

    merged = dict(user)
    for f in _WALLET_FIELDS:
        merged[f] = wallet.get(f)
    merged["wallet_id"] = wallet["id"]
    merged["wallet_name"] = wallet.get("name")
    return merged
