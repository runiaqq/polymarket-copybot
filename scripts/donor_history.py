"""Donor lifetime daily cashflow: buys, sells, redeems -> daily PnL proxy.

For 5-minute markets redemption follows the buy within minutes, so daily
net cashflow (redeem + sell - buy) is a good realized-PnL proxy.

Run: python -m scripts.donor_history
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

DONOR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
DATA_API = "https://data-api.polymarket.com/activity"
PAGE = 500
MAX_ROWS = 40_000


def fetch_all() -> list[dict]:
    out: list[dict] = []
    offset = 0
    while offset < MAX_ROWS:
        resp = httpx.get(
            DATA_API,
            params={"user": DONOR, "limit": PAGE, "offset": offset},
            timeout=20.0,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < PAGE:
            break
        offset += PAGE
        time.sleep(0.15)
    return out


def main() -> None:
    rows = fetch_all()
    print(f"строк активности: {len(rows)}")
    if rows:
        oldest = min(int(r.get("timestamp") or 0) for r in rows)
        print(
            "самая старая запись:",
            datetime.fromtimestamp(oldest, tz=timezone.utc).strftime("%d.%m.%Y"),
        )
        sample = rows[0]
        print("поля примера строки:", sorted(sample.keys()))
        print("пример строки:", json.dumps(sample, ensure_ascii=False)[:500])

    by_day: dict[str, dict[str, float]] = defaultdict(
        lambda: {"buy": 0.0, "sell": 0.0, "redeem": 0.0, "n_buy": 0}
    )
    types_seen: dict[str, int] = defaultdict(int)
    for r in rows:
        ts = int(r.get("timestamp") or 0)
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        kind = str(r.get("type") or "")
        types_seen[kind] += 1
        usdc = float(r.get("usdcSize") or 0)
        if kind == "TRADE":
            side = str(r.get("side") or "").upper()
            if side == "BUY":
                by_day[day]["buy"] += usdc
                by_day[day]["n_buy"] += 1
            else:
                by_day[day]["sell"] += usdc
        elif kind == "REDEEM":
            by_day[day]["redeem"] += usdc

    print("\nтипы записей:", dict(types_seen))
    print(f"\n{'день':<12}{'покупок':>9}{'buy $':>10}{'sell $':>10}{'redeem $':>10}{'PnL $':>9}")
    cumulative = 0.0
    for day in sorted(by_day):
        d = by_day[day]
        pnl = d["redeem"] + d["sell"] - d["buy"]
        cumulative += pnl
        print(
            f"{day:<12}{int(d['n_buy']):>9}{d['buy']:>10.0f}{d['sell']:>10.0f}"
            f"{d['redeem']:>10.0f}{pnl:>+9.1f}   cum={cumulative:+.1f}"
        )


if __name__ == "__main__":
    main()
