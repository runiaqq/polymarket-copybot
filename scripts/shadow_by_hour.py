"""Hour-of-day and day-by-day breakdown of settled full-variant shadow trades."""

from __future__ import annotations

from collections import defaultdict
from datetime import timezone

from dateutil import parser as dateutil_parser

from core.db.session import get_supabase


def _stats(rows: list[dict]) -> str:
    n = len(rows)
    if not n:
        return "n=   0"
    wins = sum(1 for r in rows if r["status"] == "win")
    net = sum(float(r.get("pnl_usdc") or 0) for r in rows)
    stake = sum(float(r.get("stake_usdc") or 0) for r in rows)
    roi = net / stake if stake else 0.0
    return f"n={n:4d}  WR={wins / n:6.1%}  net=${net:+8.2f} ({roi:+6.1%})"


def main() -> None:
    sb = get_supabase()
    rows = (
        sb.table("shadow_trades")
        .select("entered_at,status,pnl_usdc,stake_usdc,variant,asset")
        .in_("status", ["win", "loss"])
        .eq("variant", "full")
        .order("entered_at")
        .limit(2000)
        .execute()
        .data
        or []
    )
    print(f"настроено full-сделок: {len(rows)}")

    by_hour: dict[int, list[dict]] = defaultdict(list)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        dt = dateutil_parser.parse(str(r["entered_at"]))
        by_hour[dt.astimezone(timezone.utc).hour].append(r)
        by_day[dt.strftime("%d.%m")].append(r)

    print("\nПо часам UTC (Европа = UTC+2):")
    for hour in sorted(by_hour):
        print(f"  {hour:02d}:00-{hour:02d}:59  {_stats(by_hour[hour])}")

    print("\nАгрегаты:")
    eu_morning = [r for h in range(4, 12) for r in by_hour.get(h, [])]
    eu_evening = [r for h in range(12, 22) for r in by_hour.get(h, [])]
    night = [r for h in [*range(22, 24), *range(0, 4)] for r in by_hour.get(h, [])]
    print(f"  утро Европы  (06-14 CEST / 04-12 UTC): {_stats(eu_morning)}")
    print(f"  день-вечер   (14-24 CEST / 12-22 UTC): {_stats(eu_evening)}")
    print(f"  ночь         (00-06 CEST / 22-04 UTC): {_stats(night)}")

    print("\nПо дням:")
    for day in sorted(by_day):
        print(f"  {day}  {_stats(by_day[day])}")


if __name__ == "__main__":
    main()
