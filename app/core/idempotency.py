"""Idempotency-Key deduplication backed by Redis."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_pool: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = await aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _pool


async def check_idempotency(key: str) -> Optional[Dict[str, Any]]:
    """Return the cached response for *key*, or None if not seen before."""
    r = await get_redis()
    raw = await r.get(f"idem:{key}")
    if raw:
        log.info("Idempotency cache hit", idempotency_key=key)
        return json.loads(raw)
    return None


async def store_idempotency(key: str, response: Dict[str, Any]) -> None:
    """Cache *response* under *key* for TTL seconds."""
    r = await get_redis()
    await r.setex(
        f"idem:{key}",
        settings.IDEMPOTENCY_TTL_SEC,
        json.dumps(response),
    )
    log.debug("Idempotency response cached", idempotency_key=key)
