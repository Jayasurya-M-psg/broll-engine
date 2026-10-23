"""
BrollEngine — Standalone Demo (no Docker needed)
================================================
Runs the core pipeline end-to-end:
  1. TTS  : synthesise narration with edge-tts
  2. Fetch : search Pixabay for B-roll clips matching each scene
  3. Score : CLIP-free keyword relevance scoring (fallback when torch unavailable)
  4. Manifest : emit JSON timeline + job manifest

Usage:
    python demo_run.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT = "Saving money does not have to be difficult. Start by tracking every expense. Then cut one unnecessary subscription. Small steps lead to big savings over time."
PIXABAY_KEY = "58172223-9b5765ffbe8c6e92205607035"
OUTPUT_DIR  = Path("demo_output")
JOB_ID      = hashlib.md5(SCRIPT.encode()).hexdigest()[:12]

OUTPUT_DIR.mkdir(exist_ok=True)

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def hdr(msg: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {msg}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*60}{RESET}")

def ok(msg: str) -> None:  print(f"  {GREEN}✓{RESET}  {msg}")
def info(msg: str) -> None: print(f"  {YELLOW}→{RESET}  {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Scene segmentation (sentence splitting)
# ══════════════════════════════════════════════════════════════════════════════

def segment_script(script: str) -> List[Dict]:
    """Split script into scenes by sentence, estimate duration at ~130 wpm."""
    import re
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', script) if s.strip()]
    scenes = []
    cursor = 0.0
    for i, sent in enumerate(sentences):
        words  = len(sent.split())
        dur    = max(2.0, words / 130 * 60)        # 130 wpm
        scenes.append({"id": i, "text": sent, "start": round(cursor, 2),
                        "end": round(cursor + dur, 2), "duration": round(dur, 2)})
        cursor += dur
    return scenes


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Keyword / query extraction (KeyBERT optional, fallback to nltk-free)
# ══════════════════════════════════════════════════════════════════════════════

def extract_keywords(text: str, top_k: int = 3) -> List[str]:
    """Extract keywords. Uses KeyBERT if available, else frequency heuristic."""
    try:
        from keybert import KeyBERT  # type: ignore
        kw_model = KeyBERT()
        pairs = kw_model.extract_keywords(text, keyphrase_ngram_range=(1, 2),
                                          stop_words="english", top_n=top_k)
        return [kw for kw, _ in pairs] if pairs else _fallback_kw(text, top_k)
    except Exception:
        return _fallback_kw(text, top_k)


STOPWORDS = {
    "a","an","the","is","are","was","were","be","been","being","have","has","had",
    "do","does","did","will","would","could","should","may","might","shall",
    "to","of","in","on","at","by","for","with","from","into","over","then",
    "and","or","but","not","it","its","this","that","you","we","they","i","he",
    "she","they","them","their","our","your","my","his","her","every","one",
    "time","lead","big","small","start","does","not","have",
}

def _fallback_kw(text: str, top_k: int) -> List[str]:
    words = [w.lower().strip(".,!?") for w in text.split()]
    freq: Dict[str, int] = {}
    for w in words:
        if w not in STOPWORDS and len(w) > 3:
            freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq, key=lambda w: -freq[w])
    return ranked[:top_k] if ranked else [words[0]] if words else ["video"]


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Pixabay footage search
# ══════════════════════════════════════════════════════════════════════════════

def search_pixabay(query: str, per_page: int = 5) -> List[Dict]:
    """Search Pixabay for video clips matching query."""
    url = (
        f"https://pixabay.com/api/videos/"
        f"?key={PIXABAY_KEY}"
        f"&q={urllib.parse.quote(query)}"
        f"&per_page={per_page}"
        f"&safesearch=true"
        f"&video_type=film"
    )
    try:
        req  = urllib.request.Request(url, headers={"User-Agent": "BrollEngine/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("hits", [])
    except Exception as exc:
        info(f"Pixabay search failed for '{query}': {exc}")
        return []

import urllib.parse  # noqa: E402  (needed above)


def pick_best_clip(hits: List[Dict]) -> Dict | None:
    """Pick the clip with highest views as a proxy relevance score."""
    if not hits:
        return None
    return max(hits, key=lambda h: h.get("views", 0))


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — TTS narration synthesis
# ══════════════════════════════════════════════════════════════════════════════

async def synthesise_tts(text: str, out_path: Path) -> bool:
    """Generate speech with edge-tts (Microsoft neural voices, free)."""
    try:
        import edge_tts  # type: ignore
        communicate = edge_tts.Communicate(text, voice="en-US-JennyNeural")
        await communicate.save(str(out_path))
        return True
    except Exception as exc:
        info(f"TTS failed: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Build JSON manifest / timeline
# ══════════════════════════════════════════════════════════════════════════════

def build_manifest(job_id: str, scenes: List[Dict], clip_records: List[Dict],
                   audio_path: str, total_dur: float) -> Dict:
    timeline = []
    for scene, clip in zip(scenes, clip_records):
        timeline.append({
            "scene_id":    scene["id"],
            "text":        scene["text"],
            "start_sec":   scene["start"],
            "end_sec":     scene["end"],
            "duration_sec":scene["duration"],
            "keywords":    clip.get("keywords", []),
            "clip": {
                "source":      clip.get("source", "none"),
                "pixabay_id":  clip.get("pixabay_id"),
                "url":         clip.get("url", ""),
                "thumbnail":   clip.get("thumbnail", ""),
                "views":       clip.get("views", 0),
                "tags":        clip.get("tags", ""),
            }
        })

    return {
        "job_id":        job_id,
        "status":        "completed",
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "script":        SCRIPT,
        "total_duration": round(total_dur, 2),
        "audio_file":    audio_path,
        "aspect_ratios": ["16:9"],
        "scene_count":   len(scenes),
        "clips_found":   sum(1 for c in clip_records if c.get("url")),
        "timeline":      timeline,
        "engine": {
            "tts":         "edge-tts / en-US-JennyNeural",
            "footage":     "Pixabay API (safesearch=true)",
            "scoring":     "keyword-frequency heuristic (CPU fallback)",
            "assembly":    "FFmpeg concat demuxer (requires Docker for full run)",
        }
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  BrollEngine — Live Demo Run{RESET}")
    print(f"{BOLD}  Job ID : {JOB_ID}{RESET}")
    print(f"{BOLD}  Time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}")

    t0 = time.time()

    # ── Step 1: Segment ────────────────────────────────────────────────────────
    hdr("STEP 1 / 4 — Scene Segmentation")
    scenes = segment_script(SCRIPT)
    for s in scenes:
        ok(f"Scene {s['id']}: [{s['start']}s–{s['end']}s]  \"{s['text'][:60]}...\"" if len(s['text'])>60 else f"Scene {s['id']}: [{s['start']}s–{s['end']}s]  \"{s['text']}\"")
    info(f"Total estimated duration: {scenes[-1]['end']:.1f}s")

    # ── Step 2: TTS ────────────────────────────────────────────────────────────
    hdr("STEP 2 / 4 — TTS Narration Synthesis")
    audio_path = OUTPUT_DIR / f"narration_{JOB_ID}.mp3"
    info("Synthesising with edge-tts (en-US-JennyNeural)…")
    tts_ok = await synthesise_tts(SCRIPT, audio_path)
    if tts_ok:
        size_kb = audio_path.stat().st_size // 1024
        ok(f"Audio saved → {audio_path}  ({size_kb} KB)")
    else:
        info("TTS unavailable — audio_path recorded as placeholder")

    # ── Step 3: Footage search ─────────────────────────────────────────────────
    hdr("STEP 3 / 4 — B-roll Footage Search (Pixabay)")
    clip_records: List[Dict] = []

    for scene in scenes:
        kws = extract_keywords(scene["text"])
        query = " ".join(kws[:2])
        info(f"Scene {scene['id']} keywords: {kws}  →  query: \"{query}\"")

        hits  = search_pixabay(query)
        best  = pick_best_clip(hits)

        if best:
            videos = best.get("videos", {})
            # prefer medium quality, fallback to small/tiny
            vid_url = (
                videos.get("medium", {}).get("url") or
                videos.get("small",  {}).get("url") or
                videos.get("tiny",   {}).get("url") or ""
            )
            record = {
                "source":     "pixabay",
                "pixabay_id": best.get("id"),
                "url":        vid_url,
                "thumbnail":  best.get("videos", {}).get("tiny", {}).get("thumbnail", ""),
                "views":      best.get("views", 0),
                "tags":       best.get("tags", ""),
                "keywords":   kws,
            }
            ok(f"Scene {scene['id']} → Pixabay #{best['id']}  views={best.get('views',0):,}  tags: {best.get('tags','')[:50]}")
        else:
            record = {"source": "none", "url": "", "keywords": kws}
            info(f"Scene {scene['id']} → No clip found for \"{query}\"")

        clip_records.append(record)

    # ── Step 4: Manifest ───────────────────────────────────────────────────────
    hdr("STEP 4 / 4 — Building Job Manifest")
    total_dur = scenes[-1]["end"] if scenes else 0.0
    manifest  = build_manifest(JOB_ID, scenes, clip_records, str(audio_path), total_dur)

    manifest_path = OUTPUT_DIR / f"manifest_{JOB_ID}.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    ok(f"Manifest saved → {manifest_path}")

    # ── Timeline summary ───────────────────────────────────────────────────────
    hdr("TIMELINE SUMMARY")
    print(f"  {'Scene':<6} {'Start':>6} {'End':>6} {'Dur':>5}  {'Keywords':<30}  {'Clip Source'}")
    print(f"  {'─'*5:<6} {'─'*5:>6} {'─'*4:>6} {'─'*4:>5}  {'─'*30:<30}  {'─'*20}")
    for entry in manifest["timeline"]:
        kw_str  = ", ".join(entry["keywords"])[:28]
        src     = entry["clip"]["source"]
        pid     = entry["clip"].get("pixabay_id") or "–"
        print(f"  {entry['scene_id']:<6} {entry['start_sec']:>6.1f} {entry['end_sec']:>6.1f} {entry['duration_sec']:>5.1f}  {kw_str:<30}  {src} #{pid}")

    elapsed = time.time() - t0
    print(f"\n{BOLD}{GREEN}{'═'*60}{RESET}")
    print(f"{BOLD}{GREEN}  ✓ Pipeline complete in {elapsed:.1f}s{RESET}")
    print(f"{BOLD}{GREEN}  Scenes  : {manifest['scene_count']}{RESET}")
    print(f"{BOLD}{GREEN}  Duration: {manifest['total_duration']}s{RESET}")
    print(f"{BOLD}{GREEN}  Clips   : {manifest['clips_found']}/{manifest['scene_count']} found{RESET}")
    print(f"{BOLD}{GREEN}  Audio   : {'✓ generated' if tts_ok else '✗ skipped'}{RESET}")
    print(f"{BOLD}{GREEN}  Output  : {OUTPUT_DIR.resolve()}{RESET}")
    print(f"{BOLD}{GREEN}{'═'*60}{RESET}\n")

    # Pretty-print condensed manifest
    print(f"{BOLD}=== MANIFEST (JSON) ==={RESET}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
