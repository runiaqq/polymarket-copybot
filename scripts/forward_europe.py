"""Europe/daily bands for filter forward window."""

from __future__ import annotations

import sys
from collections import defaultdict

from dateutil import parser as dtparser

from core.config import settings
from core.db.session import get_supabase
from core.shadow_model import passes_signal_filter

SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-07-25T18:40:00+00:00"


def ok(r: dict) -> bool:
    return passes_signal_filter(
        float(r.get("edge") or 0),
        float(r.get("spot") or 0),
        float(r.get("open_price") or 0),
        min_edge=settings.shadow_filter_min_edge,
        min_strike_bp=settings.shadow_filter_min_strike_bp,
    )


def band(h: int) -> str:
    if 4 <= h < 12:
        return "утро 06-14 CEST"
    if 12 <= h < 22:
        return "день-вечер 14-24 CEST"
    return "ночь 00-06 CEST"


def agg(label: str, group: list[dict]) -> None:
    n = len(group)
    if not n:
        print(f"  {label}: n=0")
        return
    wins = sum(1 for r in group if r["status"] == "win")
    stake = sum(float(r.get("stake_usdc") or 0) for r in group)
    net = sum(float(r.get("pnl_usdc") or 0) for r in group)
    print(
        f"  {label}: n={n} WR={wins / n * 100:.1f}% "
        f"net=${net:+.2f} ({net / stake * 100:+.1f}%)"
    )


def main() -> None:
    sb = get_supabase()
    rows: list[dict] = []
    offset = 0
    while True:
        page = (
            sb.table("shadow_trades")
            .select(
                "status,variant,entered_at,stake_usdc,pnl_usdc,"
                "edge,spot,open_price"
            )
            .eq("variant", "full")
            .gte("entered_at", SINCE)
            .in_("status", ["win", "loss"])
            .order("entered_at")
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < 1000:
            break
        offset += 1000

    filt = [r for r in rows if ok(r)]
    print(f"Срез с {SINCE}: full={len(rows)}, filter={len(filt)}")

    print("Europe FILTERED:")
    by: dict[str, list] = defaultdict(list)
    for r in filt:
        by[band(dtparser.parse(r["entered_at"]).hour)].append(r)
    for k in ("утро 06-14 CEST", "день-вечер 14-24 CEST", "ночь 00-06 CEST"):
        agg(k, by.get(k, []))

    print("Europe ALL full:")
    by2: dict[str, list] = defaultdict(list)
    for r in rows:
        by2[band(dtparser.parse(r["entered_at"]).hour)].append(r)
    for k in ("утро 06-14 CEST", "день-вечер 14-24 CEST", "ночь 00-06 CEST"):
        agg(k, by2.get(k, []))

    print("Daily FILTERED:")
    byd: dict[str, list] = defaultdict(list)
    for r in filt:
        byd[dtparser.parse(r["entered_at"]).strftime("%Y-%m-%d")].append(r)
    for d in sorted(byd):
        agg(d, byd[d])

    print("Daily ALL:")
    byd2: dict[str, list] = defaultdict(list)
    for r in rows:
        byd2[dtparser.parse(r["entered_at"]).strftime("%Y-%m-%d")].append(r)
    for d in sorted(byd2):
        agg(d, byd2[d])


if __name__ == "__main__":
    main()
