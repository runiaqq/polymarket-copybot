"""Follow-up: why do 1000+ whale signals produce almost no copies?

Run: python -m scripts.audit_whale_funnel2
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from core.db.session import get_supabase


def main() -> None:
    sb = get_supabase()
    since_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    users = sb.table("users").select("*").execute().data or []
    print("=== Пользователи ===")
    for u in users:
        print(
            f"id={u['id']} tg={u['telegram_id']} copy_active={u.get('copy_active')} "
            f"signal_only={u.get('is_signal_only')} tier={u.get('sub_tier')} "
            f"sub_expires={u.get('sub_expires_at')}"
        )
        print(
            f"   paused_until={u.get('copy_paused_until')} "
            f"risk_override_until={u.get('risk_override_until')} "
            f"equity_hwm={u.get('equity_hwm')} max_pos={u.get('max_position_usdc')}"
        )

    print("\n=== Не-снайперские copy_trades за 7д по дням/пользователям ===")
    trades = (
        sb.table("copy_trades")
        .select("user_id,status,mode,size_usdc,error_msg,created_at")
        .gte("created_at", since_7d)
        .order("created_at")
        .execute()
        .data
        or []
    )
    per_day: dict[str, int] = defaultdict(int)
    per_user: dict[int, int] = defaultdict(int)
    last_rows = []
    for t in trades:
        if (t.get("mode") or "default") == "sniper":
            continue
        day = str(t.get("created_at"))[:10]
        per_day[day] += 1
        per_user[t["user_id"]] += 1
        last_rows.append(t)
    print(f"по дням: {dict(sorted(per_day.items()))}")
    print(f"по пользователям: {dict(per_user)}")
    print("последние 10:")
    for t in last_rows[-10:]:
        print(
            f"  {str(t['created_at'])[:19]} user={t['user_id']} {t['status']} "
            f"${float(t.get('size_usdc') or 0):.2f} err={str(t.get('error_msg') or '')[:60]}"
        )

    print("\n=== Сигналы за 7д по дням ===")
    signals = (
        sb.table("trade_signals")
        .select("id,created_at,size_usdc,source_wallet")
        .gte("created_at", since_7d)
        .order("created_at", desc=True)
        .limit(1000)
        .execute()
        .data
        or []
    )
    sig_day: dict[str, int] = defaultdict(int)
    for s in signals:
        sig_day[str(s.get("created_at"))[:10]] += 1
    print(f"(последние {len(signals)} строк) по дням: {dict(sorted(sig_day.items()))}")

    print("\n=== Сигналы за последние 24ч vs копии ===")
    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    sig24 = (
        sb.table("trade_signals")
        .select("id", count="exact")
        .gte("created_at", since_24h)
        .execute()
    )
    print(f"сигналов за 24ч: {sig24.count}")
    tr24 = [
        t for t in trades
        if str(t.get("created_at")) >= since_24h and (t.get("mode") or "default") != "sniper"
    ]
    print(f"не-снайперских копий за 24ч: {len(tr24)}")


if __name__ == "__main__":
    main()
