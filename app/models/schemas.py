"""Pydantic schemas for the B-roll Video Engine API."""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AspectRatio(str, Enum):
    landscape = "16:9"
    portrait = "9:16"
    square = "1:1"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class CaptionStyle(str, Enum):
    burn_in = "burn_in"
    srt_only = "srt_only"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SceneHint(BaseModel):
    text: str
    visual_hint: Optional[str] = None


class CaptionsConfig(BaseModel):
    burn_in: bool = False
    srt: bool = True


class BrollJobRequest(BaseModel):
    """Body for POST /api/v1/broll/jobs"""

    audio_url: Optional[str] = Field(
        None,
        description="URL of an existing voiceover (mp3, wav, m4a, up to 10 min)."
    )
    script: Optional[str] = Field(
        None,
        description="Plain-text script; TTS will be generated if no audio_url."
    )
    scenes: Optional[List[SceneHint]] = Field(
        None,
        description="Optional IncuBrix scene list to override auto-segmentation."
    )
    voice: Optional[str] = Field(
        "en+f3",
        description="TTS voice selector (eSpeak voice string, e.g. en+f3, hi, ta)."
    )
    aspect_ratios: List[str] = Field(
        default=["16:9"],
        description="Desired output aspect ratios, e.g. ['16:9','9:16']."
    )
    style: Optional[str] = Field(
        "bright, modern, real-world footage",
        description="Visual style hint used in search queries."
    )
    avoid: Optional[List[str]] = Field(
        None,
        description="Topics / subjects to avoid in footage search."
    )
    captions: Optional[CaptionsConfig] = Field(
        default_factory=CaptionsConfig
    )

    class Config:
        json_schema_extra = {
            "example": {
                "audio_url": "https://example.com/voiceover.mp3",
                "script": None,
                "voice": "en+f3",
                "aspect_ratios": ["16:9", "9:16"],
                "style": "bright, modern, real-world footage",
                "avoid": ["people's faces", "alcohol"],
                "captions": {"burn_in": True, "srt": True}
            }
        }


# ---------------------------------------------------------------------------
# Clip / scene data models
# ---------------------------------------------------------------------------

class ClipRecord(BaseModel):
    scene_index: int
    narration_text: str
    start_time: float           # seconds from audio start
    end_time: float
    clip_source: str            # "pexels" | "pixabay" | "ai_generated"
    clip_id: str
    clip_url: str
    licence: str
    attribution: str
    is_ai_generated: bool
    relevance_score: float = Field(..., ge=1.0, le=5.0)
    selection_reason: str


class TimelineEntry(BaseModel):
    scene_index: int
    narration_text: str
    start_time: float
    end_time: float
    clip: ClipRecord


# ---------------------------------------------------------------------------
# Output manifest
# ---------------------------------------------------------------------------

class ManifestModel(BaseModel):
    job_id: str
    created_at: str
    models: Dict[str, str]        # e.g. {"transcription": "openai/whisper-base"}
    providers: Dict[str, str]
    settings: Dict[str, Any]
    timings: Dict[str, float]     # e.g. {"total_seconds": 45.2}
    cost_estimate_usd: float
    warnings: List[str]
    licence_summary: str


# ---------------------------------------------------------------------------
# Job status response
# ---------------------------------------------------------------------------

class OutputLinks(BaseModel):
    video_16_9: Optional[str] = None
    video_9_16: Optional[str] = None
    video_1_1: Optional[str] = None
    captions_srt: Optional[str] = None
    timeline_json: Optional[str] = None
    manifest_json: Optional[str] = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: int = Field(0, ge=0, le=100)
    message: Optional[str] = None
    outputs: Optional[OutputLinks] = None
    error: Optional[Dict[str, Any]] = None
    callback_url: Optional[str] = None


class JobCreatedResponse(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.queued
    message: str = "Job accepted"
    poll_url: str
    callback_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Error model
# ---------------------------------------------------------------------------

class ApiError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: Optional[Dict[str, Any]] = None


class ErrorResponse(BaseModel):
    error: ApiError
