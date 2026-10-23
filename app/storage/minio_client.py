"""MinIO (S3-compatible) storage client."""
from __future__ import annotations

import io
import os
from datetime import timedelta
from pathlib import Path
from typing import Optional

from minio import Minio
from minio.error import S3Error

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_client: Optional[Minio] = None


def get_client() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
        # Ensure bucket exists
        try:
            if not _client.bucket_exists(settings.MINIO_BUCKET):
                _client.make_bucket(settings.MINIO_BUCKET)
                log.info("MinIO bucket created", bucket=settings.MINIO_BUCKET)
        except S3Error as exc:
            log.error("MinIO bucket init error", error=str(exc))
    return _client


def upload_file(local_path: str | Path, object_name: str) -> str:
    """Upload a local file and return a presigned download URL."""
    client = get_client()
    local_path = Path(local_path)
    content_type = _guess_content_type(local_path.suffix)

    client.fput_object(
        settings.MINIO_BUCKET,
        object_name,
        str(local_path),
        content_type=content_type,
    )
    log.info("Uploaded to MinIO", object=object_name, size_bytes=local_path.stat().st_size)

    url = client.presigned_get_object(
        settings.MINIO_BUCKET,
        object_name,
        expires=timedelta(days=7),
    )
    # Replace internal docker hostname with public URL for external access
    if settings.MINIO_PUBLIC_URL:
        internal = f"http://{settings.MINIO_ENDPOINT}"
        url = url.replace(internal, settings.MINIO_PUBLIC_URL)
    return url


def upload_bytes(data: bytes, object_name: str, content_type: str = "application/octet-stream") -> str:
    """Upload raw bytes and return a presigned URL."""
    client = get_client()
    client.put_object(
        settings.MINIO_BUCKET,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    url = client.presigned_get_object(
        settings.MINIO_BUCKET,
        object_name,
        expires=timedelta(days=7),
    )
    if settings.MINIO_PUBLIC_URL:
        internal = f"http://{settings.MINIO_ENDPOINT}"
        url = url.replace(internal, settings.MINIO_PUBLIC_URL)
    return url


def _guess_content_type(suffix: str) -> str:
    mapping = {
        ".mp4": "video/mp4",
        ".srt": "text/plain",
        ".json": "application/json",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
    }
    return mapping.get(suffix.lower(), "application/octet-stream")
