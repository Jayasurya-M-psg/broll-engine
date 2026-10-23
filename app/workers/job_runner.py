"""Main job orchestrator — coordinates all workers for a single B-roll job.

This runs inside a Redis RQ worker process (background job).
Progress updates are written back to Redis so the status endpoint can poll them.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import redis

from app.core.config import settings
from app.core.errors import BrollEngineError, FootageSourceError, VideoAssemblyError
from app.core.logging import get_logger, set_job_id
from app.models.schemas import ClipRecord, TimelineEntry, ManifestModel
from app.workers import (
    transcriber,
    query_generator,
    footage_fetcher,
    clip_selector,
    video_assembler,
)
from app.storage import minio_client

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

def _set_progress(r: redis.Redis, job_id: str, pct: int, message: str) -> None:
    r.hset(f"job:{job_id}", mapping={
        "progress": pct,
        "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    log.info("Progress update", job_id=job_id, pct=pct, message=message)


def _set_failed(r: redis.Redis, job_id: str, code: str, message: str) -> None:
    r.hset(f"job:{job_id}", mapping={
        "status": "failed",
        "progress": 0,
        "error_code": code,
        "error_message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def _set_succeeded(r: redis.Redis, job_id: str, outputs: Dict) -> None:
    r.hset(f"job:{job_id}", mapping={
        "status": "succeeded",
        "progress": 100,
        "message": "Job complete",
        "outputs": json.dumps(outputs),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Scene segmenter helper
# ---------------------------------------------------------------------------

def _segment_from_scenes(scenes: list, audio_duration: float) -> List[transcriber.TranscriptSegment]:
    """Convert user-provided scene list to TranscriptSegment objects with equal timing."""
    n = len(scenes)
    dur_each = audio_duration / n
    segs = []
    for i, sc in enumerate(scenes):
        text = sc.get("text", "") if isinstance(sc, dict) else str(sc)
        start = i * dur_each
        end = (i + 1) * dur_each
        segs.append(transcriber.TranscriptSegment(text, start, end))
    return segs


# ---------------------------------------------------------------------------
# Main job function (called by RQ worker)
# ---------------------------------------------------------------------------

async def run_broll_job(job_id: str, request_data: Dict[str, Any]) -> None:
    """
    Full pipeline for one B-roll job.

    Expects:
        job_id: unique job identifier
        request_data: dict matching BrollJobRequest fields
    """
    set_job_id(job_id)
    t_start = time.perf_counter()

    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    r.hset(f"job:{job_id}", mapping={"status": "running", "progress": 0, "message": "Starting"})

    work_dir = tempfile.mkdtemp(prefix=f"broll_{job_id[:8]}_")
    log.info("Job started", job_id=job_id, work_dir=work_dir)

    try:
        audio_url = request_data.get("audio_url")
        script = request_data.get("script")
        scenes_input = request_data.get("scenes")
        voice = request_data.get("voice", "en+f3")
        aspect_ratios = request_data.get("aspect_ratios", ["16:9"])
        style = request_data.get("style", "bright modern real-world footage")
        avoid = request_data.get("avoid") or []
        captions_cfg = request_data.get("captions", {})
        burn_in = captions_cfg.get("burn_in", False)
        srt_enabled = captions_cfg.get("srt", True)

        # ------------------------------------------------------------------
        # Step 1: Transcription / TTS
        # ------------------------------------------------------------------
        _set_progress(r, job_id, 5, "Transcribing audio / generating TTS")
        audio_path, raw_segments, audio_duration = await transcriber.process_audio_input(
            audio_url=audio_url,
            script=script,
            voice=voice,
            work_dir=work_dir,
        )

        # Override segments if user provided scenes
        if scenes_input:
            segments = _segment_from_scenes(scenes_input, audio_duration)
            log.info("Using user-provided scenes", count=len(segments))
        else:
            segments = raw_segments

        log.info("Segments ready", count=len(segments))

        # ------------------------------------------------------------------
        # Step 2: For each segment, fetch footage
        # ------------------------------------------------------------------
        _set_progress(r, job_id, 15, "Searching for B-roll footage")

        timeline: List[TimelineEntry] = []
        scene_clip_urls: List[str] = []
        scene_durations: List[float] = []
        used_clip_ids: Set[str] = set()
        warnings: List[str] = []

        # Use primary aspect ratio for footage search
        primary_ratio = aspect_ratios[0] if aspect_ratios else "16:9"

        for i, seg in enumerate(segments):
            pct = 15 + int(60 * i / max(len(segments), 1))
            _set_progress(r, job_id, pct, f"Finding clip for scene {i+1}/{len(segments)}")

            # Generate search queries from narration text
            queries = query_generator.make_queries(seg.text, style=style, n_queries=3)
            log.debug("Scene queries", scene=i, queries=queries)

            # Try each query until we find a good non-duplicate clip
            best_clip: Optional[footage_fetcher.RawClip] = None
            best_rating: float = 1.0
            best_reason: str = ""

            for q in queries:
                try:
                    candidates = await footage_fetcher.fetch_candidates(
                        query=q,
                        avoid=avoid,
                        aspect_ratio=primary_ratio,
                        work_dir=work_dir,
                        per_source=8,
                    )
                    selected, rating, reason = clip_selector.select_best_clip(
                        seg.text, candidates, used_clip_ids
                    )
                    if selected and rating > best_rating:
                        best_clip = selected
                        best_rating = rating
                        best_reason = reason
                        if rating >= 3.0:
                            break  # good enough
                except FootageSourceError:
                    warnings.append(f"Scene {i}: footage source failed for query '{q}'")
                    continue

            if best_clip is None:
                warnings.append(f"Scene {i}: no clip found, using black frame fallback")
                scene_clip_urls.append("__BLACK__")
            else:
                used_clip_ids.add(best_clip.clip_id)
                scene_clip_urls.append(best_clip.clip_url)

            scene_dur = seg.end - seg.start
            scene_durations.append(max(0.5, scene_dur))

            # Record in timeline
            clip_record = ClipRecord(
                scene_index=i,
                narration_text=seg.text,
                start_time=seg.start,
                end_time=seg.end,
                clip_source=best_clip.source if best_clip else "black_frame",
                clip_id=best_clip.clip_id if best_clip else "none",
                clip_url=best_clip.clip_url if best_clip else "",
                licence=best_clip.licence if best_clip else "N/A",
                attribution=best_clip.attribution if best_clip else "N/A",
                is_ai_generated=best_clip.is_ai_generated if best_clip else False,
                relevance_score=best_rating,
                selection_reason=best_reason,
            )
            timeline.append(TimelineEntry(
                scene_index=i,
                narration_text=seg.text,
                start_time=seg.start,
                end_time=seg.end,
                clip=clip_record,
            ))

        # ------------------------------------------------------------------
        # Step 3: Download clips
        # ------------------------------------------------------------------
        _set_progress(r, job_id, 75, "Downloading video clips")

        real_urls = [u for u in scene_clip_urls if u != "__BLACK__"]
        local_paths = video_assembler.download_clips_for_scenes(scene_clip_urls, work_dir)
        clip_duration_pairs = list(zip(local_paths, scene_durations))

        # ------------------------------------------------------------------
        # Step 4: Assemble videos for each requested aspect ratio
        # ------------------------------------------------------------------
        _set_progress(r, job_id, 82, "Assembling video")

        output_videos: Dict[str, str] = {}
        for ratio in aspect_ratios:
            vid_path = video_assembler.assemble_video(
                segments=segments,
                clip_downloads=clip_duration_pairs,
                audio_path=audio_path,
                work_dir=work_dir,
                aspect_ratio=ratio,
                burn_captions=burn_in,
            )
            output_videos[ratio] = vid_path

        # ------------------------------------------------------------------
        # Step 5: Build SRT captions
        # ------------------------------------------------------------------
        srt_path: Optional[str] = None
        if srt_enabled or burn_in:
            srt_content = video_assembler.build_srt(segments)
            srt_path = os.path.join(work_dir, "captions.srt")
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(srt_content)

        # ------------------------------------------------------------------
        # Step 6: Build timeline.json and manifest.json
        # ------------------------------------------------------------------
        _set_progress(r, job_id, 90, "Building output metadata")

        timeline_path = os.path.join(work_dir, "timeline.json")
        timeline_data = [te.model_dump() for te in timeline]
        with open(timeline_path, "w") as f:
            json.dump(timeline_data, f, indent=2)

        t_elapsed = time.perf_counter() - t_start
        manifest = ManifestModel(
            job_id=job_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            models={
                "transcription": f"openai/whisper-{settings.WHISPER_MODEL}",
                "tts": settings.TTS_ENGINE,
                "relevance": "openai/clip-vit-base-patch32" if settings.CLIP_ENABLED else "keyword_overlap",
                "query_extraction": "keybert+spacy",
            },
            providers={
                "footage_primary": "pexels.com",
                "footage_secondary": "pixabay.com",
                "footage_fallback": "replicate-svd" if settings.AI_GEN_ENABLED else "black_frame",
                "storage": "minio",
            },
            settings={
                "whisper_model": settings.WHISPER_MODEL,
                "aspect_ratios": aspect_ratios,
                "style": style,
                "avoid": avoid,
                "burn_captions": burn_in,
            },
            timings={"total_seconds": round(t_elapsed, 2)},
            cost_estimate_usd=_estimate_cost(len(segments), audio_duration),
            warnings=warnings,
            licence_summary=(
                "All stock footage from Pexels (Pexels Licence) and Pixabay (Pixabay Licence). "
                "Both licences permit free commercial use. Attribution recorded in timeline.json."
            ),
        )
        manifest_path = os.path.join(work_dir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest.model_dump(), f, indent=2)

        # ------------------------------------------------------------------
        # Step 7: Upload to MinIO
        # ------------------------------------------------------------------
        _set_progress(r, job_id, 95, "Uploading outputs")

        outputs: Dict[str, str] = {}
        for ratio, vid_path in output_videos.items():
            ratio_key = ratio.replace(":", "x")
            obj_name = f"{job_id}/video_{ratio_key}.mp4"
            url = minio_client.upload_file(vid_path, obj_name)
            outputs[f"video_{ratio_key}"] = url

        if srt_path:
            url = minio_client.upload_file(srt_path, f"{job_id}/captions.srt")
            outputs["captions_srt"] = url

        url = minio_client.upload_file(timeline_path, f"{job_id}/timeline.json")
        outputs["timeline_json"] = url

        url = minio_client.upload_file(manifest_path, f"{job_id}/manifest.json")
        outputs["manifest_json"] = url

        # ------------------------------------------------------------------
        # Done
        # ------------------------------------------------------------------
        _set_succeeded(r, job_id, outputs)
        log.info(
            "Job succeeded",
            job_id=job_id,
            elapsed_sec=round(t_elapsed, 2),
            outputs=list(outputs.keys()),
        )

    except BrollEngineError as exc:
        log.error("Job failed (known error)", job_id=job_id, code=exc.code, message=exc.message)
        _set_failed(r, job_id, exc.code, exc.message)

    except Exception as exc:
        log.error("Job failed (unexpected)", job_id=job_id, error=str(exc), tb=traceback.format_exc())
        _set_failed(r, job_id, "UNEXPECTED_ERROR", str(exc))


def _estimate_cost(n_scenes: int, duration_sec: float) -> float:
    """Very rough USD cost estimate (Pexels/Pixabay are free; Whisper runs locally)."""
    # Only costs arise if using Replicate (≈$0.005/sec) or paid TTS
    if settings.REPLICATE_API_TOKEN and settings.AI_GEN_ENABLED:
        return round(n_scenes * 0.005, 4)
    return 0.0
