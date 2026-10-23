# BrollEngine — Script & Voiceover to B-roll Video Engine

> **IncuBrix Engineering Assessment 3**  
> Converts a voiceover audio file or plain-text script into a finished, licence-clean B-roll video, scene by scene.

---

## Quick Start (One Command)

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/broll-engine.git
cd broll-engine

# 2. Configure environment
cp .env.example .env
# Edit .env — add your PEXELS_API_KEY and PIXABAY_API_KEY

# 3. Start everything
docker compose up
```

The API is now available at **http://localhost:8000**  
- Swagger UI: http://localhost:8000/docs  
- MinIO Console: http://localhost:9001 (login: minioadmin / minioadmin)

---

## API Usage

### Submit a Job

```bash
curl -X POST http://localhost:8000/api/v1/broll/jobs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-unique-key-001" \
  -d '{
    "script": "Saving money does not have to be difficult. Start with small habits that add up over time.",
    "voice": "en-US-JennyNeural",
    "aspect_ratios": ["16:9", "9:16"],
    "style": "bright modern real-world footage",
    "avoid": ["people'\''s faces", "alcohol"],
    "captions": {"burn_in": true, "srt": true}
  }'
```

Response (202):
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "message": "Job accepted",
  "poll_url": "http://localhost:8000/api/v1/broll/jobs/550e8400-..."
}
```

### Poll for Status

```bash
curl http://localhost:8000/api/v1/broll/jobs/550e8400-e29b-41d4-a716-446655440000
```

When succeeded:
```json
{
  "job_id": "...",
  "status": "succeeded",
  "progress": 100,
  "outputs": {
    "video_16x9": "http://localhost:9000/broll-outputs/.../video_16x9.mp4?...",
    "video_9x16": "http://localhost:9000/broll-outputs/.../video_9x16.mp4?...",
    "captions_srt": "http://...",
    "timeline_json": "http://...",
    "manifest_json": "http://..."
  }
}
```

### Use Audio URL instead of script

```bash
curl -X POST http://localhost:8000/api/v1/broll/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "audio_url": "https://example.com/my-voiceover.mp3",
    "aspect_ratios": ["16:9"]
  }'
```

---

## Running the Batch Test Pack

```bash
# From inside the repo:
python batch_runner.py --test-case tests/TEST_CASE --base-url http://localhost:8000

# Results written to ./batch_results/
```

---

## Running the Test Suite

```bash
# Install test dependencies
pip install pytest pytest-asyncio httpx

# Start the API first (docker compose up), then:
pytest tests/test_api.py -v
```

---

## Architecture

```
POST /api/v1/broll/jobs
        │
        ▼
  Job Queue (Redis RQ)
        │
        ▼
  Whisper Transcription  ◄── audio_url downloaded OR TTS generated (edge-tts)
        │
        ▼
  Scene Segmenter  ── sentence-level segments with timestamps
        │
        ▼
  Query Generator  ── KeyBERT + spaCy semantic keywords per scene
        │
   ┌────▼─────────────────┐
   │  Footage Fetcher      │
   │  1. Pexels API        │
   │  2. Pixabay API       │
   │  3. Replicate SVD AI  │  ← fallback
   └────┬──────────────────┘
        │
        ▼
  CLIP Relevance Scorer  ── vision-language similarity, 1-5 rating
        │
        ▼
  Video Assembler (FFmpeg)
  ├── trim + scale + pad per scene
  ├── colour normalisation
  ├── caption burn-in (optional)
  ├── concat all scenes
  └── add narration audio track
        │
        ▼
  MinIO Upload → Presigned URLs
        │
        ▼
  Outputs: video.mp4 + captions.srt + timeline.json + manifest.json
```

---

## Input Spec

`POST /api/v1/broll/jobs`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `audio_url` | string (URL) | One of these | MP3/WAV/M4A voiceover URL (max 10 min) |
| `script` | string | One of these | Plain text; TTS is generated |
| `scenes` | array | optional | IncuBrix scene list `[{"text":"...", "visual_hint":"..."}]` |
| `voice` | string | optional | TTS voice (default: `en-US-JennyNeural`) |
| `aspect_ratios` | array | optional | `["16:9"]`, `["16:9","9:16"]`, etc. |
| `style` | string | optional | Visual style hint for search |
| `avoid` | array | optional | Topics to exclude from footage |
| `captions` | object | optional | `{"burn_in": true, "srt": true}` |

---

## Output Files

| File | Description |
|------|-------------|
| `video_{ratio}.mp4` | H.264/AAC, 30fps, 1080p-class, narration audio |
| `captions.srt` | Timed captions aligned to narration |
| `timeline.json` | Per-scene: text, timestamps, clip source, licence, attribution, relevance score |
| `manifest.json` | Models, providers, settings, timings, cost estimate |

---

## Integration Standard Compliance

| # | Requirement | Status |
|---|-------------|--------|
| 1 | One-command start (`docker compose up`) | ✅ |
| 2 | OpenAPI 3.1 (`/openapi.json`, `/docs`) | ✅ |
| 3 | Background jobs (202 + job_id, GET status) | ✅ |
| 4 | Safe retries (`Idempotency-Key` header) | ✅ |
| 5 | Clear errors (JSON `error.code/message/retryable/details`) | ✅ |
| 6 | Configuration (all from `.env`, no hard-coded secrets) | ✅ |
| 7 | Files in/out (URL or upload → MinIO presigned URLs) | ✅ |
| 8 | Logs & health (`GET /healthz`, `/ready`, structured JSON logs) | ✅ |
| 9 | Output manifest (`manifest.json` every result) | ✅ |
| 10 | Batch runner (`python batch_runner.py --test-case TEST_CASE`) | ✅ |

---

## Environment Variables

See [`.env.example`](.env.example) for all options. Key variables:

| Variable | Description |
|----------|-------------|
| `PEXELS_API_KEY` | Free key from pexels.com/api |
| `PIXABAY_API_KEY` | Free key from pixabay.com/api/docs |
| `WHISPER_MODEL` | `tiny` / `base` / `small` / `medium` / `large` |
| `TTS_ENGINE` | `edge` (default, Microsoft neural) / `espeak` |
| `CLIP_ENABLED` | `true` to use CLIP relevance scoring |
| `REPLICATE_API_TOKEN` | Optional: AI fallback when stock sources fail |

---

## Licence

All code in this repository: MIT Licence.  
All footage: Pexels Licence + Pixabay Licence (both free for commercial use).  
See [SOURCES.md](SOURCES.md) for full disclosure.
