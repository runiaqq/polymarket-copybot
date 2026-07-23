"""Implied fee rate on OUR recent sniper fills (copy_trades.fee_usdc from BP29)."""

from __future__ import annotations

from core.db.session import get_supabase


def main() -> None:
    sb = get_supabase()
    rows = (
        sb.table("copy_trades")
        .select("created_at,size_usdc,shares,fill_price,fee_usdc,mode,status")
        .eq("mode", "sniper")
        .not_.is_("fee_usdc", "null")
        .order("created_at", desc=True)
        .limit(40)
        .execute()
        .data
        or []
    )
    print(f"{'дата':<20}{'цена':>7}{'stake $':>9}{'fee $':>8}{'implied r':>11}")
    for r in rows:
        price = float(r.get("fill_price") or 0)
        shares = float(r.get("shares") or 0)
        fee = float(r.get("fee_usdc") or 0)
        if not (0 < price < 1 and shares > 0 and fee > 0):
            continue
        implied = fee / (shares * price * (1 - price))
        print(
            f"{str(r['created_at'])[:19]:<20}{price:>7.3f}"
            f"{float(r.get('size_usdc') or 0):>9.2f}{fee:>8.3f}{implied:>11.4f}"
        )


if __name__ == "__main__":
    main()
