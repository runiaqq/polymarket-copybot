"""Count live Data-API positions for the two real traders vs max_open_positions."""

from __future__ import annotations

from core.config import settings
from core.db.session import get_supabase
from core.polymarket import get_positions


def main() -> None:
    sb = get_supabase()
    users = (
        sb.table("users")
        .select("id,telegram_id,deposit_wallet_address")
        .in_("id", [1, 2])
        .execute()
        .data
        or []
    )
    for u in users:
        ps = get_positions(u["deposit_wallet_address"])
        open_ps = [p for p in ps if float(p.get("shares") or 0) > 0]
        print(
            f"user {u['id']} (tg {u['telegram_id']}): позиций с shares>0: "
            f"{len(open_ps)} (лимит max_open_positions={settings.max_open_positions})"
        )
        for p in open_ps:
            print(
                f"   {str(p.get('title'))[:58]:<60}"
                f" value=${float(p.get('current_value') or 0):>8.2f}"
                f" redeemable={p.get('redeemable')}"
            )


if __name__ == "__main__":
    main()
