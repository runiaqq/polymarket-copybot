"""Check maker rows and recent engine activity."""

from collections import defaultdict

from core.db.session import get_supabase


def main() -> None:
    sb = get_supabase()
    makers = (
        sb.table("shadow_trades")
        .select("id,status,note,created_at,asset,sim_fill_price,pnl_usdc")
        .eq("variant", "maker")
        .order("created_at", desc=True)
        .limit(500)
        .execute()
        .data
        or []
    )
    print(f"maker-строк всего (последние 500): {len(makers)}")
    by = defaultdict(int)
    for m in makers:
        by[f"{m.get('status')}/{m.get('note')}"] += 1
    print("по статусам:", dict(by))
    if makers:
        print("самая новая:", makers[0]["created_at"], "самая старая:", makers[-1]["created_at"])


if __name__ == "__main__":
    main()
