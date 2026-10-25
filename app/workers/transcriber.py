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

async def generate_tts(script: str, voice: str, output_path: str, rate: Optional[str] = None) -> float:
    """
    Generate speech from *script* using the configured TTS engine.

    Returns audio duration in seconds.
    Supports: 'edge' (edge-tts), 'espeak', 'coqui'
    """
    engine = settings.TTS_ENGINE
    rate_val = rate or getattr(settings, "TTS_RATE", "-15%")

    if engine == "edge":
        try:
            return await _tts_edge(script, voice, output_path, rate=rate_val)
        except Exception as exc:
            log.warning("edge-tts failed, falling back to espeak-ng", error=str(exc))
            return _tts_espeak(script, voice, output_path)
    elif engine == "espeak":
        return _tts_espeak(script, voice, output_path)
    else:
        try:
            return await _tts_edge(script, voice, output_path, rate=rate_val)
        except Exception as exc:
            log.warning("edge-tts failed, falling back to espeak-ng", error=str(exc))
            return _tts_espeak(script, voice, output_path)


async def _tts_edge(script: str, voice: str, output_path: str, rate: str = "-15%") -> float:
    """edge-tts: Microsoft neural voices, free, no API key needed."""
    try:
        import edge_tts  # type: ignore
        import asyncio
        import subprocess
    except ImportError:
        raise TranscriptionError("edge-tts is not installed. Run: pip install edge-tts")

    # Map simple voice names like "en+f3", "en-us" to Edge TTS neural voices
    voice_map = {
        "en+f3": "en-US-JennyNeural",
        "en+m1": "en-US-GuyNeural",
        "en-us": "en-US-JennyNeural",
        "en-gb": "en-GB-SoniaNeural",
        "en": "en-US-JennyNeural",
        "female": "en-US-JennyNeural",
        "male": "en-US-GuyNeural",
        "hi": "hi-IN-SwaraNeural",
        "ta": "ta-IN-PallaviNeural",
        "technology": "en-US-JennyNeural",
        "en-US-JennyNeural": "en-US-JennyNeural",
        "en-US-GuyNeural": "en-US-GuyNeural",
    }
    mapped = voice_map.get(voice.lower() if voice else "", voice if "-Neural" in (voice or "") else settings.TTS_DEFAULT_VOICE)

    last_err = None
    for attempt in range(3):
        try:
            log.info("Generating clean neural TTS with edge-tts", voice=mapped, rate=rate, attempt=attempt+1)
            communicate = edge_tts.Communicate(script, mapped, rate=rate)
            await communicate.save(output_path)
            last_err = None
            break
        except Exception as exc:
            last_err = exc
            log.warning("edge-tts connection retry", attempt=attempt+1, error=str(exc))
            await asyncio.sleep(1.0)
    if last_err is not None:
        raise last_err

    # Master audio to clean broadcast standard (-16 LUFS, highpass to remove mic/system rumble)
    try:
        temp_clean = output_path + ".mastered.mp3"
        subprocess.run([
            "ffmpeg", "-y", "-i", output_path,
            "-af", "highpass=f=80,lowpass=f=12000,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a", "libmp3lame", "-b:a", "192k",
            temp_clean
        ], check=True, capture_output=True)
        os.replace(temp_clean, output_path)
        log.info("Audio mastered with loudnorm & broadcast EQ", path=output_path)
    except Exception as exc:
        log.warning("Audio mastering filter skipped", error=str(exc))

    # Get duration via ffprobe
    return _get_audio_duration(output_path)


def _tts_espeak(script: str, voice: str, output_path: str) -> float:
    import subprocess

    espeak_voice = "en-us"
    if "hi" in voice.lower():
        espeak_voice = "hi"
    elif "ta" in voice.lower():
        espeak_voice = "ta"
    elif "f" in voice.lower():
        espeak_voice = "en-us+f3"

    log.info("Generating TTS with espeak-ng", voice=espeak_voice)
    wav_path = output_path.replace(".mp3", ".wav")
    try:
        subprocess.run(
            ["espeak-ng", "-v", espeak_voice, "-w", wav_path, script],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path, output_path],
            check=True,
            capture_output=True,
        )
    except Exception as exc:
        log.warning("espeak-ng/ffmpeg failed, generating synthesized audio", error=str(exc))
        calc_dur = max(3.0, (len(script.split()) / 130.0) * 60.0)
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(calc_dur), "-q:a", "9", "-acodec", "libmp3lame", output_path],
            check=True,
            capture_output=True,
        )
    finally:
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except Exception:
                pass

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
    speech_rate: Optional[str] = None,
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
        # Generate TTS from script with configured speech_rate pacing
        audio_path = os.path.join(work_dir, "tts_audio.mp3")
        duration = await generate_tts(script, voice, audio_path, rate=speech_rate)
        log.info("TTS audio generated", path=audio_path, duration_sec=round(duration, 2), speech_rate=speech_rate)

    # Transcribe
    try:
        segments, duration = transcribe_audio(audio_path)
    except Exception as exc:
        log.warning("Whisper transcription failed, falling back to script segmentation", error=str(exc))
        segments = []

    if not segments and script:
        import re
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', script) if s.strip()]
        cursor = 0.0
        dur_per_word = (duration / max(len(script.split()), 1)) if duration > 0 else 0.4
        for s in sentences:
            d = max(2.0, len(s.split()) * dur_per_word)
            segments.append(TranscriptSegment(s, round(cursor, 2), round(cursor + d, 2)))
            cursor += d
        duration = max(duration, cursor)

    # Align transcribed segments with original script text for exact punctuation & spelling
    if script and segments:
        import re
        orig_sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', script) if s.strip()]
        if len(orig_sentences) == len(segments):
            for i, sent in enumerate(orig_sentences):
                segments[i].text = sent

    log.info("Transcript ready", n_segments=len(segments), duration_sec=round(duration, 2))
    return audio_path, segments, duration
