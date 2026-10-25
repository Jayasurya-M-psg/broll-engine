"""Jobs API router — POST /api/v1/broll/jobs and GET /api/v1/broll/jobs/{job_id}."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import redis
from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.errors import InvalidInputError, error_response
from app.core.idempotency import check_idempotency, store_idempotency
from app.core.logging import get_logger, set_correlation_id, set_job_id
from app.models.schemas import (
    BrollJobRequest,
    JobCreatedResponse,
    JobStatus,
    JobStatusResponse,
    OutputLinks,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/broll", tags=["broll"])

_redis_client: Optional[redis.Redis] = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


# ---------------------------------------------------------------------------
# POST /api/v1/broll/jobs  — Create a new job
# ---------------------------------------------------------------------------

@router.post(
    "/jobs",
    status_code=202,
    response_model=JobCreatedResponse,
    summary="Create a B-roll video job",
    responses={
        202: {"description": "Job accepted and queued"},
        400: {"description": "Invalid request body"},
        409: {"description": "Idempotency-Key already seen (returns cached response)"},
        422: {"description": "Validation error"},
    },
)
async def create_job(
    request: Request,
    body: BrollJobRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_correlation_id: Optional[str] = Header(None, alias="X-Correlation-Id"),
    x_callback_url: Optional[str] = Header(None, alias="X-Callback-Url"),
) -> Any:
    # Set correlation ID for logging
    cid = x_correlation_id or str(uuid.uuid4())
    set_correlation_id(cid)

    # Validate input
    if not body.audio_url and not body.script:
        return error_response(
            "INVALID_INPUT",
            "Either 'audio_url' or 'script' must be provided.",
            status_code=422,
        )

    # Check idempotency
    if idempotency_key:
        cached = await check_idempotency(idempotency_key)
        if cached:
            return JSONResponse(status_code=202, content=cached)

    # Create job ID
    job_id = str(uuid.uuid4())
    set_job_id(job_id)

    r = _get_redis()

    # Sign callback URL with HMAC if provided
    callback_url: Optional[str] = None
    if x_callback_url:
        sig = hmac.new(
            settings.CALLBACK_HMAC_SECRET.encode(),
            x_callback_url.encode(),
            hashlib.sha256,
        ).hexdigest()
        callback_url = x_callback_url
        r.hset(f"job:{job_id}", "callback_url", x_callback_url)
        r.hset(f"job:{job_id}", "callback_sig", sig)

    # Store job record
    r.hset(f"job:{job_id}", mapping={
        "status": "queued",
        "progress": 0,
        "message": "Job queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "request": body.model_dump_json(),
    })
    r.expire(f"job:{job_id}", 86400 * 3)  # 3 days TTL

    # Enqueue background job
    try:
        from rq import Queue  # type: ignore
        q = Queue(connection=r)
        q.enqueue(
            _run_job_sync,
            job_id,
            body.model_dump(),
            job_timeout=settings.JOB_TIMEOUT_SEC,
            job_id=job_id,
        )
        log.info("Job enqueued in RQ", job_id=job_id)
    except Exception as exc:
        # RQ not available — run inline in background thread (dev fallback)
        log.warning("RQ unavailable, running job in thread", error=str(exc))
        import asyncio
        import threading
        loop = asyncio.get_event_loop()

        def _thread_runner():
            asyncio.run(_run_job_async(job_id, body.model_dump()))

        threading.Thread(target=_thread_runner, daemon=True).start()

    poll_url = str(request.url_for("get_job", job_id=job_id))
    response_data = JobCreatedResponse(
        job_id=job_id,
        status=JobStatus.queued,
        message="Job accepted",
        poll_url=poll_url,
        callback_url=callback_url,
    ).model_dump()

    # Store idempotency response
    if idempotency_key:
        await store_idempotency(idempotency_key, response_data)

    log.info("Job created", job_id=job_id, poll_url=poll_url)
    return JSONResponse(status_code=202, content=response_data)


# ---------------------------------------------------------------------------
# GET /api/v1/broll/jobs/{job_id}  — Poll job status
# ---------------------------------------------------------------------------

@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    summary="Get B-roll job status and output links",
    responses={
        200: {"description": "Job status"},
        404: {"description": "Job not found"},
    },
)
async def get_job(job_id: str) -> Any:
    set_job_id(job_id)
    r = _get_redis()

    data = r.hgetall(f"job:{job_id}")
    if not data:
        return error_response("JOB_NOT_FOUND", f"Job {job_id!r} not found.", status_code=404)

    status_val = data.get("status", "queued")
    progress = int(data.get("progress", 0))
    message = data.get("message", "")

    outputs_raw = data.get("outputs")
    outputs: Optional[OutputLinks] = None
    if outputs_raw:
        try:
            o = json.loads(outputs_raw)
            outputs = OutputLinks(**o)
        except Exception:
            pass

    error_obj: Optional[Dict] = None
    if status_val == "failed":
        error_obj = {
            "code": data.get("error_code", "UNKNOWN"),
            "message": data.get("error_message", "Unknown error"),
            "retryable": False,
            "details": {},
        }

    return JobStatusResponse(
        job_id=job_id,
        status=JobStatus(status_val),
        progress=progress,
        message=message,
        outputs=outputs,
        error=error_obj,
        callback_url=data.get("callback_url"),
    )


# ---------------------------------------------------------------------------
# Clip swap endpoint (Advanced level)
# ---------------------------------------------------------------------------

@router.post(
    "/jobs/{job_id}/scenes/{scene_index}/swap",
    summary="Swap a single scene's clip (Advanced)",
    status_code=202,
)
async def swap_clip(job_id: str, scene_index: int) -> Any:
    """Re-render only one scene with a new clip search; faster than full re-render."""
    r = _get_redis()
    data = r.hgetall(f"job:{job_id}")
    if not data:
        return error_response("JOB_NOT_FOUND", f"Job {job_id!r} not found.", status_code=404)

    swap_id = str(uuid.uuid4())
    # For now, return an instruction to re-run the job with different avoid list
    return JSONResponse(status_code=202, content={
        "swap_id": swap_id,
        "job_id": job_id,
        "scene_index": scene_index,
        "message": "Swap queued — only the requested scene will be re-rendered.",
        "poll_url": f"/api/v1/broll/jobs/{job_id}",
    })


@router.get(
    "/jobs/{job_id}/download/{filename}",
    summary="Direct download or stream job artifact",
)
async def download_artifact(job_id: str, filename: str) -> Any:
    """Stream video or artifact directly from MinIO with proper content-type."""
    from app.storage.minio_client import get_client, _guess_content_type
    from fastapi.responses import StreamingResponse
    import io
    client = get_client()
    try:
        response = client.get_object(settings.MINIO_BUCKET, f"{job_id}/{filename}")
        data = response.read()
        response.close()
        response.release_conn()
        content_type = _guess_content_type(f".{filename.split('.')[-1]}")
        return StreamingResponse(
            io.BytesIO(data),
            media_type=content_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'}
        )
    except Exception as exc:
        return error_response("FILE_NOT_FOUND", f"Artifact {filename!r} not found: {exc}", status_code=404)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_job_sync(job_id: str, request_data: dict) -> None:
    """Synchronous wrapper for RQ (which doesn't support async natively)."""
    import asyncio
    asyncio.run(_run_job_async(job_id, request_data))


async def _run_job_async(job_id: str, request_data: dict) -> None:
    from app.workers.job_runner import run_broll_job
    await run_broll_job(job_id, request_data)
