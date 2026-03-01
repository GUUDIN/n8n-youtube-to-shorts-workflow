"""
Video Shorts Service
====================
Replaces the Swiftia API with a free, self-hosted pipeline:
  - yt-dlp: downloads YouTube videos
  - faster-whisper: transcribes with word-level timestamps
  - Gemini: analyzes transcript to find best short segments
  - ffmpeg: cuts video + burns in styled captions

API is 1:1 compatible with Swiftia so the n8n workflow needs only a URL change.
"""

import os
import re
import json
import uuid
import random
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import yt_dlp
from faster_whisper import WhisperModel
import google.generativeai as genai

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STORAGE = Path(os.getenv("STORAGE_PATH", "/data"))
VIDEOS = STORAGE / "videos"
RENDERS = STORAGE / "renders"
VIDEOS.mkdir(parents=True, exist_ok=True)
RENDERS.mkdir(parents=True, exist_ok=True)

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
BASE_URL = os.getenv("BASE_URL", "http://video-shorts:8000")
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "")  # e.g. socks5://user:pass@host:port

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("video-shorts")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Video Shorts Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://clipwave.app", "http://localhost:5678"],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)
app.mount("/videos", StaticFiles(directory=str(RENDERS)), name="videos")

# ---------------------------------------------------------------------------
# In-memory stores (fine for this single-user service)
# ---------------------------------------------------------------------------
jobs: dict = {}
renders: dict = {}
pipeline_status: dict = {}  # {pipeline_id: {step, detail, error, ...}}

# ---------------------------------------------------------------------------
# Lazy-loaded singletons
# ---------------------------------------------------------------------------
_whisper: Optional[WhisperModel] = None


def get_whisper() -> WhisperModel:
    global _whisper
    if _whisper is None:
        log.info("Loading Whisper model '%s' …", WHISPER_MODEL_SIZE)
        _whisper = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
        log.info("Whisper model loaded.")
    return _whisper


def get_gemini():
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(GEMINI_MODEL_NAME)


# ===========================================================================
# 1. YouTube Download
# ===========================================================================
def download_youtube(video_id: str, output_dir: Path) -> tuple[str, dict]:
    """Download a YouTube video; returns (local_path, yt_info_dict).

    yt_info_dict includes at minimum: title, uploader, webpage_url.
    If the file is already cached, metadata is fetched without re-downloading.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    expected = output_dir / f"{video_id}.mp4"
    _cookies_file = "/app/yt-cookies.txt"
    _cookie_opt = {"cookiefile": _cookies_file} if os.path.exists(_cookies_file) else {}
    _proxy_opt = {"proxy": YTDLP_PROXY} if YTDLP_PROXY else {}
    # Use ios+web player clients to bypass sig/n challenge restrictions on cloud IPs.
    # ios client returns HLS streams (no sig/n challenge needed).
    # web client is kept as fallback and supports cookies for private/age-restricted videos.
    _extractor_args = {"youtube": {"player_client": ["ios", "web"]}}
    _meta_only_opts = {"quiet": True, "no_warnings": True, "extractor_args": _extractor_args, **_cookie_opt, **_proxy_opt}

    if expected.exists() and expected.stat().st_size > 1_000_000:
        log.info("Using cached video: %s", expected)
        try:
            with yt_dlp.YoutubeDL(_meta_only_opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
        except Exception as exc:
            log.warning("Metadata fetch failed for cached %s: %s", video_id, exc)
            info = {}
        return str(expected), info

    ydl_opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "extractor_args": _extractor_args,
        **_cookie_opt,
        **_proxy_opt,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
        # yt-dlp may merge to mp4
        vid_id = info.get("id", video_id)
        expected = output_dir / f"{vid_id}.mp4"
        if expected.exists():
            return str(expected), info
        # Fallback: find whatever file was downloaded
        files = list(output_dir.glob(f"{vid_id}.*"))
        if files:
            return str(files[0]), info
        raise FileNotFoundError(f"Downloaded file not found for {video_id}")


# ===========================================================================
# 2. Whisper Transcription
# ===========================================================================
def transcribe_video(video_path: str, language: str | None = None) -> list[dict]:
    """Transcribe with word-level timestamps via faster-whisper."""
    model = get_whisper()
    transcribe_kwargs = dict(
        word_timestamps=True,
        vad_filter=False,
    )
    if language:
        transcribe_kwargs["language"] = language
    segments_iter, info = model.transcribe(
        video_path,
        **transcribe_kwargs,
    )
    results = []
    for seg in segments_iter:
        results.append(
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "words": (
                    [
                        {"word": w.word.strip(), "start": w.start, "end": w.end}
                        for w in (seg.words or [])
                    ]
                ),
            }
        )
    return results


# ===========================================================================
# 2b. Gemini – Transcript Correction
# ===========================================================================
CORRECTION_PROMPT = """Você é um editor de legendas profissional. Corrija APENAS erros nesta transcrição.
O áudio é em {language}.

Erros comuns para corrigir:
- Nomes de marcas e substantivos próprios: corrigir grafia exata
  Exemplos: "Ran" → "RAM", "Rampade" → "Rampage", "HAN" → "RAM",
  "ficapis" → "picapes", "Piracicabo" → "Piracicaba", "agrone" → "agrônomo"
- Termos técnicos garblados pelo speech-to-text
- Palavras que não fazem sentido no contexto

REGRAS OBRIGATÓRIAS:
- Mantenha o EXATO mesmo número de palavras (conte antes e depois!)
- Só SUBSTITUA uma palavra errada por UMA palavra correta
- NÃO junte, separe, adicione ou remova palavras
- NÃO mude números — deixe-os exatamente como estão
- Se a palavra está correta, mantenha exatamente como está
- Retorne APENAS o texto corrigido, nada mais (sem explicações)

Transcrição original:
{text}"""


def correct_transcript_segment(words: list[dict], language: str = "pt") -> list[dict]:
    """Use Gemini to fix brand names, numbers, and misrecognized words."""
    if not words:
        return words
    text = " ".join(w["word"] for w in words)
    try:
        model = get_gemini()
        resp = model.generate_content(
            CORRECTION_PROMPT.format(language=language, text=text)
        )
        corrected = resp.text.strip()
        corrected_words = corrected.split()
        # Only apply if word count matches (safety check)
        if len(corrected_words) == len(words):
            for i, cw in enumerate(corrected_words):
                words[i]["word"] = cw
            log.info("Transcript corrected by Gemini")
        else:
            log.warning(
                "Gemini word count mismatch (%d vs %d), keeping original",
                len(corrected_words),
                len(words),
            )
    except Exception as e:
        log.warning("Gemini correction failed, keeping original: %s", e)

    # Post-processing: fix number/percentage spacing issues that
    # Gemini can't fix due to the same-word-count constraint
    for w in words:
        t = w["word"]
        # Fix " %" → "%" glued to preceding number
        t = re.sub(r"^%$", "%", t)  # standalone % stays
        # Remove stray commas/spaces in numbers like "2,500" that were
        # split across words — handled below at chunk level
        # Fix common patterns: "8" followed by "%" in next word
        w["word"] = t

    # Merge number fragments: if word[i] is digits and word[i+1] is "%"
    # or "," + digits, merge them
    i = 0
    while i < len(words) - 1:
        curr = words[i]["word"]
        nxt = words[i + 1]["word"]
        # "8" + "%" → "8%"
        if re.match(r"^\d+$", curr) and nxt == "%":
            words[i]["word"] = curr + "%"
            words[i]["end"] = words[i + 1]["end"]
            words.pop(i + 1)
            continue
        # "2" + ",500" or "2," + "500" → merge into one number word
        if re.match(r"^\d+,?$", curr) and re.match(r"^,?\d+$", nxt):
            merged = (curr.rstrip(",") + nxt.lstrip(","))
            # Only merge if result looks like a number
            if re.match(r"^\d+$", merged):
                words[i]["word"] = merged
                words[i]["end"] = words[i + 1]["end"]
                words.pop(i + 1)
                continue
        # "R$" + number → "R$" stays separate (OK), but "R" + "$" merge
        if curr == "R" and nxt.startswith("$"):
            words[i]["word"] = "R" + nxt
            words[i]["end"] = words[i + 1]["end"]
            words.pop(i + 1)
            continue
        i += 1

    return words


# ===========================================================================
# 3. Gemini – Intelligent Segment Selection
# ===========================================================================
SEGMENT_PROMPT = """Você é um editor de vídeo especialista em YouTube Shorts para podcasts.

Analise esta transcrição de um podcast e encontre os **{num_shorts}** melhores trechos para Shorts.

## REGRA FUNDAMENTAL DE CORTE
Cada trecho DEVE seguir a estrutura de um podcast:
1. **COMEÇAR** com a PERGUNTA do entrevistador/host (ou o início natural de um novo tópico)
2. **TERMINAR** quando o convidado COMPLETA seu raciocínio — num ponto final, numa conclusão clara
3. NUNCA cortar no meio de uma frase ou pensamento incompleto
4. NUNCA começar com o convidado já respondendo (sem contexto da pergunta)
5. O espectador precisa entender O QUE FOI PERGUNTADO e OUVIR A RESPOSTA COMPLETA

## Duração
Cada trecho DEVE ter entre {dur_min} e {dur_max} segundos.
Se a resposta completa for longa demais, inclua pelo menos o PRIMEIRO ponto completo da resposta.

## Priorize
- Perguntas que geram respostas reveladoras, engraçadas ou controversas
- Momentos de insight onde o convidado compartilha algo inesperado
- Histórias pessoais com começo e conclusão
- Frases de impacto que funcionam como gancho nos primeiros 2 segundos
- Respostas com conclusão clara ("...e foi assim que...", "...por isso eu digo...")

Duração do vídeo: {duration:.0f} segundos

Transcrição (com timestamps):
{transcript}

Retorne APENAS um JSON array (sem markdown, sem explicação):
[
  {{
    "title": "Título curto e descritivo em português",
    "start_time": 12.5,
    "end_time": 45.0,
    "reason": "Por que este trecho funciona como Short"
  }}
]

Regras:
- start_time e end_time são números em segundos
- Cada trecho {dur_min}-{dur_max} segundos
- Trechos NÃO podem se sobrepor
- start_time = momento em que o host começa a pergunta
- end_time = momento em que o convidado termina o pensamento (pausa natural)
- Retorne APENAS JSON válido"""


def analyze_transcript(
    transcript: list[dict], duration: float,
    target_dur_min: int = 45, target_dur_max: int = 59,
) -> list[dict]:
    """Ask Gemini to pick the best segments from the transcript."""
    # Build timestamped text
    lines = []
    for seg in transcript:
        lines.append(f"[{seg['start']:.1f}s–{seg['end']:.1f}s] {seg['text']}")
    full_text = "\n".join(lines)

    # Decide how many shorts based on duration
    num = min(5, max(1, int(duration // 120)))

    prompt = SEGMENT_PROMPT.format(
        num_shorts=num,
        duration=duration,
        dur_min=target_dur_min,
        dur_max=target_dur_max,
        transcript=full_text[:15000],  # cap to avoid token limits
    )

    model = get_gemini()
    resp = model.generate_content(prompt)
    text = resp.text.strip()

    # Strip markdown fences if present
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        text = match.group(0)

    segments = json.loads(text)

    # Validate & clamp
    valid = []
    for s in segments:
        st, et = float(s["start_time"]), float(s["end_time"])
        if et - st < target_dur_min * 0.5:
            continue
        if et - st > target_dur_max:
            et = st + target_dur_max
        valid.append({**s, "start_time": st, "end_time": et})

    return valid


# ===========================================================================
# 4. Caption Presets & ffmpeg – Cut & Caption
# ===========================================================================
CAPTION_PRESETS = {
    "capcut-bold": {
        "name": "CapCut Bold",
        "description": "🎬 Texto branco grande, destaque amarelo dourado — estilo CapCut/Captions",
        "preview_img": "capcut-bold.png",
        "font": "Montserrat ExtraBold",
        "font_size": 72,
        "primary_color": "&H00FFFFFF",
        "highlight_color": "&H0000DDFF",
        "outline_color": "&H00000000",
        "back_color": "&HA0000000",
        "bold": -1,
        "outline": 5,
        "shadow": 2,
        "margin_v": 500,
    },
    "clean-minimal": {
        "name": "Clean Minimal",
        "description": "\u2728 Texto branco suave, fonte fina, sem destaque \u2014 elegante",
        "preview_img": "clean-minimal.png",
        "font": "Inter",
        "font_size": 60,
        "primary_color": "&H00FFFFFF",
        "highlight_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "back_color": "&H80000000",
        "bold": 0,
        "outline": 3,
        "shadow": 1,
        "margin_v": 450,
    },
    "neon-glow": {
        "name": "Neon Glow",
        "description": "💜 Texto ciano brilhante com destaque rosa neon — futurista",
        "preview_img": "neon-glow.png",
        "font": "Montserrat ExtraBold",
        "font_size": 68,
        "primary_color": "&H00FFFF00",
        "highlight_color": "&H00FF00FF",
        "outline_color": "&H00800040",
        "back_color": "&H00000000",
        "bold": -1,
        "outline": 6,
        "shadow": 4,
        "margin_v": 500,
    },
    "mrbeast": {
        "name": "MrBeast Energy",
        "description": "🔥 Texto amarelo enorme, outline vermelho — alto impacto",
        "preview_img": "mrbeast.png",
        "font": "Impact",
        "font_size": 85,
        "primary_color": "&H0000FFFF",
        "highlight_color": "&H00FFFFFF",
        "outline_color": "&H000000FF",
        "back_color": "&H00000000",
        "bold": -1,
        "outline": 7,
        "shadow": 3,
        "margin_v": 520,
    },
    "podcast": {
        "name": "Podcast Chill",
        "description": "🎤 Texto branco, destaque azul claro — relaxado e clean",
        "preview_img": "podcast.png",
        "font": "Montserrat",
        "font_size": 64,
        "primary_color": "&H00FFFFFF",
        "highlight_color": "&H00FFCC66",
        "outline_color": "&H00000000",
        "back_color": "&H80000000",
        "bold": 0,
        "outline": 4,
        "shadow": 1,
        "margin_v": 480,
    },
    "karaoke": {
        "name": "Karaoke Pop",
        "description": "🎤 Cores vibrantes alternadas, estilo karaokê — divertido",
        "preview_img": "karaoke.png",
        "font": "Montserrat ExtraBold",
        "font_size": 74,
        "primary_color": "&H00FFFFFF",
        "highlight_color": "&H0000FF00",
        "outline_color": "&H00000000",
        "back_color": "&HA0000000",
        "bold": -1,
        "outline": 5,
        "shadow": 2,
        "margin_v": 500,
    },
}

DEFAULT_PRESET = "capcut-bold"


def _get_preset(preset_name: str | None) -> dict:
    """Resolve a preset by name, falling back to default."""
    if not preset_name or preset_name not in CAPTION_PRESETS:
        return CAPTION_PRESETS[DEFAULT_PRESET]
    return CAPTION_PRESETS[preset_name]


def format_ass_ts(seconds: float) -> str:
    """Convert seconds to ASS timestamp H:MM:SS.cc"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def generate_ass(words: list[dict], seg_start: float, out_path: Path, preset_name: str | None = None):
    """Generate ASS subtitle with word-highlight style, driven by caption preset.

    Each chunk of 2-4 words is shown (auto-sized to fit 1080px width),
    with the currently-spoken word highlighted in yellow on top of
    the white base text.  Like movie subtitles, the full chunk appears
    before the person speaks and stays centered the whole time.
    """
    p = _get_preset(preset_name)
    font = p["font"]
    fs = p["font_size"]
    pc = p["primary_color"]
    hc = p["highlight_color"]
    oc = p["outline_color"]
    bc = p["back_color"]
    bold = p["bold"]
    outline = p["outline"]
    shadow = p["shadow"]
    mv = p["margin_v"]

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{fs},{pc},&H000000FF,{oc},{bc},{bold},0,0,0,100,100,2,0,1,{outline},{shadow},2,50,50,{mv},1
Style: Active,{font},{fs},{hc},&H000000FF,{oc},{bc},{bold},0,0,0,100,100,2,0,1,{outline},{shadow},2,50,50,{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # ASS colour override tags
    HIGHLIGHT = r"{\c" + hc.replace("&H", "&H", 1) + r"&\alpha&H00&}"
    INVISIBLE = r"{\alpha&HFF&}"

    # --- Smart chunking: fit text within ~1080px width ---
    MAX_CHARS = 22  # max uppercase chars per line (safe for 1080px)
    MAX_WORDS = 4   # never more than 4 words per chunk

    def _build_chunks(words_list):
        """Split words into display chunks that fit on screen."""
        chunks = []
        current_chunk = []
        current_len = 0
        for w in words_list:
            word_upper = w["word"].upper()
            addition = len(word_upper) + (1 if current_chunk else 0)
            if current_chunk and (
                current_len + addition > MAX_CHARS
                or len(current_chunk) >= MAX_WORDS
            ):
                chunks.append(current_chunk)
                current_chunk = [w]
                current_len = len(word_upper)
            else:
                current_chunk.append(w)
                current_len += addition
        if current_chunk:
            chunks.append(current_chunk)
        return chunks

    chunks = _build_chunks(words)

    events = []
    for chunk in chunks:
        if not chunk:
            continue
        chunk_start = max(0, chunk[0]["start"] - seg_start)
        chunk_end = max(chunk_start + 0.1, chunk[-1]["end"] - seg_start)
        all_upper = " ".join(w["word"].upper() for w in chunk)

        # Layer 0: full chunk in white for the ENTIRE chunk duration.
        # Always visible, always centered — like movie subtitles.
        events.append(
            f"Dialogue: 0,{format_ass_ts(chunk_start)},{format_ass_ts(chunk_end)},"
            f"Default,,0,0,0,,{all_upper}"
        )

        # Layer 1: yellow highlight on top of the active word.
        # The yellow renders over the white — visually it just looks
        # yellow because same font/size/position, outline covers the white.
        for j, w in enumerate(chunk):
            w_start = max(0, w["start"] - seg_start)
            w_end = max(w_start + 0.05, w["end"] - seg_start)
            hl_parts = []
            for k, cw in enumerate(chunk):
                if k == j:
                    hl_parts.append(f"{HIGHLIGHT}{cw['word'].upper()}")
                else:
                    hl_parts.append(f"{INVISIBLE}{cw['word'].upper()}")
            hl_line = " ".join(hl_parts)
            events.append(
                f"Dialogue: 1,{format_ass_ts(w_start)},{format_ass_ts(w_end)},"
                f"Active,,0,0,0,,{hl_line}"
            )

    out_path.write_text(header + "\n".join(events), encoding="utf-8")


# ===========================================================================
# 5. Smart Face-Based Crop (Multi-Camera)
# ===========================================================================
def _read_frame(video_path: str, time_sec: float):
    """Read a single frame from a video at the given timestamp."""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(time_sec * fps))
    ret, frame = cap.read()
    cap.release()
    return frame if ret else None


def _detect_faces_in_frame(frame) -> list[dict]:
    """Detect faces using MediaPipe Face Detection (far more accurate than Haar).

    Uses model_selection=1 (full-range, up to 5 m) which handles side angles,
    varying lighting, and multiple faces reliably.

    Returns list of {"x", "y", "w", "h"} in pixel coordinates.
    """
    if frame is None:
        return []

    import mediapipe as mp

    h_img, w_img = frame.shape[:2]

    with mp.solutions.face_detection.FaceDetection(
        model_selection=1,            # 0 = close-range (<2 m), 1 = full-range (<5 m)
        min_detection_confidence=0.5,
    ) as detector:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = detector.process(rgb)

    if not results.detections:
        return []

    faces = []
    for det in results.detections:
        bb = det.location_data.relative_bounding_box
        x = int(bb.xmin * w_img)
        y = int(bb.ymin * h_img)
        w = int(bb.width * w_img)
        h = int(bb.height * h_img)
        # Clamp to frame boundaries
        x = max(0, x)
        y = max(0, y)
        w = min(w, w_img - x)
        h = min(h, h_img - y)
        if w > 10 and h > 10:
            faces.append({"x": x, "y": y, "w": w, "h": h})

    return faces


def detect_camera_angles(
    video_path: str, num_samples: int = 60
) -> list[dict]:
    """Detect all distinct camera angles in a multi-camera podcast.

    Samples `num_samples` frames spread across the video, detects faces in
    each, then clusters by visual layout (number of faces + their horizontal
    positions).  Each cluster = one camera angle.

    Returns list of angles::

        [
            {"crop_x": 142, "num_faces": 1, "centers_x": [0.25], "count": 12},
            {"crop_x": 890, "num_faces": 1, "centers_x": [0.72], "count": 8},
            {"crop_x": 510, "num_faces": 2, "centers_x": [0.35, 0.65], "count": 5},
        ]
    """
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    vid_duration = total_frames / fps if fps else 0
    if orig_w == 0 or orig_h == 0 or vid_duration < 5:
        return []

    # Sample evenly, skipping first/last 3 %
    t_start = vid_duration * 0.03
    t_end = vid_duration * 0.97
    step = (t_end - t_start) / max(num_samples - 1, 1)
    sample_times = [t_start + i * step for i in range(num_samples)]

    # Detect faces at each sample
    CLUSTER_THRESH = 0.12  # 12 % of frame width
    angles: list[dict] = []  # each: {num_faces, centers_x, crop_x, count}

    scale = 1920 / orig_h
    scaled_w = int(orig_w * scale)

    for t in sample_times:
        frame = _read_frame(video_path, t)
        faces = _detect_faces_in_frame(frame)
        if not faces:
            continue
        n = len(faces)
        centers = sorted((f["x"] + f["w"] / 2) / orig_w for f in faces)

        # Try to match to an existing angle
        matched = False
        for angle in angles:
            if n != angle["num_faces"]:
                continue
            diffs = [abs(a - b) for a, b in zip(centers, angle["centers_x"])]
            if all(d < CLUSTER_THRESH for d in diffs):
                # Running average of center positions
                c = angle["count"]
                angle["centers_x"] = [
                    (old * c + new) / (c + 1)
                    for old, new in zip(angle["centers_x"], centers)
                ]
                angle["count"] += 1
                matched = True
                break

        if not matched:
            angles.append({
                "num_faces": n,
                "centers_x": list(centers),
                "count": 1,
            })

    # Compute crop_x for each angle.
    # The crop is 1080px wide. We want ALL faces comfortably inside it
    # with padding on each side of the face bounding box.
    for angle in angles:
        centers_px = [c * orig_w * scale for c in angle["centers_x"]]
        # Approximate face half-width as ~4% of scaled frame width
        est_face_half = orig_w * scale * 0.04
        leftmost = min(centers_px) - est_face_half
        rightmost = max(centers_px) + est_face_half
        group_center = (leftmost + rightmost) / 2

        # Center the 1080 crop on the face group center
        crop_x = int(group_center - 540)
        crop_x = max(0, min(crop_x, scaled_w - 1080))
        angle["crop_x"] = crop_x

    # Sort by popularity (most seen first)
    angles.sort(key=lambda a: a["count"], reverse=True)

    # Filter out noise: keep only angles seen ≥ 2 times, or top-5 if all are low
    if len(angles) > 5:
        # Keep angles seen at least twice, always keep top-5
        reliable = [a for a in angles if a["count"] >= 2]
        if len(reliable) < 2:
            reliable = angles[:5]
        angles = reliable

    log.info(
        "Detected %d camera angle(s): %s",
        len(angles),
        [(a["num_faces"], a["crop_x"], a["count"]) for a in angles],
    )
    return angles


def _detect_scene_changes(
    video_path: str,
    start: float,
    duration: float,
    threshold: float = 15.0,
    min_scene_len: float = 0.4,
) -> list[float]:
    """Find exact scene-change timestamps using frame differencing.

    Reads every frame in the segment and computes the mean absolute
    difference between consecutive frames.  A spike above *threshold*
    signals a hard camera cut.

    Returns a list of *relative* timestamps (seconds from segment start)
    where cuts happen.  Always includes 0.0 as the first "cut".
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    start_frame = int(start * fps)
    end_frame = int((start + duration) * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    cuts: list[float] = [0.0]
    prev_gray = None

    for fno in range(start_frame, end_frame):
        ret, frame = cap.read()
        if not ret:
            break
        # Downsample for speed (160px height is plenty for diff)
        small = cv2.resize(frame, (0, 0), fx=0.15, fy=0.15)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray).mean()
            if diff > threshold:
                t_rel = (fno - start_frame) / fps
                # Ignore if too close to the previous cut
                if t_rel - cuts[-1] >= min_scene_len:
                    cuts.append(t_rel)
        prev_gray = gray

    cap.release()
    return cuts


def _match_frame_to_angle(
    video_path: str,
    t_abs: float,
    angles: list[dict],
    orig_w: int,
    orig_h: int,
) -> int:
    """Read one frame, detect faces, compute crop_x.

    Only used for single-face scenes.  Multi-face scenes go through
    _detect_active_speaker_crop() instead.

    Strategy:
    - 1 face  → match to a known 1-face angle for consistency, else center
                on the detected face.
    - 0 faces → center-crop fallback.
    """
    scale = 1920 / orig_h
    scaled_w = int(orig_w * scale)
    default_cx = max(0, (scaled_w - 1080) // 2)

    frame = _read_frame(video_path, t_abs)
    faces = _detect_faces_in_frame(frame)
    if not faces:
        return default_cx

    # Single face — try to match a known 1-face angle for consistency
    cx_norm = (faces[0]["x"] + faces[0]["w"] / 2) / orig_w
    for angle in angles:
        if angle["num_faces"] == 1:
            if abs(cx_norm - angle["centers_x"][0]) < 0.15:
                return angle["crop_x"]
    # No matching angle — center on the detected face
    face_cx = cx_norm * orig_w * scale
    crop_x = int(face_cx - 540)
    return max(0, min(crop_x, scaled_w - 1080))


def _detect_active_speaker_crop(
    video_path: str,
    t_scene_abs: float,
    scene_duration: float,
    orig_w: int,
    orig_h: int,
) -> int | None:
    """For multi-face scenes, find the ACTIVE SPEAKER via lip movement.

    Samples ~10 frames spread across the scene, runs MediaPipe Face Mesh
    to get lip landmarks, and computes mouth-open variance for each tracked
    face.  The face with the highest variance is speaking.

    Returns crop_x centred on the speaker, or None if detection fails
    (e.g. nobody clearly speaking, too few frames).
    """
    import mediapipe as mp

    scale = 1920 / orig_h
    scaled_w = int(orig_w * scale)

    # Sample 10 frames spread across up to 4 s of the scene
    # Start 0.2 s after the cut to avoid transition frames
    sample_span = min(4.0, scene_duration * 0.8)
    n_samples = 10
    sample_times = [
        t_scene_abs + 0.2 + i * sample_span / max(n_samples - 1, 1)
        for i in range(n_samples)
    ]

    # Track mouth-open ratio per face position across frames
    # Key = approximate normalised center-x, Value = list of mouth ratios
    face_tracks: dict[float, list[float]] = {}
    TRACK_THRESH = 0.12  # 12 % of frame width to match same person

    with mp.solutions.face_mesh.FaceMesh(
        max_num_faces=5,
        refine_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    ) as mesh:
        for t in sample_times:
            frame = _read_frame(video_path, t)
            if frame is None:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = mesh.process(rgb)
            if not results.multi_face_landmarks:
                continue

            for face_lm in results.multi_face_landmarks:
                lm = face_lm.landmark

                # Face horizontal centre: average of left-cheek (234) and
                # right-cheek (454) landmarks for a stable centre estimate
                cx_norm = (lm[234].x + lm[454].x) / 2

                # Mouth openness: distance between inner lip centres
                # normalised by face height (forehead 10 → chin 152)
                top_lip_y = lm[13].y
                bottom_lip_y = lm[14].y
                face_top_y = lm[10].y
                face_bot_y = lm[152].y
                face_h = abs(face_bot_y - face_top_y)
                if face_h < 0.01:
                    continue

                mouth_ratio = abs(bottom_lip_y - top_lip_y) / face_h

                # Match to existing track by horizontal position
                matched_key = None
                for tk in face_tracks:
                    if abs(cx_norm - tk) < TRACK_THRESH:
                        matched_key = tk
                        break
                if matched_key is not None:
                    face_tracks[matched_key].append(mouth_ratio)
                else:
                    face_tracks[cx_norm] = [mouth_ratio]

    if not face_tracks:
        return None

    # Log per-face stats for debugging
    for cx, ratios in face_tracks.items():
        log.info(
            "    Face cx=%.2f: %d samples, mean=%.4f, var=%.6f",
            cx, len(ratios), float(np.mean(ratios)), float(np.var(ratios)),
        )

    # Identify the speaker using a COMBINED score:
    #   score = mean_mouth_ratio + 10 × std_dev
    # - High mean  = mouth is open more often (speaking)
    # - High stdev = mouth opens & closes rhythmically (speaking)
    # The speaker must score ≥ 3× the runner-up to be confident.
    MIN_SAMPLES = 4

    scores: dict[float, float] = {}
    for cx_norm, ratios in face_tracks.items():
        if len(ratios) < MIN_SAMPLES:
            continue
        m = float(np.mean(ratios))
        s = float(np.std(ratios))
        scores[cx_norm] = m + 10 * s

    if not scores:
        log.info("  Speaker detection: not enough samples for any face")
        return None

    sorted_faces = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_cx, best_score = sorted_faces[0]

    # Log scores
    for cx, sc in sorted_faces:
        log.info("    Face cx=%.2f → speaker score=%.4f", cx, sc)

    # Confidence check: winner must score ≥ 3× the runner-up
    if len(sorted_faces) > 1:
        runner_up_score = sorted_faces[1][1]
        if runner_up_score > 0 and best_score / runner_up_score < 3.0:
            log.info(
                "  Speaker detection ambiguous (ratio=%.1f, need ≥3.0)",
                best_score / runner_up_score,
            )
            return None
    elif best_score < 0.005:
        log.info("  Speaker detection: score too low (%.4f)", best_score)
        return None

    face_px = best_cx * orig_w * scale
    crop_x = int(face_px - 540)
    crop_x = max(0, min(crop_x, scaled_w - 1080))
    log.info(
        "  Active speaker at cx=%.2f (score=%.4f) → crop_x=%d",
        best_cx, best_score, crop_x,
    )
    return crop_x


def _compute_crop_timeline(
    video_path: str,
    start: float,
    duration: float,
    angles: list[dict],
    orig_w: int,
    orig_h: int,
) -> list[tuple[float, int]]:
    """Build a crop-x timeline using scene-change detection + speaker tracking.

    1. Scan the segment for exact camera-cut timestamps (frame-diff)
    2. For each scene:
       - 0-1 faces → match to known angle (fast, single frame)
       - 2+ faces  → detect who is SPEAKING via lip movement (Face Mesh)
                     and center the crop on that person

    Returns [(relative_time_sec, crop_x), ...]
    """
    scale = 1920 / orig_h
    scaled_w = int(orig_w * scale)
    default_cx = max(0, (scaled_w - 1080) // 2)

    # Step 1: find exact cut timestamps
    cuts = _detect_scene_changes(video_path, start, duration)
    log.info("Scene changes at: %s", [f"{c:.2f}" for c in cuts])

    # Step 2: classify each scene and compute crop_x
    timeline: list[tuple[float, int]] = []

    for i, cut_t in enumerate(cuts):
        # Scene duration = time to next cut (or end of segment)
        if i + 1 < len(cuts):
            scene_dur = cuts[i + 1] - cut_t
        else:
            scene_dur = duration - cut_t

        sample_t = start + cut_t + 0.15  # just past the cut

        # Quick face count via one frame
        frame = _read_frame(video_path, sample_t)
        faces = _detect_faces_in_frame(frame)
        n_faces = len(faces)

        if n_faces <= 1:
            # Single face or no face — use angle matching (fast)
            crop_x = _match_frame_to_angle(
                video_path, sample_t, angles, orig_w, orig_h,
            )
            log.info(
                "  Scene %.2f: %d face(s) → angle match → crop_x=%d",
                cut_t, n_faces, crop_x,
            )
        else:
            # Multi-face — detect active speaker via lip tracking
            speaker_cx = _detect_active_speaker_crop(
                video_path, start + cut_t, scene_dur, orig_w, orig_h,
            )
            if speaker_cx is not None:
                crop_x = speaker_cx
            else:
                # Fallback: try matching to a known multi-face angle
                centers = sorted(
                    (f["x"] + f["w"] / 2) / orig_w for f in faces
                )
                matched_angle = None
                for angle in angles:
                    if angle["num_faces"] != n_faces:
                        continue
                    diffs = [
                        abs(a - b)
                        for a, b in zip(centers, angle["centers_x"])
                    ]
                    if all(d < 0.15 for d in diffs):
                        matched_angle = angle
                        break

                if matched_angle:
                    crop_x = matched_angle["crop_x"]
                    log.info(
                        "  Scene %.2f: speaker inconclusive, angle match → crop_x=%d",
                        cut_t, crop_x,
                    )
                else:
                    # Last resort: center on the largest face
                    largest = max(faces, key=lambda f: f["w"] * f["h"])
                    face_cx = (
                        (largest["x"] + largest["w"] / 2) / orig_w * scaled_w
                    )
                    crop_x = int(face_cx - 540)
                    crop_x = max(0, min(crop_x, scaled_w - 1080))
                    log.info(
                        "  Scene %.2f: all fallbacks, largest face → crop_x=%d",
                        cut_t, crop_x,
                    )

        timeline.append((cut_t, crop_x))

    return timeline


def _build_crop_x_expr(
    timeline: list[tuple[float, int]],
    transition: float = 0.0,
) -> str:
    """Build a crop-x expression from a timeline for ffmpeg.

    With transition=0 (default), the crop snaps instantly at each camera
    cut — matching the hard cut in the source video.  Set transition>0
    for a smooth pan if desired.
    """
    if not timeline:
        return "(iw-1080)/2"

    # Deduplicate consecutive identical positions
    simplified = [timeline[0]]
    for t, cx in timeline[1:]:
        if cx != simplified[-1][1]:
            simplified.append((t, cx))

    if len(simplified) == 1:
        return str(simplified[0][1])

    if transition <= 0:
        # Hard cuts: nested if(lt(t, boundary), prev_val, ...)
        # Right-fold: if(lt(t,t1), cx0, if(lt(t,t2), cx1, ... cxN))
        expr = str(simplified[-1][1])
        for i in range(len(simplified) - 2, -1, -1):
            t_boundary = simplified[i + 1][0]
            expr = f"if(lt(t\\,{t_boundary:.3f})\\,{simplified[i][1]}\\,{expr})"
        return expr

    # Smooth transitions (additive lerp steps)
    expr = str(simplified[0][1])
    for i in range(1, len(simplified)):
        delta = simplified[i][1] - simplified[i - 1][1]
        if delta == 0:
            continue
        t_change = simplified[i][0]
        t_start = max(0, t_change - transition / 2)
        expr += f"+({delta})*max(0\\,min(1\\,(t-{t_start:.2f})/{transition:.2f}))"

    return expr


# ===========================================================================
# 6. Cut, Caption & Render
# ===========================================================================
def cut_and_caption(
    video_path: str,
    transcript: list[dict],
    start: float,
    end: float,
    render_id: str,
    angles: list[dict] | None = None,
    orig_w: int = 0,
    orig_h: int = 0,
    preset_name: str | None = None,
    language: str | None = None,
) -> Path:
    """Cut a segment, add captions, and output as 9:16 vertical MP4."""
    duration = end - start
    output = RENDERS / f"{render_id}.mp4"
    ass_path = RENDERS / f"{render_id}.ass"

    # Collect word-level timestamps within this segment
    words = []
    for seg in transcript:
        for w in seg.get("words", []):
            if w["start"] >= start - 0.5 and w["end"] <= end + 0.5:
                words.append(w)

    has_subs = bool(words)

    # Correct captions with Gemini before rendering
    if has_subs:
        words = correct_transcript_segment(words, language=language or "pt")
        generate_ass(words, start, ass_path, preset_name=preset_name)

    # --- Scene-based speaker-following crop ---
    # Detect exact camera cuts → match each scene to a known angle
    if angles and orig_w > 0 and orig_h > 0:
        timeline = _compute_crop_timeline(
            video_path, start, duration, angles, orig_w, orig_h,
        )
        crop_x_expr = _build_crop_x_expr(timeline)
        log.info(
            "[%s] Scene-based crop: %d scenes, expr=%s",
            render_id, len(timeline), crop_x_expr[:120],
        )
    else:
        crop_x_expr = "(iw-1080)/2"
        log.info("[%s] No angles/dimensions — using center crop", render_id)

    # Build ffmpeg filter chain
    # 1. Scale to fill 1920 height (zoom-in)
    # 2. Dynamic crop that follows the active speaker
    # 3. Overlay ASS captions if available
    vf_parts = [
        "scale=-2:1920",
        f"crop=1080:1920:{crop_x_expr}:0",
    ]
    if has_subs:
        escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
        vf_parts.append(f"ass='{escaped}'")

    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        video_path,
        "-t",
        str(duration),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-threads",
        "2",
        str(output),
    ]

    log.info("Rendering: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        log.error("ffmpeg stderr: %s", result.stderr[-2000:])
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-500:]}")

    # Cleanup
    ass_path.unlink(missing_ok=True)

    return output


# ===========================================================================
# Background Job Processing
# ===========================================================================
def process_job(job_id: str, video_id: str, target_dur_min: int = 45, target_dur_max: int = 59, language: str | None = None):
    """Full pipeline: download → transcribe → analyze."""
    try:
        job_dir = VIDEOS / job_id
        job_dir.mkdir(exist_ok=True)

        # Check if a previous job already downloaded this video
        cached = None
        for d in VIDEOS.iterdir():
            candidate = d / f"{video_id}.mp4"
            if candidate.exists() and candidate.stat().st_size > 1_000_000:
                cached = candidate
                break

        if cached and cached.parent != job_dir:
            # Symlink into our job_dir so download_youtube finds it
            link = job_dir / f"{video_id}.mp4"
            if not link.exists():
                link.symlink_to(cached)

        jobs[job_id]["step"] = "downloading"
        jobs[job_id]["step_detail"] = "Baixando vídeo do YouTube..."
        log.info("[%s] Downloading YouTube video %s …", job_id, video_id)
        video_path, video_info = download_youtube(video_id, job_dir)
        video_title = video_info.get("title", "")
        video_uploader = video_info.get("uploader", "")
        log.info("[%s] Download complete: %s (título: %s)", job_id, video_path, video_title or "—")

        # Detect all camera angles (multi-camera podcast support)
        log.info("[%s] Detecting camera angles …", job_id)
        camera_angles = detect_camera_angles(video_path, num_samples=60)
        # Cache video dimensions for dynamic crop
        _cap = cv2.VideoCapture(video_path)
        orig_w = int(_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        _cap.release()

        jobs[job_id]["step"] = "transcribing"
        jobs[job_id]["step_detail"] = "Transcrevendo áudio com IA..."
        log.info("[%s] Transcribing (language=%s) …", job_id, language or "auto")
        transcript = transcribe_video(video_path, language=language)
        duration = transcript[-1]["end"] if transcript else 0
        log.info(
            "[%s] Transcription complete: %d segments, %.0fs total",
            job_id,
            len(transcript),
            duration,
        )

        jobs[job_id]["step"] = "analyzing"
        jobs[job_id]["step_detail"] = "IA analisando e selecionando trechos..."
        log.info("[%s] Analyzing transcript with Gemini (target %d-%ds) …", job_id, target_dur_min, target_dur_max)
        segments = analyze_transcript(transcript, duration, target_dur_min, target_dur_max)
        log.info("[%s] Found %d short candidates", job_id, len(segments))

        # Build shorts array matching Swiftia's format
        shorts = []
        for i, seg in enumerate(segments):
            # Collect transcript text for this segment
            text_parts = []
            for t in transcript:
                if t["end"] >= seg["start_time"] and t["start"] <= seg["end_time"]:
                    text_parts.append(t["text"])
            shorts.append(
                {
                    "id": i,
                    "title": seg["title"],
                    "text": " ".join(text_parts),
                    "reason": seg["reason"],
                    "start_time": seg["start_time"],
                    "end_time": seg["end_time"],
                }
            )

        jobs[job_id] = {
            "id": job_id,
            "status": "COMPLETED",
            "videoId": video_id,
            "videoTitle": video_title,
            "videoUploader": video_uploader,
            "videoUrl": f"https://www.youtube.com/watch?v={video_id}",
            "data": {"shorts": shorts},
            # Internal – not exposed in API but used by render
            "_video_path": video_path,
            "_transcript": transcript,
            "_camera_angles": camera_angles,
            "_orig_w": orig_w,
            "_orig_h": orig_h,
            "_language": language,
        }
        log.info("[%s] Job COMPLETED ✓", job_id)

    except Exception as e:
        log.exception("[%s] Job FAILED", job_id)
        jobs[job_id] = {"id": job_id, "status": "ERROR", "error": str(e)}


def process_render(
    job_id: str, target_idx: int, render_id: str, styling: dict
):
    """Cut and caption a single short segment."""
    try:
        job = jobs.get(job_id)
        if not job or job["status"] != "COMPLETED":
            raise ValueError(f"Job {job_id} not ready")

        short = job["data"]["shorts"][target_idx]
        video_path = job["_video_path"]
        transcript = job["_transcript"]

        # Resolve preset name from styling (string = preset name, dict = legacy)
        preset_name = styling if isinstance(styling, str) else None

        log.info(
            "[%s] Rendering short #%d (%.1fs–%.1fs) preset=%s …",
            render_id,
            target_idx,
            short["start_time"],
            short["end_time"],
            preset_name or "default",
        )
        output = cut_and_caption(
            video_path, transcript, short["start_time"], short["end_time"],
            render_id,
            angles=job.get("_camera_angles", []),
            orig_w=job.get("_orig_w", 0),
            orig_h=job.get("_orig_h", 0),
            preset_name=preset_name,
            language=job.get("_language"),
        )
        renders[render_id] = {
            "type": "done",
            "url": f"{BASE_URL}/videos/{render_id}.mp4",
            "renderId": render_id,
        }
        log.info("[%s] Render complete ✓ → %s", render_id, output)

    except Exception as e:
        log.exception("[%s] Render FAILED", render_id)
        renders[render_id] = {"type": "error", "error": str(e), "renderId": render_id}


# ===========================================================================
# API Models
# ===========================================================================
class JobRequest(BaseModel):
    functionName: str = "VideoShorts"
    youtubeVideoId: str
    videoSource: str = "youtube"
    targetDurationMin: int = 45
    targetDurationMax: int = 59
    language: str | None = None  # Whisper language hint (e.g. 'pt', 'en', 'es')


# ===========================================================================
# API Endpoints (Swiftia-compatible)
# ===========================================================================
@app.post("/api/jobs")
async def create_job(request: JobRequest, background_tasks: BackgroundTasks):
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "status": "PROCESSING", "step": "queued", "step_detail": "Job na fila...", "_created": datetime.now(timezone.utc).timestamp()}
    background_tasks.add_task(
        process_job, job_id, request.youtubeVideoId,
        request.targetDurationMin, request.targetDurationMax,
        request.language,
    )
    return {"id": job_id, "status": "PROCESSING"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    job = jobs[job_id]
    # Don't leak internal fields
    return {
        k: v for k, v in job.items() if not k.startswith("_")
    }


@app.post("/api/render/")
@app.post("/api/render")
async def create_render(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    job_id = body.get("id", "")
    target = int(body.get("target", 0))
    styling = body.get("options") or body.get("preset") or {}

    render_id = uuid.uuid4().hex[:8]
    renders[render_id] = {"type": "processing", "renderId": render_id}
    background_tasks.add_task(process_render, job_id, target, render_id, styling)
    return {"renderId": render_id}


@app.get("/api/render/{render_id}")
async def get_render(render_id: str):
    if render_id not in renders:
        return JSONResponse({"error": "Render not found"}, status_code=404)
    return renders[render_id]


@app.get("/api/pipeline/latest")
async def get_latest_pipeline():
    """Return combined status of the latest job + renders for loading page polling."""
    if not jobs:
        return {"step": "waiting", "detail": "Nenhum job encontrado", "error": None}

    # Find the most recent job
    latest_id = max(jobs.keys(), key=lambda k: jobs[k].get("_created", 0))
    job = jobs[latest_id]
    status = job.get("status", "PROCESSING")
    step = job.get("step", "unknown")
    error = job.get("error") if status == "ERROR" else None

    # Map internal step to loading page step index
    step_map = {
        "downloading": 3,      # Baixando e transcrevendo
        "transcribing": 3,     # Baixando e transcrevendo
        "analyzing": 2,        # IA selecionando momentos
    }
    step_index = step_map.get(step, 0)

    if status == "COMPLETED":
        # Check if any renders are in progress
        active_renders = [r for r in renders.values() if r.get("type") == "processing"]
        done_renders = [r for r in renders.values() if r.get("type") == "done"]
        error_renders = [r for r in renders.values() if r.get("type") == "error"]

        if error_renders:
            return {"step": "error", "stepIndex": 5, "detail": error_renders[0].get("error", "Render failed"), "error": True, "jobId": latest_id}
        elif active_renders:
            return {"step": "rendering", "stepIndex": 5, "detail": f"Renderizando shorts ({len(done_renders)} prontos)...", "error": None, "jobId": latest_id}
        elif done_renders:
            return {"step": "done", "stepIndex": 6, "detail": "Todos os shorts foram processados!", "error": None, "jobId": latest_id, "renderCount": len(done_renders)}
        else:
            return {"step": "cutting", "stepIndex": 4, "detail": "Análise completa, aguardando renderização...", "error": None, "jobId": latest_id}

    return {
        "step": step,
        "stepIndex": step_index,
        "detail": job.get("step_detail", "Processando..."),
        "error": error,
        "jobId": latest_id,
    }


@app.get("/api/caption-presets")
async def list_caption_presets():
    """Return available caption style presets with descriptions."""
    return {
        key: {
            "name": p["name"],
            "description": p["description"],
            "preview_img": p.get("preview_img", ""),
        }
        for key, p in CAPTION_PRESETS.items()
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ===========================================================================
# Trend-Aware Style Director
# ===========================================================================
STYLE_TONES = [
    "casual e descontraído, como se falasse com um amigo",
    "storytelling envolvente, como um mini-documentário",
    "educacional e empolgante, como um professor apaixonado",
    "humorístico e inteligente, sem exagero ou sarcasmo forçado",
    "conversacional usando o vocabulário natural do público-alvo",
    "jornalístico e factual, com dados e contexto real",
    "analítico e direto, como um especialista do setor",
    "inspiracional com base em resultados reais e concretos",
]

# These are content framing approaches — how the content angle is presented,
# NOT clickbait hook styles. They inform tone/framing, never title wording directly.
HOOK_STYLES = [
    "abre com o dado, resultado ou nome central do conteúdo",
    "apresenta o contexto da situação antes de revelar o insight",
    "nomeia a marca, pessoa ou evento antes de qualquer adjetivo",
    "destaca a pergunta real que foi feita no vídeo",
    "aponta o aprendizado ou conclusão principal do trecho",
    "apresenta de forma factual o contraste entre expectativa e resultado",
]

CTA_STYLES = [
    "sem CTA — deixar o conteúdo falar por si",
    "CTA sutil no final: 'vídeo completo no canal'",
    "CTA engajador: 'comenta aqui se tu concorda'",
    "CTA de curiosidade: 'parte 2 em breve...'",
    "CTA de compartilhamento: 'manda pra alguém que precisa ver isso'",
    "CTA de debate: 'qual sua opinião? comenta aí'",
]

EMOJI_STRATEGIES = [
    "zero emojis — visual limpo e sério",
    "1-2 emojis estratégicos no título apenas",
    "emojis moderados na descrição (3-4 max)",
    "emojis como separadores visuais na descrição",
]

HASHTAG_STRATEGIES = [
    "poucos hashtags de alto volume (3-4 broad tags)",
    "mix de nicho + broad (2 específicos + 2 genéricos)",
    "hashtags trending do momento + #shorts",
    "hashtags long-tail específicos do conteúdo (5-7)",
    "apenas #shorts + 1 hashtag de nicho",
]

TREND_STYLE_PROMPT = """You are a YouTube Shorts distribution analyst. Your job is NOT to generate titles or descriptions — that happens in a separate step. Your job is to provide style and tone guidance so that the metadata (title, description, tags) reaches the target audience organically on the platform.

Current date: {date}
Channel niche: {niche}
{audience_context}

Style parameters for this batch:
- Writing tone: {tone}
- Content framing: {hook_style}
- CTA approach: {cta_style}
- Emoji usage: {emoji_strategy}
- Hashtag approach: {hashtag_strategy}

Generate a JSON object with guidance for metadata generation:
{{
  "style_directive": "2-3 sentences describing the TONE and WRITING STYLE appropriate for this audience. Focus on how the language, formality and framing matches what the target audience expects. Do NOT suggest alarm language, urgency manipulation, sensationalism or clickbait of any kind.",
  "avoid_patterns": [
    "lista com 5-7 padrões ESPECÍFICOS a evitar que prejudicam alcance orgânico ou credibilidade com este público. SEMPRE inclua: títulos com emoji-alarme (🚨💀), 'Você está MORTO', 'choque de realidade', 'questionar o status quo', 'disruptivo', perguntas retóricas de medo, frases de coach motivacional genérico"
  ],
  "trending_hashtags": ["5-8 hashtags que ajudam este conteúdo a chegar organicamente ao público de {niche} — mistura de tags de nicho específicas e tags de descoberta ampla"],
  "title_format": "Formato simples e profissional para títulos que reflita o tom do público (exemplo: 'NOME/MARCA + TEMA + RESULTADO')",
  "description_format": "Estrutura breve para descrição focada em contexto e palavras-chave para alcance orgânico",
  "seasonal_context": "Contexto sazonal ou cultural relevante para {date} que possa guiar relevância de tópico — ou string vazia se não houver nada relevante"
}}

IMPORTANT:
- style_directive must NEVER suggest urgency, alarm, provocative framing or sensationalism
- avoid_patterns must always explicitly include emoji-alarm titles, 'Você está MORTO', 'choque de realidade', coach-speak
- trending_hashtags must prioritize organic discoverability for this specific audience, not general virality
- Return ONLY valid JSON, no markdown fences"""


@app.get("/api/trend-style")
async def get_trend_style(niche: str = "tecnologia e internet", audience: str = ""):
    """Generate a fresh, trend-aware style directive for metadata generation.

    Each call produces different style parameters to ensure content variety.
    """
    try:
        # Randomly select style parameters for variety
        tone = random.choice(STYLE_TONES)
        hook_style = random.choice(HOOK_STYLES)
        cta_style = random.choice(CTA_STYLES)
        emoji_strategy = random.choice(EMOJI_STRATEGIES)
        hashtag_strategy = random.choice(HASHTAG_STRATEGIES)

        audience_context = (
            f"Target audience: {audience}" if audience
            else "Target audience: general Brazilian internet-savvy audience, 18-35 years old"
        )

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%B %d, %Y")

        prompt = TREND_STYLE_PROMPT.format(
            date=date_str,
            niche=niche,
            audience_context=audience_context,
            tone=tone,
            hook_style=hook_style,
            cta_style=cta_style,
            emoji_strategy=emoji_strategy,
            hashtag_strategy=hashtag_strategy,
        )

        model = get_gemini()
        resp = model.generate_content(prompt)
        text = resp.text.strip()

        # Strip markdown fences if present
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            text = match.group(0)

        style_data = json.loads(text)

        # Include the randomized parameters for transparency
        style_data["_applied_parameters"] = {
            "tone": tone,
            "hook_style": hook_style,
            "cta_style": cta_style,
            "emoji_strategy": emoji_strategy,
            "hashtag_strategy": hashtag_strategy,
            "generated_at": now.isoformat(),
        }

        log.info(
            "Generated trend style: tone='%s', hook='%s'",
            tone[:30], hook_style[:30],
        )
        return style_data

    except Exception as e:
        log.exception("Trend style generation failed, returning defaults")
        # Fallback: return minimal style directive so the workflow doesn't break
        return {
            "style_directive": (
                f"Use um tom {random.choice(STYLE_TONES)}. "
                f"Enquadre o conteúdo de forma a {random.choice(HOOK_STYLES)}. "
                "Títulos profissionais com nomes, marcas e resultados concretos."
            ),
            "avoid_patterns": [
                "emojis de alarme (🚨💀) no título",
                "Você está MORTO",
                "choque de realidade",
                "questionar o status quo",
                "frases de coach motivacional genérico",
                "perguntas retóricas de medo",
                "clickbait sensacionalista",
            ],
            "trending_hashtags": ["#shorts", "#youtubeshorts"],
            "title_format": "NOME/MARCA + TEMA + RESULTADO",
            "description_format": "Contexto real do trecho + hashtags de nicho + #shorts",
            "seasonal_context": "",
            "_error": str(e),
        }
