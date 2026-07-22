"""Check whether BP30.1 variant rows are being written."""

from collections import defaultdict

from core.db.session import get_supabase


def main() -> None:
    sb = get_supabase()
    rows = (
        sb.table("shadow_trades")
        .select("variant,created_at,asset")
        .order("created_at", desc=True)
        .limit(300)
        .execute()
        .data
        or []
    )
    by_variant: dict[str, int] = defaultdict(int)
    for r in rows:
        by_variant[str(r.get("variant"))] += 1
    print("последние 300 строк по вариантам:", dict(by_variant))
    print("самая новая:", rows[0]["created_at"] if rows else "—")
    recent = [r for r in rows if str(r["created_at"]) >= "2026-07-21T21:00"]
    by_variant_recent: dict[str, int] = defaultdict(int)
    for r in recent:
        by_variant_recent[str(r.get("variant"))] += 1
    print("после деплоя BP30.1 (21.07 21:00 UTC):", dict(by_variant_recent))


if __name__ == "__main__":
    main()
