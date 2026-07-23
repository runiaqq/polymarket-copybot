"""Is BP30.2 live? Check sigma_fast/q_cal on recent rows and entry rate by hour."""

from collections import defaultdict

from dateutil import parser as dateutil_parser

from core.db.session import get_supabase


def main() -> None:
    sb = get_supabase()
    rows = (
        sb.table("shadow_trades")
        .select("created_at,variant,sigma_fast,q_cal,edge,model_p,sim_fill_price")
        .order("created_at", desc=True)
        .limit(400)
        .execute()
        .data
        or []
    )
    with_fast = [r for r in rows if r.get("sigma_fast") is not None]
    with_qcal = [r for r in rows if r.get("q_cal") is not None]
    print(f"из последних {len(rows)}: sigma_fast заполнен у {len(with_fast)}, q_cal у {len(with_qcal)}")
    if with_qcal:
        newest = with_qcal[0]["created_at"]
        oldest = with_qcal[-1]["created_at"]
        print(f"q_cal строки: {oldest} … {newest}")

    per_hour = defaultdict(int)
    for r in rows:
        if r.get("variant") != "full":
            continue
        dt = dateutil_parser.parse(str(r["created_at"]))
        per_hour[dt.strftime("%d.%m %H")] += 1
    print("\nfull-входов по часам (последние 400 строк):")
    for k in sorted(per_hour):
        print(f"  {k}: {per_hour[k]}")


if __name__ == "__main__":
    main()
