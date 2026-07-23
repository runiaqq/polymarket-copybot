"""Decompose donor PnL into winrate vs all-in entry cost, before/after 17.07.

Donor never sells (holds to resolution), so: won condition <=> REDEEM exists.
All-in cost per share includes fee AND slippage (usdcSize is what he actually paid).
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

DONOR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
DATA_API = "https://data-api.polymarket.com/activity"
PAGE = 500
CUTOVER = "2026-07-17"


def fetch_all() -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while offset < 40_000:
        batch = httpx.get(
            DATA_API,
            params={"user": DONOR, "limit": PAGE, "offset": offset},
            timeout=20.0,
        ).json()
        if not isinstance(batch, list):
            time.sleep(2.0)
            continue
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
        time.sleep(0.15)
    return rows


def main() -> None:
    rows = fetch_all()
    now = time.time()
    conds: dict[str, dict] = defaultdict(
        lambda: {"paid": 0.0, "shares": 0.0, "redeem": 0.0, "first_ts": None, "quoted": 0.0}
    )
    for r in rows:
        cid = str(r.get("conditionId") or "")
        if not cid:
            continue
        kind = str(r.get("type") or "")
        usdc = float(r.get("usdcSize") or 0)
        c = conds[cid]
        if kind == "TRADE" and str(r.get("side") or "").upper() == "BUY":
            price = float(r.get("price") or 0)
            size = float(r.get("size") or 0)
            c["paid"] += usdc
            c["shares"] += size
            c["quoted"] += price * size
            ts = int(r.get("timestamp") or 0)
            if c["first_ts"] is None or ts < c["first_ts"]:
                c["first_ts"] = ts
        elif kind == "REDEEM":
            c["redeem"] += usdc

    periods: dict[str, dict] = {
        "до 17.07": defaultdict(float),
        "после 17.07": defaultdict(float),
    }
    for c in conds.values():
        if not c["shares"] or c["first_ts"] is None:
            continue
        if now - c["first_ts"] < 1800:  # ещё не резолвлен — пропустить
            continue
        day = datetime.fromtimestamp(c["first_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        p = periods["до 17.07"] if day < CUTOVER else periods["после 17.07"]
        p["n"] += 1
        p["wins"] += 1 if c["redeem"] > 0 else 0
        p["paid"] += c["paid"]
        p["shares"] += c["shares"]
        p["quoted"] += c["quoted"]
        p["redeem"] += c["redeem"]

    stats = {}
    for name, p in periods.items():
        n = int(p["n"])
        if not n:
            continue
        wr = p["wins"] / n
        cost = p["paid"] / p["shares"]          # всё включено: цена+комиссия+слиппедж
        quoted = p["quoted"] / p["shares"]      # котировка без издержек
        roi = (p["redeem"] - p["paid"]) / p["paid"]
        stats[name] = (n, wr, quoted, cost, roi, p)
        print(
            f"{name}: рынков {n}, WR={wr:.1%}, котировка {quoted:.4f}, "
            f"все-в цене за акцию {cost:.4f} (издержки {cost / quoted - 1:+.2%}), "
            f"ROI {roi:+.2%}, PnL ${p['redeem'] - p['paid']:+.1f}"
        )
        print(
            f"   breakeven WR при этих издержках: {cost:.1%} | "
            f"ROI при 100% WR: {1 / cost - 1:+.1%}"
        )

    if len(stats) == 2:
        (_, wr_b, _, cost_b, roi_b, _) = stats["до 17.07"]
        (_, wr_a, _, cost_a, roi_a, _) = stats["после 17.07"]
        print("\nКонтрфакты (share-weighted, ROI = WR/cost - 1):")
        print(f"  старый WR, старые издержки: {wr_b / cost_b - 1:+.2%}")
        print(f"  старый WR, НОВЫЕ издержки:  {wr_b / cost_a - 1:+.2%}")
        print(f"  НОВЫЙ WR, старые издержки:  {wr_a / cost_b - 1:+.2%}")
        print(f"  новый WR, новые издержки:   {wr_a / cost_a - 1:+.2%}")


if __name__ == "__main__":
    main()
