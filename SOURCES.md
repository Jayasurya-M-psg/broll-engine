# SOURCES.md — All Data Sources, APIs, Models, and Libraries

## Footage Sources

| Source | Licence | Commercial Use | Attribution Required | URL |
|--------|---------|----------------|----------------------|-----|
| Pexels | Pexels Licence | ✅ Free | Not required (we record it) | https://www.pexels.com/license/ |
| Pixabay | Pixabay Licence | ✅ Free | Not required (we record it) | https://pixabay.com/service/license-summary/ |
| Replicate SVD | Stability AI Community Licence | ✅ (commercial use permitted per terms) | Required | https://stability.ai/community-license-agreement |

## AI Models

| Model | Licence | Use in this project |
|-------|---------|---------------------|
| OpenAI Whisper | MIT | Local audio transcription |
| OpenAI CLIP (ViT-B/32) | MIT | Vision-language relevance scoring |
| Microsoft edge-tts (neural voices) | Free for use, no commercial restriction for synthesis | TTS from plain-text script |
| Stability AI SVD (via Replicate) | Stability AI Community Licence | AI clip fallback (optional) |
| KeyBERT | MIT | Semantic keyword extraction |
| spaCy en_core_web_sm | MIT | NLP noun-chunk extraction |
| sentence-transformers | Apache 2.0 | Embeddings for KeyBERT |

## Python Libraries

| Library | Licence | Version |
|---------|---------|---------|
| FastAPI | MIT | 0.115.0 |
| Uvicorn | BSD | 0.30.6 |
| Pydantic | MIT | 2.9.2 |
| pydantic-settings | MIT | 2.5.2 |
| redis-py | MIT | 5.1.1 |
| rq | BSD | 1.16.2 |
| minio | Apache 2.0 | 7.2.9 |
| httpx | BSD | 0.27.2 |
| Pillow | HPND | 10.4.0 |
| torch | BSD | 2.4.1 |
| replicate | Apache 2.0 | 0.34.4 |

## System Tools

| Tool | Licence | Use |
|------|---------|-----|
| FFmpeg | LGPL 2.1+ | Video trimming, scaling, concatenation, audio mixing |
| espeak-ng | GPL 3 | Fallback TTS (not in default path; edge-tts is default) |
| Docker / Docker Compose | Apache 2.0 | Containerisation |
| Redis 7 | BSD | Job queue and idempotency store |
| MinIO | AGPL 3.0 (server) / Apache 2.0 (client SDK) | S3-compatible output storage |

> **Note on MinIO AGPL**: The MinIO server is used as an infrastructure service, not shipped as part of the application code. The MinIO Python client SDK (Apache 2.0) is the only MinIO code in this repository. This is consistent with IncuBrix's requirement to avoid AGPL in the shipped service.

## Data

No training data, datasets, or personal data used. All media retrieved at runtime from Pexels and Pixabay under their respective commercial licences.
