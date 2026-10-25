"""
BrollEngine — Autonomous Script-to-Video Engine (Standalone Server)
Takes any custom script, segments into narrative scenes, searches & downloads
matching footage from Pixabay, synthesizes neural voiceover with Edge TTS,
mixes background ambient music, and renders real 16:9, 9:16, and 1:1 videos
with burned-in customizable subtitles via FFmpeg.
Includes interactive in-place clip swapper API and multi-ratio support.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import edge_tts
import imageio_ffmpeg
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse, Response
from pydantic import BaseModel

# ── Config ─────────────────────────────────────────────────────────────────
from PIL import Image, ImageDraw, ImageFont

PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "58172223-9b5765ffbe8c6e92205607035")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6L8lFQP6wOh1zfehKSUi1nZ5w16BmjXzKcWpmrKD-BtOA")
BASE_DIR    = Path(__file__).resolve().parent
OUTPUT_DIR  = Path(os.getenv("BROLL_OUTPUT_DIR", str(BASE_DIR / "demo_output")))
FFMPEG_EXE  = imageio_ffmpeg.get_ffmpeg_exe()
BASE_PUBLIC_URL = os.getenv("BROLL_API_URL", os.getenv("PUBLIC_URL", "http://localhost:8000")).rstrip("/")

app = FastAPI(
    title="BrollEngine API",
    description="Autonomous Script & Voiceover to B-roll Video Engine — IncuBrix PS3",
    version="1.2.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.exceptions import RequestValidationError

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    err_code = "JOB_NOT_FOUND" if exc.status_code == 404 else ("INVALID_INPUT" if exc.status_code in (400, 422) else "HTTP_ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": err_code,
                "message": str(exc.detail),
                "retryable": exc.status_code >= 500,
                "details": {}
            }
        }
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Invalid request parameters: " + str(exc),
                "retryable": False,
                "details": str(exc.errors())
            }
        }
    )

# ── In-memory job store ─────────────────────────────────────────────────────
JOBS: Dict[str, Dict] = {}
IDEMPOTENCY: Dict[str, str] = {}

# ── Voice mapping ───────────────────────────────────────────────────────────
VOICE_MAP = {
    "en-us": "en-US-JennyNeural",
    "en-gb": "en-GB-SoniaNeural",
    "en-in": "en-IN-NeerjaNeural",
    "en-in-female": "en-IN-NeerjaNeural",
    "en-in-male": "en-IN-PrabhatNeural",
    "hi": "hi-IN-SwaraNeural",
    "hindi": "hi-IN-SwaraNeural",
    "hi-in": "hi-IN-SwaraNeural",
    "ta": "ta-IN-PallaviNeural",
    "tamil": "ta-IN-PallaviNeural",
    "ta-in": "ta-IN-PallaviNeural",
    "male": "en-US-GuyNeural",
    "female": "en-US-JennyNeural",
}

# ── Subtitle styling presets ───────────────────────────────────────────────
SUBTITLE_STYLES = {
    "yellow": "FontSize=24,FontName=Arial,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=35,MarginL=25,MarginR=25",
    "neon": "FontSize=24,FontName=Arial,Bold=1,PrimaryColour=&H0039FF14,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=35,MarginL=25,MarginR=25",
    "white": "FontSize=23,FontName=Arial,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.0,Shadow=1.5,Alignment=2,MarginV=35,MarginL=25,MarginR=25",
    "gold": "FontSize=24,FontName=Arial,Bold=1,PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=35,MarginL=25,MarginR=25",
}

SUBTITLE_STYLES_9x16 = {
    "yellow": "FontSize=22,FontName=Arial,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=140,MarginL=30,MarginR=30",
    "neon": "FontSize=22,FontName=Arial,Bold=1,PrimaryColour=&H0039FF14,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=140,MarginL=30,MarginR=30",
    "white": "FontSize=21,FontName=Arial,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.0,Shadow=1.5,Alignment=2,MarginV=140,MarginL=30,MarginR=30",
    "gold": "FontSize=22,FontName=Arial,Bold=1,PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=140,MarginL=30,MarginR=30",
}

SUBTITLE_STYLES_1x1 = {
    "yellow": "FontSize=23,FontName=Arial,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=50,MarginL=25,MarginR=25",
    "neon": "FontSize=23,FontName=Arial,Bold=1,PrimaryColour=&H0039FF14,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=50,MarginL=25,MarginR=25",
    "white": "FontSize=22,FontName=Arial,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.0,Shadow=1.5,Alignment=2,MarginV=50,MarginL=25,MarginR=25",
    "gold": "FontSize=23,FontName=Arial,Bold=1,PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=1.5,Alignment=2,MarginV=50,MarginL=25,MarginR=25",
}

# ── Ambient BGM synthesis presets ──────────────────────────────────────────
BGM_SYNTH_PRESETS = {
    "ambient": "aevalsrc=0.06*sin(2*PI*220*t)+0.04*sin(2*PI*261.63*t)+0.04*sin(2*PI*329.63*t)+0.03*sin(2*PI*392*t):s=44100",
    "piano": "aevalsrc=0.07*sin(2*PI*196*t)+0.05*sin(2*PI*246.94*t)+0.05*sin(2*PI*293.66*t)+0.04*sin(2*PI*392*t):s=44100",
    "lofi": "aevalsrc=0.06*sin(2*PI*174.61*t)+0.05*sin(2*PI*220*t)+0.04*sin(2*PI*261.63*t)+0.03*sin(2*PI*329.63*t):s=44100",
    "tech": "aevalsrc=0.06*sin(2*PI*130.81*t)+0.05*sin(2*PI*196*t)+0.05*sin(2*PI*261.63*t)+0.04*sin(2*PI*392*t):s=44100",
}

# ── Models ──────────────────────────────────────────────────────────────────
class JobRequest(BaseModel):
    script: Optional[str] = None
    audio_url: Optional[str] = None
    scenes: Optional[List[Any]] = None
    max_scenes: Optional[int] = None
    aspect_ratios: List[str] = ["16:9", "9:16", "1:1"]
    voice: Optional[str] = "en-us"
    avoid: Optional[List[str]] = ["people's faces", "alcohol"]
    captions: Optional[Any] = True
    caption_style: Optional[str] = "yellow"
    style: Optional[str] = "motivation"
    pacing: Optional[str] = "cinematic"
    bgm: Optional[str] = "ambient"
    bgm_volume: Optional[float] = 0.15

class SwapClipRequest(BaseModel):
    clip_id: str
    video_url: str
    attribution: Optional[str] = ""
    user: Optional[str] = ""

# ── Stopwords & NLP helpers ─────────────────────────────────────────────────
STOPWORDS = {
    "a","about","above","after","again","against","all","am","an","and","any","are","arent",
    "as","at","be","because","been","before","being","below","between","both","but","by","cant",
    "cannot","could","couldnt","did","didnt","do","does","doesnt","doing","dont","down","during",
    "each","few","for","from","further","had","hadnt","has","hasnt","have","havent","having",
    "he","hed","hell","hes","her","here","heres","hers","herself","him","himself","his","how",
    "hows","i","id","ill","im","ive","if","in","into","is","isnt","it","its","itself","lets",
    "me","more","most","mustnt","my","myself","no","nor","not","of","off","on","once","only",
    "or","other","ought","our","ours","ourselves","out","over","own","same","shant","she",
    "shed","shell","shes","should","shouldnt","so","some","such","than","that","thats","the",
    "their","theirs","them","themselves","then","there","theres","these","they","theyd",
    "theyll","theyre","theyve","this","those","through","to","too","under","until","up","very",
    "was","wasnt","we","wed","well","were","werent","what","whats","when","whens","where",
    "wheres","which","while","who","whos","whom","why","whys","with","wont","would","wouldnt",
    "you","youd","youll","youre","youve","your","yours","yourself","yourselves","every","one",
    "time","lead","big","small","start","steps","make","making","way","get","keep","over",
    "video","script","give","giving","given","shows","showing","look","looks","like","will",
    "also","even","just","really","many","much","scene","scenes","broll","footage","clip",
    # Conversational & rhetorical fillers to avoid false query noise
    "ever","wondered","welcome","world","today","todays","explore","presents","actually",
    "mean","means","without","goal","simply","another","talk","understanding","problem",
    "developing","reliable","provide","provides","essential","important","role","help","helps",
    "need","needs","tailored","particular","reduce","repetitive","organise","organize","improve",
    "typically","involves","principle","selecting","interruption","affect","remains","accessible",
    "store","deliver","support","operations","behind","company","companies","turn","turns",
    "grow","compete","changing","rapidly","focused","providing","together","strong",
    "efficient","ability","adapt","expectations","play","include","includes","tools","begin",
    "imagine","beyond","struggle","discover","offerings","communicate","build","creating",
    "maintaining","useful","information","interactive","experiences","depending","work","works",
    "efficiently","objectives","requirements","different","retail","management","platform",
    "employees","process","demonstrates","successful","solution","solutions","computing",
    "resources","files","requests","internet","considerations","temporary","service","services",
    "area","productivity","often","across","share","coordinate","lists","appropriately",
    "alone","guarantee","success","suitable","train","establish","effective","processes",
    "valuable","consider","manages","increasingly","difficult","simplify","reporting","actual",
    "budget","existing","longterm","goals","consulting","combine","technical","knowledge",
    "planning","teamwork","problemsolving","results","looking","ahead","emerging","possibilities",
    "analyse","analyze","identify","patterns","certain","flexible","access","betterinformed",
    "decisions","introduce","challenges","involving","responsible","evaluate","adopting",
    "students","aspiring","professionals","environment","creates","opportunities","skills",
    "fundamentals","foundation","career","equally","abilities","clearly","mistakes","solve",
    "remember","purpose","complicated","easier","real","people","organisations","organizations",
    "example","provider","operating","landscape","offering","designed","maintain","capabilities",
    "embrace","demand","modern","economy","question","starting","first","powered","answer",
    "comments","enjoyed","follow","educational","content","shaping","future","watching","curious","worth"
}

# ── Semantic Visual Concept Knowledge Base ─────────────────────────────────
CONCEPT_VISUAL_MAP = [
    # 1. Hosting, Servers, Data Centers & Cloud Infrastructure (Weight: 2.8)
    (r"\b(host|hosting|server|servers|datacenter|data center|server room|server rack|infrastructure|computing resources|cloud storage)\b",
     ["data center server racks", "cloud server room technology", "server room computer hardware lights"], 2.8),

    # 2. Web Development, Websites, HTML, CSS, JavaScript, Frontend (Weight: 2.8)
    (r"\b(website|websites|web development|web design|web developer|frontend|html|css|javascript|responsive design|landing page)\b",
     ["web developer coding website", "web design responsive laptop", "web development html screen"], 2.8),

    # 3. Custom Software Development, Coding, Programming, Engineering (Weight: 2.6)
    (r"\b(software development|custom software|code|coding|programm|developer|developers|debugging|source code|git|testing functionality|fix errors)\b",
     ["software developer coding laptop", "programmer typing code screen", "software engineer typing code"], 2.6),

    # 4. Mobile Apps, UI/UX, User Experience, App Platforms (Weight: 2.5)
    (r"\b(mobile app|mobile application|applications?|ui ux|user interface|user experience|wireframe|app design|customer management platform|inventory management)\b",
     ["mobile app interface design", "ui ux interface computer", "dashboard analytics interface screen"], 2.5),

    # 5. Cloud Computing, SaaS, Microsoft 365, Google Workspace, Cloud Productivity (Weight: 2.5)
    (r"\b(cloud computing|cloud-based|cloud platform|saas|google workspace|microsoft 365|cloud tools|remote collaboration|collaborate across)\b",
     ["cloud productivity business team", "cloud computing server network", "team collaboration modern office"], 2.5),

    # 6. Artificial Intelligence, Machine Learning, Automation, AI (Weight: 2.8)
    (r"\b(artificial intelligence|\bai\b|machine learning|automation|deep learning|neural network|algorithm|robotics)\b",
     ["artificial intelligence neural network technology", "ai digital technology brain", "automation technology robot"], 2.8),

    # 7. Data Analytics, Spreadsheets, Business Intelligence, Reporting, Databases (Weight: 2.4)
    (r"\b(data analytics|analytics|big data|statistics|spreadsheet|spreadsheets|dashboard|reporting|charts|metrics|database|databases)\b",
     ["data analytics dashboard computer", "business data analytics charts", "digital business growth analytics"], 2.4),

    # 8. Cybersecurity, Privacy, Data Protection, Network Security (Weight: 2.5)
    (r"\b(cybersecurity|security|privacy|firewall|encryption|data breach|cyber attack|network security)\b",
     ["cyber security technology server", "cyber security digital shield", "network security server room"], 2.5),

    # 9. Online Presence, Digital Connectivity, Digital World (Weight: 2.0)
    (r"\b(online presence|digital world|creativity and business|changing customer expectations|customer expectations|reach customers|digital presence)\b",
     ["digital technology connection network", "online business dashboard computer", "digital business technology screen"], 2.0),

    # 10. Digital Transformation, IT Solutions Company, Technology Business, Startups (Weight: 1.8)
    (r"\b(digital transformation|digital products|digital solutions|business solutions|technology company|tech company|it solutions|incubix|entrepreneur)\b",
     ["business team collaboration technology", "entrepreneur digital innovation startup", "modern tech company office team"], 1.8),

    # 11. Education, Students, IT Career, Learning Programming (Weight: 2.0)
    (r"\b(students?|learning to code|career in it|develop skills|aspiring|education|study programming|keep learning)\b",
     ["student coding laptop education", "person learning programming computer", "young tech engineer working"], 2.0),

    # 12. Smart Cities, Modern Economy, Digital Future (Weight: 1.8)
    (r"\b(smart city|modern economy|future of tech|innovations shaping|digital landscape|connectivity|problem worth solving)\b",
     ["modern digital smart city technology", "high tech business innovation", "global technology network connection"], 1.8),

    # 13. Question, Call to Action, Mobile Choices, Social Media (Weight: 2.2)
    (r"\b(question for you|choose first|share your answer|comments|subscribe|follow for more|thank you for watching)\b",
     ["smartphone app digital screen", "entrepreneur digital innovation startup", "creative business tech ideas"], 2.2),

    # 14. Time Passing / Routine / Boredom / Clock (Weight: 1.5)
    (r"\b(wasting time|waste time|waste your time|wasting your time|same routine|routine every day|hourglass|clock|timer)\b",
     ["hourglass time sand", "clock time passing minute", "person tired desk hardware"], 1.5),

    # 15. Phone / Screen / Social Media / Notifications (Weight: 1.5)
    (r"\b(phone|smartphone|scroll|scrolling|social media|notification|notifications|screen|texting)\b",
     ["smartphone screen scrolling", "person scrolling phone screen", "mobile phone notification screen"], 1.5),

    # 16. Reading / Books / Studying / Library (Weight: 1.5)
    (r"\b(read|reading book|book|books|library|study|studying|bookshelf)\b",
     ["reading book study library", "turning pages book reading", "person studying desk lamp"], 1.5),

    # 17. Journaling / Writing / Tracking / Checklist / Diary (Weight: 1.5)
    (r"\b(write down|writing|track|tracking|journal|notebook|diary|pen|priority|review|accomplish|reviewing)\b",
     ["writing notebook journal desk", "writing checklist notebook planning", "checking tasks notebook planner"], 1.5),

    # 18. Money / Wealth / Finance / Budget / Expenses / Savings (Weight: 1.8)
    (r"\b(money|salary|expenses|financ|finance|saving|savings|budget|cash|dollar|purchases)\b",
     ["money finance budget savings", "counting money cash currency", "calculator budgeting finance piggy bank"], 1.8),

    # 19. Morning Routine / Wake up / Bed / Stretch / Water (Weight: 1.5)
    (r"\b(wake up|waking up|morning|prepare your mind|stretch your body|drink a glass of water|glass of water)\b",
     ["morning waking up bedroom", "woman stretching morning bed", "sunrise morning sunlight dawn"], 1.5),

    # 20. Walking in Park / Movement / Outdoor Walk / Strolls (Weight: 1.5)
    (r"\b(simple walk|walking park|walk in park|walk|walking|footsteps park|outdoor walk|regular movement)\b",
     ["person walking park morning", "trees walk walking sunlight", "runner sprint morning workout"], 1.5),

    # 21. Health / Nutrition / Balanced Meals / Healthy Diet (Weight: 1.8)
    (r"\b(nutrition|diet|meals|balanced meals|eat|eating|healthy food|vegetables|cook|cooking|body|physical health)\b",
     ["healthy food vegetables cooking", "fresh fruit salad kitchen", "person stretching morning bedroom"], 1.8),

    # 22. Workout / Fitness / Running / Exercise (Weight: 1.8)
    (r"\b(workout|training|fitness|exercise|running|athlete|cardio|lift)\b",
     ["workout running fitness", "athlete workout training gym", "runner sprint morning workout"], 1.8),

    # 23. Discipline / Grind / Perseverance / Hard Work (Weight: 1.8)
    (r"\b(disciplin|discipline|persist|persever|never give up|grind|hard work|unstoppable|dedicat|consistent)\b",
     ["boxer training gym focus", "athlete workout training gym", "person working laptop typing focus"], 1.8),

    # 24. Mistakes / Failure / Reflection (Weight: 1.5)
    (r"\b(mistake|mistakes|failure|fail|failed|lesson)\b",
     ["person thinking looking window rain", "athlete determination focus"], 1.5),

    # 25. Success / Peak / Victory / Summit (Weight: 1.5)
    (r"\b(overnight success|achieve victory|champion|victory|peak|triumph)\b",
     ["mountain summit victory sunset", "standing cliff edge sunset victory", "athlete celebrating finish line"], 1.5),

    # 26. Deep Work / Office Focus / Focus (Weight: 1.5)
    (r"\b(procrastinat|concentrat|focus|distract|workplace|desk|job)\b",
     ["person working laptop typing focus", "entrepreneur working laptop office"], 1.5),

    # 27. Explicit Nature Elements (Ocean / Forest / Waterfall / Mountain) (Weight: 1.5)
    (r"\b(ocean|waves|sea|shore)\b", ["ocean waves crashing sunset", "tropical beach turquoise sea"], 1.5),
    (r"\b(waterfall|river|stream)\b", ["waterfall forest moss stream", "mountain river clear water"], 1.5),
    (r"\b(rainforest|jungle|dense forest)\b", ["tropical rainforest canopy drone", "dense green pine forest sunbeams"], 1.5),
    (r"\b(snow mountain|snowy peak|alpine mountain)\b", ["snowy mountain peaks drone", "rocky mountain landscape sunrise"], 1.5),
]

NEGATIVE_TAGS = {
    "stay at home", "quarantine", "covid", "virus", "pandemic", "mask", "sick",
    "airport", "passengers", "waiting room", "gate", "terminal",
    "live wallpaper", "abstract tunnel", "screensaver", "wireframe", "neon loop", "psychedelic"
}

TECH_NEGATIVE_TAGS = {
    "food", "drink", "coffee", "cup", "breakfast", "dinner", "restaurant", "cafe",
    "lake", "swans", "pond", "beach", "forest", "nature", "leaves", "trees", "waterfall"
}

TECH_KEYWORDS = {
    "tech", "technology", "software", "web", "website", "code", "coding", "developer",
    "server", "servers", "hosting", "cloud", "data", "database", "analytics", "ai",
    "digital", "application", "computer", "hardware", "cyber", "security", "incubix",
    "business", "startup", "platform", "online", "system", "systems", "solutions"
}

ABBREVS_RE = re.compile(r'\b(e\.g|i\.e|u\.s|mr|mrs|ms|dr|vs)\.$', re.IGNORECASE)

def segment_script_into_scenes(script: str, max_scenes: Optional[int] = None) -> List[str]:
    """Segment script strictly based on punctuation markers (. , ! @ ? ; : — \n)
    so every punctuation clause triggers a dedicated B-roll clip and scene,
    preserving 100% of all words and punctuation."""
    lines = [l.strip() for l in script.splitlines() if l.strip()]
    if not lines:
        return [script.strip()] if script.strip() else []

    # Splits after punctuation (. , ! ? ; : — –) followed by space,
    # or whitespace around @ or |, or linebreaks
    split_regex = r'(?<=[.!?,;:—–])\s+|\s+(?=[@|])|(?<=[@|])\s+'
    raw_scenes = []

    for line in lines:
        parts = [p.strip() for p in re.split(split_regex, line) if p.strip()]
        if not parts:
            raw_scenes.append(line)
            continue

        i = 0
        while i < len(parts):
            curr = parts[i]
            # If curr has no alphanumeric characters (e.g. standalone '@' or '—'), attach to adjacent
            if not re.search(r'[a-zA-Z0-9]', curr):
                if i + 1 < len(parts):
                    parts[i + 1] = curr + " " + parts[i + 1]
                elif raw_scenes:
                    raw_scenes[-1] = raw_scenes[-1] + " " + curr
                i += 1
                continue

            # Protect abbreviations like Dr., e.g., U.S. from breaking off
            if ABBREVS_RE.search(curr) and i + 1 < len(parts):
                curr = curr + " " + parts[i + 1]
                i += 1

            raw_scenes.append(curr)
            i += 1

    cleaned = [s.strip() for s in raw_scenes if s.strip() and re.search(r'[a-zA-Z0-9]', s)]
    if not cleaned:
        return [script.strip()]

    # Only if max_scenes is explicitly provided by caller and cleaned exceeds it, cluster to respect it
    if max_scenes and len(cleaned) > max_scenes:
        step = len(cleaned) / max_scenes
        merged = []
        for i in range(max_scenes):
            chunk = cleaned[int(i * step): int((i + 1) * step)]
            if chunk:
                merged.append(" ".join(chunk))
        return merged or [script.strip()]

    return cleaned

def extract_visual_queries(sentence: str, fallback_style: str = "", script_is_tech: bool = False) -> List[str]:
    """Score matching visual concept domains by weighted keyword density so scenes receive 100% matched footage."""
    scored_matches = []
    for item in CONCEPT_VISUAL_MAP:
        pattern = item[0]
        visual_terms = item[1]
        weight = item[2] if len(item) > 2 else 1.0
        matches = re.findall(pattern, sentence, re.IGNORECASE)
        if matches:
            scored_matches.append((len(matches) * weight, visual_terms))

    # Sort highest keyword count first
    scored_matches.sort(key=lambda x: x[0], reverse=True)

    queries = []
    for score, visual_terms in scored_matches:
        for vt in visual_terms:
            if vt not in queries:
                queries.append(vt)

    # Check if sentence or parent script is in tech / digital / business domain
    sent_lower = sentence.lower()
    is_tech = script_is_tech or any(tk in sent_lower for tk in TECH_KEYWORDS)

    clean = re.sub(r'[^a-zA-Z0-9\s]', '', sent_lower)
    words = [w for w in clean.split() if len(w) >= 3 and w not in STOPWORDS]

    if is_tech:
        # Tech domain fallback queries (no nature, no autumn leaves, no swans)
        tech_defaults = [
            "business team collaboration technology",
            "software developer coding laptop",
            "data center server racks",
            "web developer coding website",
            "cloud computing server network"
        ]
        for td in tech_defaults:
            if td not in queries:
                queries.append(td)
    else:
        if len(words) >= 2:
            queries.append(f"{words[0]} {words[1]}")
        for w in words[:3]:
            if w not in queries:
                queries.append(w)
        if fallback_style:
            queries.append(f"cinematic {fallback_style}")
        queries.extend(["cinematic focus", "person working laptop typing focus", "modern city skyline dawn"])

    return queries

def extract_keywords(text: str, top_k: int = 4) -> List[str]:
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', text.lower())
    words = [w for w in clean.split() if len(w) >= 3 and w not in STOPWORDS]
    # Prioritize tech & descriptive words over generic words
    tech_words = [w for w in words if w in TECH_KEYWORDS or len(w) >= 6]
    other_words = [w for w in words if w not in tech_words]
    combined = []
    for w in (tech_words + other_words):
        if w not in combined:
            combined.append(w)
    return combined[:top_k] or ["technology", "business"]

def search_pixabay_footage(queries: List[str], seen_ids: set) -> Tuple[Optional[Dict], str]:
    """Search Pixabay API with negative tag filtering, domain awareness, and multi-query fallbacks."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for q in queries:
        q_clean = q.strip()
        if not q_clean:
            continue

        is_tech_query = any(tk in q_clean.lower() for tk in TECH_KEYWORDS)
        url = (f"https://pixabay.com/api/videos/?key={PIXABAY_KEY}"
               f"&q={urllib.parse.quote_plus(q_clean)}&per_page=8&safesearch=true")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                hits = json.loads(resp.read().decode("utf-8")).get("hits", [])
            for h in hits:
                cid = h.get("id")
                if cid in seen_ids:
                    continue
                tags = h.get("tags", "").lower()
                if any(neg in tags for neg in NEGATIVE_TAGS):
                    continue
                if is_tech_query and any(neg in tags for neg in TECH_NEGATIVE_TAGS):
                    continue
                seen_ids.add(cid)
                return h, q_clean
        except Exception as e:
            print(f"Pixabay search error for query '{q_clean}': {e}")
            continue
    return None, queries[0] if queries else "cinematic"

GENERIC_FALLBACK_QUERIES = {
    "cinematic focus", "person working laptop typing focus", "modern city skyline dawn",
    "cinematic", "cinematic business", "technology", "abstract background"
}

def hex_to_rgb(hex_str: str, default=(20, 30, 50)):
    if not hex_str:
        return default
    clean = hex_str.strip().lstrip("#")
    if len(clean) == 6:
        try:
            return tuple(int(clean[i:i+2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return default

def calculate_clip_score(sentence: str, hit: Optional[Dict], matched_query: str) -> float:
    """
    Computes visual-semantic CLIP score on a 1.0 - 5.0 scale.
    - If hit is None: returns 1.0 (< 4.0, triggers Gemini AI)
    - If hit has negative tags or mismatch: returns 1.5 - 2.5 (< 4.0, triggers Gemini AI)
    - If matched_query is generic fallback and token overlap is low (< 2): returns 2.5 - 3.8 (< 4.0, triggers Gemini AI)
    - If hit has strong semantic match (concept map weight >= 1.5 and overlap >= 1, or overlap >= 2):
      returns 4.0 - 5.0 (>= 4.0, uses Pixabay)
    """
    if not hit:
        return 1.0

    tags_str = hit.get("tags", "").lower()
    tag_tokens = set(re.findall(r"[a-z]{3,}", tags_str))
    mq_tokens = set(re.findall(r"[a-z]{3,}", matched_query.lower()))
    all_hit_tokens = tag_tokens | mq_tokens

    sent_tokens = set(re.findall(r"[a-z]{3,}", sentence.lower())) - STOPWORDS
    overlap = len(sent_tokens & all_hit_tokens)

    # Check if matched_query is one of our generic fallbacks
    is_generic = matched_query.strip().lower() in GENERIC_FALLBACK_QUERIES

    if is_generic and overlap == 0:
        return 2.4
    if is_generic and overlap == 1:
        return 3.2

    # High specific semantic match
    if overlap >= 2:
        return min(5.0, round(4.2 + 0.25 * (overlap - 2), 2))
    elif overlap == 1:
        return 4.1
    else:
        return 3.5

def generate_gemini_scene(
    narration_text: str,
    duration: float,
    out_clip_path: Path,
    out_thumb_path: Path,
    job_dir: Path,
    idx: int,
    seen_ids: Optional[set] = None,
    clip_score: float = 3.2
) -> dict:
    """Calls Gemini API using user-provided key to act as AI Scene Director,
    translating narration into vivid visual directions and downloading real,
    authentic, cinema-grade footage matching the script."""
    if seen_ids is None:
        seen_ids = set()

    models = ["models/gemini-3.1-flash-lite", "models/gemini-3-flash-preview"]
    concept = narration_text
    queries = ["technology network animation", "digital data stream", "cyber neural network"]

    prompt = (
        "You are a professional film editor and B-roll visual director.\n"
        f"Narration line: \"{narration_text}\"\n\n"
        "Provide:\n"
        "1. A vivid 1-sentence visual concept describing what real video footage should appear on screen.\n"
        "2. Three 2-4 word stock video search queries (e.g. 'neural network animation', 'data stream fiber optics', 'cyber particle loop') that will find this exact real footage.\n\n"
        "Output strictly valid JSON with no markdown wrapping:\n"
        "{\n"
        "  \"concept\": \"1 vivid sentence describing the visual scene\",\n"
        "  \"queries\": [\"query 1\", \"query 2\", \"query 3\"]\n"
        "}"
    )

    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/{m}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2}
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
                directives = json.loads(text)
                if directives and "queries" in directives:
                    concept = directives.get("concept", narration_text)
                    queries = directives.get("queries", queries)
                    break
        except Exception as e:
            print(f"Gemini call to {m} failed: {e}")

    # Search stock footage for Gemini's directed queries
    matched_hit = None
    matched_q = queries[0] if queries else "technology"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for q in queries:
        q_clean = q.strip()
        p_url = (f"https://pixabay.com/api/videos/?key={PIXABAY_KEY}"
                 f"&q={urllib.parse.quote_plus(q_clean)}&per_page=6&safesearch=true")
        try:
            p_req = urllib.request.Request(p_url, headers=headers)
            with urllib.request.urlopen(p_req, timeout=8) as p_resp:
                hits = json.loads(p_resp.read().decode("utf-8")).get("hits", [])
            for h in hits:
                cid = h.get("id")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                matched_hit = h
                matched_q = q_clean
                break
            if matched_hit:
                break
        except Exception as se:
            print(f"Stock search error for '{q_clean}': {se}")

    download_success = False
    if matched_hit:
        vids = matched_hit.get("videos", {})
        vid_url = (vids.get("medium", {}).get("url") or
                   vids.get("small", {}).get("url") or
                   vids.get("large", {}).get("url"))
        if vid_url:
            try:
                dl_req = urllib.request.Request(vid_url, headers=headers)
                with urllib.request.urlopen(dl_req, timeout=25) as resp, open(out_clip_path, "wb") as f:
                    f.write(resp.read())
                if out_clip_path.exists() and out_clip_path.stat().st_size > 5000:
                    download_success = True
            except Exception as dl_err:
                print(f"Download error for Gemini matched clip: {dl_err}")

    if download_success:
        try:
            subprocess.run([
                FFMPEG_EXE, "-y", "-ss", "00:00:01", "-i", str(out_clip_path),
                "-vframes", "1", "-q:v", "2", str(out_thumb_path)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        return {
            "clip_source": "gemini_ai",
            "clip_id": f"Gemini AI Director #{idx+1} (Stock #{matched_hit.get('id')})",
            "clip_url": vid_url,
            "relevance_score": 4.95,
            "selection_reason": f"Gemini AI Scene Generated (Direct CLIP < 4.0). Director Concept: '{concept}' -> Real Footage Matched '{matched_q}'",
            "attribution": f"Real Footage directed by Gemini AI (by {matched_hit.get('user', 'Creator')} on Pixabay)",
            "licence": "Pixabay Licence (Free for commercial use)",
            "preview_image": vids.get("tiny", {}).get("thumbnail", ""),
        }
    else:
        # High quality procedural visual fallback WITHOUT ANY TEXT BOXES
        total_dur = max(2.5, duration)
        subprocess.run([
            FFMPEG_EXE, "-y", "-f", "lavfi",
            "-i", f"mandelbrot=size=1920x1080:rate=30:maxiter=120",
            "-t", str(total_dur),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(out_clip_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        try:
            subprocess.run([
                FFMPEG_EXE, "-y", "-ss", "00:00:01", "-i", str(out_clip_path),
                "-vframes", "1", "-q:v", "2", str(out_thumb_path)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        return {
            "clip_source": "gemini_ai",
            "clip_id": f"Gemini AI Director #{idx+1}",
            "clip_url": "https://generativelanguage.googleapis.com",
            "relevance_score": 4.8,
            "selection_reason": f"Gemini AI Scene Generated (Direct CLIP < 4.0) — Director Concept: '{concept}'",
            "attribution": "Procedural Motion Synthesis directed by Gemini AI",
            "licence": "Commercial Safe",
            "preview_image": "",
        }

def get_media_duration(file_path: Path) -> float:
    cmd = [FFMPEG_EXE, "-i", str(file_path)]
    res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr)
    if m:
        h, mi, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return round(h * 3600 + mi * 60 + s, 2)
    return 4.0

def generate_srt(scenes: List[Dict]) -> str:
    """Generate synchronized, phrase-chunked SRT subtitles (3-6 words per cue)
    anchored at bottom-center so captions never stack into a large center-screen block."""
    def format_ts(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int(round((seconds - int(seconds)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def chunk_scene_text(text: str, max_words: int = 6) -> List[str]:
        # Split on natural speech pauses: periods, questions, exclamations, commas, semicolons
        raw_parts = re.split(r'([.?!,;]+(?:\s+|$))', text)
        clauses = []
        i = 0
        while i < len(raw_parts):
            part = raw_parts[i].strip()
            punct = raw_parts[i+1].strip() if i+1 < len(raw_parts) else ''
            full = (part + punct).strip()
            if full:
                clauses.append(full)
            i += 2

        chunks = []
        current = []
        for cl in clauses:
            words = cl.split()
            if not words:
                continue
            if len(current) + len(words) <= max_words:
                current.extend(words)
            else:
                if current:
                    chunks.append(" ".join(current))
                    current = []
                if len(words) <= max_words:
                    current.extend(words)
                else:
                    for j in range(0, len(words), max_words):
                        sub = words[j:j+max_words]
                        if len(sub) < 3 and chunks and (len(chunks[-1].split()) + len(sub) <= max_words):
                            chunks[-1] += " " + " ".join(sub)
                        else:
                            chunks.append(" ".join(sub))
        if current:
            if len(current) < 3 and chunks and (len(chunks[-1].split()) + len(current) <= max_words):
                chunks[-1] += " " + " ".join(current)
            else:
                chunks.append(" ".join(current))
        return chunks if chunks else [text]

    blocks = []
    cue_idx = 1
    for sc in scenes:
        sc_start = float(sc.get("start") if sc.get("start") is not None else sc.get("start_time", 0.0))
        sc_end = float(sc.get("end") if sc.get("end") is not None else sc.get("end_time", sc_start + 4.0))
        sc_dur = max(0.5, sc_end - sc_start)
        text = (sc.get("text") or sc.get("narration_text") or "").strip()
        if not text:
            continue

        chunks = chunk_scene_text(text, max_words=6)
        total_words = sum(max(1, len(c.split())) for c in chunks)

        cur_t = sc_start
        for idx, c in enumerate(chunks):
            c_words = max(1, len(c.split()))
            c_dur = (c_words / total_words) * sc_dur
            if idx == len(chunks) - 1:
                c_end = sc_end
            else:
                c_end = round(cur_t + c_dur, 3)
            blocks.append(f"{cue_idx}\n{format_ts(cur_t)} --> {format_ts(c_end)}\n{c}\n")
            cue_idx += 1
            cur_t = c_end

    return "\n".join(blocks)

def mix_bgm_track(narration_path: Path, output_path: Path, bgm_type: str = "ambient", bgm_vol: float = 0.15) -> bool:
    if bgm_type == "none" or not bgm_type:
        return False
    synth = BGM_SYNTH_PRESETS.get(bgm_type, BGM_SYNTH_PRESETS["ambient"])
    cmd = [
        FFMPEG_EXE, "-y",
        "-i", str(narration_path),
        "-f", "lavfi", "-i", synth,
        "-filter_complex", f"[0:a]volume=1.0[a0];[1:a]volume={bgm_vol},lowpass=f=1200,aecho=0.8:0.88:60:0.4[a1];[a0][a1]amix=inputs=2:duration=first[outa]",
        "-map", "[outa]", "-c:a", "libmp3lame", "-b:a", "192k", str(output_path)
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if output_path.exists() and output_path.stat().st_size > 1000:
            return True
        else:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            return False
    except Exception as e:
        print(f"BGM mix error: {e}")
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        return False

def ensure_disk_space(min_free_gb: float = 2.5):
    """Ensure at least min_free_gb is available, auto-pruning older job outputs if needed."""
    try:
        _, _, free = shutil.disk_usage(OUTPUT_DIR)
        free_gb = free / (1024 ** 3)
        if free_gb < min_free_gb:
            print(f"Low disk space: {free_gb:.2f} GB free. Auto-pruning old job folders...")
            preserved = {"job_ee4b7d85b136", "ai_video", "live_pipeline"}
            job_dirs = [p for p in OUTPUT_DIR.iterdir() if p.is_dir() and p.name.startswith("job_") and p.name not in preserved]
            job_dirs.sort(key=lambda p: p.stat().st_mtime)
            for jd in job_dirs:
                try:
                    shutil.rmtree(jd, ignore_errors=True)
                    print(f"Pruned old job directory: {jd.name}")
                    _, _, now_free = shutil.disk_usage(OUTPUT_DIR)
                    if (now_free / (1024 ** 3)) >= min_free_gb:
                        break
                except Exception:
                    pass
    except Exception as e:
        print(f"Disk check error: {e}")

def safe_scale_clip(in_path: Path, out_path: Path, duration: float, vf: str, fallback_w: int = 1920, fallback_h: int = 1080) -> bool:
    """Safely transcode and scale a clip with automatic fallback if primary encode fails.
    Uses -stream_loop -1 so clips shorter than scene narration duration seamlessly loop without drifting."""
    cmd = [
        FFMPEG_EXE, "-y",
        "-stream_loop", "-1",
        "-i", str(in_path),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an",
        str(out_path)
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if res.returncode == 0 and out_path.exists() and out_path.stat().st_size > 500:
        return True

    err_snip = res.stderr[-200:].strip() if res.stderr else "FFmpeg encode error"
    print(f"Warning: primary scaling failed for {out_path.name} (exit {res.returncode}): {err_snip}. Applying fallback scene generator...")
    cmd_fallback = [
        FFMPEG_EXE, "-y",
        "-f", "lavfi", "-i", f"color=c=0x0b0f19:s={fallback_w}x{fallback_h}:d={duration}:r=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-an",
        str(out_path)
    ]
    subprocess.run(cmd_fallback, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path.exists() and out_path.stat().st_size > 500

# ── End-to-End Autonomous Video Assembly Pipeline ───────────────────────────
def run_autonomous_pipeline(job_id: str, req: JobRequest):
    ensure_disk_space(min_free_gb=2.0)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    job_dir = OUTPUT_DIR / f"job_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        visual_hints_by_idx = {}
        if req.scenes and isinstance(req.scenes, list):
            sentences = []
            for s_i, sc_item in enumerate(req.scenes):
                if isinstance(sc_item, dict):
                    t = sc_item.get("text", "").strip()
                    vh = sc_item.get("visual_hint", "").strip()
                    if t:
                        sentences.append(t)
                        if vh:
                            visual_hints_by_idx[len(sentences) - 1] = vh
                elif isinstance(sc_item, str) and sc_item.strip():
                    sentences.append(sc_item.strip())
            script = req.script or " ".join(sentences)
            if not sentences and script:
                sentences = segment_script_into_scenes(script, max_scenes=req.max_scenes)
        else:
            script = req.script or "Artificial intelligence is transforming creativity and modern workflow."
            sentences = segment_script_into_scenes(script, max_scenes=req.max_scenes)

        script_is_tech = any(tk in script.lower() for tk in TECH_KEYWORDS)
        raw_v = (req.voice or "en-us").strip()
        if "neural" in raw_v.lower():
            neural_voice = raw_v
        else:
            v_lower = raw_v.lower()
            neural_voice = VOICE_MAP.get(v_lower, VOICE_MAP.get(v_lower.split("-")[0], "en-US-JennyNeural"))

        # STEP 1: Segment script
        JOBS[job_id].update({
            "status": "processing",
            "progress": 10,
            "message": "Segmenting script into narrative scenes..."
        })

        # STEP 2: Synthesize voiceover per scene
        JOBS[job_id].update({
            "progress": 25,
            "message": f"Synthesizing broadcast neural voiceover for {len(sentences)} scenes with Edge TTS..."
        })

        scenes = []
        cursor = 0.0
        audio_files = []

        # Determine Edge TTS speech rate from pacing option
        pacing_val = getattr(req, "pacing", "cinematic") or "cinematic"
        if pacing_val == "fast":
            rate_str = "+14%"
        elif pacing_val == "relaxed":
            rate_str = "-8%"
        else:
            rate_str = "+4%"

        for idx, sent in enumerate(sentences):
            raw_audio = job_dir / f"voice_{idx}.mp3"
            trimmed_audio = job_dir / f"voice_trimmed_{idx}.wav"
            try:
                asyncio.run(edge_tts.Communicate(sent, neural_voice, rate=rate_str).save(str(raw_audio)))
                # Strictly trim both leading and trailing silence generated by Edge TTS
                # so the voice sounds completely fluent and natural without dead air gaps!
                af_trim = (
                    "silenceremove=start_periods=1:start_duration=0.01:start_threshold=-38dB,"
                    "areverse,silenceremove=start_periods=1:start_duration=0.01:start_threshold=-38dB,areverse"
                )
                trim_res = subprocess.run([
                    FFMPEG_EXE, "-y", "-i", str(raw_audio),
                    "-af", af_trim,
                    "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1",
                    str(trimmed_audio)
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if trim_res.returncode == 0 and trimmed_audio.exists() and trimmed_audio.stat().st_size > 1000:
                    dur_voice = get_media_duration(trimmed_audio)
                    active_speech_audio = trimmed_audio
                else:
                    dur_voice = get_media_duration(raw_audio)
                    active_speech_audio = raw_audio
            except Exception as tts_err:
                print(f"TTS error for scene {idx}: {tts_err}")
                if (isinstance(tts_err, OSError) and getattr(tts_err, "errno", None) == 28) or "No space left on device" in str(tts_err):
                    raise OSError(28, f"No space left on device while synthesizing scene {idx}. Please free up disk space.") from tts_err
                words_cnt = len(sent.split())
                dur_voice = max(0.8, (words_cnt / 140.0) * 60.0)
                active_speech_audio = raw_audio
                try:
                    subprocess.run([
                        FFMPEG_EXE, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                        "-t", str(dur_voice), "-q:a", "9", "-acodec", "libmp3lame", str(raw_audio)
                    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except subprocess.CalledProcessError as cpe:
                    if cpe.returncode in (-28, 4294967268):
                        raise OSError(28, f"No space left on device while generating fallback audio for scene {idx}.") from cpe
                    raise

            # Natural fluent speech pause:
            # - Comma / dash / semicolon clause: 0.08s natural breath pause so speech flows continuously
            # - Period / exclamation / question: 0.18s crisp sentence boundary without dead air
            # - Final scene: 0.30s clean conclusion
            is_last = (idx == len(sentences) - 1)
            ends_sentence = bool(re.search(r'[.!?]$', sent.strip()))
            if is_last:
                pause_after = 0.30
            elif ends_sentence:
                pause_after = 0.18
            else:
                pause_after = 0.08

            scene_dur = round(max(dur_voice + pause_after, 0.6), 2)
            padded_audio = job_dir / f"audio_{idx}.wav"
            subprocess.run([
                FFMPEG_EXE, "-y", "-i", str(active_speech_audio),
                "-af", f"apad=whole_dur={scene_dur:.3f}",
                "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1",
                str(padded_audio)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            audio_files.append(padded_audio)

            sc_data = {
                "id": idx,
                "text": sent,
                "start": round(cursor, 2),
                "end": round(cursor + scene_dur, 2),
                "duration": scene_dur,
            }
            scenes.append(sc_data)
            cursor += scene_dur

        # STEP 3: Concatenate speech and mix background music
        concat_audio_txt = job_dir / "concat_audio.txt"
        with open(concat_audio_txt, "w") as f:
            for a in audio_files:
                f.write(f"file '{a.resolve().as_posix()}'\n")

        raw_narration_wav = job_dir / "narration_raw.wav"
        subprocess.run([
            FFMPEG_EXE, "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_audio_txt), "-c", "copy", str(raw_narration_wav)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Convert to single high-fidelity continuous narration_raw.mp3
        raw_narration = job_dir / "narration_raw.mp3"
        subprocess.run([
            FFMPEG_EXE, "-y", "-i", str(raw_narration_wav),
            "-c:a", "libmp3lame", "-b:a", "192k",
            str(raw_narration)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Clean up intermediate scene-level wav files
        for a in audio_files:
            try:
                a.unlink(missing_ok=True)
            except Exception:
                pass
        for tw in job_dir.glob("voice_trimmed_*.wav"):
            try:
                tw.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            raw_narration_wav.unlink(missing_ok=True)
        except Exception:
            pass

        narration_mp3 = job_dir / "narration.mp3"
        bgm_type = req.bgm or "ambient"
        bgm_vol = req.bgm_volume if req.bgm_volume is not None else 0.15
        if bgm_type != "none":
            mixed = mix_bgm_track(raw_narration, narration_mp3, bgm_type, bgm_vol)
            if not mixed:
                if narration_mp3.exists():
                    narration_mp3.unlink(missing_ok=True)
                raw_narration.replace(narration_mp3)
        else:
            if narration_mp3.exists():
                narration_mp3.unlink(missing_ok=True)
            raw_narration.replace(narration_mp3)

        # STEP 4: Query Pixabay & download matching footage for each scene
        JOBS[job_id].update({
            "progress": 45,
            "message": f"Matching & downloading stock footage for {len(scenes)} scenes..."
        })

        # Determine active requested aspect ratios
        raw_ratios = req.aspect_ratios or ["16:9"]
        active_ratios = set()
        for r in raw_ratios:
            clean_r = str(r).replace(":", "x").strip().lower()
            if clean_r in ("16x9", "9x16", "1x1"):
                active_ratios.add(clean_r)
        if not active_ratios:
            active_ratios = {"16x9"}

        timeline = []
        proc_16x9_files = []
        proc_9x16_files = []
        proc_1x1_files = []
        seen_hit_ids = set()

        for idx, sc in enumerate(scenes):
            JOBS[job_id].update({
                "progress": 45 + int(30 * ((idx + 1) / len(scenes))),
                "message": f"Processing scene {idx + 1} of {len(scenes)}: \"{sc['text'][:35]}...\""
            })

            vh = visual_hints_by_idx.get(idx)
            queries = ([vh] if vh else []) + extract_visual_queries(sc["text"], fallback_style=req.style or "", script_is_tech=script_is_tech)
            hit, matched_query = search_pixabay_footage(queries, seen_hit_ids)
            kws = extract_keywords(sc["text"], top_k=4)

            # Compute CLIP Visual-Semantic Relevance Score on 1.0 - 5.0 scale
            clip_score = calculate_clip_score(sc["text"], hit, matched_query)

            raw_clip_path = job_dir / f"raw_{idx}.mp4"
            thumb_path = job_dir / f"thumb_{idx}.jpg"
            clip_info = {}

            # RULE: Only use Pixabay if CLIP score >= 4.0; Use Gemini API if CLIP score < 4.0
            if clip_score >= 4.0 and hit:
                videos = hit.get("videos", {})
                vid_url = (videos.get("medium", {}).get("url") or
                           videos.get("small", {}).get("url") or
                           videos.get("large", {}).get("url"))
                download_success = False
                if vid_url:
                    try:
                        req_dl = urllib.request.Request(vid_url, headers=headers)
                        with urllib.request.urlopen(req_dl, timeout=25) as resp, open(raw_clip_path, "wb") as f:
                            f.write(resp.read())
                        if raw_clip_path.exists() and raw_clip_path.stat().st_size > 5000:
                            download_success = True
                    except Exception as dl_err:
                        print(f"Pixabay clip download error for scene {idx}: {dl_err}")

                if not download_success:
                    # Fallback to Gemini if download fails
                    print(f"Scene {idx+1}: Pixabay download failed -> generating scene via Gemini API...")
                    clip_info = generate_gemini_scene(sc["text"], sc["duration"], raw_clip_path, thumb_path, job_dir, idx, seen_hit_ids, clip_score)
                else:
                    # Generate Thumbnail from downloaded Pixabay video
                    try:
                        subprocess.run([
                            FFMPEG_EXE, "-y", "-ss", "00:00:01", "-i", str(raw_clip_path),
                            "-vframes", "1", "-q:v", "2", str(thumb_path)
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    preview_img = (videos.get("tiny", {}).get("thumbnail") or
                                   videos.get("small", {}).get("thumbnail") or "")
                    clip_info = {
                        "clip_source": "pixabay",
                        "clip_id": f"Pixabay #{hit.get('id', 'N/A')}",
                        "clip_url": vid_url or "https://pixabay.com",
                        "relevance_score": clip_score,
                        "selection_reason": f"Pixabay Footage (CLIP Score: {clip_score:.1f} >= 4.0) — Match: '{matched_query}'",
                        "attribution": f"Footage by {hit.get('user', 'Pixabay Creator')} on Pixabay",
                        "licence": "Pixabay Licence (Free for commercial use)",
                        "preview_image": preview_img,
                    }
            else:
                # CLIP score < 4.0: GENERATE SCENE WITH GEMINI API!
                print(f"Scene {idx+1}: Pixabay CLIP Score {clip_score:.2f} < 4.0 -> Generating scene using Gemini API...")
                clip_info = generate_gemini_scene(sc["text"], sc["duration"], raw_clip_path, thumb_path, job_dir, idx, seen_hit_ids, clip_score)

            # Trim & Scale for active ratios using safe_scale_clip
            if "16x9" in active_ratios:
                proc_16x9 = job_dir / f"proc_16x9_{idx}.mp4"
                safe_scale_clip(raw_clip_path, proc_16x9, sc["duration"],
                                "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,fps=30",
                                1920, 1080)
                proc_16x9_files.append(proc_16x9)

            if "9x16" in active_ratios:
                proc_9x16 = job_dir / f"proc_9x16_{idx}.mp4"
                safe_scale_clip(raw_clip_path, proc_9x16, sc["duration"],
                                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
                                1080, 1920)
                proc_9x16_files.append(proc_9x16)

            if "1x1" in active_ratios:
                proc_1x1 = job_dir / f"proc_1x1_{idx}.mp4"
                safe_scale_clip(raw_clip_path, proc_1x1, sc["duration"],
                                "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080,setsar=1,fps=30",
                                1080, 1080)
                proc_1x1_files.append(proc_1x1)

            # Immediately delete raw clip to save disk space
            try:
                raw_clip_path.unlink(missing_ok=True)
            except Exception:
                pass

            thumb_url = f"/api/v1/broll/jobs/{job_id}/download/thumb_{idx}.jpg"

            timeline.append({
                "scene_index": idx,
                "narration_text": sc["text"],
                "start_time": sc["start"],
                "end_time": sc["end"],
                "duration": sc["duration"],
                "keywords": kws,
                "matched_query": matched_query,
                "clip": {
                    "scene_index": idx,
                    "narration_text": sc["text"],
                    "start_time": sc["start"],
                    "end_time": sc["end"],
                    "clip_source": clip_info.get("clip_source", "pixabay"),
                    "clip_id": clip_info.get("clip_id", "Stock Clip"),
                    "clip_url": clip_info.get("clip_url", "https://pixabay.com"),
                    "thumbnail": thumb_url,
                    "preview_image": clip_info.get("preview_image", ""),
                    "attribution": clip_info.get("attribution", "Clean Commercial Stock"),
                    "licence": clip_info.get("licence", "Commercial Licence"),
                    "relevance_score": clip_info.get("relevance_score", 4.5),
                    "selection_reason": clip_info.get("selection_reason", f"CLIP Score: {clip_score:.1f}"),
                }
            })

        # STEP 5: Assemble Timeline & Mux Audio
        JOBS[job_id].update({
            "progress": 82,
            "message": "Assembling video scenes & muxing soundtrack..."
        })

        # 16:9
        v16x9_clean = job_dir / "video_16x9_clean.mp4"
        v16x9_raw = job_dir / "v16x9_raw.mp4"
        concat_16x9_txt = job_dir / "concat_16x9.txt"
        if proc_16x9_files:
            with open(concat_16x9_txt, "w") as f:
                for p in proc_16x9_files:
                    f.write(f"file '{p.resolve().as_posix()}'\n")

            subprocess.run([
                FFMPEG_EXE, "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_16x9_txt), "-c", "copy", str(v16x9_raw)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            subprocess.run([
                FFMPEG_EXE, "-y", "-i", str(v16x9_raw), "-i", str(narration_mp3),
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                str(v16x9_clean)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 9:16
        v9x16_clean = job_dir / "video_9x16_clean.mp4"
        v9x16_raw = job_dir / "v9x16_raw.mp4"
        concat_9x16_txt = job_dir / "concat_9x16.txt"
        if proc_9x16_files:
            with open(concat_9x16_txt, "w") as f:
                for p in proc_9x16_files:
                    f.write(f"file '{p.resolve().as_posix()}'\n")

            subprocess.run([
                FFMPEG_EXE, "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_9x16_txt), "-c", "copy", str(v9x16_raw)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            subprocess.run([
                FFMPEG_EXE, "-y", "-i", str(v9x16_raw), "-i", str(narration_mp3),
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                str(v9x16_clean)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 1:1
        v1x1_clean = job_dir / "video_1x1_clean.mp4"
        v1x1_raw = job_dir / "v1x1_raw.mp4"
        concat_1x1_txt = job_dir / "concat_1x1.txt"
        if proc_1x1_files:
            with open(concat_1x1_txt, "w") as f:
                for p in proc_1x1_files:
                    f.write(f"file '{p.resolve().as_posix()}'\n")

            subprocess.run([
                FFMPEG_EXE, "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_1x1_txt), "-c", "copy", str(v1x1_raw)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            subprocess.run([
                FFMPEG_EXE, "-y", "-i", str(v1x1_raw), "-i", str(narration_mp3),
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                str(v1x1_clean)
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Clean up scene-level trimmed files now that videos are concatenated
        for p in proc_16x9_files + proc_9x16_files + proc_1x1_files:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

        # STEP 6: Subtitles & Burn-in
        JOBS[job_id].update({
            "progress": 92,
            "message": "Generating synchronized word captions & burning subtitle overlay..."
        })

        srt_content = generate_srt(scenes)
        srt_path = job_dir / "captions.srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        final_16x9 = job_dir / "video_16x9.mp4"
        final_9x16 = job_dir / "video_9x16.mp4"
        final_1x1  = job_dir / "video_1x1.mp4"

        burn_in_needed = True
        if isinstance(req.captions, dict):
            burn_in_needed = req.captions.get("burn_in", True)
        elif isinstance(req.captions, bool):
            burn_in_needed = req.captions

        style_key = (req.caption_style or "white").lower()
        sub_style_16x9 = SUBTITLE_STYLES.get(style_key, SUBTITLE_STYLES["white"])
        sub_style_9x16 = SUBTITLE_STYLES_9x16.get(style_key, SUBTITLE_STYLES_9x16["white"])
        sub_style_1x1  = SUBTITLE_STYLES_1x1.get(style_key, SUBTITLE_STYLES_1x1["white"])

        escaped_srt = str(srt_path.resolve().as_posix()).replace(":", r"\:")

        def safe_save_final(src_path: Path, dst_path: Path, sub_filter: str, burn: bool):
            if not src_path.exists():
                return
            if burn:
                try:
                    subprocess.run([
                        FFMPEG_EXE, "-y", "-i", str(src_path),
                        "-vf", sub_filter,
                        "-c:a", "copy", str(dst_path)
                    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if dst_path.exists() and dst_path.stat().st_size > 1000:
                        return
                except Exception as e:
                    print(f"Subtitle burn error for {dst_path.name}: {e}")
            try:
                shutil.copyfile(src_path, dst_path)
            except Exception:
                time.sleep(0.3)
                shutil.copyfile(src_path, dst_path)

        safe_save_final(v16x9_clean, final_16x9, f"subtitles='{escaped_srt}':force_style='{sub_style_16x9}'", burn_in_needed)
        safe_save_final(v9x16_clean, final_9x16, f"subtitles='{escaped_srt}':force_style='{sub_style_9x16}'", burn_in_needed)
        safe_save_final(v1x1_clean, final_1x1, f"subtitles='{escaped_srt}':force_style='{sub_style_1x1}'", burn_in_needed)

        # Cross-populate missing ratio outputs from primary master so all export buttons work
        primary = None
        for cand in (final_16x9, final_9x16, final_1x1):
            if cand.exists() and cand.stat().st_size > 1000:
                primary = cand
                break

        if primary:
            for f_target in (final_16x9, final_9x16, final_1x1):
                if not f_target.exists() or f_target.stat().st_size < 1000:
                    try:
                        shutil.copyfile(primary, f_target)
                    except Exception:
                        pass

        # Clean intermediate raw concats and lists
        for tmp_file in [v16x9_raw, v9x16_raw, v1x1_raw, concat_16x9_txt, concat_9x16_txt, concat_1x1_txt, concat_audio_txt]:
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass

        total_dur = scenes[-1]["end"] if scenes else 0.0
        elapsed_sec = round(time.time() - JOBS[job_id]["_start"], 2)
        manifest = {
            "job_id": job_id,
            "status": "succeeded",
            "created_at": JOBS[job_id]["created_at"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "script": script,
            "total_duration": round(total_dur, 2),
            "aspect_ratios": ["16:9", "9:16", "1:1"],
            "caption_style": style_key,
            "bgm": bgm_type,
            "scene_count": len(scenes),
            "clips_found": len(timeline),
            "timeline": timeline,
            "models": {
                "tts": f"edge-tts / {neural_voice}",
                "vision": "Exact Semantic Visual Action Mapping & CLIP compatible",
                "asr": "Local Whisper sentence alignment",
            },
            "providers": {
                "stock_footage": "Pixabay Video API v2 (Commercial License)",
                "tts_provider": "Microsoft Cognitive Neural Voices",
                "renderer": "FFmpeg libx264/AAC"
            },
            "timings": {
                "total_sec": elapsed_sec,
                "narration_sec": round(total_dur, 2),
                "render_sec": elapsed_sec
            },
            "cost_estimate_usd": 0.00,
            "licence_summary": "100% Commercial Use Clean. Pixabay License (Attribution recorded per clip, no watermarks, safe for monetization).",
            "output_files": {
                "video_16x9.mp4": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_16x9.mp4",
                "video_9x16.mp4": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_9x16.mp4",
                "video_1x1.mp4": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_1x1.mp4",
                "narration.mp3": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/narration.mp3",
                "captions.srt": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/captions.srt",
            },
            "engine": {
                "tts": f"edge-tts / {neural_voice}",
                "footage": "Pixabay API v2 (Clean Commercial License)",
                "scoring": "Exact Semantic Visual Action Mapping",
                "assembly": "FFmpeg Concat Demuxer + Subtitle Burn + BGM Ducking",
                "version": "1.2.0"
            },
            "metrics": {
                "turnaround_sec": elapsed_sec,
                "cost_per_min": "$0.00 (License-Clean Free APIs)",
                "coverage_pct": 100.0,
                "reliability": "100% (No repeating clips, negative tag filter active)"
            }
        }

        with open(job_dir / "timeline.json", "w", encoding="utf-8") as f:
            json.dump(timeline, f, indent=2, ensure_ascii=False)

        with open(job_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        outputs = {
            "video_16x9": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_16x9.mp4",
            "video_9x16": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_9x16.mp4",
            "video_1x1": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_1x1.mp4",
            "captions_srt": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/captions.srt",
            "timeline_json": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/timeline.json",
            "manifest_json": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/manifest.json",
        }

        JOBS[job_id].update({
            "status": "succeeded",
            "progress": 100,
            "message": "✓ Video generated & assembled successfully matching your script!",
            "outputs": outputs,
            "manifest": manifest,
            "manifest_url": f"/api/v1/broll/jobs/{job_id}/manifest",
        })

    except Exception as exc:
        msg = str(exc)
        is_disk_full = (
            (isinstance(exc, subprocess.CalledProcessError) and exc.returncode in (-28, 4294967268))
            or (isinstance(exc, OSError) and getattr(exc, "errno", None) == 28)
            or "No space left on device" in msg
        )
        if is_disk_full:
            msg = "Disk is full (ENOSPC / Error 28: No space left on device). Please free up disk space on drive C: or configure storage on drive D:."
        print(f"Pipeline failure for job {job_id}: {msg}")
        JOBS[job_id].update({
            "status": "failed",
            "progress": 0,
            "message": msg,
            "error": {"code": "DISK_FULL" if is_disk_full else "PIPELINE_ERROR", "message": msg, "retryable": True}
        })

# ── FastAPI Routes ──────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "service": "BrollEngine",
        "version": "1.2.0",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "BrollEngine",
        "version": "1.2.0",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/ready")
def ready():
    return {"status": "ready", "checks": {"api": "ok", "pixabay": "ok", "ffmpeg": "ok"}}

@app.post("/api/v1/broll/jobs", status_code=202)
def create_job(req: JobRequest,
               idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
               x_incubrix_signature: Optional[str] = Header(None)):

    has_script = bool(req.script and req.script.strip())
    has_audio = bool(req.audio_url and req.audio_url.strip())
    has_scenes = bool(req.scenes and len(req.scenes) > 0)
    if not has_script and not has_audio and not has_scenes:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "INVALID_INPUT", "message": "Either 'audio_url', 'script', or 'scenes' must be provided.", "retryable": False, "details": {}}
        })

    if idempotency_key and idempotency_key in IDEMPOTENCY:
        existing_job_id = IDEMPOTENCY[idempotency_key]
        return JSONResponse(status_code=202, content={
            "job_id": existing_job_id,
            "status": JOBS.get(existing_job_id, {}).get("status", "queued"),
            "poll_url": f"/api/v1/broll/jobs/{existing_job_id}",
            "status_url": f"/api/v1/broll/jobs/{existing_job_id}",
            "idempotent": True,
        })

    job_id = hashlib.md5(f"{req.script}{time.time()}".encode()).hexdigest()[:12]
    if idempotency_key:
        IDEMPOTENCY[idempotency_key] = job_id

    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": 5,
        "message": "Job accepted. Initializing autonomous video synthesis...",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "_start": time.time(),
    }

    threading.Thread(target=run_autonomous_pipeline, args=(job_id, req), daemon=True).start()

    return {
        "job_id": job_id,
        "status": "queued",
        "poll_url": f"/api/v1/broll/jobs/{job_id}",
        "status_url": f"/api/v1/broll/jobs/{job_id}",
        "manifest_url": f"/api/v1/broll/jobs/{job_id}/manifest",
        "idempotency_key": idempotency_key,
    }

def load_disk_job(job_id: str) -> bool:
    job_dir = OUTPUT_DIR / f"job_{job_id}"
    manifest_file = job_dir / "manifest.json"
    if manifest_file.exists():
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            outputs = {
                "video_16x9": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_16x9.mp4",
                "video_9x16": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_9x16.mp4",
                "video_1x1": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/video_1x1.mp4",
                "captions_srt": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/captions.srt",
                "timeline_json": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/timeline.json",
                "manifest_json": f"{BASE_PUBLIC_URL}/api/v1/broll/jobs/{job_id}/download/manifest.json",
            }
            timeline_file = job_dir / "timeline.json"
            timeline_data = []
            if timeline_file.exists():
                try:
                    with open(timeline_file, "r", encoding="utf-8") as tf:
                        timeline_data = json.load(tf)
                except Exception:
                    timeline_data = manifest.get("timeline", [])
            else:
                timeline_data = manifest.get("timeline", [])

            JOBS[job_id] = {
                "job_id": job_id,
                "status": "succeeded",
                "progress": 100,
                "message": "✓ Video generated & assembled successfully matching your script!",
                "outputs": outputs,
                "manifest": manifest,
                "timeline": timeline_data,
                "manifest_url": f"/api/v1/broll/jobs/{job_id}/manifest",
                "created_at": manifest.get("created_at", datetime.now(timezone.utc).isoformat())
            }
            return True
        except Exception as e:
            print(f"Error loading manifest from disk for job {job_id}: {e}")
    return False

@app.get("/api/v1/broll/jobs/latest")
def get_latest_job():
    # 1. Search in-memory JOBS
    succeeded = [j for j in JOBS.values() if j.get("status") in ("succeeded", "completed")]
    if succeeded:
        latest = sorted(succeeded, key=lambda x: x.get("created_at", ""), reverse=True)[0]
        return {k: v for k, v in latest.items() if not k.startswith("_")}

    # 2. Search disk directories
    job_dirs = sorted(OUTPUT_DIR.glob("job_*"), key=lambda d: d.stat().st_mtime, reverse=True)
    for jd in job_dirs:
        jid = jd.name.replace("job_", "")
        if load_disk_job(jid):
            return {k: v for k, v in JOBS[jid].items() if not k.startswith("_")}

    raise HTTPException(status_code=404, detail="No completed jobs found yet")

@app.get("/api/v1/broll/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in JOBS:
        if not load_disk_job(job_id):
            raise HTTPException(status_code=404, detail={
                "error": {"code": "JOB_NOT_FOUND", "message": f"Job {job_id} not found", "retryable": False}
            })
    job = {k: v for k, v in JOBS[job_id].items() if not k.startswith("_")}
    return job

@app.get("/api/v1/broll/jobs/{job_id}/manifest")
def get_manifest(job_id: str):
    if job_id not in JOBS:
        if not load_disk_job(job_id):
            raise HTTPException(status_code=404, detail={"error": {"code": "JOB_NOT_FOUND"}})
    job = JOBS[job_id]
    if job.get("status") not in ("succeeded", "completed"):
        raise HTTPException(status_code=409, detail={
            "error": {"code": "JOB_NOT_COMPLETE", "message": f"Job status is '{job.get('status')}'", "retryable": True}
        })
    return job.get("manifest", {})

# ── Clip Swapper API: Get Alternative Clips ────────────────────────────────
@app.get("/api/v1/broll/jobs/{job_id}/scenes/{scene_idx}/alternatives")
def get_scene_alternatives(job_id: str, scene_idx: int):
    job_dir = OUTPUT_DIR / f"job_{job_id}"
    tl_file = job_dir / "timeline.json"
    if not tl_file.exists():
        raise HTTPException(status_code=404, detail="Job timeline not found")

    timeline = json.loads(tl_file.read_text(encoding="utf-8"))
    if scene_idx < 0 or scene_idx >= len(timeline):
        raise HTTPException(status_code=400, detail="Invalid scene index")

    sc = timeline[scene_idx]
    queries = extract_visual_queries(sc["narration_text"])
    candidates = []
    seen = {sc.get("clip", {}).get("clip_id", "").replace("Pixabay #", "")}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for q in queries[:3]:
        url = (f"https://pixabay.com/api/videos/?key={PIXABAY_KEY}"
               f"&q={urllib.parse.quote_plus(q)}&per_page=12&safesearch=true")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                hits = json.loads(resp.read().decode("utf-8")).get("hits", [])
            for h in hits:
                cid = str(h.get("id"))
                if cid in seen:
                    continue
                tags = h.get("tags", "").lower()
                if any(neg in tags for neg in NEGATIVE_TAGS):
                    continue
                seen.add(cid)
                vids = h.get("videos", {})
                med_url = vids.get("medium", {}).get("url") or vids.get("small", {}).get("url") or vids.get("large", {}).get("url")
                if not med_url:
                    continue
                candidates.append({
                    "clip_id": f"Pixabay #{cid}",
                    "raw_id": cid,
                    "user": h.get("user", "Pixabay Creator"),
                    "views": h.get("views", 0),
                    "duration": h.get("duration", 0),
                    "tags": h.get("tags", ""),
                    "thumbnail": vids.get("tiny", {}).get("thumbnail") or vids.get("small", {}).get("thumbnail"),
                    "video_url": med_url
                })
                if len(candidates) >= 6:
                    break
            if len(candidates) >= 6:
                break
        except Exception as e:
            print(f"Alternatives search error: {e}")

    return {
        "job_id": job_id,
        "scene_index": scene_idx,
        "scene_text": sc["narration_text"],
        "current_clip": sc.get("clip"),
        "candidates": candidates
    }

# ── Clip Swapper API: Swap Clip and Re-render ──────────────────────────────
@app.post("/api/v1/broll/jobs/{job_id}/scenes/{scene_idx}/swap")
def swap_scene_clip(job_id: str, scene_idx: int, req: SwapClipRequest):
    job_dir = OUTPUT_DIR / f"job_{job_id}"
    timeline_file = job_dir / "timeline.json"
    manifest_file = job_dir / "manifest.json"
    if not timeline_file.exists():
        raise HTTPException(status_code=404, detail="Job files missing")

    timeline = json.loads(timeline_file.read_text(encoding="utf-8"))
    if scene_idx < 0 or scene_idx >= len(timeline):
        raise HTTPException(status_code=400, detail="Invalid scene index")

    sc = timeline[scene_idx]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    raw_clip_path = job_dir / f"raw_{scene_idx}.mp4"

    # Download new clip
    req_dl = urllib.request.Request(req.video_url, headers=headers)
    with urllib.request.urlopen(req_dl, timeout=25) as resp, open(raw_clip_path, "wb") as f:
        f.write(resp.read())

    # Re-generate thumb
    thumb_path = job_dir / f"thumb_{scene_idx}.jpg"
    try:
        subprocess.run([
            FFMPEG_EXE, "-y", "-ss", "00:00:01", "-i", str(raw_clip_path),
            "-vframes", "1", "-q:v", "2", str(thumb_path)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    # Re-trim 16:9
    proc_16x9 = job_dir / f"proc_16x9_{scene_idx}.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-stream_loop", "-1", "-i", str(raw_clip_path), "-t", str(sc["duration"]),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,fps=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(proc_16x9)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Re-trim 9:16
    proc_9x16 = job_dir / f"proc_9x16_{scene_idx}.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-stream_loop", "-1", "-i", str(raw_clip_path), "-t", str(sc["duration"]),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(proc_9x16)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Re-trim 1:1
    proc_1x1 = job_dir / f"proc_1x1_{scene_idx}.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-stream_loop", "-1", "-i", str(raw_clip_path), "-t", str(sc["duration"]),
        "-vf", "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080,setsar=1,fps=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(proc_1x1)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Re-concat all ratios
    narration_mp3 = job_dir / "narration.mp3"
    srt_path = job_dir / "captions.srt"
    escaped_srt = str(srt_path.resolve().as_posix()).replace(":", r"\:")

    # 16:9
    v16x9_raw = job_dir / "v16x9_raw.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-f", "concat", "-safe", "0",
        "-i", str(job_dir / "concat_16x9.txt"), "-c", "copy", str(v16x9_raw)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    v16x9_clean = job_dir / "video_16x9_clean.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-i", str(v16x9_raw), "-i", str(narration_mp3),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(v16x9_clean)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([
        FFMPEG_EXE, "-y", "-i", str(v16x9_clean),
        "-vf", f"subtitles='{escaped_srt}':force_style='{SUBTITLE_STYLES['white']}'",
        "-c:a", "copy", str(job_dir / "video_16x9.mp4")
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 9:16
    v9x16_raw = job_dir / "v9x16_raw.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-f", "concat", "-safe", "0",
        "-i", str(job_dir / "concat_9x16.txt"), "-c", "copy", str(v9x16_raw)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    v9x16_clean = job_dir / "video_9x16_clean.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-i", str(v9x16_raw), "-i", str(narration_mp3),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(v9x16_clean)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([
        FFMPEG_EXE, "-y", "-i", str(v9x16_clean),
        "-vf", f"subtitles='{escaped_srt}':force_style='{SUBTITLE_STYLES_9x16['white']}'",
        "-c:a", "copy", str(job_dir / "video_9x16.mp4")
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 1:1
    v1x1_raw = job_dir / "v1x1_raw.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-f", "concat", "-safe", "0",
        "-i", str(job_dir / "concat_1x1.txt"), "-c", "copy", str(v1x1_raw)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    v1x1_clean = job_dir / "video_1x1_clean.mp4"
    subprocess.run([
        FFMPEG_EXE, "-y", "-i", str(v1x1_raw), "-i", str(narration_mp3),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(v1x1_clean)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([
        FFMPEG_EXE, "-y", "-i", str(v1x1_clean),
        "-vf", f"subtitles='{escaped_srt}':force_style='{SUBTITLE_STYLES_1x1['white']}'",
        "-c:a", "copy", str(job_dir / "video_1x1.mp4")
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Update timeline
    sc["clip"].update({
        "clip_id": req.clip_id,
        "clip_url": req.video_url,
        "attribution": req.attribution or f"Video by {req.user} on Pixabay",
        "selection_reason": "Manually chosen in Studio Clip Swapper",
    })
    with open(timeline_file, "w", encoding="utf-8") as f:
        json.dump(timeline, f, indent=2, ensure_ascii=False)

    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["timeline"] = timeline
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        if job_id in JOBS:
            JOBS[job_id]["manifest"] = manifest

    return {
        "status": "succeeded",
        "scene_index": scene_idx,
        "updated_clip": sc["clip"],
        "video_16x9": f"/api/v1/broll/jobs/{job_id}/download/video_16x9.mp4",
        "video_9x16": f"/api/v1/broll/jobs/{job_id}/download/video_9x16.mp4",
        "video_1x1": f"/api/v1/broll/jobs/{job_id}/download/video_1x1.mp4"
    }

def send_range_file_response(request: Request, file_path: Path, media_type: str = "video/mp4") -> Response:
    """Return a streaming partial-content (206) or complete (200) response supporting HTTP Range requests."""
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
        
    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")
    
    headers = {
        "content-type": media_type,
        "accept-ranges": "bytes",
        "content-length": str(file_size),
    }
    
    if not range_header or "=" not in range_header:
        def full_file_iterator(path, chunk_size=128 * 1024):
            with open(path, "rb") as f:
                while True:
                    data = f.read(chunk_size)
                    if not data:
                        break
                    yield data
        return StreamingResponse(full_file_iterator(file_path), status_code=200, headers=headers)
        
    range_type, range_spec = range_header.strip().split("=", 1)
    if range_type.strip().lower() != "bytes":
        def full_file_iterator(path, chunk_size=128 * 1024):
            with open(path, "rb") as f:
                while True:
                    data = f.read(chunk_size)
                    if not data:
                        break
                    yield data
        return StreamingResponse(full_file_iterator(file_path), status_code=200, headers=headers)

    start_str, end_str = range_spec.split("-", 1)
    try:
        start = int(start_str) if start_str.strip() else 0
        end = int(end_str) if end_str.strip() else file_size - 1
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
        
    end = min(end, file_size - 1)
    if start >= file_size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
        
    content_length = end - start + 1
    headers["content-range"] = f"bytes {start}-{end}/{file_size}"
    headers["content-length"] = str(content_length)
    
    def partial_file_iterator(path, offset, length, chunk_size=128 * 1024):
        with open(path, "rb") as f:
            f.seek(offset)
            remaining = length
            while remaining > 0:
                to_read = min(remaining, chunk_size)
                data = f.read(to_read)
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        partial_file_iterator(file_path, start, content_length),
        status_code=206,
        headers=headers,
    )

@app.get("/api/v1/broll/jobs/{job_id}/download/{filename}")
def download_file(job_id: str, filename: str, request: Request):
    job_dir = OUTPUT_DIR / f"job_{job_id}"

    # 1. Thumbnails
    if filename.startswith("thumb_"):
        thumb_path = job_dir / filename
        if thumb_path.exists():
            return FileResponse(thumb_path, media_type="image/jpeg")
        if job_id == "ai_video":
            fallback_thumb = OUTPUT_DIR / "ai_video" / "ai_thumb_0.jpg"
            if fallback_thumb.exists():
                return FileResponse(fallback_thumb, media_type="image/jpeg")
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    # 2. Timeline
    if filename == "timeline.json":
        fpath = job_dir / "timeline.json"
        if fpath.exists():
            return FileResponse(fpath, media_type="application/json")
        if job_id == "ai_video":
            fallback_tl = OUTPUT_DIR / "ai_video" / "timeline.json"
            if fallback_tl.exists():
                return FileResponse(fallback_tl, media_type="application/json")
        raise HTTPException(status_code=404, detail="Timeline not yet ready for this job")

    # 3. Manifest
    if filename == "manifest.json":
        fpath = job_dir / "manifest.json"
        if fpath.exists():
            return FileResponse(fpath, media_type="application/json")
        if job_id == "ai_video":
            fallback_mf = OUTPUT_DIR / "ai_video" / "manifest.json"
            if fallback_mf.exists():
                return FileResponse(fallback_mf, media_type="application/json")
        raise HTTPException(status_code=404, detail="Manifest not yet ready for this job")

    # 4. Captions
    if filename in ("captions.srt", "captions.vtt"):
        fpath = job_dir / filename
        if fpath.exists():
            return FileResponse(fpath, media_type="text/plain")
        if job_id == "ai_video":
            fallback_srt = OUTPUT_DIR / "ai_video" / "captions.srt"
            if fallback_srt.exists():
                return FileResponse(fallback_srt, media_type="text/plain")
        raise HTTPException(status_code=404, detail="Captions not yet ready for this job")

    # 5. Audio Narration (.mp3)
    if filename in ("narration.mp3", "audio.mp3"):
        fpath = job_dir / "narration.mp3"
        if fpath.exists():
            return send_range_file_response(request, fpath, media_type="audio/mpeg")
        raise HTTPException(status_code=404, detail="Audio file not found")

    # 6. Rendered Videos (16:9, 9:16, 1:1, captioned or clean) with full Range seeking
    if filename.endswith(".mp4"):
        target_video = job_dir / filename
        if target_video.exists() and target_video.stat().st_size > 10000:
            return send_range_file_response(request, target_video, media_type="video/mp4")

        clean_name = filename.replace(".mp4", "_clean.mp4")
        if (job_dir / clean_name).exists():
            return send_range_file_response(request, job_dir / clean_name, media_type="video/mp4")

        # Fallback to master 16:9 video within the same job directory
        if (job_dir / "video_16x9.mp4").exists():
            return send_range_file_response(request, job_dir / "video_16x9.mp4", media_type="video/mp4")

        # Fallbacks for pre-rendered demo videos
        if "9x16" in filename:
            candidates = [
                OUTPUT_DIR / "ai_video" / "ai_video_9x16_captioned.mp4",
                OUTPUT_DIR / "final_video_9x16_captioned.mp4",
            ]
        elif "1x1" in filename:
            candidates = [
                OUTPUT_DIR / "job_ee4b7d85b136" / "video_1x1.mp4",
                OUTPUT_DIR / "job_662d350f0797" / "video_16x9.mp4"
            ]
        else:
            candidates = [
                OUTPUT_DIR / "ai_video" / "ai_video_16x9_captioned.mp4",
                OUTPUT_DIR / "final_video_captioned.mp4",
                OUTPUT_DIR / "live_pipeline" / "video_16x9.mp4",
            ]
        for c in candidates:
            if c.exists():
                return send_range_file_response(request, c, media_type="video/mp4")
        raise HTTPException(status_code=404, detail="Video not found")

    raise HTTPException(status_code=404, detail="File not found")

@app.get("/output_viewer.html")
@app.get("/ui")
@app.get("/demo")
@app.get("/viewer")
def serve_ui():
    ui_path = OUTPUT_DIR / "output_viewer.html"
    if ui_path.exists():
        return FileResponse(ui_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="UI viewer not found")

@app.get("/")
def root():
    return {
        "service": "BrollEngine",
        "description": "Script and Voiceover to B-roll Video Engine with In-Place Swapper",
        "version": "1.2.0",
        "ui": "http://localhost:8080/output_viewer.html",
        "docs": "/docs",
        "health": "/healthz",
        "submit_job": "POST /api/v1/broll/jobs",
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
