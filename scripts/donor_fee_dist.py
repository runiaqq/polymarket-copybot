"""Distribution of per-trade implied fee rate for the donor, before vs after 17.07."""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone

import httpx

DONOR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
DATA_API = "https://data-api.polymarket.com/activity"
PAGE = 500
CUTOVER = "2026-07-17"


def main() -> None:
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

    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    examples_after: list[str] = []
    for r in rows:
        if str(r.get("type")) != "TRADE" or str(r.get("side") or "").upper() != "BUY":
            continue
        price = float(r.get("price") or 0)
        size = float(r.get("size") or 0)
        usdc = float(r.get("usdcSize") or 0)
        if not (0 < price < 1 and size > 0 and usdc > 0):
            continue
        implied = (usdc / (price * size) - 1) / (1 - price)
        bucket = f"{round(implied, 2):.2f}"
        day = datetime.fromtimestamp(
            int(r.get("timestamp") or 0), tz=timezone.utc
        ).strftime("%Y-%m-%d")
        if day < CUTOVER:
            before[bucket] += 1
        else:
            after[bucket] += 1
            if len(examples_after) < 8:
                examples_after.append(
                    f"p={price:.3f} s={size:.2f} usdc={usdc:.4f} r={implied:.4f} {day}"
                )

    print("до 17.07:", dict(sorted(before.items())))
    print("после 17.07:", dict(sorted(after.items())))
    print("\nпримеры после 17.07:")
    for e in examples_after:
        print(" ", e)


if __name__ == "__main__":
    main()
