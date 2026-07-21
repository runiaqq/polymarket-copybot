"""Repair settled sniper cost/share/PnL rows from authenticated CLOB trades.

Dry-run is the default. Pass --apply only after reviewing the printed changes.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from core.clob import _make_client
from core.db import get_supabase
from core.db.wallets import resolve_signing_wallet
from core.wallet import decrypt_key

MATCH_WINDOW_SEC = 120


@dataclass(frozen=True)
class Repair:
    trade_id: int
    old_size: Decimal
    new_size: Decimal
    old_shares: Decimal
    new_shares: Decimal
    old_pnl: Decimal
    new_pnl: Decimal
    order_id: str


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _timestamp(value: str | int | None) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        # dateutil, not fromisoformat: Python 3.10 rejects Supabase timestamps
        # with non-3/6-digit fractional seconds (e.g. "…:47.63253+00:00").
        try:
            from dateutil.parser import parse as _parse_dt
            parsed = _parse_dt(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            return 0


def _group_trades(trades: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        if str(trade.get("side") or "").upper() != "BUY":
            continue
        if str(trade.get("trader_side") or "").upper() != "TAKER":
            continue
        if str(trade.get("status") or "").upper() != "CONFIRMED":
            continue
        order_id = str(trade.get("taker_order_id") or "")
        if not order_id:
            continue
        groups[order_id].append(trade)
    return groups


def _match_trades(row: dict, trades: list[dict]) -> tuple[list[dict], str | None]:
    groups = _group_trades(trades)
    created_ts = _timestamp(row.get("created_at"))
    if not created_ts:
        return [], "copy_trade created_at missing or invalid"
    nearby = [
        group
        for group in groups.values()
        if any(
            _timestamp(trade.get("match_time"))
            and abs(_timestamp(trade.get("match_time")) - created_ts) <= MATCH_WINDOW_SEC
            for trade in group
        )
    ]
    if len(nearby) == 1:
        return nearby[0], None
    if not groups:
        return [], "no confirmed taker BUY history for token"
    if not nearby:
        return [], f"no CLOB order within {MATCH_WINDOW_SEC}s of ledger row"
    return [], f"ambiguous CLOB history ({len(nearby)} nearby taker orders)"


def _build_repair(row: dict, matched: list[dict]) -> Repair | None:
    if not matched:
        return None

    shares = sum((_decimal(trade.get("size")) for trade in matched), Decimal(0))
    cost = sum(
        (_decimal(trade.get("size")) * _decimal(trade.get("price")) for trade in matched),
        Decimal(0),
    )
    if shares <= 0 or cost <= 0:
        return None

    result = str(row.get("result") or "").lower()
    pnl = shares - cost if result == "win" else -cost
    return Repair(
        trade_id=int(row["id"]),
        old_size=_decimal(row.get("size_usdc")),
        new_size=cost,
        old_shares=_decimal(row.get("shares")),
        new_shares=shares,
        old_pnl=_decimal(row.get("realized_pnl")),
        new_pnl=pnl,
        order_id=str(matched[0].get("taker_order_id") or ""),
    )


def _client_for_row(row: dict, clients: dict[tuple[int, int | None], object]):
    user_id = int(row["user_id"])
    wallet_id = row.get("wallet_id")
    key = (user_id, int(wallet_id) if wallet_id is not None else None)
    if key in clients:
        return clients[key]

    wallet = resolve_signing_wallet(user_id, key[1])
    if not wallet:
        raise RuntimeError("signing wallet not found")
    required = (
        "wallet_private_key_enc",
        "deposit_wallet_address",
        "clob_api_key",
        "clob_secret",
        "clob_passphrase",
    )
    if any(not wallet.get(field) for field in required):
        raise RuntimeError("wallet or CLOB credentials missing")

    creds = {
        "clob_api_key": wallet["clob_api_key"],
        "clob_secret": wallet["clob_secret"],
        "clob_passphrase": wallet["clob_passphrase"],
    }
    client = _make_client(
        decrypt_key(wallet["wallet_private_key_enc"]),
        creds,
        funder=wallet["deposit_wallet_address"],
    )
    clients[key] = client
    return client


def _print_repairs(repairs: list[Repair], skipped: list[tuple[int, str]]) -> None:
    print("id     size: было -> станет       shares: было -> станет     pnl: было -> станет")
    print("-" * 92)
    for item in repairs:
        print(
            f"{item.trade_id:<6} "
            f"{float(item.old_size):>8.2f} -> {float(item.new_size):<8.2f}   "
            f"{float(item.old_shares):>8.4f} -> {float(item.new_shares):<8.4f}   "
            f"{float(item.old_pnl):>8.2f} -> {float(item.new_pnl):<8.2f}"
        )
    if skipped:
        print("\nПропущено:")
        for trade_id, reason in skipped:
            print(f"  id={trade_id}: {reason}")
    old_size = sum((item.old_size for item in repairs), Decimal(0))
    new_size = sum((item.new_size for item in repairs), Decimal(0))
    old_pnl = sum((item.old_pnl for item in repairs), Decimal(0))
    new_pnl = sum((item.new_pnl for item in repairs), Decimal(0))
    print(f"\nК изменению: {len(repairs)}; пропущено: {len(skipped)}")
    print(
        f"Итого size: ${float(old_size):.2f} -> ${float(new_size):.2f}; "
        f"PnL: ${float(old_pnl):+.2f} -> ${float(new_pnl):+.2f}"
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write reviewed repairs to production (default: dry-run)",
    )
    args = parser.parse_args()

    from py_clob_client_v2 import TradeParams

    sb = get_supabase()
    rows = (
        sb.table("copy_trades")
        .select(
            "id,user_id,wallet_id,token_id,size_usdc,shares,"
            "realized_pnl,result,created_at"
        )
        .eq("mode", "sniper")
        .in_("result", ["win", "loss"])
        .order("id")
        .execute()
    ).data or []

    clients: dict[tuple[int, int | None], object] = {}
    trade_cache: dict[tuple[int, int | None, str], list[dict]] = {}
    repairs: list[Repair] = []
    skipped: list[tuple[int, str]] = []

    for row in rows:
        token_id = str(row.get("token_id") or "")
        if not token_id:
            skipped.append((int(row["id"]), "token_id missing"))
            continue
        wallet_key = (
            int(row["user_id"]),
            int(row["wallet_id"]) if row.get("wallet_id") is not None else None,
        )
        cache_key = (*wallet_key, token_id)
        try:
            if cache_key not in trade_cache:
                client = _client_for_row(row, clients)
                trade_cache[cache_key] = client.get_trades(
                    TradeParams(asset_id=token_id),
                    only_first_page=False,
                )
            trades = trade_cache[cache_key]
            matched, reason = _match_trades(row, trades)
            if reason:
                skipped.append((int(row["id"]), reason))
                continue
            repair = _build_repair(row, matched)
            if repair is None:
                skipped.append((int(row["id"]), "matched trade has zero size or cost"))
                continue
            repairs.append(repair)
        except Exception as exc:
            skipped.append((int(row["id"]), str(exc)[:160]))

    _print_repairs(repairs, skipped)
    if not args.apply:
        print("\nDRY-RUN: база не изменена. Для записи используйте --apply.")
        return

    for item in repairs:
        sb.table("copy_trades").update({
            "size_usdc": round(float(item.new_size), 6),
            "shares": round(float(item.new_shares), 6),
            "realized_pnl": round(float(item.new_pnl), 4),
        }).eq("id", item.trade_id).execute()
    print(f"\nAPPLIED: обновлено строк: {len(repairs)}")


if __name__ == "__main__":
    main()
