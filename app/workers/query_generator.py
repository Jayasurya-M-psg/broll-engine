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


# Domain-specific phrase maps for accurate B-roll footage retrieval
_PHRASE_NORMALIZATION = {
    "self-driving cars": "self driving car",
    "self-driving car": "self driving car",
    "autonomous cars": "autonomous car",
    "smart assistants": "smart assistant",
    "smart assistant": "smart assistant",
    "artificial intelligence": "artificial intelligence",
    "complex problems": "technology problem solving",
    "people and machines": "robot human collaboration",
    "work together": "teamwork office",
    "new technology": "future technology",
    "keep learning": "technology learning",
    "saving money": "saving money",
    "tracking every expense": "budget expense tracking",
    "unnecessary subscription": "mobile subscription digital",
    "small steps": "stepping forward journey",
    "big savings": "piggy bank savings coins",
    "digital transformation": "business team collaboration technology",
    "digital products": "business team collaboration technology",
    "digital solutions": "business team collaboration technology",
    "business solutions": "business team collaboration technology",
    "online presence": "digital technology connection network",
    "software development": "software developer coding laptop",
    "web development": "web developer coding website",
    "web design": "web design responsive laptop",
    "hosting solutions": "data center server racks",
    "server solutions": "data center server racks",
    "hosting services": "data center server racks",
    "cloud-based": "cloud productivity business team",
    "cloud computing": "cloud computing server network",
    "user experience": "ui ux interface computer",
    "custom software": "software developer coding laptop",
    "inventory management": "dashboard analytics interface screen",
    "customer management": "dashboard analytics interface screen",
    "data analytics": "data analytics dashboard computer",
    "cyber security": "cyber security technology server",
    "smart city": "modern digital smart city technology",
}

def make_queries(
    text: str,
    style: Optional[str] = None,
    n_queries: int = 4,
) -> List[str]:
    """
    Extract the top *n_queries* search queries from *text*.
    Returns high-intent, stock-video-optimized search terms.
    """
    text_clean = _clean_text(text)
    text_lower = text_clean.lower()
    queries: List[str] = []

    # 1. Match known high-intent compound phrases
    for phrase, target in _PHRASE_NORMALIZATION.items():
        if phrase in text_lower and target not in queries:
            queries.append(target)

    # 2. Extract using KeyBERT
    try:
        kb = _keybert_queries(text_clean, n_queries)
        for q in kb:
            if q not in queries:
                queries.append(q)
    except Exception as exc:
        log.debug("KeyBERT unavailable", error=str(exc))

    # 3. Extract using spaCy
    try:
        sp = _spacy_queries(text_clean, n_queries)
        for q in sp:
            if q not in queries:
                queries.append(q)
    except Exception as exc:
        log.debug("spaCy unavailable", error=str(exc))

    # 4. Fallback if needed
    if not queries:
        queries = _simple_queries(text_clean, n_queries)

    # 5. Add style-enhanced query if style provided (e.g. "technology", "finance")
    if style and queries:
        primary = queries[0]
        if style.lower() not in primary.lower():
            queries.append(f"{primary} {style.strip()}")

    # Apply safety map
    safe_queries = [_apply_safe_map(q) for q in queries]

    # Deduplicate while preserving order
    seen: set = set()
    result = []
    for q in safe_queries:
        if q not in seen:
            seen.add(q)
            result.append(q)

    log.debug("Generated queries", text_snippet=text[:50], queries=result[:n_queries])
    return result[:n_queries]


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


_KEYBERT_MODEL = None
_KEYBERT_FAILED = False

def _keybert_queries(text: str, n: int) -> List[str]:
    global _KEYBERT_MODEL, _KEYBERT_FAILED
    if _KEYBERT_FAILED:
        return []
    try:
        if _KEYBERT_MODEL is None:
            from keybert import KeyBERT  # type: ignore
            _KEYBERT_MODEL = KeyBERT()
        keywords = _KEYBERT_MODEL.extract_keywords(
            text,
            keyphrase_ngram_range=(1, 3),
            stop_words="english",
            top_n=n,
            diversity=0.7,
        )
        return [kw for kw, score in keywords if kw.lower() not in _STOPWORDS]
    except Exception as e:
        log.debug("KeyBERT unavailable, falling back to spaCy", error=str(e))
        _KEYBERT_FAILED = True
        return []


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
