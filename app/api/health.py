"""Health and readiness endpoints."""
from __future__ import annotations

import time
from typing import Any

import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["health"])

_start_time = time.time()


@router.get("/healthz", summary="Liveness probe")
@router.get("/health", summary="Liveness probe")
async def healthz() -> Any:
    """Returns 200 if the process is alive."""
    return {"status": "ok", "uptime_sec": round(time.time() - _start_time, 1)}


@router.get("/ready", summary="Readiness probe")
async def ready() -> Any:
    """Returns 200 only when all dependencies (Redis, MinIO) are reachable."""
    checks: dict = {}
    overall_ok = True

    # Redis
    try:
        r = redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, decode_responses=True)
        r.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"
        overall_ok = False

    # MinIO
    try:
        from app.storage.minio_client import get_client
        client = get_client()
        client.bucket_exists(settings.MINIO_BUCKET)
        checks["minio"] = "ok"
    except Exception as exc:
        checks["minio"] = f"error: {exc}"
        overall_ok = False

    status_code = 200 if overall_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if overall_ok else "not_ready", "checks": checks},
    )
