"""Query generator — extracts meaningful search queries from narration text.

Uses KeyBERT + spaCy for semantic keyword extraction.
Falls back to simple noun-phrase extraction if models are unavailable.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Stopwords to avoid useless queries
# ---------------------------------------------------------------------------
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "this", "that", "these", "those", "is",
    "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "should", "could", "may", "might",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "our", "their", "its", "just", "very",
    "really", "also", "so", "then", "than", "when", "where", "who", "how",
    "what", "why", "which", "now", "up", "down", "out", "about", "like",
    "trust", "slowly", "earned",  # abstract words that don't yield good footage
}

# ---------------------------------------------------------------------------
# Hard-safety: map named people / brands to generic alternatives
# ---------------------------------------------------------------------------
_SAFE_QUERY_MAP = {
    # If the narration contains these proper nouns, replace with safe alternatives
    "google": "technology office",
    "apple": "technology product",
    "microsoft": "software development",
    "amazon": "online shopping",
    "facebook": "social media",
    "meta": "virtual reality technology",
    "twitter": "social media platform",
    "elon musk": "entrepreneur",
    "steve jobs": "technology innovation",
}


def make_queries(
    text: str,
    style: Optional[str] = None,
    n_queries: int = 3,
) -> List[str]:
    """
    Extract the top *n_queries* search queries from *text*.

    Strategy:
      1. Try KeyBERT for semantic multi-word keyphrases
      2. Fall back to spaCy noun-chunk extraction
      3. Ultimate fallback: simple regex word extraction
    """
    text_clean = _clean_text(text)
    style_suffix = f" {style}" if style else ""

    queries: List[str] = []

    # Try KeyBERT
    try:
        queries = _keybert_queries(text_clean, n_queries)
    except Exception as exc:
        log.debug("KeyBERT unavailable", error=str(exc))

    # Fall back to spaCy
    if not queries:
        try:
            queries = _spacy_queries(text_clean, n_queries)
        except Exception as exc:
            log.debug("spaCy unavailable", error=str(exc))

    # Ultimate fallback
    if not queries:
        queries = _simple_queries(text_clean, n_queries)

    # Apply safety map
    safe_queries = [_apply_safe_map(q) for q in queries]

    # Append style suffix to first query
    if safe_queries:
        safe_queries[0] = safe_queries[0] + style_suffix

    log.debug("Generated queries", text_snippet=text[:50], queries=safe_queries)
    return safe_queries


def _clean_text(text: str) -> str:
    # Remove filler words, percentages, statistics that make bad footage queries
    text = re.sub(r"\d+%|\$[\d,]+|[\d,]+ (million|billion|percent)", "", text)
    text = re.sub(r"[\"'()[\]{}]", "", text)
    return text.strip()


def _apply_safe_map(query: str) -> str:
    q_lower = query.lower()
    for dangerous, safe in _SAFE_QUERY_MAP.items():
        if dangerous in q_lower:
            query = re.sub(re.escape(dangerous), safe, query, flags=re.IGNORECASE)
    return query


def _keybert_queries(text: str, n: int) -> List[str]:
    from keybert import KeyBERT  # type: ignore

    kw_model = KeyBERT()
    keywords = kw_model.extract_keywords(
        text,
        keyphrase_ngram_range=(1, 3),
        stop_words="english",
        top_n=n,
        diversity=0.7,
    )
    return [kw for kw, score in keywords if kw.lower() not in _STOPWORDS]


def _spacy_queries(text: str, n: int) -> List[str]:
    import spacy  # type: ignore

    try:
        nlp = spacy.load("en_core_web_sm")
    except OSError:
        import subprocess
        subprocess.run(
            ["python", "-m", "spacy", "download", "en_core_web_sm"],
            check=True,
            capture_output=True,
        )
        nlp = spacy.load("en_core_web_sm")

    doc = nlp(text)
    phrases = []
    for chunk in doc.noun_chunks:
        chunk_text = chunk.text.lower().strip()
        words = chunk_text.split()
        filtered = [w for w in words if w not in _STOPWORDS]
        if filtered:
            phrases.append(" ".join(filtered))

    # Also add named entities (excluding person names)
    for ent in doc.ents:
        if ent.label_ not in ("PERSON", "GPE") and len(ent.text) > 3:
            phrases.append(ent.text.lower())

    # Deduplicate while preserving order
    seen: set = set()
    unique = []
    for p in phrases:
        if p not in seen and p not in _STOPWORDS:
            seen.add(p)
            unique.append(p)

    return unique[:n]


def _simple_queries(text: str, n: int) -> List[str]:
    """Fallback: pick the longest non-stop words."""
    words = re.findall(r"[a-zA-Z]{4,}", text)
    words = [w.lower() for w in words if w.lower() not in _STOPWORDS]
    # Remove duplicates
    seen: set = set()
    unique = [w for w in words if not (w in seen or seen.add(w))]
    # Return as bigrams where possible
    queries = []
    i = 0
    while i < len(unique) and len(queries) < n:
        if i + 1 < len(unique):
            queries.append(f"{unique[i]} {unique[i+1]}")
            i += 2
        else:
            queries.append(unique[i])
            i += 1
    return queries or ["abstract background"]
