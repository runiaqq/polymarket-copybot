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
    """Return True only the first time `key` is seen (Redis SETNX with TTL)."""
    try:
        return bool(_client().set(f"once:{key}", "1", nx=True, ex=ttl))
    except Exception:
        # Fail-open: a dead Redis means the worker is degraded anyway.
        return True


def claim(key: str, ttl: int = 7 * 86400) -> None:
    """Mark `key` as seen without caring about the previous state."""
    try:
        _client().set(f"once:{key}", "1", nx=True, ex=ttl)
    except Exception:
        pass
