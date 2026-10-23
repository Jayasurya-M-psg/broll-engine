"""FastAPI application entry point for the B-roll Video Engine."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import jobs, health
from app.core.config import settings
from app.core.errors import BrollEngineError
from app.core.logging import configure_logging, get_logger, set_correlation_id

# Configure structured logging before anything else
configure_logging()
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "BrollEngine starting",
        version=settings.APP_VERSION,
        whisper_model=settings.WHISPER_MODEL,
        clip_enabled=settings.CLIP_ENABLED,
    )
    # Ensure MinIO bucket exists on startup
    try:
        from app.storage.minio_client import get_client
        get_client()
        log.info("MinIO connected")
    except Exception as exc:
        log.warning("MinIO connection failed at startup", error=str(exc))

    yield

    log.info("BrollEngine shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="BrollEngine — Script & Voiceover to B-roll Video",
    description=(
        "Takes a voiceover audio URL or plain-text script and returns a finished, "
        "licence-clean B-roll video timed to the narration. "
        "Built for IncuBrix Engineering Assessment 3."
    ),
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS (allow all origins for demo; restrict in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Middleware: inject X-Correlation-Id into every request
# ---------------------------------------------------------------------------

@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    set_correlation_id(cid)
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = cid
    return response


# ---------------------------------------------------------------------------
# Exception handlers — always return Integration Standard JSON envelope
# ---------------------------------------------------------------------------

@app.exception_handler(BrollEngineError)
async def broll_error_handler(request: Request, exc: BrollEngineError) -> JSONResponse:
    return exc.to_response()


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request body validation failed.",
                "retryable": False,
                "details": {"errors": exc.errors()},
            }
        },
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("Unhandled exception", error=str(exc), path=str(request.url))
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected internal error occurred.",
                "retryable": True,
                "details": {},
            }
        },
    )


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(health.router)
app.include_router(jobs.router)


# ---------------------------------------------------------------------------
# Root redirect to docs
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root() -> Any:
    return JSONResponse({"service": "BrollEngine", "docs": "/docs", "version": settings.APP_VERSION})
