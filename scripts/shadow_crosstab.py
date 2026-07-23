"""Cross-tab: edge bin x strike-distance bin for settled full-variant trades."""

from __future__ import annotations

import math
from collections import defaultdict

from core.db.session import get_supabase


def _edge_bin(edge: float) -> str:
    if edge < 0.07:
        return "edge 5-7%"
    if edge < 0.10:
        return "edge 7-10%"
    return "edge >=10%"


def _strike_bin(spot: float, open_price: float) -> str:
    if spot <= 0 or open_price <= 0:
        return "?"
    bps = abs(math.log(spot / open_price)) * 10_000
    if bps < 3:
        return "<3bp"
    if bps < 8:
        return "3-8bp"
    return ">=8bp"


def _line(rows: list[dict]) -> str:
    n = len(rows)
    if not n:
        return "n=  0"
    wins = sum(1 for r in rows if r["status"] == "win")
    net = sum(float(r.get("pnl_usdc") or 0) for r in rows)
    stake = sum(float(r.get("stake_usdc") or 0) for r in rows)
    model = sum(float(r.get("model_p") or 0) for r in rows) / n
    return (
        f"n={n:4d}  WR={wins / n:6.1%}  model={model:6.1%}  "
        f"net=${net:+8.2f} ({net / stake if stake else 0:+6.1%})"
    )


def main() -> None:
    sb = get_supabase()
    rows = (
        sb.table("shadow_trades")
        .select("status,pnl_usdc,stake_usdc,edge,spot,open_price,model_p,sim_fill_price")
        .in_("status", ["win", "loss"])
        .eq("variant", "full")
        .limit(2000)
        .execute()
        .data
        or []
    )
    print(f"full-сделок: {len(rows)}")
    grid: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        key = (
            _edge_bin(float(r.get("edge") or 0)),
            _strike_bin(float(r.get("spot") or 0), float(r.get("open_price") or 0)),
        )
        grid[key].append(r)
    for edge_bin in ["edge 5-7%", "edge 7-10%", "edge >=10%"]:
        print(f"\n{edge_bin}")
        for strike_bin in ["<3bp", "3-8bp", ">=8bp"]:
            print(f"  {strike_bin:<7} {_line(grid.get((edge_bin, strike_bin), []))}")

    print("\nПо цене входа (full):")
    by_price: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        px = float(r.get("sim_fill_price") or 0)
        label = "<0.6" if px < 0.6 else ("0.6-0.75" if px < 0.75 else ("0.75-0.87" if px < 0.87 else ">=0.87"))
        by_price[label].append(r)
    for label in ["<0.6", "0.6-0.75", "0.75-0.87", ">=0.87"]:
        print(f"  {label:<9} {_line(by_price.get(label, []))}")


if __name__ == "__main__":
    main()
