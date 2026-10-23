"""Centralised error helpers — always produce Integration Standard JSON errors."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def error_response(
    code: str,
    message: str,
    status_code: int = 400,
    retryable: bool = False,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    """Return a FastAPI JSONResponse with the IncuBrix error envelope."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": details or {},
            }
        },
    )


class BrollEngineError(Exception):
    """Base application exception."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        return error_response(
            code=self.code,
            message=self.message,
            status_code=self.status_code,
            retryable=self.retryable,
            details=self.details,
        )


# Specific error types
class FootageSourceError(BrollEngineError):
    def __init__(self, source: str, detail: str = "") -> None:
        super().__init__(
            code="FOOTAGE_SOURCE_ERROR",
            message=f"Footage source '{source}' failed: {detail}",
            status_code=502,
            retryable=True,
            details={"source": source},
        )


class TranscriptionError(BrollEngineError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            code="TRANSCRIPTION_ERROR",
            message=f"Transcription failed: {detail}",
            status_code=422,
            retryable=False,
        )


class VideoAssemblyError(BrollEngineError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            code="VIDEO_ASSEMBLY_ERROR",
            message=f"Video assembly failed: {detail}",
            status_code=500,
            retryable=True,
        )


class InvalidInputError(BrollEngineError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            code="INVALID_INPUT",
            message=f"Invalid input: {detail}",
            status_code=422,
            retryable=False,
        )
