"""Detect taker fee empirically: ratio usdcSize / (price*size) per day for donor BUYs."""

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

    by_day: dict[str, list[float]] = defaultdict(list)
    rebates = []
    for r in rows:
        kind = str(r.get("type") or "")
        ts = int(r.get("timestamp") or 0)
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if kind == "TAKER_REBATE":
            rebates.append((day, float(r.get("usdcSize") or 0)))
            continue
        if kind != "TRADE" or str(r.get("side") or "").upper() != "BUY":
            continue
        price = float(r.get("price") or 0)
        size = float(r.get("size") or 0)
        usdc = float(r.get("usdcSize") or 0)
        if price > 0 and size > 0 and usdc > 0:
            by_day[day].append(usdc / (price * size))

    print(f"{'день':<12}{'n':>5}{'средний usdc/(p*s)':>20}")
    for day in sorted(by_day):
        vals = by_day[day]
        print(f"{day:<12}{len(vals):>5}{sum(vals) / len(vals):>20.5f}")
    print("\nTAKER_REBATE записи:", rebates)


if __name__ == "__main__":
    main()
