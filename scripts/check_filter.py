"""Quick check: BP30.4 filter section on collected full-variant rows (no donor RPC)."""

from __future__ import annotations

from core.config import settings
from core.db.session import get_supabase
from core.shadow_model import passes_signal_filter


def main() -> None:
    sb = get_supabase()
    rows: list[dict] = []
    offset = 0
    while True:
        page = (
            sb.table("shadow_trades")
            .select("status,stake_usdc,pnl_usdc,edge,spot,open_price,q_cal")
            .eq("variant", "full")
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

    def report(label: str, subset: list[dict]) -> None:
        passed = [
            r
            for r in subset
            if passes_signal_filter(
                float(r.get("edge") or 0),
                float(r.get("spot") or 0),
                float(r.get("open_price") or 0),
                min_edge=settings.shadow_filter_min_edge,
                min_strike_bp=settings.shadow_filter_min_strike_bp,
            )
        ]
        rejected = [r for r in subset if r not in passed]
        for name, group in (("ПРОШЛИ", passed), ("ОТСЕЯНЫ", rejected)):
            n = len(group)
            if not n:
                print(f"  {name}: n=0")
                continue
            wins = sum(1 for r in group if r["status"] == "win")
            stake = sum(float(r.get("stake_usdc") or 0) for r in group)
            net = sum(float(r.get("pnl_usdc") or 0) for r in group)
            print(
                f"  {name}: n={n} WR={wins / n * 100:.1f}% net=${net:+.2f} "
                f"({net / stake * 100:+.1f}%)"
            )

    print(f"Фильтр: edge>={settings.shadow_filter_min_edge}, дистанция>={settings.shadow_filter_min_strike_bp}bp")
    print("Все закрытые full-сделки:")
    report("all", rows)
    bp302 = [r for r in rows if r.get("q_cal") is not None]
    print(f"Эпоха BP30.2 (q_cal задан), n={len(bp302)}:")
    report("bp302", bp302)


if __name__ == "__main__":
    main()
