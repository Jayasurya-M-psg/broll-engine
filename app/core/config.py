"""Application configuration — all values from environment variables or .env file."""
from __future__ import annotations

from typing import List, Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # App
    # -------------------------------------------------------------------------
    APP_NAME: str = "BrollEngine"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # -------------------------------------------------------------------------
    # API server
    # -------------------------------------------------------------------------
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 1

    # -------------------------------------------------------------------------
    # Redis (job queue + idempotency store)
    # -------------------------------------------------------------------------
    REDIS_URL: str = "redis://redis:6379/0"

    # -------------------------------------------------------------------------
    # MinIO / S3 storage
    # -------------------------------------------------------------------------
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET: str = "broll-outputs"
    MINIO_SECURE: bool = False
    # Public URL base for presigned links (override when behind a proxy)
    MINIO_PUBLIC_URL: str = "http://localhost:9000"

    # -------------------------------------------------------------------------
    # Footage sources (API keys)
    # -------------------------------------------------------------------------
    PEXELS_API_KEY: str = ""
    PIXABAY_API_KEY: str = ""

    # -------------------------------------------------------------------------
    # AI / Generative fallback
    # -------------------------------------------------------------------------
    # If both stock sources fail, use Replicate (Stable Video Diffusion) as fallback
    REPLICATE_API_TOKEN: str = ""
    REPLICATE_MODEL_SVD: str = "stability-ai/stable-video-diffusion:3f0457e4619daac51203dedb472816fd4af51f3149fa7a9e0b5ffcf1b8172438"
    AI_GEN_ENABLED: bool = True

    # -------------------------------------------------------------------------
    # Transcription
    # -------------------------------------------------------------------------
    WHISPER_MODEL: str = "base"   # tiny | base | small | medium | large
    WHISPER_DEVICE: str = "cpu"   # cpu | cuda

    # -------------------------------------------------------------------------
    # TTS (text-to-speech for script input)
    # -------------------------------------------------------------------------
    TTS_ENGINE: str = "edge"      # edge | espeak | coqui
    TTS_DEFAULT_VOICE: str = "en-US-JennyNeural"
    TTS_RATE: str = "-15%"        # Narration pacing rate (increases video duration based on script)
    MIN_SCENE_DURATION_SEC: float = 3.5

    # -------------------------------------------------------------------------
    # CLIP relevance scoring
    # -------------------------------------------------------------------------
    CLIP_MODEL: str = "openai/clip-vit-base-patch32"
    CLIP_ENABLED: bool = True
    CLIP_MIN_RATING_THRESHOLD: float = 4.0

    # -------------------------------------------------------------------------
    # Video output
    # -------------------------------------------------------------------------
    VIDEO_FPS: int = 30
    VIDEO_WIDTH_16_9: int = 1920
    VIDEO_HEIGHT_16_9: int = 1080
    DEFAULT_ASPECT_RATIOS: str = "16:9"   # comma-separated
    MAX_AUDIO_DURATION_SEC: int = 600     # 10 minutes

    # -------------------------------------------------------------------------
    # Job settings
    # -------------------------------------------------------------------------
    JOB_TIMEOUT_SEC: int = 900            # 15 min hard timeout
    IDEMPOTENCY_TTL_SEC: int = 86400      # 24 h
    CALLBACK_HMAC_SECRET: str = "change-me-in-production"

    # -------------------------------------------------------------------------
    # Batch runner
    # -------------------------------------------------------------------------
    BATCH_OUTPUT_DIR: str = "batch_results"

    @field_validator("DEFAULT_ASPECT_RATIOS")
    @classmethod
    def _validate_ratios(cls, v: str) -> str:
        allowed = {"16:9", "9:16", "1:1"}
        for r in v.split(","):
            r = r.strip()
            if r not in allowed:
                raise ValueError(f"Invalid aspect ratio: {r}")
        return v


settings = Settings()
