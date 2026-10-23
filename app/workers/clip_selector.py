"""Clip selector — scores candidates with CLIP and picks the best non-duplicate clip.

CLIP (Contrastive Language–Image Pretraining, MIT licence) computes a
vision-language similarity score between the narration text and each candidate
clip's thumbnail image. This gives a meaningful relevance score on a 1-5 scale.

Falls back to keyword overlap scoring when CLIP is not available.
"""
from __future__ import annotations

import hashlib
import io
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.workers.footage_fetcher import RawClip

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level CLIP cache so the model loads only once
# ---------------------------------------------------------------------------
_clip_model = None
_clip_preprocess = None
_clip_device = None


def _load_clip():
    global _clip_model, _clip_preprocess, _clip_device
    if _clip_model is not None:
        return _clip_model, _clip_preprocess, _clip_device

    try:
        import torch
        import clip as openai_clip  # type: ignore  # pip install git+https://github.com/openai/CLIP

        device = "cuda" if torch.cuda.is_available() else "cpu"
        _clip_device = device
        _clip_model, _clip_preprocess = openai_clip.load("ViT-B/32", device=device)
        log.info("CLIP model loaded", device=device)
        return _clip_model, _clip_preprocess, _clip_device
    except Exception as exc:
        log.warning("CLIP unavailable, using keyword scoring fallback", error=str(exc))
        return None, None, None


# ---------------------------------------------------------------------------
# CLIP scoring
# ---------------------------------------------------------------------------

def _score_with_clip(text: str, image_url: str) -> float:
    """
    Compute CLIP cosine similarity between *text* and the image at *image_url*.
    Returns a float in [0, 1].
    """
    model, preprocess, device = _load_clip()
    if model is None:
        return 0.5  # neutral if CLIP unavailable

    try:
        import torch
        import clip as openai_clip
        from PIL import Image

        # Fetch the thumbnail
        with urllib.request.urlopen(image_url, timeout=8) as resp:
            img_data = resp.read()
        image = preprocess(Image.open(io.BytesIO(img_data)).convert("RGB")).unsqueeze(0).to(device)

        text_tokens = openai_clip.tokenize([text[:77]]).to(device)

        with torch.no_grad():
            image_features = model.encode_image(image)
            text_features = model.encode_text(text_tokens)
            image_features /= image_features.norm(dim=-1, keepdim=True)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            similarity = (image_features @ text_features.T).squeeze().item()

        # similarity is in [-1, 1]; clamp and map to [0, 1]
        return max(0.0, min(1.0, float(similarity)))
    except Exception as exc:
        log.debug("CLIP scoring error", error=str(exc), image_url=image_url[:80])
        return 0.3


# ---------------------------------------------------------------------------
# Keyword overlap scoring (fallback)
# ---------------------------------------------------------------------------

def _score_keyword_overlap(text: str, clip: RawClip) -> float:
    """Simple token overlap between narration text and clip tags/attribution."""
    text_words = set(re.findall(r"[a-z]+", text.lower()))
    tag_words = set()
    for tag in clip.tags:
        tag_words.update(re.findall(r"[a-z]+", tag.lower()))
    tag_words.update(re.findall(r"[a-z]+", clip.attribution.lower()))

    overlap = len(text_words & tag_words)
    # Map overlap count to [0, 1]
    return min(1.0, overlap / max(1, min(len(text_words), 10)))


import re


def score_clip(text: str, clip: RawClip) -> float:
    """
    Return a similarity score in [0, 1].
    Uses CLIP if available, keyword overlap otherwise.
    """
    if settings.CLIP_ENABLED and clip.preview_image_url:
        return _score_with_clip(text, clip.preview_image_url)
    return _score_keyword_overlap(text, clip)


def similarity_to_rating(score: float) -> float:
    """Map a [0,1] CLIP similarity to a 1-5 reviewer-style rating."""
    # Typical CLIP text-image scores range 0.15–0.40 for good matches
    # We normalise: 0.15 → 2.0, 0.30 → 4.0
    normalised = (score - 0.10) / 0.25   # stretch [0.10, 0.35] → [0, 1]
    rating = 1.0 + normalised * 4.0
    return max(1.0, min(5.0, round(rating, 2)))


# ---------------------------------------------------------------------------
# Best-clip selector with deduplication
# ---------------------------------------------------------------------------

def select_best_clip(
    text: str,
    candidates: List[RawClip],
    used_ids: Set[str],
) -> Tuple[Optional[RawClip], float, str]:
    """
    Pick the highest-scoring unused candidate clip.

    Returns:
        (clip, relevance_rating_1_to_5, selection_reason)
    """
    if not candidates:
        return None, 1.0, "no candidates available"

    scored: List[Tuple[float, RawClip]] = []
    for c in candidates:
        if c.clip_id in used_ids:
            continue
        raw_score = score_clip(text, c)
        scored.append((raw_score, c))

    if not scored:
        # All candidates already used — allow reuse of lowest-scored (emergency)
        log.warning("All candidates already used, forcing reuse")
        c = candidates[0]
        return c, 1.5, "forced reuse (all unique clips exhausted)"

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_clip = scored[0]
    rating = similarity_to_rating(best_score)

    reason = (
        f"CLIP similarity {best_score:.3f} → rating {rating:.1f}/5; "
        f"source: {best_clip.source}; query-matched"
    )
    log.debug(
        "Clip selected",
        clip_id=best_clip.clip_id,
        score=round(best_score, 3),
        rating=rating,
        reason=reason,
    )
    return best_clip, rating, reason
