"""One-off audit: are tracked whales trading, and does the funnel copy them?

Run: python -m scripts.audit_whale_funnel
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from core.config import settings
from core.db.session import get_supabase

DATA_API = "https://data-api.polymarket.com/activity"
DAYS = 7
PAGE = 500


def _whale_trades(address: str, since_ts: float) -> list[dict]:
    """All TRADE activity rows for a wallet since since_ts (paged)."""
    out: list[dict] = []
    offset = 0
    while offset < 3000:
        resp = httpx.get(
            DATA_API,
            params={"user": address, "limit": PAGE, "offset": offset},
            timeout=15.0,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            break
        for t in rows:
            if t.get("type") != "TRADE":
                continue
            ts = int(t.get("timestamp") or 0)
            if ts < since_ts:
                return out
            out.append(t)
        if len(rows) < PAGE:
            break
        offset += PAGE
    return out


def main() -> None:
    sb = get_supabase()
    now = time.time()
    since_ts = now - DAYS * 86400
    since_iso = datetime.now(timezone.utc) - timedelta(days=DAYS)

    wallets = sb.table("tracked_wallets").select("*").execute().data or []
    default_wallets = [w for w in wallets if (w.get("mode") or "default") != "sniper"]
    print(f"tracked_wallets всего: {len(wallets)}, default (киты): {len(default_wallets)}")

    signals = (
        sb.table("trade_signals")
        .select("source_wallet,market_id,token_id,size_usdc,created_at")
        .gte("created_at", since_iso.isoformat())
        .execute()
        .data
        or []
    )
    signals_by_wallet: dict[str, list[dict]] = defaultdict(list)
    for s in signals:
        signals_by_wallet[(s.get("source_wallet") or "").lower()].append(s)

    trades = (
        sb.table("copy_trades")
        .select("id,status,mode,size_usdc,error_msg,created_at")
        .gte("created_at", since_iso.isoformat())
        .execute()
        .data
        or []
    )
    non_sniper = [t for t in trades if (t.get("mode") or "default") != "sniper"]
    by_status: dict[str, int] = defaultdict(int)
    for t in non_sniper:
        by_status[str(t.get("status"))] += 1
    print(f"\ncopy_trades за {DAYS}д: всего {len(trades)}, из них НЕ-снайпер: {len(non_sniper)}")
    print(f"  по статусам: {dict(by_status)}")
    errors = [t for t in non_sniper if t.get("error_msg")]
    for t in errors[:10]:
        print(f"  err: {str(t.get('error_msg'))[:100]}")

    print(f"\n{'кошелёк':<14}{'BUY-филы':>9}{'бёрсты':>8}{'>=порога':>10}"
          f"{'порог$':>8}{'сигналы':>9}  последний BUY")
    total_qualifying = 0
    for w in default_wallets:
        addr = (w.get("address") or "").lower()
        avg = float(w.get("avg_trade_usdc") or 0)
        threshold = max(
            settings.tracked_min_copy_usdc,
            settings.slice_conviction_frac * avg if avg > 0 else 0,
        )
        try:
            raw = _whale_trades(addr, since_ts)
        except Exception as exc:
            print(f"{addr[:12]:<14} DATA-API ERROR: {str(exc)[:60]}")
            continue
        buys = [t for t in raw if (t.get("side") or "").upper() == "BUY"]
        bursts: dict[tuple[str, str], float] = defaultdict(float)
        for t in buys:
            bursts[(t.get("conditionId") or "", t.get("asset") or "")] += float(
                t.get("usdcSize") or 0
            )
        qualifying = {k: v for k, v in bursts.items() if v >= threshold}
        total_qualifying += len(qualifying)
        last_buy = max((int(t.get("timestamp") or 0) for t in buys), default=0)
        last_str = (
            datetime.fromtimestamp(last_buy, tz=timezone.utc).strftime("%d.%m %H:%M")
            if last_buy
            else "—"
        )
        n_signals = len(signals_by_wallet.get(addr, []))
        print(f"{addr[:12]:<14}{len(buys):>9}{len(bursts):>8}{len(qualifying):>10}"
              f"{threshold:>8.0f}{n_signals:>9}  {last_str}")
        for (cond, _token), size in sorted(qualifying.items(), key=lambda x: -x[1])[:5]:
            has_signal = any(
                s.get("market_id") == cond for s in signals_by_wallet.get(addr, [])
            )
            mark = "OK сигнал" if has_signal else "!! БЕЗ СИГНАЛА"
            print(f"    {cond[:20]}…  ${size:.0f}  {mark}")

    print(f"\nИтого бёрстов >= порога за {DAYS}д: {total_qualifying}, "
          f"сигналов в базе: {len(signals)}")


if __name__ == "__main__":
    main()
