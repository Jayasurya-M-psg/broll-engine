"""Footage fetcher — searches Pexels and Pixabay for licence-clean B-roll clips.

Fallback chain:
  1. Pexels (free commercial use, no attribution required but we record it)
  2. Pixabay (CC0 / Pixabay Licence, free commercial use)
  3. AI-generated clip via Replicate SVD (stable-video-diffusion)

Every clip record includes: source, id, URL, licence, attribution, is_ai_generated.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import httpx

from app.core.config import settings
from app.core.errors import FootageSourceError
from app.core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Clip data class (raw candidate before relevance scoring)
# ---------------------------------------------------------------------------

@dataclass
class RawClip:
    source: str                    # "pexels" | "pixabay" | "ai_generated"
    clip_id: str
    clip_url: str                  # Direct video file URL (HD quality)
    preview_image_url: str         # For CLIP scoring
    licence: str
    attribution: str
    is_ai_generated: bool = False
    width: int = 1920
    height: int = 1080
    duration: float = 0.0
    tags: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pexels source
# ---------------------------------------------------------------------------

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_LICENCE = "Pexels Licence (free for commercial use, no attribution required)"
PEXELS_ATTRIBUTION_TMPL = "Video by {photographer} on Pexels (https://www.pexels.com)"


async def _search_pexels(
    query: str,
    avoid: Optional[List[str]],
    orientation: str = "landscape",
    per_page: int = 10,
) -> List[RawClip]:
    if not settings.PEXELS_API_KEY:
        log.warning("PEXELS_API_KEY not set, skipping Pexels")
        return []

    avoid_lower = [a.lower() for a in (avoid or [])]

    params = {
        "query": query,
        "orientation": orientation,
        "size": "medium",
        "per_page": per_page,
    }
    headers = {"Authorization": settings.PEXELS_API_KEY}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(PEXELS_SEARCH_URL, params=params, headers=headers)
        if resp.status_code != 200:
            log.warning("Pexels API error", status=resp.status_code, body=resp.text[:200])
            return []

        data = resp.json()
        clips: List[RawClip] = []
        for video in data.get("videos", []):
            # Safety: skip if any avoid term appears in alt / url / tags
            alt = (video.get("url", "") + " " + str(video.get("tags", []))).lower()
            if any(a in alt for a in avoid_lower):
                continue

            # Pick best quality video file
            files = sorted(
                [f for f in video.get("video_files", []) if f.get("quality") in ("hd", "sd")],
                key=lambda f: f.get("width", 0),
                reverse=True,
            )
            if not files:
                continue
            best = files[0]

            photographer = video.get("user", {}).get("name", "Unknown")
            clips.append(RawClip(
                source="pexels",
                clip_id=f"pexels_{video['id']}",
                clip_url=best["link"],
                preview_image_url=video.get("image", ""),
                licence=PEXELS_LICENCE,
                attribution=PEXELS_ATTRIBUTION_TMPL.format(photographer=photographer),
                width=best.get("width", 1920),
                height=best.get("height", 1080),
                duration=float(video.get("duration", 0)),
            ))

        log.info("Pexels results", query=query, count=len(clips))
        return clips


# ---------------------------------------------------------------------------
# Pixabay source
# ---------------------------------------------------------------------------

PIXABAY_SEARCH_URL = "https://pixabay.com/api/videos/"
PIXABAY_LICENCE = "Pixabay Licence (free for commercial use)"
PIXABAY_ATTRIBUTION_TMPL = "Video by {user} on Pixabay (https://pixabay.com)"


async def _search_pixabay(
    query: str,
    avoid: Optional[List[str]],
    orientation: str = "horizontal",
    per_page: int = 10,
) -> List[RawClip]:
    if not settings.PIXABAY_API_KEY:
        log.warning("PIXABAY_API_KEY not set, skipping Pixabay")
        return []

    avoid_lower = [a.lower() for a in (avoid or [])]

    params = {
        "key": settings.PIXABAY_API_KEY,
        "q": query,
        "video_type": "film",
        "orientation": orientation,
        "per_page": per_page,
        "safesearch": "true",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(PIXABAY_SEARCH_URL, params=params)
        if resp.status_code != 200:
            log.warning("Pixabay API error", status=resp.status_code, body=resp.text[:200])
            return []

        data = resp.json()
        clips: List[RawClip] = []
        for hit in data.get("hits", []):
            tags_str = hit.get("tags", "").lower()
            if any(a in tags_str for a in avoid_lower):
                continue

            videos = hit.get("videos", {})
            # Prefer large > medium > small
            chosen = (
                videos.get("large") or videos.get("medium") or videos.get("small")
            )
            if not chosen:
                continue

            user = hit.get("user", "Unknown")
            preview = hit.get("userImageURL") or chosen.get("thumbnail", "")
            clips.append(RawClip(
                source="pixabay",
                clip_id=f"pixabay_{hit['id']}",
                clip_url=chosen["url"],
                preview_image_url=preview,
                licence=PIXABAY_LICENCE,
                attribution=PIXABAY_ATTRIBUTION_TMPL.format(user=user),
                width=chosen.get("width", 1280),
                height=chosen.get("height", 720),
                duration=float(hit.get("duration", 0)),
            ))

        log.info("Pixabay results", query=query, count=len(clips))
        return clips


# ---------------------------------------------------------------------------
# AI-generated fallback (Replicate SVD)
# ---------------------------------------------------------------------------

async def _generate_ai_clip(prompt: str, work_dir: str) -> Optional[RawClip]:
    """Use Replicate Stable Video Diffusion to generate a short clip."""
    if not settings.AI_GEN_ENABLED or not settings.REPLICATE_API_TOKEN:
        return None

    try:
        import replicate  # type: ignore
    except ImportError:
        log.warning("replicate package not installed, skipping AI fallback")
        return None

    log.info("Generating AI clip via Replicate SVD", prompt=prompt)
    os.environ["REPLICATE_API_TOKEN"] = settings.REPLICATE_API_TOKEN

    try:
        output = replicate.run(
            settings.REPLICATE_MODEL_SVD,
            input={
                "input_image": "https://images.pexels.com/photos/1108099/pexels-photo-1108099.jpeg",
                "sizing_strategy": "maintain_aspect_ratio",
                "frames_per_second": 6,
                "num_frames": 25,
                "motion_bucket_id": 127,
                "cond_aug": 0.02,
            },
        )
        if output:
            url = output[0] if isinstance(output, list) else str(output)
            clip_id = f"ai_{hashlib.md5(prompt.encode()).hexdigest()[:8]}"
            return RawClip(
                source="ai_generated",
                clip_id=clip_id,
                clip_url=url,
                preview_image_url=url,
                licence="Replicate / Stability AI Community Licence (commercial use allowed per terms)",
                attribution=f"AI-generated clip via Stable Video Diffusion on Replicate",
                is_ai_generated=True,
                width=1024,
                height=576,
                duration=4.0,
            )
    except Exception as exc:
        log.error("Replicate SVD failed", error=str(exc))
    return None


# ---------------------------------------------------------------------------
# Public entrypoint with fallback chain
# ---------------------------------------------------------------------------

async def fetch_candidates(
    query: str,
    avoid: Optional[List[str]],
    aspect_ratio: str,
    work_dir: str,
    per_source: int = 8,
) -> List[RawClip]:
    """
    Return candidate clips from Pexels → Pixabay → AI fallback.

    The avoid list blocks unwanted content at the search level.
    Orientation is derived from aspect_ratio.
    """
    orientation_map = {
        "16:9": ("landscape", "horizontal"),
        "9:16": ("portrait", "vertical"),
        "1:1": ("square", "all"),
    }
    pexels_orient, pixabay_orient = orientation_map.get(aspect_ratio, ("landscape", "horizontal"))

    candidates: List[RawClip] = []

    # 1. Pexels
    try:
        pexels = await _search_pexels(query, avoid, pexels_orient, per_source)
        candidates.extend(pexels)
    except Exception as exc:
        log.warning("Pexels search failed", error=str(exc))

    # 2. Pixabay
    try:
        pixabay = await _search_pixabay(query, avoid, pixabay_orient, per_source)
        candidates.extend(pixabay)
    except Exception as exc:
        log.warning("Pixabay search failed", error=str(exc))

    # 3. AI fallback if no results
    if not candidates and settings.AI_GEN_ENABLED:
        log.info("No stock clips found, trying AI generation", query=query)
        ai_clip = await _generate_ai_clip(query, work_dir)
        if ai_clip:
            candidates.append(ai_clip)

    if not candidates:
        raise FootageSourceError("all", f"No footage found for query: {query!r}")

    return candidates
