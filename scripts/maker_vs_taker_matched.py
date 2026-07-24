"""Maker vs full-taker comparison restricted to the maker-era period (same days/markets)."""

from __future__ import annotations

from collections import defaultdict

from dateutil import parser as dtparser

from core.db.session import get_supabase


def fetch_all(sb, variant: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch = (
            sb.table("shadow_trades")
            .select("variant,status,created_at,stake_usdc,pnl_usdc,fee_usdc,sim_fill_price,model_p,condition_id,note")
            .eq("variant", variant)
            .order("created_at", desc=False)
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def agg(rows: list[dict]) -> dict:
    settled = [r for r in rows if r["status"] in ("win", "loss")]
    n = len(settled)
    if not n:
        return {"n": 0}
    wins = sum(1 for r in settled if r["status"] == "win")
    stake = sum(float(r.get("stake_usdc") or 0) for r in settled)
    net = sum(float(r.get("pnl_usdc") or 0) for r in settled)
    fees = sum(float(r.get("fee_usdc") or 0) for r in settled)
    px = sum(float(r.get("sim_fill_price") or 0) for r in settled) / n
    return {"n": n, "wr": wins / n, "stake": stake, "net": net, "fees": fees, "px": px}


def main() -> None:
    sb = get_supabase()
    makers = fetch_all(sb, "maker")
    fulls = fetch_all(sb, "full")
    if not makers:
        print("no maker rows")
        return
    era_start = min(dtparser.parse(r["created_at"]) for r in makers)
    print(f"maker era start: {era_start.isoformat()}")

    fulls_era = [r for r in fulls if dtparser.parse(r["created_at"]) >= era_start]
    maker_conds = {r["condition_id"] for r in makers if r["status"] in ("win", "loss")}
    fulls_matched = [r for r in fulls_era if r["condition_id"] in maker_conds]

    for name, rows in (
        ("MAKER (filled+settled)", makers),
        ("FULL taker, maker era", fulls_era),
        ("FULL taker, matched markets only", fulls_matched),
    ):
        a = agg(rows)
        if not a["n"]:
            print(f"{name}: n=0")
            continue
        print(
            f"{name:34s} n={a['n']:4d} WR={a['wr'] * 100:5.1f}% px={a['px']:.3f} "
            f"stake=${a['stake']:8.2f} net=${a['net']:+8.2f} ({a['net'] / a['stake'] * 100:+5.1f}%) fees=${a['fees']:.2f}"
        )

    # unfilled maker orders: what would taker have done on those markets?
    unfilled_conds = {r["condition_id"] for r in makers if r["status"] == "unfilled"}
    fulls_on_unfilled = [r for r in fulls_era if r["condition_id"] in unfilled_conds]
    a = agg(fulls_on_unfilled)
    if a.get("n"):
        print(
            f"{'FULL taker on maker-UNFILLED mkts':34s} n={a['n']:4d} WR={a['wr'] * 100:5.1f}% px={a['px']:.3f} "
            f"stake=${a['stake']:8.2f} net=${a['net']:+8.2f} ({a['net'] / a['stake'] * 100:+5.1f}%) fees=${a['fees']:.2f}"
        )

    print("\nПо дням (UTC), maker vs full:")
    by_day: dict[str, dict[str, dict]] = defaultdict(dict)
    for label, rows in (("maker", makers), ("full", fulls_era)):
        per: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            per[dtparser.parse(r["created_at"]).strftime("%d.%m")].append(r)
        for day, rs in per.items():
            by_day[day][label] = agg(rs)
    for day in sorted(by_day):
        parts = []
        for label in ("maker", "full"):
            a = by_day[day].get(label) or {"n": 0}
            if a["n"]:
                parts.append(f"{label}: n={a['n']:3d} WR={a['wr'] * 100:5.1f}% net=${a['net']:+7.2f} ({a['net'] / a['stake'] * 100:+5.1f}%)")
            else:
                parts.append(f"{label}: n=  0")
        print(f"  {day}  " + "   ".join(parts))


if __name__ == "__main__":
    main()
