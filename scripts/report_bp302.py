"""BP30.2-era report: settled trades with q_cal present, all cuts."""

from __future__ import annotations

import math
from collections import defaultdict

from dateutil import parser as dateutil_parser

from core.db.session import get_supabase


def _line(rows: list[dict]) -> str:
    n = len(rows)
    if not n:
        return "n=   0"
    wins = sum(1 for r in rows if r["status"] == "win")
    net = sum(float(r.get("pnl_usdc") or 0) for r in rows)
    stake = sum(float(r.get("stake_usdc") or 0) for r in rows)
    model = sum(float(r.get("model_p") or 0) for r in rows) / n
    price = sum(float(r.get("sim_fill_price") or 0) for r in rows) / n
    return (
        f"n={n:4d}  WR={wins / n:6.1%}  px={price:.3f}  model={model:6.1%}  "
        f"net=${net:+8.2f} ({net / stake if stake else 0:+6.1%})"
    )


def main() -> None:
    sb = get_supabase()
    rows = (
        sb.table("shadow_trades")
        .select(
            "entered_at,status,pnl_usdc,stake_usdc,variant,asset,edge,"
            "model_p,sim_fill_price,q_cal,spot,open_price,time_left_sec"
        )
        .not_.is_("q_cal", "null")
        .in_("status", ["win", "loss"])
        .order("entered_at")
        .limit(3000)
        .execute()
        .data
        or []
    )
    full = [r for r in rows if r.get("variant") == "full"]
    print(f"BP30.2-эпоха: закрытых строк всего {len(rows)}, full: {len(full)}")
    print(f"\nfull ИТОГО:      {_line(full)}")

    print("\nПо вариантам времени:")
    by_var: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_var[str(r.get("variant"))].append(r)
    for v in ["full", "t20-30", "t30-60", "t60-90", "t90-120"]:
        print(f"  {v:<9} {_line(by_var.get(v, []))}")

    print("\nfull по edge:")
    by_edge: dict[str, list[dict]] = defaultdict(list)
    for r in full:
        e = float(r.get("edge") or 0)
        label = "5-7%" if e < 0.07 else ("7-10%" if e < 0.10 else ">=10%")
        by_edge[label].append(r)
    for label in ["5-7%", "7-10%", ">=10%"]:
        print(f"  {label:<7} {_line(by_edge.get(label, []))}")

    print("\nfull по расстоянию до страйка:")
    by_bp: dict[str, list[dict]] = defaultdict(list)
    for r in full:
        spot = float(r.get("spot") or 0)
        op = float(r.get("open_price") or 0)
        bps = abs(math.log(spot / op)) * 10_000 if spot > 0 and op > 0 else -1
        label = "<3bp" if bps < 3 else ("3-8bp" if bps < 8 else ">=8bp")
        by_bp[label].append(r)
    for label in ["<3bp", "3-8bp", ">=8bp"]:
        print(f"  {label:<7} {_line(by_bp.get(label, []))}")

    print("\nfull по часам UTC:")
    by_hour: dict[int, list[dict]] = defaultdict(list)
    for r in full:
        by_hour[dateutil_parser.parse(str(r["entered_at"])).hour].append(r)
    for h in sorted(by_hour):
        print(f"  {h:02d}  {_line(by_hour[h])}")

    print("\nfull по активам:")
    by_asset: dict[str, list[dict]] = defaultdict(list)
    for r in full:
        by_asset[str(r.get("asset"))].append(r)
    for a in sorted(by_asset):
        print(f"  {a:<5} {_line(by_asset[a])}")


if __name__ == "__main__":
    main()
