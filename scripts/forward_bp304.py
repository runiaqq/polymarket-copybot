"""Forward check for BP30.4: all DB-only slices since the filter deploy (no donor RPC)."""

from __future__ import annotations

import sys
from collections import defaultdict

from dateutil import parser as dtparser

from core.config import settings
from core.db.session import get_supabase
from core.shadow_model import passes_signal_filter

SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-07-24T16:20:00+00:00"


def fetch(sb) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = (
            sb.table("shadow_trades")
            .select(
                "status,variant,note,entered_at,placed_at,stake_usdc,pnl_usdc,fee_usdc,"
                "edge,spot,open_price,sim_fill_price,model_p,time_left_sec,asset"
            )
            .gte("entered_at", SINCE)
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
    return rows


def passes(r: dict) -> bool:
    return passes_signal_filter(
        float(r.get("edge") or 0),
        float(r.get("spot") or 0),
        float(r.get("open_price") or 0),
        min_edge=settings.shadow_filter_min_edge,
        min_strike_bp=settings.shadow_filter_min_strike_bp,
    )


def line(label: str, group: list[dict]) -> None:
    n = len(group)
    if not n:
        print(f"  {label:<22} n=0")
        return
    wins = sum(1 for r in group if r["status"] == "win")
    stake = sum(float(r.get("stake_usdc") or 0) for r in group)
    net = sum(float(r.get("pnl_usdc") or 0) for r in group)
    fees = sum(float(r.get("fee_usdc") or 0) for r in group)
    px = sum(float(r.get("sim_fill_price") or 0) for r in group) / n
    mp = sum(float(r.get("model_p") or 0) for r in group) / n
    print(
        f"  {label:<22} n={n:4d} WR={wins / n * 100:5.1f}% px={px:.3f} model={mp * 100:5.1f}% "
        f"fees=${fees:6.2f} net=${net:+8.2f} ({net / stake * 100:+5.1f}%)"
    )


def main() -> None:
    sb = get_supabase()
    rows = fetch(sb)
    print(f"Срез с {SINCE}, всего строк: {len(rows)}")

    full = [r for r in rows if r["variant"] == "full"]
    settled = [r for r in full if r["status"] in ("win", "loss")]
    open_n = sum(1 for r in full if r["status"] == "open")
    print(f"\nFULL: закрыто {len(settled)}, открыто {open_n}")
    line("ВСЕ FULL", settled)
    line("ПРОШЛИ ФИЛЬТР", [r for r in settled if passes(r)])
    line("ОТСЕЯНЫ", [r for r in settled if not passes(r)])

    print("\nПо вариантам времени (закрытые):")
    for name in ("t20-30", "t30-60", "t60-90", "t90-120"):
        vs = [r for r in rows if r["variant"] == name and r["status"] in ("win", "loss")]
        line(name, vs)
        line(f"{name} +фильтр", [r for r in vs if passes(r)])

    makers = [r for r in rows if r["variant"] == "maker"]
    m_filled = [r for r in makers if r.get("note") == "filled"]
    m_settled = [r for r in m_filled if r["status"] in ("win", "loss")]
    print(
        f"\nMAKER: размещено {len(makers)}, заполнено {len(m_filled)} "
        f"({len(m_filled) / len(makers) * 100 if makers else 0:.0f}%), "
        f"отменено {sum(1 for r in makers if r.get('note') == 'cancelled_edge_lost')}, "
        f"истекло {sum(1 for r in makers if r.get('note') == 'expired')}"
    )
    line("MAKER закрытые", m_settled)
    line("MAKER +фильтр", [r for r in m_settled if passes(r)])

    print("\nПрошедшие фильтр по часам UTC (full):")
    by_hour: dict[int, list[dict]] = defaultdict(list)
    for r in settled:
        if passes(r):
            by_hour[dtparser.parse(r["entered_at"]).hour].append(r)
    for hour in sorted(by_hour):
        line(f"{hour:02d}:00", by_hour[hour])

    print("\nПрошедшие фильтр: по активам")
    by_asset: dict[str, list[dict]] = defaultdict(list)
    for r in settled:
        if passes(r):
            by_asset[str(r.get("asset") or "?")].append(r)
    for asset in sorted(by_asset):
        line(asset.upper(), by_asset[asset])


if __name__ == "__main__":
    main()
