"""
EOA re-test harness — answers ONE question with a live call:
is a plain EOA still rejected by Polymarket V2 ("maker address not allowed"),
or does a tiny order actually go through?

This exercises the exact production path (core.clob.place_order, no funder /
signature_type override) so the result reflects what the bot would do.

A rejected order costs $0 (400 before matching). A successful order spends ~$1.

Run from repo root:
    python scripts/test_eoa.py state
    python scripts/test_eoa.py trade           # auto-picks a liquid token
    python scripts/test_eoa.py trade <token_id> # force a specific token
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import settings
from core.db import get_supabase
from core.wallet import decrypt_key


def _admin_row() -> dict:
    sb = get_supabase()
    res = (sb.table("users")
           .select("id,wallet_address,wallet_private_key_enc,wallet_registered,"
                   "clob_api_key,clob_secret,clob_passphrase")
           .eq("telegram_id", settings.admin_telegram_id).maybe_single().execute())
    if not res or not res.data or not res.data.get("wallet_private_key_enc"):
        raise SystemExit("No admin wallet in DB (telegram_id=ADMIN_TELEGRAM_ID).")
    return res.data


def state() -> None:
    from core.polygon import get_balances

    row = _admin_row()
    addr = row["wallet_address"]
    print("EOA address:      ", addr)
    print("wallet_registered:", row.get("wallet_registered"))
    print("has CLOB creds:   ", bool(row.get("clob_api_key")))
    print("balances:         ", get_balances(addr))


def _creds(row: dict) -> dict:
    if row.get("clob_api_key"):
        return {
            "clob_api_key": row["clob_api_key"],
            "clob_secret": row["clob_secret"],
            "clob_passphrase": row["clob_passphrase"],
        }
    from core.clob import generate_api_creds
    print("No stored creds — deriving via L1 auth…")
    return generate_api_creds(row["wallet_private_key_enc"])


def _pick_liquid_token() -> tuple[str, dict, float]:
    """Find a tradeable token with a real ask near mid-book."""
    from core.polymarket import get_watch_markets, get_order_book, normalize_book

    markets = get_watch_markets()
    if not markets:
        raise SystemExit("get_watch_markets() returned nothing — no fast markets right now. "
                         "Pass a token_id explicitly: python scripts/test_eoa.py trade <token_id>")
    best = None
    for token_id, meta in markets.items():
        raw = get_order_book(token_id)
        if not raw:
            continue
        book = normalize_book(raw)
        ask = book.get("best_ask")
        if ask is None or not (0.05 <= ask <= 0.95):
            continue
        # prefer prices near 0.5 (tightest, most liquid)
        score = abs(ask - 0.5)
        if best is None or score < best[2]:
            best = (token_id, meta, ask)
    if best is None:
        raise SystemExit("No liquid token found among fast markets. Pass a token_id explicitly.")
    return best[0], best[1], best[2]


def trade(token_id: str | None = None) -> None:
    from core.clob import place_order
    from core.polymarket import get_order_book, normalize_book

    row = _admin_row()
    creds = _creds(row)

    if token_id:
        meta = {"tick_size": "0.01", "neg_risk": False}
        raw = get_order_book(token_id)
        ask = normalize_book(raw).get("best_ask") if raw else None
        if ask is None:
            raise SystemExit(f"No ask for token {token_id}.")
        if raw:
            meta["tick_size"] = str(raw.get("tick_size", "0.01"))
            meta["neg_risk"] = bool(raw.get("neg_risk", False))
    else:
        token_id, meta, ask = _pick_liquid_token()

    print(f"Token:    {token_id}")
    print(f"Title:    {meta.get('title', '—')}")
    print(f"Best ask: {ask}  tick={meta.get('tick_size')}  negRisk={meta.get('neg_risk')}")
    print("Placing a $1 BUY (FAK)…")
    try:
        resp = place_order(
            private_key_enc=row["wallet_private_key_enc"],
            api_creds=creds,
            token_id=token_id,
            side="BUY",
            price=float(ask),
            size_usdc=1.0,
            tick_size=str(meta.get("tick_size", "0.01")),
            neg_risk=bool(meta.get("neg_risk", False)),
            slippage_pct=0.03,
        )
        print("\n✅ ORDER RESPONSE:", resp)
        print("\n=> EOA path is NOT blocked. Auto-trading is achievable without Builder Program.")
    except Exception as exc:
        msg = str(exc)
        print("\n❌ ORDER FAILED:", msg[:500])
        if "maker address not allowed" in msg.lower() or "deposit wallet" in msg.lower():
            print("\n=> Confirmed: EOA is blocked on V2. Deposit-wallet flow (Builder Program) required.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"
    if cmd == "state":
        state()
    elif cmd == "trade":
        trade(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print("usage: state | trade [token_id]")
