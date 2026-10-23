# AI_USE.md — AI Tools and Models Used in This Project

This document discloses every AI coding assistant, AI model, and AI-generated content
used in building this project, as required by IncuBrix Assessment rules.

---

## AI Coding Assistants

| Tool | Role | What it did |
|------|------|-------------|
| Antigravity (Google DeepMind) | Primary coding assistant | Scaffolded full project structure, all Python modules, Docker configuration, documentation |
| Claude Sonnet 4.6 (Anthropic) | Model powering the assistant | Generated all code, tests, and documentation |

---

## AI Models Used at Runtime (in the product)

| Model | Library/API | What it does | Licence |
|-------|-------------|--------------|---------|
| OpenAI Whisper (base) | `openai-whisper` | Transcribes audio to timed word segments | MIT |
| OpenAI CLIP (ViT-B/32) | `openai/CLIP` | Scores visual relevance of footage vs narration | MIT |
| Microsoft Neural TTS | `edge-tts` | Converts plain-text script to audio | Free for use |
| KeyBERT (all-MiniLM-L6-v2) | `keybert` | Extracts semantic keyphrases for footage search | Apache 2.0 |
| spaCy en_core_web_sm | `spacy` | Noun-chunk extraction for query generation | MIT |
| Stability AI SVD | `replicate` | AI-generated fallback video clips (optional) | Stability AI Community Licence |

---

## AI-Generated Content in Default Path

- **No AI-generated video** is used by default. Pexels and Pixabay stock footage are the primary sources.
- AI clip generation (Replicate SVD) is an **optional fallback** only triggered when both stock sources return zero results.
- All AI-generated clips are flagged with `"is_ai_generated": true` in `timeline.json`.
- **No AI-generated faces of real people** are produced. The Replicate SVD model is used only for abstract/object motion clips.

---

## What AI Did NOT Do

- Did not fabricate footage search results or licence information
- Did not use any real person's voice or face without consent
- Did not invent metrics — all numbers in REPORT.md are measured from actual runs
- Did not access or copy anyone else's codebase

---

## Prompting Approach

All code was generated through interactive pair-programming with the AI assistant, with the developer reviewing, testing, and correcting each module. The developer is fully able to explain, modify, and reproduce every part of the codebase.
