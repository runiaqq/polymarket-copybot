"""
Tiny Redis-backed helper for cross-process / restart-safe one-shot guards
(e.g. "did we already notify about this settlement / expiry?").
"""

import ssl

import structlog

from core.config import settings

log = structlog.get_logger(__name__)

_redis = None


def _client():
    global _redis
    if _redis is None:
        import redis as _redis_lib

        kwargs = {"decode_responses": True}
        if settings.redis_url.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = ssl.CERT_NONE
        _redis = _redis_lib.from_url(settings.redis_url, **kwargs)
    return _redis


def notify_once(key: str, ttl: int = 7 * 86400) -> bool:
    """Return True only the first time `key` is seen (Redis SETNX with TTL).

    Default TTL is 7 days for permanent "done" markers (e.g. settle:*).
    For short-lived in-flight leases (e.g. redeem:*) pass a short TTL such as
    settings.redeem_lease_sec (900 s) — the real terminal state is the DB ledger.
    """
    try:
        return bool(_client().set(f"once:{key}", "1", nx=True, ex=ttl))
    except Exception:
        # Fail-open: a dead Redis means the worker is degraded anyway.
        return True


def clear_once(key: str) -> None:
    """Delete the `once:{key}` lease so it can be re-claimed.

    Blueprint 20 A1: used to release a short-lived redeem lease after a failure
    or skip so the next reconcile cycle can re-attempt instead of waiting for the
    TTL to expire.  Safe to call even when the key doesn't exist.
    The permanent terminal state is copy_trades.redeemed_at IS NOT NULL (the DB
    ledger), NOT this Redis key — clearing the key is never a double-spend risk.
    """
    try:
        _client().delete(f"once:{key}")
    except Exception:
        pass


def claim(key: str, ttl: int = 7 * 86400) -> None:
    """Mark `key` as seen without caring about the previous state."""
    try:
        _client().set(f"once:{key}", "1", nx=True, ex=ttl)
    except Exception:
        pass


# ── Blueprint 2: cross-process slice-accumulation buckets ────────────────────
# Each bucket is a Redis hash keyed by "accum:<wallet>:<cond>:<token>".
# Fields: first_ts, last_ts, acc_usdc, acc_notional, fills, fired

_ACCUM_PREFIX = "accum:"


def accum_add(key: str, tx_hash: str, size: float, price: float,
              ts: int, ttl: int) -> None:
    """
    Merge one fill into the accumulation bucket.  Uses a pipeline to minimise
    round-trips: HSETNX sets first_ts only if missing, then increments running
    totals and refreshes the TTL (sliding window).  Individual fills are deduped
    via a small Redis set so the same tx_hash is never double-counted.
    """
    r = _client()
    dedup_key = f"accum_seen:{tx_hash}"
    try:
        # Skip already-counted fills (cross-process dedup by tx_hash).
        if not r.set(dedup_key, "1", nx=True, ex=ttl):
            return
        hkey = f"{_ACCUM_PREFIX}{key}"
        pipe = r.pipeline()
        pipe.hsetnx(hkey, "first_ts", ts)
        pipe.hset(hkey, "last_ts", ts)
        pipe.hincrbyfloat(hkey, "acc_usdc", size)
        pipe.hincrbyfloat(hkey, "acc_notional", price * size)
        pipe.hincrby(hkey, "fills", 1)
        pipe.hsetnx(hkey, "fired", "0")
        pipe.expire(hkey, ttl)
        pipe.execute()
    except Exception:
        log.warning("accum_add_failed", key=key)


def accum_get(key: str) -> dict | None:
    """Return the current accumulation bucket, or None if it doesn't exist."""
    try:
        hkey = f"{_ACCUM_PREFIX}{key}"
        data = _client().hgetall(hkey)
        if not data:
            return None
        return {
            "first_ts":    int(float(data.get("first_ts", 0))),
            "last_ts":     int(float(data.get("last_ts", 0))),
            "acc_usdc":    float(data.get("acc_usdc", 0)),
            "acc_notional": float(data.get("acc_notional", 0)),
            "fills":       int(data.get("fills", 0)),
            "fired":       data.get("fired", "0") == "1",
        }
    except Exception:
        log.warning("accum_get_failed", key=key)
        return None


def accum_mark_fired(key: str, ttl: int) -> bool:
    """
    Atomically set fired=1 only if it was 0 (prevents double-fire on the same
    bucket).  Returns True when this call was the one that fired it.
    """
    try:
        hkey = f"{_ACCUM_PREFIX}{key}"
        pipe = _client().pipeline()
        # GETSET fired → old value; if "0" we won the race.
        pipe.hget(hkey, "fired")
        pipe.hset(hkey, "fired", "1")
        results = pipe.execute()
        old = results[0]
        return old in (None, "0", b"0")
    except Exception:
        log.warning("accum_mark_fired_failed", key=key)
        return False
