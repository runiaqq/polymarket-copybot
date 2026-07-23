"""Check implied fee rate for other wallets trading 5-min updown markets."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

DATA_API = "https://data-api.polymarket.com"


def main() -> None:
    now = int(time.time())
    # a recent already-closed btc window
    slug = f"btc-updown-5m-{(now // 300) * 300 - 600}"
    ev = httpx.get(
        "https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=15
    ).json()
    cond = ev[0]["markets"][0]["conditionId"]
    trades = httpx.get(
        f"{DATA_API}/trades", params={"market": cond, "limit": 100}, timeout=15
    ).json()
    wallets: list[str] = []
    for t in trades:
        w = t.get("proxyWallet")
        if w and w not in wallets:
            wallets.append(w)
    print(f"рынок {slug}, трейдов {len(trades)}, кошельков {len(wallets)}")

    for w in wallets[:4]:
        acts = httpx.get(
            f"{DATA_API}/activity", params={"user": w, "limit": 500}, timeout=20
        ).json()
        by_day: dict[str, list[float]] = defaultdict(list)
        for a in acts:
            if str(a.get("type")) != "TRADE" or str(a.get("side") or "").upper() != "BUY":
                continue
            if "updown" not in str(a.get("slug") or ""):
                continue
            price = float(a.get("price") or 0)
            size = float(a.get("size") or 0)
            usdc = float(a.get("usdcSize") or 0)
            if not (0 < price < 1 and size > 0 and usdc > 0):
                continue
            day = datetime.fromtimestamp(
                int(a.get("timestamp") or 0), tz=timezone.utc
            ).strftime("%m-%d")
            by_day[day].append((usdc / (price * size) - 1) / (1 - price))
        line = ", ".join(
            f"{d}: r={sum(v) / len(v):.3f} (n={len(v)})" for d, v in sorted(by_day.items())
        )
        print(f"{w[:10]}…  {line or 'нет updown-покупок'}")


if __name__ == "__main__":
    main()
