"""Why do signals not become orders: balances, open positions, sizing settings.

Run: python -m scripts.audit_whale_funnel3
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.config import settings
from core.db.session import get_supabase
from core.polygon import get_balances


def main() -> None:
    sb = get_supabase()
    print("=== Глобальные настройки ===")
    print(f"sizing_mode={settings.sizing_mode}")
    print(f"wallet_track_mode={getattr(settings, 'wallet_track_mode', '?')}")
    print(f"max_open_positions={settings.max_open_positions}")
    print(f"exchange_min_order_usdc={settings.exchange_min_order_usdc}")
    print(f"kelly_fraction={getattr(settings, 'kelly_fraction', '?')}")
    print(f"kelly_max_price={getattr(settings, 'kelly_max_price', '?')}")

    users = (
        sb.table("users")
        .select("id,telegram_id,is_signal_only,sizing_mode,deposit_wallet_address,max_position_usdc")
        .eq("is_signal_only", False)
        .execute()
        .data
        or []
    )
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    print("\n=== Торгующие пользователи (signal_only=False) ===")
    for u in users:
        dw = u.get("deposit_wallet_address")
        bal = None
        if dw:
            try:
                bal = get_balances(dw).get("pusd")
            except Exception as exc:
                bal = f"ERR {str(exc)[:40]}"
        open_rows = (
            sb.table("copy_trades")
            .select("id,mode", count="exact")
            .eq("user_id", u["id"])
            .eq("status", "confirmed")
            .gte("created_at", since)
            .execute()
        )
        open_non_sniper = sum(
            1 for r in (open_rows.data or []) if (r.get("mode") or "default") != "sniper"
        )
        print(
            f"id={u['id']} tg={u['telegram_id']} sizing={u.get('sizing_mode')} "
            f"balance_pusd={bal} open_confirmed={open_rows.count} "
            f"(non-sniper: {open_non_sniper}) max_pos={u.get('max_position_usdc')} dw={'да' if dw else 'НЕТ'}"
        )

    print("\n=== Цены последних 30 сигналов ===")
    sigs = (
        sb.table("trade_signals")
        .select("price,size_usdc,source_wallet,created_at")
        .order("created_at", desc=True)
        .limit(30)
        .execute()
        .data
        or []
    )
    for s in sigs[:30]:
        print(
            f"  {str(s['created_at'])[:19]} px={s.get('price')} "
            f"${float(s.get('size_usdc') or 0):>10.0f} {str(s.get('source_wallet'))[:10]}"
        )


if __name__ == "__main__":
    main()
