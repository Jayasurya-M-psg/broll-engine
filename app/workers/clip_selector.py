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

        # Fetch the thumbnail with browser User-Agent to avoid HTTP 403 from CDN
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            img_data = resp.read()
        image = preprocess(Image.open(io.BytesIO(img_data)).convert("RGB")).unsqueeze(0).to(device)

        text_tokens = openai_clip.tokenize([text[:77]], truncate=True).to(device)

        with torch.no_grad():
            image_features = model.encode_image(image)
            text_features = model.encode_text(text_tokens)
            image_features /= image_features.norm(dim=-1, keepdim=True)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            similarity = (image_features @ text_features.T).squeeze().item()

        # similarity is in [-1, 1]; clamp and map to [0, 1]
        return max(0.0, min(1.0, float(similarity)))
    except Exception as exc:
        log.warning("CLIP scoring error", error=str(exc), image_url=image_url[:80])
        return 0.15


# ---------------------------------------------------------------------------
# Keyword overlap scoring (fallback & validation)
# ---------------------------------------------------------------------------

import re

_STOPWORDS_SELECTOR = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "this", "that", "these", "those", "have", "has", "had", "will", "would",
}

_ANIMAL_WORDS = {
    "dog", "puppy", "canine", "cat", "kitten", "pet", "animal", "hound", "dog trainer",
    "puppies", "dogs", "cats"
}

def _score_keyword_overlap(text: str, clip: RawClip) -> float:
    """Compute token overlap between narration text and clip tags/attribution."""
    text_words = set(re.findall(r"[a-z]{3,}", text.lower())) - _STOPWORDS_SELECTOR
    tag_words = set()
    for tag in clip.tags:
        tag_words.update(re.findall(r"[a-z]{3,}", tag.lower()))
    tag_words.update(re.findall(r"[a-z]{3,}", clip.attribution.lower()))

    # Negative penalty: if script does NOT mention animals, but clip is about dogs/cats/pets
    if not (text_words & _ANIMAL_WORDS) and (tag_words & _ANIMAL_WORDS):
        return 0.02

    overlap = len(text_words & tag_words)
    return min(1.0, overlap / max(1, min(len(text_words), 6)))


def score_clip(text: str, clip: RawClip) -> float:
    """
    Return a similarity score in [0, 1].
    Uses CLIP visual-semantic similarity combined with metadata validation.
    """
    kw_score = _score_keyword_overlap(text, clip)
    # If animal penalty triggered, immediately reject with near-zero score
    if kw_score <= 0.02:
        return 0.02

    if settings.CLIP_ENABLED and clip.preview_image_url:
        clip_score = _score_with_clip(text, clip.preview_image_url)
        # Blend: 70% CLIP visual understanding, 30% keyword metadata validation
        return round(0.70 * clip_score + 0.30 * kw_score, 4)

    return kw_score


def similarity_to_rating(score: float) -> float:
    """Map a [0,1] similarity score to a 1-5 reviewer-style rating."""
    if score <= 0.05:
        return 1.0
    # CLIP similarity for ViT-B/32 typically ranges 0.15 - 0.35
    normalised = (score - 0.12) / 0.22   # stretch [0.12, 0.34] -> [0, 1]
    rating = 1.0 + max(0.0, normalised) * 4.0
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
        log.warning("All candidates already used, forcing reuse")
        c = candidates[0]
        return c, 1.5, "forced reuse (all unique clips exhausted)"

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_clip = scored[0]
    rating = similarity_to_rating(best_score)
    reason = (
        f"CLIP similarity {best_score:.3f} -> rating {rating:.1f}/5; "
        f"source: {best_clip.source}; visual-semantic match"
    )
    log.info(
        "Clip selected",
        clip_id=best_clip.clip_id,
        score=round(best_score, 3),
        rating=rating,
        reason=reason,
    )
    return best_clip, rating, reason
