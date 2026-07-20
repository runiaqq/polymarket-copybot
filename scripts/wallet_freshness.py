"""One-off: how fresh is each tracked wallet's activity (Data API)."""
import time

from core.db import list_tracked_wallets
from core.polymarket import fetch_donor_recent_trades

now = time.time()
for w in list_tracked_wallets():
    addr = (w.get("address") or "").lower()
    try:
        trades = fetch_donor_recent_trades(addr, limit=10)
        ts = [t["timestamp"] for t in trades]
        age = round((now - max(ts)) / 3600, 1) if ts else "no trades"
        n24 = sum(1 for t in ts if now - t < 86400)
    except Exception as e:
        age, n24 = f"err: {str(e)[:40]}", "-"
    label = str(w.get("label"))[:20]
    mode = w.get("mode", "default")
    print(f"{mode:8s} {addr[:12]} {label:20s} last, h ago: {str(age):10s} fills 24h(of 10): {n24}")
