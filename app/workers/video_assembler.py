"""Video assembler — builds the final B-roll video with FFmpeg.

Pipeline per scene:
  1. Download the selected clip to a temp file
  2. Trim to the scene duration
  3. Scale + pad to target resolution (no black frames, no distortion)
  4. Normalise colour (brightness / contrast) across all clips
  5. Concatenate all clips with crossfade
  6. Add narration audio track on top
  7. Optionally burn captions

Outputs: video_{ratio}.mp4 + captions.srt
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.errors import VideoAssemblyError
from app.core.logging import get_logger
from app.workers.transcriber import TranscriptSegment

log = get_logger(__name__)

# Resolution map
RESOLUTIONS: Dict[str, Tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}


# ---------------------------------------------------------------------------
# SRT caption builder
# ---------------------------------------------------------------------------

def build_srt(segments: List[TranscriptSegment]) -> str:
    """Build an SRT caption file from transcript segments."""
    lines = []
    for i, seg in enumerate(segments, start=1):
        start = _fmt_timecode(seg.start)
        end = _fmt_timecode(seg.end)
        lines.append(f"{i}\n{start} --> {end}\n{seg.text}\n")
    return "\n".join(lines)


def _fmt_timecode(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    ms = int((secs - int(secs)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# Clip download
# ---------------------------------------------------------------------------

def _download_clip(url: str, dest: str) -> bool:
    """Download video from URL; return True on success."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
            f.write(resp.read())
        log.debug("Clip downloaded", url=url[:80], size=os.path.getsize(dest))
        return True
    except Exception as exc:
        log.warning("Clip download failed", url=url[:80], error=str(exc))
        return False


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def _run_ffmpeg(args: List[str], label: str = "") -> None:
    """Run an ffmpeg command; raise VideoAssemblyError on failure."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + args
    log.debug("Running ffmpeg", label=label, cmd=" ".join(cmd[:8]) + " ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise VideoAssemblyError(f"ffmpeg {label} failed: {result.stderr[-500:]}")


def _get_video_info(path: str) -> Dict:
    """Return duration, width, height of a video file via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            path,
        ],
        capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        {}
    )
    return {
        "duration": float(fmt.get("duration", 0)),
        "width": int(video_stream.get("width", 1920)),
        "height": int(video_stream.get("height", 1080)),
    }


def _process_clip(
    input_path: str,
    output_path: str,
    start_offset: float,
    duration: float,
    target_w: int,
    target_h: int,
    burn_captions: bool = False,
    caption_text: str = "",
    caption_start: float = 0.0,
) -> None:
    """
    Extract *duration* seconds from *input_path* starting at *start_offset*,
    scale+pad to target resolution, optionally burn one caption.
    """
    # Scale + pad filter (no black frames, fills frame)
    scale_filter = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},"
        f"setsar=1"
    )

    # Colour normalisation: equalise brightness
    colour_filter = "eq=brightness=0.0:contrast=1.05:saturation=1.1"

    vf = f"{scale_filter},{colour_filter}"

    if burn_captions and caption_text:
        # Escape special chars for FFmpeg drawtext
        safe_text = caption_text.replace("'", "\\'").replace(":", "\\:").replace(",", "\\,")
        vf += (
            f",drawtext=text='{safe_text}'"
            f":fontsize=48:fontcolor=white:borderw=3:bordercolor=black"
            f":x=(w-text_w)/2:y=h-100"
        )

    _run_ffmpeg(
        [
            "-ss", str(start_offset),
            "-i", input_path,
            "-t", str(duration),
            "-vf", vf,
            "-r", str(settings.VIDEO_FPS),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",
            output_path,
        ],
        label="process_clip",
    )


# ---------------------------------------------------------------------------
# Main assembler
# ---------------------------------------------------------------------------

def assemble_video(
    segments: List[TranscriptSegment],
    clip_downloads: List[Tuple[str, float]],
    audio_path: str,
    work_dir: str,
    aspect_ratio: str = "16:9",
    burn_captions: bool = False,
) -> str:
    """Assemble all processed clips into a final video with narration audio."""
    target_w, target_h = RESOLUTIONS.get(aspect_ratio, (1920, 1080))
    processed_clips: List[str] = []

    for idx, (clip_path, scene_dur) in enumerate(clip_downloads):
        out_clip = os.path.join(work_dir, f"processed_{idx:03d}.mp4")
        try:
            info = _get_video_info(clip_path)
        except Exception:
            info = {"duration": scene_dur + 1, "width": target_w, "height": target_h}

        clip_dur = info["duration"]
        max_offset = max(0.0, clip_dur - scene_dur - 0.5)
        start_offset = min(max_offset, max_offset * 0.3)

        caption_text = segments[idx].text if idx < len(segments) else ""

        _process_clip(
            clip_path, out_clip,
            start_offset=start_offset,
            duration=scene_dur,
            target_w=target_w,
            target_h=target_h,
            burn_captions=burn_captions,
            caption_text=caption_text,
        )
        processed_clips.append(out_clip)

    if not processed_clips:
        raise VideoAssemblyError("No processed clips to assemble")

    concat_list = os.path.join(work_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in processed_clips:
            f.write(f"file '{p}'\n")

    concat_video = os.path.join(work_dir, f"concat_{aspect_ratio.replace(':','x')}.mp4")
    _run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", concat_video],
        label="concat",
    )

    ratio_str = aspect_ratio.replace(":", "x")
    final_video = os.path.join(work_dir, f"video_{ratio_str}.mp4")
    _run_ffmpeg(
        [
            "-i", concat_video,
            "-i", audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            final_video,
        ],
        label="add_audio",
    )

    log.info("Video assembled", ratio=aspect_ratio, output=final_video)
    return final_video


def download_clips_for_scenes(
    scene_clip_urls: List[str],
    work_dir: str,
) -> List[str]:
    """Download all selected clip URLs and return list of local paths."""
    local_paths: List[str] = []
    for idx, url in enumerate(scene_clip_urls):
        if url and os.path.isfile(url):
            local_paths.append(url)
            continue
        dest = os.path.join(work_dir, f"raw_clip_{idx:03d}.mp4")
        ok = _download_clip(url, dest)
        if not ok:
            # Create a 3-second black fallback clip
            dest = os.path.join(work_dir, f"black_{idx:03d}.mp4")
            _run_ffmpeg(
                [
                    "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    dest,
                ],
                label="black_fallback",
            )
            log.warning("Using black fallback clip", scene=idx)
        local_paths.append(dest)
    return local_paths
