"""One-off smoke test: capture a few RTDS orders_matched frames and dump fields."""
import json
import time

from websockets.sync.client import connect

SUB = json.dumps({"action": "subscribe",
                  "subscriptions": [{"topic": "activity", "type": "orders_matched"}]},
                 separators=(",", ":"))

seen = 0
with connect("wss://ws-live-data.polymarket.com", open_timeout=20,
             max_size=None, ping_interval=None) as ws:
    ws.send(SUB)
    print("connected+subscribed")
    last_ping = time.time()
    deadline = time.time() + 40
    while time.time() < deadline and seen < 5:
        if time.time() - last_ping >= 5:
            ws.send("ping")
            last_ping = time.time()
        try:
            raw = ws.recv(timeout=5)
        except TimeoutError:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode()
        if "payload" not in raw:
            print("non-data frame:", raw[:80])
            continue
        msg = json.loads(raw)
        p = msg.get("payload") or {}
        recv_ts = time.time()
        trade_ts = p.get("timestamp")
        print(json.dumps({
            "topic": msg.get("topic"), "type": msg.get("type"),
            "payload_is_list": isinstance(msg.get("payload"), list),
            "proxyWallet": p.get("proxyWallet"), "side": p.get("side"),
            "price": p.get("price"), "size": p.get("size"),
            "conditionId": (p.get("conditionId") or "")[:16],
            "asset_prefix": str(p.get("asset") or "")[:12],
            "outcome": p.get("outcome"), "outcomeIndex": p.get("outcomeIndex"),
            "eventSlug": p.get("eventSlug"), "slug": p.get("slug"),
            "timestamp": trade_ts,
            "lag_sec": round(recv_ts - trade_ts, 2) if isinstance(trade_ts, (int, float)) and trade_ts < 1e12 else
                       round(recv_ts - trade_ts / 1000, 2) if isinstance(trade_ts, (int, float)) else None,
            "tx": (p.get("transactionHash") or "")[:14],
        }, ensure_ascii=False))
        seen += 1
print("done, frames:", seen)
