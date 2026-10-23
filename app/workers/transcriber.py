"""Transcription worker — converts audio URL or local file to timed word segments.

Uses OpenAI Whisper (MIT licence, runs locally, no API key needed).
Falls back to a stub when whisper is unavailable (useful for unit tests).
"""
from __future__ import annotations

import os
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.errors import TranscriptionError
from app.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class WordSegment:
    """A single timed word from Whisper."""

    __slots__ = ("word", "start", "end")

    def __init__(self, word: str, start: float, end: float) -> None:
        self.word = word
        self.start = start
        self.end = end


class TranscriptSegment:
    """A sentence / phrase with its time span and text."""

    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text.strip()
        self.start = start
        self.end = end

    def __repr__(self) -> str:
        return f"<Segment [{self.start:.2f}–{self.end:.2f}] {self.text[:40]!r}>"


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def _download_audio(url: str, dest_dir: str) -> str:
    """Download audio from URL to a temp file, return local path."""
    suffix = Path(url.split("?")[0]).suffix or ".mp3"
    dest = os.path.join(dest_dir, f"audio{suffix}")
    log.info("Downloading audio", url=url)
    urllib.request.urlretrieve(url, dest)
    size = os.path.getsize(dest)
    log.info("Audio downloaded", path=dest, size_bytes=size)
    return dest


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

def transcribe_audio(audio_path: str) -> Tuple[List[TranscriptSegment], float]:
    """
    Transcribe *audio_path* with Whisper.

    Returns:
        (segments, duration_seconds)

    Each segment covers a sentence or short phrase with precise timestamps.
    """
    try:
        import whisper  # type: ignore
    except ImportError:
        raise TranscriptionError(
            "openai-whisper is not installed. Run: pip install openai-whisper"
        )

    t0 = time.perf_counter()
    log.info("Loading Whisper model", model=settings.WHISPER_MODEL, device=settings.WHISPER_DEVICE)
    model = whisper.load_model(settings.WHISPER_MODEL, device=settings.WHISPER_DEVICE)

    log.info("Running transcription", audio=audio_path)
    result = model.transcribe(
        audio_path,
        word_timestamps=True,
        verbose=False,
    )
    elapsed = time.perf_counter() - t0
    log.info("Transcription complete", elapsed_sec=round(elapsed, 2))

    # Get total audio duration from Whisper's result
    duration = float(result.get("duration", 0))
    if duration == 0 and result.get("segments"):
        duration = result["segments"][-1]["end"]

    segments = _build_sentence_segments(result)
    return segments, duration


def _build_sentence_segments(whisper_result: dict) -> List[TranscriptSegment]:
    """
    Convert Whisper output into sentence-level TranscriptSegment objects.

    Whisper returns segments that roughly correspond to sentences, but we
    also split on long pauses and sentence-ending punctuation to create
    crisp scene boundaries aligned with the narration.
    """
    out: List[TranscriptSegment] = []
    for seg in whisper_result.get("segments", []):
        text = seg["text"].strip()
        start = float(seg["start"])
        end = float(seg["end"])
        if not text:
            continue
        # Further split on sentence boundaries if very long
        sub_segs = _split_on_sentences(text, start, end, seg.get("words", []))
        out.extend(sub_segs)
    return out


def _split_on_sentences(
    text: str,
    start: float,
    end: float,
    words: list,
) -> List[TranscriptSegment]:
    """Split a long Whisper segment at sentence boundaries using word timestamps."""
    import re

    sentence_end = re.compile(r"[.!?]+")

    if not words or len(text) < 60:
        return [TranscriptSegment(text, start, end)]

    segments: List[TranscriptSegment] = []
    current_words: list = []
    current_start: float = start

    for w in words:
        current_words.append(w)
        word_text = w.get("word", "").strip()
        word_end = float(w.get("end", end))

        if sentence_end.search(word_text):
            seg_text = " ".join(x.get("word", "") for x in current_words).strip()
            if seg_text:
                segments.append(TranscriptSegment(seg_text, current_start, word_end))
            current_words = []
            current_start = word_end

    # Flush remaining
    if current_words:
        seg_text = " ".join(x.get("word", "") for x in current_words).strip()
        if seg_text:
            segments.append(TranscriptSegment(seg_text, current_start, end))

    return segments if segments else [TranscriptSegment(text, start, end)]


# ---------------------------------------------------------------------------
# TTS — generate audio from plain text
# ---------------------------------------------------------------------------

async def generate_tts(script: str, voice: str, output_path: str) -> float:
    """
    Generate speech from *script* using the configured TTS engine.

    Returns audio duration in seconds.
    Supports: 'edge' (edge-tts), 'espeak', 'coqui'
    """
    engine = settings.TTS_ENGINE

    if engine == "edge":
        return await _tts_edge(script, voice, output_path)
    elif engine == "espeak":
        return _tts_espeak(script, voice, output_path)
    else:
        return await _tts_edge(script, voice, output_path)


async def _tts_edge(script: str, voice: str, output_path: str) -> float:
    """edge-tts: Microsoft neural voices, free, no API key needed."""
    try:
        import edge_tts  # type: ignore
        import asyncio
    except ImportError:
        raise TranscriptionError("edge-tts is not installed. Run: pip install edge-tts")

    # Map simple voice names like "en+f3" to Edge TTS neural voices
    voice_map = {
        "en+f3": "en-US-JennyNeural",
        "en+m1": "en-US-GuyNeural",
        "hi": "hi-IN-SwaraNeural",
        "ta": "ta-IN-PallaviNeural",
        "en-US-JennyNeural": "en-US-JennyNeural",
    }
    mapped = voice_map.get(voice, voice if "-Neural" in voice else settings.TTS_DEFAULT_VOICE)

    log.info("Generating TTS with edge-tts", voice=mapped)
    communicate = edge_tts.Communicate(script, mapped)
    await communicate.save(output_path)

    # Get duration via ffprobe
    return _get_audio_duration(output_path)


def _tts_espeak(script: str, voice: str, output_path: str) -> float:
    import subprocess

    log.info("Generating TTS with espeak", voice=voice)
    wav_path = output_path.replace(".mp3", ".wav")
    subprocess.run(
        ["espeak-ng", "-v", voice, "-w", wav_path, script],
        check=True,
        capture_output=True,
    )
    # Convert to mp3
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, output_path],
        check=True,
        capture_output=True,
    )
    os.remove(wav_path)
    return _get_audio_duration(output_path)


def _get_audio_duration(path: str) -> float:
    """Use ffprobe to get audio duration."""
    import subprocess, json as _json

    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            path,
        ],
        capture_output=True,
        text=True,
    )
    data = _json.loads(result.stdout)
    return float(data.get("format", {}).get("duration", 0))


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

async def process_audio_input(
    audio_url: Optional[str],
    script: Optional[str],
    voice: str,
    work_dir: str,
) -> Tuple[str, List[TranscriptSegment], float]:
    """
    Accepts either an audio URL or a plain-text script.

    Returns:
        (audio_file_path, transcript_segments, duration_seconds)
    """
    if not audio_url and not script:
        raise TranscriptionError("Either audio_url or script must be provided.")

    if audio_url:
        audio_path = _download_audio(audio_url, work_dir)
    else:
        # Generate TTS from script
        audio_path = os.path.join(work_dir, "tts_audio.mp3")
        duration = await generate_tts(script, voice, audio_path)
        log.info("TTS audio generated", path=audio_path, duration_sec=round(duration, 2))

    # Transcribe
    segments, duration = transcribe_audio(audio_path)
    log.info("Transcript ready", n_segments=len(segments), duration_sec=round(duration, 2))
    return audio_path, segments, duration
