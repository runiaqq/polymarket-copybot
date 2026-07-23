"""Implied fee rate r per day for donor BUYs: r = (usdc/(p*s) - 1) / (1 - p)."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

DONOR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
DATA_API = "https://data-api.polymarket.com/activity"
PAGE = 500


def main() -> None:
    rows: list[dict] = []
    offset = 0
    while offset < 40_000:
        resp = httpx.get(
            DATA_API,
            params={"user": DONOR, "limit": PAGE, "offset": offset},
            timeout=20.0,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
        time.sleep(0.15)

    by_day: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if str(r.get("type")) != "TRADE" or str(r.get("side") or "").upper() != "BUY":
            continue
        price = float(r.get("price") or 0)
        size = float(r.get("size") or 0)
        usdc = float(r.get("usdcSize") or 0)
        if not (0 < price < 1 and size > 0 and usdc > 0):
            continue
        ratio = usdc / (price * size)
        implied_r = (ratio - 1.0) / (1.0 - price)
        day = datetime.fromtimestamp(
            int(r.get("timestamp") or 0), tz=timezone.utc
        ).strftime("%Y-%m-%d")
        by_day[day].append((price, implied_r))

    print(f"{'день':<12}{'n':>5}{'ср. цена входа':>15}{'implied r':>12}")
    for day in sorted(by_day):
        vals = by_day[day]
        avg_p = sum(v[0] for v in vals) / len(vals)
        avg_r = sum(v[1] for v in vals) / len(vals)
        print(f"{day:<12}{len(vals):>5}{avg_p:>15.3f}{avg_r:>12.4f}")


if __name__ == "__main__":
    main()
