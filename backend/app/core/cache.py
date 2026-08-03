"""
Lightweight Redis cache helpers.

Use these on hot read paths to avoid recomputing expensive aggregates on every
request. Falls back gracefully to a no-op when Redis is unreachable so the API
stays available even without the cache layer.

Usage:
    from app.core.cache import cache_get_json, cache_set_json, cache_delete

    cached = cache_get_json("my:key:v1")
    if cached is None:
        cached = expensive_computation()
        cache_set_json("my:key:v1", cached, ttl_seconds=60)

    # Invalidate when the underlying data changes:
    cache_delete("my:key:v1")
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_client = None
_client_failed = False


def get_redis():
    """Return a singleton Redis client or None if unavailable."""
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    try:
        import redis  # type: ignore

        _client = redis.Redis.from_url(
            settings.REDIS_URL, decode_responses=True, socket_timeout=2
        )
        _client.ping()
        return _client
    except Exception as e:
        _client_failed = True
        logger.warning(f"[cache] Redis unavailable, caching disabled: {e}")
        return None


def cache_get_json(key: str) -> Optional[Any]:
    r = get_redis()
    if r is None:
        return None
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning(f"[cache] get failed for {key}: {e}")
        return None


def cache_set_json(key: str, value: Any, ttl_seconds: int = 60) -> None:
    r = get_redis()
    if r is None:
        return
    try:
        r.setex(key, ttl_seconds, json.dumps(value, default=str))
    except Exception as e:
        logger.warning(f"[cache] set failed for {key}: {e}")


def cache_delete(*keys: str) -> None:
    r = get_redis()
    if r is None or not keys:
        return
    try:
        r.delete(*keys)
    except Exception as e:
        logger.warning(f"[cache] delete failed: {e}")
