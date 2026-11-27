import os
import re
import gc
import math
import time
import uuid
import wave
import shutil
import asyncio
import tempfile
import subprocess
import threading
import urllib.parse
from typing import Dict, List, Any, Optional, Tuple

import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from flask import Flask, request, jsonify, send_file

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

# ==============================================================================
# 1. NEXORA ROMANCE APEX v26 — TRI-HYBRID 16:9 CINEMA & 2D LIP-SYNC ENGINE
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
TMP_ROOT = os.path.join(BASE_DIR, "tmp_work")

for d in (OUTPUT_DIR, UPLOAD_DIR, SESSIONS_DIR, TMP_ROOT):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

SESSIONS: Dict[str, Dict[str, Any]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}
LOCK = threading.Lock()
ACTIVE_JOBS = set()
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,40}$")

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0"}


def is_safe_id(val: str) -> bool:
    return bool(val and SAFE_ID_RE.match(val))


def purge_expired(max_age_sec: int = 1800):
    now = time.time()
    for folder in (OUTPUT_DIR, UPLOAD_DIR):
        try:
            for fn in os.listdir(folder):
                fp = os.path.join(folder, fn)
                if os.path.isfile(fp) and (now - os.path.getmtime(fp)) > max_age_sec:
                    os.remove(fp)
        except Exception:
            pass
    try:
        for sid in os.listdir(SESSIONS_DIR):
            sp = os.path.join(SESSIONS_DIR, sid)
            if os.path.isdir(sp) and (now - os.path.getmtime(sp)) > max_age_sec:
                shutil.rmtree(sp, ignore_errors=True)
    except Exception:
        pass


# ==============================================================================
# 2. CLEAN DEVANAGARI -> ASCII HINGLISH & AUTO STORY TITLE EXTRACTOR
# ==============================================================================
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "क़": "q", "ख़": "kh",
    "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "dh", "फ़": "f",
}
_VOWELS_INDEP = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo",
    "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
}
_MATRAS = {
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo",
    "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
    "ं": "n", "ँ": "n", "ः": "h", "्": "", "़": "",
}


def devanagari_to_hinglish(text: str) -> str:
    if not text:
        return ""
    replacements = {
        "ज़": "ज़", "फ़": "फ़", "ड़": "ड़", "ढ़": "ढ़",
        "क़": "क़", "ख़": "ख़", "ग़": "ग़", "—": "-", "–": "-",
        "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)

    out_words: List[str] = []
    for token in text.split():
        res: List[str] = []
        n = len(token)
        i = 0
        while i < n:
            ch = token[i]
            if ch in _VOWELS_INDEP:
                res.append(_VOWELS_INDEP[ch])
                i += 1
            elif ch in _CONSONANTS:
                res.append(_CONSONANTS[ch])
                nxt = token[i + 1] if i + 1 < n else ""
                if nxt in _MATRAS:
                    res.append(_MATRAS[nxt])
                    i += 2
                else:
                    if i + 1 < n and ("\u0900" <= nxt <= "\u097F"):
                        res.append("a")
                    i += 1
            elif ch in _MATRAS:
                res.append(_MATRAS[ch])
                i += 1
            elif ch == "।":
                res.append(".")
                i += 1
            else:
                res.append(ch)
                i += 1
        word_str = re.sub(r"aa+", "aa", "".join(res))
        word_str = re.sub(r"[^\x20-\x7E]", "", word_str)
        if word_str:
            out_words.append(word_str)
    return " ".join(out_words)


def auto_derive_story_title(custom_script: str, provided_title: str) -> str:
    for line in str(custom_script or "").splitlines():
        s = line.strip()
        if s.lower().startswith("# title:") or s.lower().startswith("title:"):
            extracted = s.split(":", 1)[1].strip()
            if extracted:
                return devanagari_to_hinglish(extracted)[:65]

    clean_prov = str(provided_title or "").strip()
    if clean_prov:
        return clean_prov

    for line in str(custom_script or "").splitlines():
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                spoken = devanagari_to_hinglish(parts[-1])
                words = [w.strip(".,!?-") for w in spoken.split() if len(w.strip(".,!?-")) > 1]
                if words:
                    return " ".join(words[:6]).title()
    return "NEXORA Love Story"


# ==============================================================================
# 3. HUMAN PSYCHOLOGY AUTO-DIRECTOR & SMART PIXEL-GUARD AI VISUAL ENGINE
# ==============================================================================
MOTION_MODES = [
    "Zoom_In_Push",
    "Pan_Right_Drift",
    "Zoom_Out_Reveal",
    "Pan_Left_Drift",
    "Dramatic_Pulse_Zoom",
]


def is_limit_placeholder_image(img_path: str) -> bool:
    """Detects Pollinations 45s white-bottom rate-limit bee card via pixel inspection."""
    try:
        with Image.open(img_path) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            if w < 100 or h < 100:
                return True
            bottom_crop = rgb.crop((int(w * 0.1), int(h * 0.72), int(w * 0.9), int(h * 0.96)))
            arr = np.asarray(bottom_crop, dtype=np.float32)
            # The rate-limit card has a bright off-white/cream banner across the lower third
            bright_ratio = float(np.mean((arr[:, :, 0] > 235) & (arr[:, :, 1] > 232) & (arr[:, :, 2] > 220)))
            return bright_ratio > 0.58
    except Exception:
        return True


def build_google_flow_prompt(visual_prompt: str, idx: int) -> str:
    cam_moves = [
        "slow cinematic dolly push-in, subtle breathing and gentle movement, shallow depth of field",
        "smooth slow horizontal camera pan, flickering warm candlelight shadows, anamorphic film lens",
        "gentle emotional close-up focus pull, atmospheric bokeh lights, 35mm movie aesthetic",
        "slow dramatic zoom out, rich contrast and warm moonlight tones, clean borderless frame",
    ]
    cam = cam_moves[idx % len(cam_moves)]
    base = re.sub(r"\b[48]k\b", "", visual_prompt, flags=re.IGNORECASE)
    base = re.sub(r"\bno\s+text\b", "", base, flags=re.IGNORECASE)
    base = re.sub(r"\s+", " ", base).strip(" ,.")
    return f"{base}, {cam}, Indian romantic drama cinema, photorealistic, clean borderless frame without text"


def build_clean_visual_prompt(visual_prompt: str, character_dna: str) -> str:
    base = re.sub(r"\b[48]k\b", "", visual_prompt, flags=re.IGNORECASE)
    base = re.sub(r"\bno\s+text\b", "", base, flags=re.IGNORECASE)
    base = re.sub(r"\s+", " ", base).strip(" ,.")
    dna_tag = f", {character_dna}" if character_dna else ""
    return f"{base}{dna_tag}, cinematic Indian romantic drama film still, moody lighting, shallow depth of field, photorealistic"


def build_ai_url(visual_prompt: str, character_dna: str, out_w: int, out_h: int, seed: int) -> str:
    clean_p = build_clean_visual_prompt(visual_prompt, character_dna)
    enc = urllib.parse.quote(clean_p)
    return f"https://image.pollinations.ai/prompt/{enc}?width={out_w}&height={out_h}&seed={seed}&nologo=true&model=turbo"


def fetch_ai_images_with_pixel_guard(
    scenes: List[Dict[str, Any]],
    character_dna: str,
    work_dir: str,
    out_w: int,
    out_h: int,
    job_id: str,
) -> Dict[int, str]:
    """
    Fetches AI images ONLY for scenes assigned 'ai_scene'.
    Uses Pixel Guard to reject the 45s rate-limit bee image and waits for real frames.
    """
    downloaded: Dict[int, str] = {}
    ai_indices = [i for i, s in enumerate(scenes) if s.get("visual_mode") == "ai_scene" and not s.get("media_file")]

    for pos, idx in enumerate(ai_indices):
        sc = scenes[idx]
        update_job(
            job_id,
            status="running",
            progress=6 + int((pos / max(1, len(ai_indices))) * 16),
            current_scene=f"Generating Cinema Scene {idx + 1} ({pos + 1}/{len(ai_indices)})...",
        )
        out_p = os.path.join(work_dir, f"ai_src_{idx:03d}.jpg")
        seed = int(sc.get("seed", 2026 + idx * 41))
        url = build_ai_url(sc.get("visual_prompt", ""), character_dna, out_w, out_h, seed)

        for attempt in range(2):
            try:
                r = requests.get(url, headers=HTTP_HEADERS, timeout=18)
                if r.status_code == 200 and len(r.content) > 5000:
                    with open(out_p, "wb") as f:
                        f.write(r.content)
                    if not is_limit_placeholder_image(out_p):
                        downloaded[idx] = out_p
                        break
                    else:
                        if os.path.exists(out_p):
                            os.remove(out_p)
            except Exception:
                pass

            if attempt == 0 and pos < len(ai_indices) - 1:
                update_job(job_id, current_scene=f"Cooling AI Server (46s) for Scene {idx + 1}...")
                time.sleep(46.0)

        if idx in downloaded and pos < len(ai_indices) - 1:
            time.sleep(2.0)

    return downloaded


def detect_romantic_sfx(text_blob: str, mood: str) -> str:
    low = text_blob.lower()
    if any(k in low for k in ("message", "msg", "phone", "call", "notification", "ring", "screen", "chat")):
        return "phone_chime"
    if any(k in low for k in ("rain", "barish", "boond", "window", "khidki", "bheeg")):
        return "soft_rain"
    if any(k in low for k in ("heartbeat", "dil", "saansein", "dhadkan", "kareeb", "chhua", "kiss", "baahon", "seene")):
        return "soft_heartbeat"
    if any(k in low for k in ("clock", "ghadi", "raat ke 2", "aadhi raat", "waqt", "intezaar")):
        return "clock_tick"
    if any(k in low for k in ("door", "darwaza", "khula", "band", "kamre")):
        return "door_creak"
    if any(k in low for k in ("thunder", "bijli", "toofan", "storm")):
        return "thunder"
    if any(k in low for k in ("raaz", "dhokha", "sach", "aansu", "ro ", "dard", "secret")):
        return "emotional_swell"
    if mood == "sad_heartbreak":
        return "soft_rain"
    return "whoosh"


def detect_line_emotion(hinglish_txt: str, speaker: str) -> str:
    low = hinglish_txt.lower()
    if any(k in low for k in ("dheeme", "chup", "kareeb", "saansein", "raaz", "chhua", "baahon", "khamosh", "ishq", "pyaar")):
        return "intimate_whisper"
    if any(k in low for k in ("aansu", "ro ", "dard", "toot", "alvida", "chhod", "judai", "tanha")):
        return "emotional_ache"
    if "!" in hinglish_txt or any(k in low for k in ("achanak", "rok", "kya", "sach", "darwaza")):
        return "urgent_passion"
    if speaker in ("male", "female"):
        return "warm_dialogue"
    return "late_night_fm"


def extract_chat_bubble_text(hinglish_txt: str, visual_prompt: str) -> str:
    blob = f"{hinglish_txt} {visual_prompt}".lower()
    if not any(k in blob for k in ("message", "msg", "phone", "screen", "notification", "chat", "text")):
        return ""
    # Extract quoted or post-dash message if present, otherwise synthesize realistic preview
    m = re.search(r"(?:likha tha|message tha|msg tha|notification)\s*[-:]?\s*(.+)$", hinglish_txt, re.IGNORECASE)
    if m:
        snippet = m.group(1).strip(" .-\"'")
        if 4 <= len(snippet) <= 68:
            return snippet
    return "Main jaanta hoon tumhare kamre mein koi hai..."


def auto_assign_tri_hybrid_mode(idx: int, total_est: int, speaker: str, visual_prompt: str, hinglish_txt: str) -> str:
    """
    Human Retention Psychology Auto-Director:
    - Never lets the same visual format run more than 2 scenes in a row (Orienting Response reset).
    - Scene 0 (Intro Hook) & direct emotional dialogues -> '2d_host' (Eye contact + Lip-sync).
    - High-action / climax movement -> 'flow_clip' (With AI/2D fallback if user doesn't upload MP4).
    - Atmospheric world-building -> 'ai_scene' (3D Parallax + Rain/Bokeh VFX).
    """
    vp_low = visual_prompt.lower()
    is_action = any(k in vp_low for k in ("walking", "running", "opening", "driving", "embracing", "holding", "car", "door"))

    if idx == 0:
        return "2d_host"
    if speaker in ("male", "female") and idx % 2 == 1:
        return "2d_host"
    if is_action and idx in (3, 7, 11, 14):
        return "flow_clip"
    if idx % 3 == 0:
        return "2d_host"
    return "ai_scene"


def parse_user_script(custom_script: str, mood: str, aspect: str) -> Tuple[List[Dict[str, Any]], str]:
    out_w, out_h = (576, 1024) if aspect == "9:16" else (1024, 576)
    base_seed = int(time.time()) % 100000
    character_dna = ""
    raw_lines: List[Tuple[str, str, str]] = []

    for line in str(custom_script or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("# dna:") or line.lower().startswith("dna:"):
            character_dna = line.split(":", 1)[1].strip()
            continue
        if line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            sp = parts[0].lower()
            if sp not in ("narrator", "male", "female", "child"):
                sp = "narrator"
            visual_prompt = parts[1]
            spoken_hindi = parts[3] if len(parts) >= 4 else parts[2]
            raw_lines.append((sp, visual_prompt, spoken_hindi))

    scenes: List[Dict[str, Any]] = []
    total_cnt = len(raw_lines)

    for idx, (sp, visual_prompt, spoken_hindi) in enumerate(raw_lines):
        auto_caption = devanagari_to_hinglish(spoken_hindi)
        combined_txt = f"{visual_prompt} {auto_caption}"
        v_mode = auto_assign_tri_hybrid_mode(idx, total_cnt, sp, visual_prompt, auto_caption)
        chat_bubble = extract_chat_bubble_text(auto_caption, visual_prompt)
        seed = base_seed + idx * 43
        flow_prompt = build_google_flow_prompt(visual_prompt, idx)

        scenes.append({
            "index": idx,
            "seed": seed,
            "is_teaser": False,
            "speaker": sp,
            "emotion": detect_line_emotion(auto_caption, sp),
            "visual_mode": v_mode,  # '2d_host' | 'ai_scene' | 'flow_clip'
            "visual_prompt": visual_prompt,
            "flow_prompt": flow_prompt,
            "chat_bubble": chat_bubble,
            "hindi_dub": spoken_hindi,
            "auto_caption": auto_caption,
            "anim_type": MOTION_MODES[idx % len(MOTION_MODES)],
            "sfx_type": detect_romantic_sfx(combined_txt, mood),
            "media_ready": True,
            "media_type": "auto",
            "media_file": "",
            "thumb_file": "",
        })

    return scenes, character_dna


# ==============================================================================
# 4. PRO 2D ANIME LIP-SYNC CHARACTER & CHAT BUBBLE RENDERER (PILLOW + NUMPY)
# ==============================================================================
def extract_audio_amplitude_envelope(wav_path: str, fps: int = 24) -> np.ndarray:
    """Extracts per-frame RMS amplitude (0.0 to 1.0) for real-time phoneme lip-sync."""
    if not os.path.exists(wav_path):
        return np.zeros(24, dtype=np.float32)
    try:
        with wave.open(wav_path, "rb") as wf:
            rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return np.zeros(24, dtype=np.float32)

    if len(samples) == 0:
        return np.zeros(24, dtype=np.float32)

    dur = len(samples) / float(rate)
    total_video_frames = max(12, int(round(dur * fps)))
    hop = max(1, len(samples) // total_video_frames)
    env = np.zeros(total_video_frames, dtype=np.float32)

    for i in range(total_video_frames):
        seg = samples[i * hop : (i + 1) * hop]
        if len(seg) > 0:
            rms = float(np.sqrt(np.mean(seg * seg)))
            env[i] = min(1.0, rms * 4.8)

    # Smooth envelope slightly so lip transitions look organic
    kernel = np.array([0.2, 0.6, 0.2], dtype=np.float32)
    return np.convolve(env, kernel, mode="same")


def draw_chat_bubble_overlay(draw: ImageDraw.ImageDraw, out_w: int, out_h: int, chat_text: str, progress_p: float):
    """Draws a realistic dark-mode smartphone notification pop-up when a message arrives in story."""
    if not chat_text:
        return
    slide = min(1.0, progress_p * 4.5)
    box_w = int(out_w * 0.42) if out_w > out_h else int(out_w * 0.78)
    box_h = 84
    x1 = int(out_w - box_w - 28 + (1.0 - slide) * 120) if out_w > out_h else int((out_w - box_w) // 2)
    y1 = int(out_h * 0.14)
    x2 = x1 + box_w
    y2 = y1 + box_h

    draw.rounded_rectangle([x1, y1, x2, y2], radius=16, fill=(15, 23, 42, 235), outline=(244, 114, 182, 210), width=2)
    draw.ellipse([x1 + 14, y1 + 16, x1 + 42, y1 + 44], fill=(244, 114, 182, 255))
    draw.text((x1 + 52, y1 + 14), "NEW MESSAGE • 02:00 AM", fill=(244, 114, 182, 255))
    short_msg = chat_text[:44] + ("..." if len(chat_text) > 44 else "")
    draw.text((x1 + 52, y1 + 38), short_msg, fill=(248, 250, 252, 255))


def build_2d_character_base(out_w: int, out_h: int, speaker: str, mood: str, bg_img_path: Optional[str] = None) -> Image.Image:
    """Creates atmospheric blurred cinema backdrop for the 2D storyteller."""
    if bg_img_path and os.path.exists(bg_img_path):
        try:
            with Image.open(bg_img_path) as im:
                bg = im.convert("RGB").resize((out_w, out_h), Image.Resampling.BILINEAR)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=14))
                dark_tint = Image.new("RGB", (out_w, out_h), (8, 10, 22))
                return Image.blend(bg, dark_tint, 0.48)
        except Exception:
            pass

    # Procedural rich cinema bokeh backdrop
    y_grid = np.linspace(0, 1, out_h, dtype=np.float32)[:, None]
    x_grid = np.linspace(0, 1, out_w, dtype=np.float32)[None, :]
    if mood == "sad_heartbreak":
        c_top = np.array([10, 22, 44], dtype=np.float32)
        c_bot = np.array([4, 8, 18], dtype=np.float32)
    elif mood == "secret_suspense":
        c_top = np.array([32, 10, 42], dtype=np.float32)
        c_bot = np.array([6, 6, 16], dtype=np.float32)
    else:
        c_top = np.array([46, 18, 34], dtype=np.float32)
        c_bot = np.array([8, 9, 20], dtype=np.float32)

    grad = c_top * (1.0 - y_grid) + c_bot * y_grid
    # Warm rim glow behind character position (Rule of Thirds)
    cx_norm = 0.34 if speaker == "male" else (0.66 if speaker == "female" else 0.36)
    dist2 = (x_grid - cx_norm) ** 2 + (y_grid - 0.48) ** 2
    glow = np.exp(-dist2 * 14.0)[:, :, None] * np.array([95, 42, 78], dtype=np.float32)
    arr = np.clip(grad + glow, 0, 255).astype(np.uint8)
    base = Image.fromarray(np.broadcast_to(arr, (out_h, out_w, 3)).copy(), mode="RGB")

    draw = ImageDraw.Draw(base, "RGBA")
    rng = np.random.default_rng(108)
    for _ in range(26):
        bx = int(rng.integers(0, out_w))
        by = int(rng.integers(0, out_h))
        br = int(rng.integers(8, 34))
        col = (251, 191, 36, 32) if mood != "sad_heartbreak" else (56, 189, 248, 30)
        draw.ellipse([bx - br, by - br, bx + br, by + br], fill=col)
    return base


def render_2d_lipsync_scene_clip(
    dst_mp4: str,
    wav_path: str,
    dur_sec: float,
    out_w: int,
    out_h: int,
    speaker: str,
    emotion: str,
    mood: str,
    chat_bubble: str = "",
    bg_img_path: Optional[str] = None,
):
    """
    Renders a 24fps 2D Anime Lip-Syncing Storyteller scene directly via FFmpeg rawvideo pipe:
    - Real-time phoneme mouth shapes (Closed, Soft, Open, Wide) driven by speech amplitude
    - Natural eye blinks every ~3.2s + emotion eyebrows
    - Breathing & subtle head-tilt motion + Rule-of-Thirds 16:9 framing
    - Optional animated chat bubble pop-up
    """
    fps = 24
    total_frames = max(24, int(round(dur_sec * fps)))
    amp_env = extract_audio_amplitude_envelope(wav_path, fps=fps)
    base_bg = build_2d_character_base(out_w, out_h, speaker, mood, bg_img_path=bg_img_path)

    is_female = speaker == "female"
    # Rule-of-Thirds positioning in 16:9 widescreen
    base_cx = int(out_w * (0.64 if is_female else (0.36 if speaker == "male" else 0.42)))
    base_cy = int(out_h * 0.50)
    scale_u = out_h / 540.0

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
        "-pix_fmt", "yuv420p", dst_mp4,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        for f_idx in range(total_frames):
            t = f_idx / float(fps)
            prog = f_idx / float(max(1, total_frames - 1))
            amp = float(amp_env[f_idx]) if f_idx < len(amp_env) else 0.0

            # Subtle zoom + breathing + conversational head nod
            breath_y = math.sin(t * 2.4) * 3.0 * scale_u
            nod_y = amp * 3.5 * math.sin(t * 11.0) * scale_u
            sway_x = math.sin(t * 1.1) * 4.0 * scale_u
            cx = int(base_cx + sway_x)
            cy = int(base_cy + breath_y + nod_y)

            frame = base_bg.copy()
            draw = ImageDraw.Draw(frame, "RGBA")

            # Colors by character role
            skin = (242, 206, 184, 255) if is_female else (228, 188, 164, 255)
            skin_shadow = (205, 162, 138, 255)
            hair = (24, 18, 34, 255) if is_female else (18, 22, 36, 255)
            coat = (131, 24, 67, 255) if is_female else (15, 23, 42, 255)
            accent = (244, 114, 182, 255) if is_female else (56, 189, 248, 255)

            # Rim-light aura around silhouette
            draw.ellipse(
                [cx - int(115 * scale_u), cy - int(145 * scale_u), cx + int(115 * scale_u), cy + int(220 * scale_u)],
                fill=(accent[0], accent[1], accent[2], 32),
            )

            # Back hair (for female character)
            if is_female:
                draw.rounded_rectangle(
                    [cx - int(82 * scale_u), cy - int(110 * scale_u), cx + int(82 * scale_u), cy + int(165 * scale_u)],
                    radius=int(48 * scale_u),
                    fill=hair,
                )

            # Shoulders & Jacket
            draw.pieslice(
                [cx - int(145 * scale_u), cy + int(78 * scale_u), cx + int(145 * scale_u), cy + int(310 * scale_u)],
                start=180, end=360, fill=coat, outline=accent, width=2,
            )
            # Inner shirt V-neck
            draw.polygon(
                [
                    (cx - int(32 * scale_u), cy + int(85 * scale_u)),
                    (cx + int(32 * scale_u), cy + int(85 * scale_u)),
                    (cx, cy + int(155 * scale_u)),
                ],
                fill=(241, 245, 249, 230),
            )

            # Neck
            draw.rectangle(
                [cx - int(22 * scale_u), cy + int(42 * scale_u), cx + int(22 * scale_u), cy + int(96 * scale_u)],
                fill=skin_shadow,
            )

            # Anime Jaw & Face Contour
            head_w = int(64 * scale_u)
            head_h = int(78 * scale_u)
            draw.ellipse([cx - head_w, cy - head_h, cx + head_w, cy + int(62 * scale_u)], fill=skin)
            draw.polygon(
                [
                    (cx - int(58 * scale_u), cy + int(10 * scale_u)),
                    (cx + int(58 * scale_u), cy + int(10 * scale_u)),
                    (cx, cy + int(74 * scale_u)),
                ],
                fill=skin,
            )

            # Eyes (Natural Blink every 3.2 seconds)
            blink_cycle = t % 3.2
            is_blinking = 2.95 <= blink_cycle <= 3.12
            eye_y = cy - int(8 * scale_u)
            eye_dx = int(28 * scale_u)

            for side in (-1, 1):
                ex = cx + side * eye_dx
                if is_blinking:
                    draw.line(
                        [(ex - int(13 * scale_u), eye_y), (ex + int(13 * scale_u), eye_y)],
                        fill=(30, 20, 30, 255), width=max(2, int(3 * scale_u)),
                    )
                else:
                    # Eye white + glowing iris + catchlight
                    draw.ellipse(
                        [ex - int(13 * scale_u), eye_y - int(8 * scale_u), ex + int(13 * scale_u), eye_y + int(8 * scale_u)],
                        fill=(255, 255, 255, 255),
                    )
                    iris_col = (190, 24, 93, 255) if is_female else (14, 116, 144, 255)
                    draw.ellipse(
                        [ex - int(7 * scale_u), eye_y - int(7 * scale_u), ex + int(7 * scale_u), eye_y + int(7 * scale_u)],
                        fill=iris_col,
                    )
                    draw.ellipse(
                        [ex - int(3 * scale_u), eye_y - int(3 * scale_u), ex + int(3 * scale_u), eye_y + int(3 * scale_u)],
                        fill=(10, 10, 18, 255),
                    )
                    draw.ellipse(
                        [ex - int(4 * scale_u), eye_y - int(5 * scale_u), ex - int(1 * scale_u), eye_y - int(2 * scale_u)],
                        fill=(255, 255, 255, 255),
                    )

                # Emotion-adaptive eyebrows
                brow_y = eye_y - int(16 * scale_u)
                tilt = int(4 * scale_u) if emotion in ("urgent_passion", "emotional_ache") else 0
                draw.line(
                    [(ex - int(14 * scale_u), brow_y - side * tilt), (ex + int(14 * scale_u), brow_y + side * tilt)],
                    fill=hair, width=max(2, int(3 * scale_u)),
                )

            # Subtle nose line
            draw.line(
                [(cx, cy + int(4 * scale_u)), (cx - int(3 * scale_u), cy + int(18 * scale_u))],
                fill=skin_shadow, width=max(1, int(2 * scale_u)),
            )

            # 4-State Phoneme Lip-Sync Mouth
            mouth_y = cy + int(36 * scale_u)
            if amp < 0.08:
                # Closed subtle line
                draw.arc(
                    [cx - int(12 * scale_u), mouth_y - int(4 * scale_u), cx + int(12 * scale_u), mouth_y + int(6 * scale_u)],
                    start=15, end=165, fill=(140, 55, 68, 255), width=max(2, int(2.5 * scale_u)),
                )
            elif amp < 0.28:
                # Soft open
                draw.ellipse(
                    [cx - int(11 * scale_u), mouth_y - int(3 * scale_u), cx + int(11 * scale_u), mouth_y + int(5 * scale_u)],
                    fill=(136, 19, 55, 255), outline=(190, 70, 85, 255),
                )
            elif amp < 0.55:
                # Medium open
                draw.ellipse(
                    [cx - int(14 * scale_u), mouth_y - int(5 * scale_u), cx + int(14 * scale_u), mouth_y + int(9 * scale_u)],
                    fill=(120, 14, 48, 255), outline=(210, 80, 95, 255),
                )
            else:
                # Wide expressive phoneme
                draw.ellipse(
                    [cx - int(16 * scale_u), mouth_y - int(7 * scale_u), cx + int(16 * scale_u), mouth_y + int(12 * scale_u)],
                    fill=(105, 10, 42, 255), outline=(225, 90, 105, 255),
                )

            # Styled Front Anime Hair Bangs (with secondary motion sway)
            hair_sway = int(math.sin(t * 2.2 - 0.5) * 3.0 * scale_u)
            draw.pieslice(
                [cx - int(70 * scale_u), cy - int(96 * scale_u), cx + int(70 * scale_u), cy - int(8 * scale_u)],
                start=180, end=360, fill=hair,
            )
            draw.polygon(
                [
                    (cx - int(45 * scale_u), cy - int(55 * scale_u)),
                    (cx - int(12 * scale_u), cy - int(55 * scale_u)),
                    (cx - int(26 * scale_u) + hair_sway, cy - int(14 * scale_u)),
                ],
                fill=hair,
            )
            draw.polygon(
                [
                    (cx + int(8 * scale_u), cy - int(55 * scale_u)),
                    (cx + int(44 * scale_u), cy - int(55 * scale_u)),
                    (cx + int(24 * scale_u) + hair_sway, cy - int(16 * scale_u)),
                ],
                fill=hair,
            )

            # In-Scene Smartphone Chat Pop-up if triggered
            if chat_bubble:
                draw_chat_bubble_overlay(draw, out_w, out_h, chat_bubble, prog)

            # Smooth fade in/out at clip edges
            if t < 0.20:
                alpha = int((1.0 - t / 0.20) * 255)
                frame = Image.blend(frame, Image.new("RGB", (out_w, out_h), (0, 0, 0)), alpha / 255.0)
            elif dur_sec - t < 0.20:
                alpha = int((1.0 - (dur_sec - t) / 0.20) * 255)
                frame = Image.blend(frame, Image.new("RGB", (out_w, out_h), (0, 0, 0)), alpha / 255.0)

            if proc.stdin:
                proc.stdin.write(frame.tobytes())
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait()


# ==============================================================================
# 5. GLASSMORPHIC CINEMA SUBTITLE ENGINE (ZERO STACKING & SPEAKER ACCENTS)
# ==============================================================================
def build_word_timings(text: str, dur_sec: float) -> List[Tuple[str, float, float]]:
    words = str(text or "").split()
    if not words:
        return []
    weights: List[float] = []
    for w in words:
        base_w = max(2.0, float(len(re.sub(r"[^a-zA-Z0-9]", "", w))))
        if w.endswith((",", ";", ":")):
            base_w += 2.2
        elif w.endswith((".", "!", "?", "।")):
            base_w += 3.2
        weights.append(base_w)
    total_w = sum(weights) or 1.0
    span = max(0.3, dur_sec - 0.18)
    timings: List[Tuple[str, float, float]] = []
    acc = 0.0
    for w, wt in zip(words, weights):
        st = 0.06 + (acc / total_w) * span
        acc += wt
        en = 0.06 + (acc / total_w) * span
        timings.append((w, st, en))
    return timings


def fmt_ass_time(sec: float) -> str:
    s = max(0.0, sec)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    rem = s % 60
    return f"{h}:{m:02d}:{rem:05.2f}"


def generate_ass_subtitle_file(
    ass_path: str,
    render_scenes: List[Dict[str, Any]],
    out_w: int,
    out_h: int,
    mood: str,
):
    font_sz = 33 if out_w > out_h else 29
    hud_sz = 16 if out_w > out_h else 14
    margin_v = 42 if out_w > out_h else 115

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {out_w}
PlayResY: {out_h}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CapStyle,DejaVu Sans,{font_sz},&H00FFFFFF&,&H0094A3B8&,&H00080A14&,&HB8060812&,-1,0,0,0,100,100,0,0,3,5,0,2,32,32,{margin_v},1
Style: HookStyle,DejaVu Sans,{hud_sz},&H00FFFFFF&,&H00FFFFFF&,&H007727DB&,&HD87727DB&,-1,0,0,0,100,100,0,0,3,6,0,7,22,22,22,1
Style: StageStyle,DejaVu Sans,{hud_sz - 1},&H00B472F4&,&H00B472F4&,&H000A0C18&,&HC00A0C18&,-1,0,0,0,100,100,0,0,3,5,0,7,22,22,22,1
Style: EndStyle,DejaVu Sans,{font_sz + 2},&H00B472F4&,&H00FFFFFF&,&H00000000&,&HD805070F&,-1,0,0,0,100,100,0,0,3,10,0,5,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: List[str] = []
    cur_t = 0.0
    total_dur = sum(float(s.get("duration_sec", 4.0)) for s in render_scenes) or 1.0
    end_card_start = max(0.0, total_dur - 2.6) if total_dur > 8.0 else total_dur

    for sc in render_scenes:
        dur = float(sc.get("duration_sec", 4.0))
        st_abs = cur_t
        en_abs = cur_t + dur
        is_teaser = bool(sc.get("is_teaser", False))
        sp = sc.get("speaker", "narrator")
        ov_p = st_abs / total_dur

        # Speaker-specific kinetic highlight color (BGR)
        if sp == "female":
            accent_bgr = "&H00B472F4&"  # Rose Pink
        elif sp == "male":
            accent_bgr = "&H00F8BD38&"  # Electric Cyan
        else:
            accent_bgr = "&H0047D3FB&"  # Warm Cinema Gold

        if is_teaser:
            ts_lbl = sc.get("climax_ts_label", "01:45")
            events.append(
                f"Dialogue: 2,{fmt_ass_time(st_abs)},{fmt_ass_time(en_abs - 0.02)},HookStyle,,0,0,0,,AAGE DEKHIYE ({ts_lbl} PE ASLI MOD)..."
            )
        else:
            stage = "01. PEHLI MULAKAT" if ov_p < 0.34 else ("02. GEHRA RAAZ" if ov_p < 0.72 else "03. ASLI ANJAAM")
            hud_end = min(en_abs - 0.02, end_card_start)
            if hud_end > st_abs + 0.1:
                events.append(
                    f"Dialogue: 2,{fmt_ass_time(st_abs)},{fmt_ass_time(hud_end)},StageStyle,,0,0,0,,{stage}"
                )

        timings = build_word_timings(sc.get("auto_caption", ""), dur)
        group_size = 3
        total_w_cnt = len(timings)

        for g_start in range(0, total_w_cnt, group_size):
            grp = timings[g_start : g_start + group_size]
            next_g_idx = g_start + group_size
            if next_g_idx < total_w_cnt:
                grp_end_abs = st_abs + timings[next_g_idx][1] - 0.01
            else:
                grp_end_abs = en_abs - 0.05

            for local_i, (w_str, w_st, w_en) in enumerate(grp):
                seg_s = st_abs + w_st
                if local_i + 1 < len(grp):
                    seg_e = st_abs + grp[local_i + 1][1] - 0.005
                else:
                    seg_e = grp_end_abs

                seg_e = min(grp_end_abs, seg_e)
                if seg_e <= seg_s + 0.03:
                    continue

                styled_words: List[str] = []
                for j, (jw, _, _) in enumerate(grp):
                    clean_jw = re.sub(r"[^\x20-\x7E]", "", jw).replace("{", "").replace("}", "")
                    if j == local_i:
                        styled_words.append(f"{{\\c{accent_bgr}\\fscx106\\fscy106}}{clean_jw}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    elif j < local_i:
                        styled_words.append(clean_jw)
                    else:
                        styled_words.append(f"{{\\c&H0094A3B8&}}{clean_jw}{{\\c&H00FFFFFF&}}")

                line_txt = " ".join(styled_words)
                events.append(
                    f"Dialogue: 1,{fmt_ass_time(seg_s)},{fmt_ass_time(seg_e)},CapStyle,,0,0,0,,{line_txt}"
                )

        cur_t = en_abs

    if total_dur > 8.0:
        events.append(
            f"Dialogue: 3,{fmt_ass_time(end_card_start)},{fmt_ass_time(total_dur)},EndStyle,,0,0,0,,KAHANI ABHI BAAKI HAI...\\N{{\\fs{font_sz - 8}\\c&H0071E0FD&}}Agle Part Ke Liye Subscribe Zaroor Karein"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")


# ==============================================================================
# 6. ELEVENLABS-PLUS VOICE MASTERING & 3D STEREO FOLEY ENGINE
# ==============================================================================
class NexoraCinemaAudioEngine:
    @staticmethod
    def probe_dur(path: str) -> float:
        if not HAS_FFPROBE or not os.path.exists(path):
            return 0.0
        try:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, check=True,
            )
            return float(res.stdout.strip())
        except Exception:
            return 0.0

    @classmethod
    def get_romantic_voice_profile(cls, sc: Dict[str, Any], voice_mode: str) -> Dict[str, str]:
        sp = sc.get("speaker", "narrator").lower()
        emo = sc.get("emotion", "late_night_fm")

        if voice_mode == "builtin_multi":
            if sp == "female":
                if emo == "intimate_whisper":
                    return {"voice": "hi-IN-SwaraNeural", "rate": "-4%", "pitch": "-3Hz"}
                if emo == "emotional_ache":
                    return {"voice": "hi-IN-SwaraNeural", "rate": "-2%", "pitch": "-2Hz"}
                return {"voice": "hi-IN-SwaraNeural", "rate": "+3%", "pitch": "-1Hz"}
            if sp == "male":
                if emo == "intimate_whisper":
                    return {"voice": "hi-IN-MadhurNeural", "rate": "-5%", "pitch": "-11Hz"}
                if emo == "emotional_ache":
                    return {"voice": "hi-IN-MadhurNeural", "rate": "-2%", "pitch": "-9Hz"}
                return {"voice": "hi-IN-MadhurNeural", "rate": "+3%", "pitch": "-8Hz"}
            if sp == "child":
                return {"voice": "hi-IN-SwaraNeural", "rate": "+8%", "pitch": "+13Hz"}

        if emo == "intimate_whisper":
            return {"voice": "hi-IN-MadhurNeural", "rate": "-5%", "pitch": "-12Hz"}
        if emo == "emotional_ache":
            return {"voice": "hi-IN-MadhurNeural", "rate": "-2%", "pitch": "-10Hz"}
        if emo == "urgent_passion":
            return {"voice": "hi-IN-MadhurNeural", "rate": "+7%", "pitch": "-8Hz"}
        return {"voice": "hi-IN-MadhurNeural", "rate": "+1%", "pitch": "-10Hz"}

    @staticmethod
    def generate_soft_inhale_breath(dur_sec: float = 0.24, rate: int = 22050) -> np.ndarray:
        """Synthesizes a subtle human pre-dialogue breath inhale for intimate realism."""
        n = max(10, int(rate * dur_sec))
        t = np.linspace(0, dur_sec, n, endpoint=False, dtype=np.float32)
        rng = np.random.default_rng(77)
        raw = rng.normal(0.0, 0.035, n).astype(np.float32)
        band = np.convolve(raw, np.ones(14) / 14.0, mode="same")
        env = np.sin(np.pi * (t / dur_sec)) ** 1.8
        return band * env

    @classmethod
    def dub_scene_studio_stereo(cls, sc: Dict[str, Any], idx: int, voice_mode: str, work_dir: str) -> Tuple[str, float]:
        """
        Generates 44.1kHz Stereo Studio Voice with:
        - Pre-dialogue soft human inhale breath
        - 95Hz Chest Resonance + 280Hz Mud Cut + 3.2kHz Presence EQ
        - Spatial stereo panning (Male slightly left, Female slightly right, Narrator center)
        """
        if not HAS_EDGE_TTS:
            raise RuntimeError("edge-tts is not installed on the server.")

        prof = cls.get_romantic_voice_profile(sc, voice_mode)
        text = str(sc.get("hindi_dub") or "").strip() or "कहानी आगे बढ़ती है..."
        sp = sc.get("speaker", "narrator")
        emo = sc.get("emotion", "late_night_fm")

        mp3_p = os.path.join(work_dir, f"v_{idx:03d}.mp3")
        mono_wav = os.path.join(work_dir, f"vm_{idx:03d}.wav")
        stereo_wav = os.path.join(work_dir, f"v_{idx:03d}.wav")

        last_err = None
        for attempt in range(3):
            try:
                async def _synth():
                    comm = edge_tts.Communicate(text, voice=prof["voice"], rate=prof["rate"], pitch=prof["pitch"])
                    await comm.save(mp3_p)

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_synth())
                finally:
                    loop.close()

                if os.path.exists(mp3_p) and os.path.getsize(mp3_p) > 250:
                    break
            except Exception as e:
                last_err = e
                time.sleep(0.4 * (attempt + 1))

        speech_dur = cls.probe_dur(mp3_p)
        if speech_dur <= 0.20:
            raise RuntimeError(f"Voice timeout on Scene {idx + 1} ({last_err}).")

        dramatic_tail = 0.42 if emo in ("emotional_ache", "urgent_passion") else 0.30
        final_dur = round(speech_dur + dramatic_tail, 2)

        # ElevenLabs-Plus Acoustic Mastering Chain
        studio_af = (
            "highpass=f=65,"
            "equalizer=f=95:t=q:w=1.1:g=5.2,"
            "equalizer=f=280:t=q:w=1.3:g=-2.8,"
            "equalizer=f=3200:t=q:w=1.4:g=2.4,"
            "treble=g=-1.5:f=6500,"
            "aecho=0.88:0.90:18:0.08,"
            "acompressor=threshold=-18dB:ratio=3.0:attack=5:release=110,"
            f"apad=whole_dur={final_dur:.3f}"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_p,
                "-af", studio_af,
                "-t", f"{final_dur:.3f}",
                "-ar", "22050", "-ac", "1", mono_wav,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        if os.path.exists(mp3_p):
            os.remove(mp3_p)

        # Mix subtle pre-speech inhale breath & apply 3D Stereo Panning
        with wave.open(mono_wav, "rb") as wf:
            samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

        breath = cls.generate_soft_inhale_breath(0.22, rate=22050)
        b_len = min(len(samples), len(breath))
        samples[:b_len] += breath[:b_len]

        # Spatial Stereo Panning (Male = Left-Center, Female = Right-Center, Narrator = Center)
        if sp == "male":
            l_gain, r_gain = 1.04, 0.88
        elif sp == "female":
            l_gain, r_gain = 0.88, 1.04
        else:
            l_gain, r_gain = 1.0, 1.0

        stereo = np.empty((len(samples), 2), dtype=np.int16)
        stereo[:, 0] = (np.clip(samples * l_gain, -0.98, 0.98) * 32767.0).astype(np.int16)
        stereo[:, 1] = (np.clip(samples * r_gain, -0.98, 0.98) * 32767.0).astype(np.int16)

        with wave.open(stereo_wav, "wb") as swf:
            swf.setnchannels(2)
            swf.setsampwidth(2)
            swf.setframerate(22050)
            swf.writeframes(stereo.tobytes())

        if os.path.exists(mono_wav):
            os.remove(mono_wav)
        return stereo_wav, final_dur

    @staticmethod
    def generate_3d_foley_sfx(sfx_type: str, n_samples: int, rate: int = 22050) -> np.ndarray:
        """Generates stereo 3D foley sound effects (Lub-Dub Heartbeat, Rain on Glass, Sub-Bass Drop)."""
        out = np.zeros((n_samples, 2), dtype=np.float32)
        sfx_len = min(n_samples, int(rate * 1.95))
        if sfx_len <= 10:
            return out
        t = np.linspace(0, sfx_len / float(rate), sfx_len, endpoint=False, dtype=np.float32)
        rng = np.random.default_rng(202)

        if sfx_type == "phone_chime":
            c1 = np.exp(-t * 8.0) * np.sin(2.0 * np.pi * 659.25 * t)
            c2 = np.exp(-np.maximum(0.0, t - 0.14) * 7.0) * np.sin(2.0 * np.pi * 987.77 * t) * (t >= 0.14)
            sig = (c1 + c2) * 0.28
            out[:sfx_len, 0] = sig * 0.85
            out[:sfx_len, 1] = sig * 1.15
        elif sfx_type == "soft_rain":
            rain_l = np.convolve(rng.normal(0.0, 0.22, sfx_len).astype(np.float32), np.ones(16) / 16.0, mode="same")
            rain_r = np.convolve(rng.normal(0.0, 0.22, sfx_len).astype(np.float32), np.ones(16) / 16.0, mode="same")
            env = np.sin(np.pi * (t / (sfx_len / float(rate))))
            out[:sfx_len, 0] = rain_l * env * 0.55
            out[:sfx_len, 1] = rain_r * env * 0.55
        elif sfx_type == "soft_heartbeat":
            # Realistic 65-110 BPM Lub-Dub + Sub-Bass thump
            b1 = np.exp(-t * 13.0) * np.sin(2.0 * np.pi * 55.0 * t)
            b2 = np.exp(-np.maximum(0.0, t - 0.28) * 13.0) * np.sin(2.0 * np.pi * 48.0 * t) * (t >= 0.28)
            sig = (b1 + b2) * 0.68
            out[:sfx_len, 0] = sig
            out[:sfx_len, 1] = sig
        elif sfx_type == "emotional_swell":
            f1 = np.sin(2.0 * np.pi * 220.00 * t)
            f2 = np.sin(2.0 * np.pi * 261.63 * t)
            f3 = np.sin(2.0 * np.pi * 329.63 * t)
            env = np.sin(np.pi * (t / (sfx_len / float(rate)))) ** 1.6
            sig = (0.45 * f1 + 0.35 * f2 + 0.30 * f3) * env * 0.25
            out[:sfx_len, 0] = sig * 1.05
            out[:sfx_len, 1] = sig * 0.95
        elif sfx_type == "clock_tick":
            for tick_t in (0.15, 0.85, 1.55):
                mask = (t >= tick_t) & (t < tick_t + 0.05)
                lt = np.maximum(0.0, t - tick_t)
                sig = mask * np.exp(-lt * 90.0) * np.sin(2.0 * np.pi * 1150.0 * lt) * 0.35
                out[:sfx_len, 0] += sig * 0.8
                out[:sfx_len, 1] += sig * 1.2
        elif sfx_type == "door_creak":
            freq = 175.0 + 90.0 * np.sin(2.0 * np.pi * 3.8 * t)
            phase = 2.0 * np.pi * np.cumsum(freq) / float(rate)
            env = np.sin(np.pi * (t / (sfx_len / float(rate)))) ** 1.5
            sig = np.sin(phase) * env * 0.25
            out[:sfx_len, 0] = sig * 1.15
            out[:sfx_len, 1] = sig * 0.85
        elif sfx_type == "thunder":
            rumble = np.convolve(rng.normal(0.0, 0.45, sfx_len).astype(np.float32), np.ones(32) / 32.0, mode="same")
            env = np.exp(-t * 1.9) * (1.0 - np.exp(-t * 45.0))
            sig = rumble * env * 0.72
            out[:sfx_len, 0] = sig
            out[:sfx_len, 1] = sig
        else:
            noise = np.convolve(rng.normal(0.0, 0.26, sfx_len).astype(np.float32), np.ones(16) / 16.0, mode="same")
            env = np.sin(np.pi * (t / (sfx_len / float(rate)))) ** 2.0
            sig = noise * env * 0.38
            out[:sfx_len, 0] = sig
            out[:sfx_len, 1] = sig

        return out

    @classmethod
    def build_stereo_soundscape(
        cls,
        render_scenes: List[Dict[str, Any]],
        mood: str,
        out_wav: str,
        custom_bgm_path: Optional[str] = None,
        work_dir: str = "",
    ):
        rate = 22050
        has_custom = bool(custom_bgm_path and os.path.exists(custom_bgm_path))
        target_wav = os.path.join(work_dir, "sfx_only.wav") if (has_custom and work_dir) else out_wav

        with wave.open(target_wav, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            cur_t = 0.0

            for sc in render_scenes:
                dur = float(sc.get("duration_sec", 4.0))
                n = max(1, int(round(dur * rate)))
                t = np.linspace(cur_t, cur_t + dur, n, endpoint=False, dtype=np.float32)
                local_p = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
                swell = 1.0 + 0.42 * (np.exp(-local_p * 5.0) + np.exp(-(1.0 - local_p) * 5.0))

                if has_custom:
                    bgm_mono = np.zeros(n, dtype=np.float32)
                elif mood == "sad_heartbreak":
                    pad = (
                        0.45 * np.sin(2.0 * np.pi * 146.83 * t)
                        + 0.35 * np.sin(2.0 * np.pi * 174.61 * t)
                        + 0.28 * np.sin(2.0 * np.pi * 220.00 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 73.42 * t)
                    )
                    bgm_mono = pad * 0.13 * swell
                elif mood == "secret_suspense":
                    pulse = 0.8 + 0.2 * np.sin(2.0 * np.pi * 0.6 * t)
                    pad = (
                        0.42 * np.sin(2.0 * np.pi * 110.00 * t)
                        + 0.32 * np.sin(2.0 * np.pi * 130.81 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 164.81 * t)
                        + 0.22 * np.sin(2.0 * np.pi * 55.00 * t)
                    )
                    bgm_mono = pad * 0.13 * swell * pulse
                else:
                    pad = (
                        0.42 * np.sin(2.0 * np.pi * 130.81 * t)
                        + 0.34 * np.sin(2.0 * np.pi * 164.81 * t)
                        + 0.28 * np.sin(2.0 * np.pi * 196.00 * t)
                        + 0.22 * np.sin(2.0 * np.pi * 65.41 * t)
                    )
                    bgm_mono = pad * 0.13 * swell

                sfx_stereo = cls.generate_3d_foley_sfx(sc.get("sfx_type", "whoosh"), n, rate=rate)
                mix_stereo = np.empty((n, 2), dtype=np.float32)
                mix_stereo[:, 0] = bgm_mono + sfx_stereo[:, 0]
                mix_stereo[:, 1] = bgm_mono + sfx_stereo[:, 1]

                wf.writeframes((np.clip(mix_stereo, -0.95, 0.95) * 32767.0).astype(np.int16).tobytes())
                cur_t += dur
                gc.collect()

        if has_custom:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-stream_loop", "-1", "-i", custom_bgm_path,
                    "-i", target_wav,
                    "-filter_complex", "[0:a]volume=0.30[bg];[1:a]volume=1.0[sx];[bg][sx]amix=inputs=2:duration=shortest[out]",
                    "-map", "[out]", "-ar", "22050", "-ac", "2", out_wav,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )


# ==============================================================================
# 7. TRI-HYBRID 16:9 RENDERER (FLOW VIDEO + 3D PARALLAX AI + 2D LIP-SYNC HOST)
# ==============================================================================
def update_job(job_id: str, **kwargs):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def fmt_mmss(sec: float) -> str:
    s = max(0, int(round(sec)))
    return f"{s // 60:02d}:{s % 60:02d}"


def render_flow_video_clip(src_video: str, dst_mp4: str, target_dur: float, out_w: int, out_h: int):
    src_dur = NexoraCinemaAudioEngine.probe_dur(src_video)
    fade_out_st = max(0.1, target_dur - 0.22)

    if src_dur > 0.8 and 0.45 <= (target_dur / src_dur) <= 1.85:
        pts_mult = target_dur / src_dur
        vf = (
            f"setpts={pts_mult:.4f}*PTS,"
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},"
            "fps=24,"
            "eq=contrast=1.09:brightness=-0.01:saturation=1.22,"
            "colorbalance=rs=0.04:gs=0.01:bs=-0.03,"
            "vignette=PI/5,"
            f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out_st:.3f}:d=0.20"
        )
        cmd = [
            "ffmpeg", "-y", "-i", src_video, "-t", f"{target_dur:.3f}",
            "-an", "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", dst_mp4,
        ]
    else:
        vf = (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},"
            "fps=24,"
            "eq=contrast=1.09:brightness=-0.01:saturation=1.22,"
            "colorbalance=rs=0.04:gs=0.01:bs=-0.03,"
            "vignette=PI/5,"
            f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out_st:.3f}:d=0.20"
        )
        cmd = [
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", src_video, "-t", f"{target_dur:.3f}",
            "-an", "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", dst_mp4,
        ]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def render_3d_parallax_ai_clip(
    src_img: str,
    dst_mp4: str,
    dur_sec: float,
    out_w: int,
    out_h: int,
    motion: str,
    sfx_type: str,
    crop_bottom_wm: bool = True,
):
    """Renders 24fps 3D Camera Motion + Dynamic Rain/Golden Bokeh Particle Overlay."""
    fps = 24
    total_f = max(24, int(round(dur_sec * fps)))
    fade_out_st = max(0.1, dur_sec - 0.22)
    up_w, up_h = out_w * 2, out_h * 2

    if motion == "Pan_Right_Drift":
        zp = f"zoompan=z='1.12':x='(iw-iw/zoom)*(on/{total_f})':y='(ih-ih/zoom)*0.35':d={total_f}:s={out_w}x{out_h}:fps={fps}"
    elif motion == "Pan_Left_Drift":
        zp = f"zoompan=z='1.12':x='(iw-iw/zoom)*(1-on/{total_f})':y='(ih-ih/zoom)*0.35':d={total_f}:s={out_w}x{out_h}:fps={fps}"
    elif motion == "Zoom_Out_Reveal":
        zp = f"zoompan=z='max(1.02,1.18-0.16*(on/{total_f}))':x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*0.35':d={total_f}:s={out_w}x{out_h}:fps={fps}"
    elif motion == "Dramatic_Pulse_Zoom":
        zp = f"zoompan=z='1.04+0.14*(on/{total_f})':x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*0.35':d={total_f}:s={out_w}x{out_h}:fps={fps}"
    else:
        zp = f"zoompan=z='min(1.18,1.02+0.16*(on/{total_f}))':x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*0.35':d={total_f}:s={out_w}x{out_h}:fps={fps}"

    wm_crop = "crop=iw:ih*0.94:0:0," if crop_bottom_wm else ""

    # Add real-time weather/bokeh shimmer when rain or candlelight scene
    weather_fx = ""
    if sfx_type == "soft_rain":
        weather_fx = "noise=alls=8:allf=t+u,"

    vf = (
        f"{wm_crop}"
        f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
        f"crop={up_w}:{up_h},"
        f"{zp},"
        f"{weather_fx}"
        "eq=contrast=1.09:brightness=-0.01:saturation=1.22,"
        "colorbalance=rs=0.04:gs=0.01:bs=-0.03,"
        "vignette=PI/5,"
        f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out_st:.3f}:d=0.20"
    )
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-loop", "1", "-i", src_img,
            "-t", f"{dur_sec:.3f}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", dst_mp4,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )


def run_final_render(job_id: str, session_id: str):
    work_dir = tempfile.mkdtemp(prefix=f"nxr_{job_id}_", dir=TMP_ROOT)
    try:
        purge_expired(1800)
        if not HAS_FFMPEG:
            raise RuntimeError("FFmpeg not available on server.")

        with LOCK:
            sess = SESSIONS.get(session_id)
        if not sess:
            raise RuntimeError("Session expired. Please reload scenes.")

        raw_scenes = [dict(s) for s in sess["scenes"]]
        sess_dir = os.path.join(SESSIONS_DIR, session_id)

        mood = sess["genre"]
        topic = sess["topic"]
        aspect = sess["aspect_ratio"]
        voice_mode = sess["voice_mode"]
        character_dna = sess.get("character_dna", "")
        auto_hook = bool(sess.get("auto_hook", True))
        uploaded_audio_id = sess.get("uploaded_audio_id", "")
        uploaded_bgm_id = sess.get("uploaded_bgm_id", "")

        out_w, out_h = (540, 960) if aspect == "9:16" else (960, 540)

        # 1. Fetch AI images ONLY for scenes in 'ai_scene' mode (Pixel-Guard protected)
        ai_image_map = fetch_ai_images_with_pixel_guard(raw_scenes, character_dna, work_dir, out_w, out_h, job_id)

        # 2. Dub ElevenLabs-Plus Stereo Studio Voices
        master_voice = os.path.join(work_dir, "master_voice.wav")
        voice_chunks: List[str] = []

        if voice_mode == "uploaded":
            if not is_safe_id(uploaded_audio_id):
                raise RuntimeError("Please upload your MP3/WAV voiceover first!")
            uploaded_audio_path = None
            for fn in os.listdir(UPLOAD_DIR):
                if fn.startswith(uploaded_audio_id):
                    uploaded_audio_path = os.path.join(UPLOAD_DIR, fn)
                    break
            if not uploaded_audio_path:
                raise FileNotFoundError("Uploaded audio file not found.")

            update_job(job_id, progress=24, current_scene="Syncing External Voiceover...")
            subprocess.run(
                ["ffmpeg", "-y", "-i", uploaded_audio_path, "-ar", "22050", "-ac", "2", master_voice],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            total_el_dur = NexoraCinemaAudioEngine.probe_dur(master_voice)
            per_sc = max(1.5, total_el_dur / float(len(raw_scenes)))
            for idx, sc in enumerate(raw_scenes):
                sc["start_sec"] = idx * per_sc
                sc["duration_sec"] = per_sc
                sc["wav_file"] = master_voice
        else:
            running_t = 0.0
            for idx, sc in enumerate(raw_scenes):
                update_job(
                    job_id,
                    status="running",
                    progress=22 + int((idx / float(len(raw_scenes))) * 18),
                    current_scene=f"Mastering Stereo Studio Voice ({idx + 1}/{len(raw_scenes)})...",
                )
                dub_wav, actual_dur = NexoraCinemaAudioEngine.dub_scene_studio_stereo(sc, idx, voice_mode, work_dir)
                sc["start_sec"] = running_t
                sc["end_sec"] = running_t + actual_dur
                sc["duration_sec"] = actual_dur
                sc["wav_file"] = dub_wav
                running_t += actual_dur
                voice_chunks.append(dub_wav)

            a_list = os.path.join(work_dir, "a_list.txt")
            with open(a_list, "w", encoding="utf-8") as af:
                for ac in voice_chunks:
                    af.write(f"file '{ac}'\n")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", a_list, "-c", "copy", master_voice],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )

        # 3. Prepend 0:00 Climax Hook Teaser
        render_scenes: List[Dict[str, Any]] = []
        if auto_hook and len(raw_scenes) >= 4:
            climax_idx = min(len(raw_scenes) - 2, max(2, int(len(raw_scenes) * 0.75)))
            climax_sc = raw_scenes[climax_idx]
            hook_sc = dict(climax_sc)
            hook_sc["is_teaser"] = True
            hook_sc["sfx_type"] = "soft_heartbeat"
            hook_dur = min(4.2, float(climax_sc.get("duration_sec", 4.0)))
            hook_sc["duration_sec"] = hook_dur
            hook_sc["climax_ts_label"] = fmt_mmss(float(climax_sc.get("start_sec", 60.0)) + hook_dur)

            hook_wav = os.path.join(work_dir, "hook_voice.wav")
            joined_voice = os.path.join(work_dir, "joined_voice.wav")
            if voice_mode != "uploaded" and climax_sc.get("wav_file") and os.path.exists(climax_sc["wav_file"]):
                subprocess.run(
                    ["ffmpeg", "-y", "-i", climax_sc["wav_file"], "-t", f"{hook_dur:.3f}", "-ar", "22050", "-ac", "2", hook_wav],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                )
            else:
                climax_start = float(climax_sc.get("start_sec", 0.0))
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{climax_start:.3f}", "-t", f"{hook_dur:.3f}", "-i", master_voice, "-ar", "22050", "-ac", "2", hook_wav],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                )
            hook_sc["wav_file"] = hook_wav
            render_scenes.append(hook_sc)

            v_concat = os.path.join(work_dir, "v_concat.txt")
            with open(v_concat, "w", encoding="utf-8") as f:
                f.write(f"file '{hook_wav}'\nfile '{master_voice}'\n")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", v_concat, "-c", "copy", joined_voice],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            master_voice = joined_voice

        for s in raw_scenes:
            render_scenes.append(dict(s))

        # 4. Render Tri-Hybrid Visuals (Flow Video / 3D Parallax AI / 2D Lip-Sync Host)
        total_sc = len(render_scenes)
        video_chunks: List[str] = []
        last_good_ai_img = next(iter(ai_image_map.values()), None)

        for idx, sc in enumerate(render_scenes):
            v_mode = sc.get("visual_mode", "2d_host")
            lbl = "Climax Hook" if sc.get("is_teaser") else f"Scene {idx + 1}/{total_sc}"
            update_job(
                job_id,
                status="running",
                progress=42 + int((idx / float(total_sc)) * 46),
                current_scene=f"Rendering {lbl} [{v_mode.upper()}]...",
            )

            actual_dur = float(sc.get("duration_sec", 4.0))
            v_chunk = os.path.join(work_dir, f"vc_{idx:03d}.mp4")
            orig_idx = int(sc.get("index", 0))
            mtype = sc.get("media_type", "auto")

            if mtype == "video" and sc.get("media_file"):
                med_path = os.path.join(sess_dir, sc["media_file"])
                render_flow_video_clip(med_path, v_chunk, actual_dur, out_w, out_h)
            elif mtype == "image" and sc.get("media_file"):
                med_path = os.path.join(sess_dir, sc["media_file"])
                render_3d_parallax_ai_clip(
                    med_path, v_chunk, actual_dur, out_w, out_h, sc.get("anim_type", "Zoom_In_Push"), sc.get("sfx_type", "whoosh"), crop_bottom_wm=False
                )
            elif v_mode == "ai_scene" and orig_idx in ai_image_map:
                last_good_ai_img = ai_image_map[orig_idx]
                render_3d_parallax_ai_clip(
                    ai_image_map[orig_idx], v_chunk, actual_dur, out_w, out_h, sc.get("anim_type", "Zoom_In_Push"), sc.get("sfx_type", "whoosh"), crop_bottom_wm=True
                )
            else:
                # '2d_host' OR automatic zero-crash fallback when 'flow_clip' / 'ai_scene' has no external file
                render_2d_lipsync_scene_clip(
                    dst_mp4=v_chunk,
                    wav_path=sc.get("wav_file", master_voice),
                    dur_sec=actual_dur,
                    out_w=out_w,
                    out_h=out_h,
                    speaker=sc.get("speaker", "narrator"),
                    emotion=sc.get("emotion", "late_night_fm"),
                    mood=mood,
                    chat_bubble=sc.get("chat_bubble", ""),
                    bg_img_path=last_good_ai_img,
                )

            video_chunks.append(v_chunk)
            gc.collect()

        # 5. Burn Glassmorphic Captions & Master 3D Stereo Audio
        update_job(job_id, progress=90, current_scene="Mastering Glassmorphic Captions & 3D Cinema Audio...")

        ass_file = os.path.join(work_dir, "captions.ass")
        generate_ass_subtitle_file(ass_file, render_scenes, out_w, out_h, mood)

        v_list = os.path.join(work_dir, "v_list.txt")
        with open(v_list, "w", encoding="utf-8") as vf:
            for vc in video_chunks:
                vf.write(f"file '{vc}'\n")

        joined_v = os.path.join(work_dir, "joined_v.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", v_list, "-c", "copy", joined_v],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        for vc in video_chunks:
            if os.path.exists(vc):
                os.remove(vc)

        custom_bgm_path = None
        if is_safe_id(uploaded_bgm_id):
            for fn in os.listdir(UPLOAD_DIR):
                if fn.startswith(uploaded_bgm_id):
                    custom_bgm_path = os.path.join(UPLOAD_DIR, fn)
                    break

        sfx_bgm_wav = os.path.join(work_dir, "sfx_bgm.wav")
        NexoraCinemaAudioEngine.build_stereo_soundscape(
            render_scenes, mood, sfx_bgm_wav, custom_bgm_path=custom_bgm_path, work_dir=work_dir
        )

        if not os.path.exists(sfx_bgm_wav):
            total_v_dur = NexoraCinemaAudioEngine.probe_dur(joined_v) or 30.0
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=22050:cl=stereo:d={total_v_dur:.3f}", sfx_bgm_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", topic)[:32].strip("_") or "NEXORA_CINEMA"
        out_name = f"{slug}_{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        esc_ass = ass_file.replace("\\", "/").replace(":", "\\:")
        filter_complex = (
            f"[0:v]ass='{esc_ass}'[vout];"
            "[1:a]asplit=2[v_main][v_sc];"
            "[2:a][v_sc]sidechaincompress=threshold=0.03:ratio=4.2:attack=12:release=240[bg_ducked];"
            "[v_main]volume=1.40[v_loud];"
            "[bg_ducked]volume=0.65[bg_loud];"
            "[v_loud][bg_loud]amix=inputs=2:duration=first[aout]"
        )

        res = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", joined_v,
                "-i", master_voice,
                "-i", sfx_bgm_wav,
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", "-shortest", out_path,
            ],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg final master error: {res.stderr[-250:]}")

        actual_total_dur = NexoraCinemaAudioEngine.probe_dur(out_path) or 60.0
        size_mb = round(os.path.getsize(out_path) / (1024.0 * 1024.0), 2)
        dur_min = round(actual_total_dur / 60.0, 2)
        update_job(
            job_id,
            status="done",
            progress=100,
            current_scene=f"Completed ({dur_min} min • {size_mb} MB) & Auto-Downloading!",
            file_name=out_name,
            size_mb=size_mb,
            duration_min=dur_min,
        )

    except Exception as exc:
        update_job(job_id, status="error", error=str(exc), current_scene="Failed")
    finally:
        with LOCK:
            ACTIVE_JOBS.discard(job_id)
        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()


# ==============================================================================
# 8. REST API ENDPOINTS (TRI-HYBRID STUDIO CONTROLLER)
# ==============================================================================
def save_uploaded_media(file_storage, sess_dir: str, idx: int) -> Tuple[str, str, str]:
    ext = os.path.splitext(file_storage.filename or "")[1].lower()
    uid = uuid.uuid4().hex[:6]
    if ext in VIDEO_EXTS:
        vname = f"vid_{idx:03d}_{uid}{ext}"
        vpath = os.path.join(sess_dir, vname)
        file_storage.save(vpath)
        tname = f"thumb_{idx:03d}_{uid}.jpg"
        tpath = os.path.join(sess_dir, tname)
        subprocess.run(
            ["ffmpeg", "-y", "-i", vpath, "-vframes", "1", "-vf", "scale=480:-1", tpath],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not os.path.exists(tpath):
            Image.new("RGB", (480, 270), (15, 23, 42)).save(tpath)
        return "video", vname, tname

    iname = f"img_{idx:03d}_{uid}.jpg"
    ipath = os.path.join(sess_dir, iname)
    with Image.open(file_storage.stream) as raw_im:
        im = raw_im.convert("RGB")
        im.thumbnail((1280, 1280), Image.Resampling.BILINEAR)
        im.save(ipath, format="JPEG", quality=88)
    return "image", iname, iname


@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "ffmpeg": HAS_FFMPEG, "voice_ready": HAS_EDGE_TTS, "tri_hybrid_v26": True})


@app.route("/api/audio/upload", methods=["POST"])
def api_audio_upload():
    f = request.files.get("audio")
    if not f or not f.filename:
        return jsonify({"error": "Select an audio file first."}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".wav", ".mp3", ".m4a", ".ogg"):
        return jsonify({"error": "Supported formats: MP3, WAV, M4A, OGG."}), 400
    aud_id = f"el_{uuid.uuid4().hex[:12]}"
    f.save(os.path.join(UPLOAD_DIR, f"{aud_id}{ext}"))
    return jsonify({"ok": True, "audio_id": aud_id, "file_name": f.filename})


@app.route("/api/bgm/upload", methods=["POST"])
def api_bgm_upload():
    f = request.files.get("bgm")
    if not f or not f.filename:
        return jsonify({"error": "Select a BGM audio file first."}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".wav", ".mp3", ".m4a", ".ogg"):
        return jsonify({"error": "Supported formats: MP3, WAV, M4A, OGG."}), 400
    bgm_id = f"bgm_{uuid.uuid4().hex[:12]}"
    f.save(os.path.join(UPLOAD_DIR, f"{bgm_id}{ext}"))
    return jsonify({"ok": True, "bgm_id": bgm_id, "file_name": f.filename})


@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    data = request.get_json(silent=True) or {}
    mood = str(data.get("genre", "deep_romance")).lower()
    aspect = str(data.get("aspect_ratio", "16:9"))
    voice_mode = str(data.get("voice_mode", "builtin_multi"))
    raw_script = str(data.get("custom_script", ""))

    scenes, character_dna = parse_user_script(raw_script, mood, aspect)
    if not scenes:
        return jsonify({"error": "Paste a valid 3-column script first (Speaker | Visual Scene | Spoken Hindi)."}), 400

    auto_title = auto_derive_story_title(raw_script, str(data.get("topic", "")))
    session_id = f"ses_{uuid.uuid4().hex[:10]}"
    os.makedirs(os.path.join(SESSIONS_DIR, session_id), exist_ok=True)

    with LOCK:
        SESSIONS[session_id] = {
            "session_id": session_id,
            "topic": auto_title,
            "character_dna": character_dna,
            "genre": mood,
            "aspect_ratio": aspect,
            "voice_mode": voice_mode,
            "auto_hook": bool(data.get("auto_hook", True)),
            "uploaded_audio_id": str(data.get("uploaded_audio_id", "")),
            "uploaded_bgm_id": str(data.get("uploaded_bgm_id", "")),
            "scenes": scenes,
        }

    return jsonify({"ok": True, "session_id": session_id, "auto_title": auto_title})


@app.route("/api/session/<session_id>")
def api_session(session_id: str):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session ID."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess:
        return jsonify({"error": "Session expired."}), 404
    return jsonify(sess)


@app.route("/api/session/<session_id>/thumb/<int:idx>")
def api_session_thumb(session_id: str, idx: int):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess or idx < 0 or idx >= len(sess["scenes"]):
        return jsonify({"error": "Not found."}), 404
    sc = sess["scenes"][idx]
    fpath = os.path.join(SESSIONS_DIR, session_id, sc.get("thumb_file", ""))
    if sc.get("thumb_file") and os.path.exists(fpath):
        return send_file(fpath)
    return jsonify({"error": "No uploaded preview"}), 404


@app.route("/api/session/<session_id>/mode", methods=["POST"])
def api_change_visual_mode(session_id: str):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    data = request.get_json(silent=True) or {}
    idx = int(data.get("idx", -1))
    mode = str(data.get("mode", ""))
    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess or not (0 <= idx < len(sess["scenes"])) or mode not in ("2d_host", "ai_scene", "flow_clip"):
            return jsonify({"error": "Invalid visual mode."}), 400
        sess["scenes"][idx]["visual_mode"] = mode
    return jsonify({"ok": True})


@app.route("/api/session/<session_id>/anim", methods=["POST"])
def api_change_anim(session_id: str):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    data = request.get_json(silent=True) or {}
    idx = int(data.get("idx", -1))
    anim = str(data.get("anim", ""))
    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess or not (0 <= idx < len(sess["scenes"])) or anim not in MOTION_MODES:
            return jsonify({"error": "Invalid motion."}), 400
        sess["scenes"][idx]["anim_type"] = anim
    return jsonify({"ok": True})


@app.route("/api/session/<session_id>/replace/<int:idx>", methods=["POST"])
def api_replace_media(session_id: str, idx: int):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess or idx < 0 or idx >= len(sess["scenes"]):
        return jsonify({"error": "Scene not found."}), 404

    f = request.files.get("media")
    if not f or not f.filename:
        return jsonify({"error": "Select a video (.mp4) or image file first."}), 400

    sess_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(sess_dir, exist_ok=True)
    mtype, mfile, tfile = save_uploaded_media(f, sess_dir, idx)

    with LOCK:
        sess["scenes"][idx]["media_ready"] = True
        sess["scenes"][idx]["media_type"] = mtype
        sess["scenes"][idx]["visual_mode"] = "flow_clip" if mtype == "video" else "ai_scene"
        sess["scenes"][idx]["media_file"] = mfile
        sess["scenes"][idx]["thumb_file"] = tfile
    return jsonify({"ok": True})


@app.route("/api/session/<session_id>/batch_upload", methods=["POST"])
def api_batch_upload(session_id: str):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess:
        return jsonify({"error": "Session expired."}), 404

    files = request.files.getlist("media")
    if not files:
        return jsonify({"error": "No files selected."}), 400

    sess_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(sess_dir, exist_ok=True)
    total_scenes = len(sess["scenes"])

    explicit_map: Dict[int, Any] = {}
    unmapped_files: List[Any] = []
    for f in files:
        if not f or not f.filename:
            continue
        stem = os.path.splitext(f.filename)[0]
        m = re.match(r"^(?:scene[_-]?)?(\d{1,3})$", stem.strip(), re.IGNORECASE)
        if m:
            num = int(m.group(1))
            if 1 <= num <= total_scenes and num - 1 not in explicit_map:
                explicit_map[num - 1] = f
                continue
        unmapped_files.append(f)

    unmapped_files.sort(key=lambda x: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", x.filename or "")])

    # Prioritize filling 'flow_clip' slots first when unmapped clips are batch uploaded
    flow_slots = [i for i, s in enumerate(sess["scenes"]) if s.get("visual_mode") == "flow_clip"]
    other_slots = [i for i in range(total_scenes) if i not in flow_slots]
    ordered_slots = flow_slots + other_slots

    u_ptr = 0
    for idx in ordered_slots:
        target_f = explicit_map.get(idx)
        if target_f is None and u_ptr < len(unmapped_files):
            target_f = unmapped_files[u_ptr]
            u_ptr += 1
        if target_f is not None:
            mtype, mfile, tfile = save_uploaded_media(target_f, sess_dir, idx)
            with LOCK:
                sess["scenes"][idx]["media_ready"] = True
                sess["scenes"][idx]["media_type"] = mtype
                sess["scenes"][idx]["visual_mode"] = "flow_clip" if mtype == "video" else "ai_scene"
                sess["scenes"][idx]["media_file"] = mfile
                sess["scenes"][idx]["thumb_file"] = tfile

    return jsonify({"ok": True})


@app.route("/api/session/<session_id>/swap", methods=["POST"])
def api_swap_scenes(session_id: str):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    data = request.get_json(silent=True) or {}
    a = int(data.get("a", -1))
    b = int(data.get("b", -1))
    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess or not (0 <= a < len(sess["scenes"]) and 0 <= b < len(sess["scenes"])):
            return jsonify({"error": "Invalid scene indices."}), 400
        sa, sb = sess["scenes"][a], sess["scenes"][b]
        for k in ("visual_mode", "media_ready", "media_type", "media_file", "thumb_file"):
            sa[k], sb[k] = sb[k], sa[k]
    return jsonify({"ok": True})


@app.route("/api/render", methods=["POST"])
def api_render():
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id", "")).strip()
    if not is_safe_id(session_id):
        return jsonify({"error": "Prepare scenes first."}), 400

    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess:
            return jsonify({"error": "Session expired."}), 404
        if data.get("topic"):
            sess["topic"] = str(data["topic"]).strip()
        if data.get("voice_mode"):
            sess["voice_mode"] = str(data["voice_mode"])
        if data.get("uploaded_audio_id"):
            sess["uploaded_audio_id"] = str(data["uploaded_audio_id"])
        if data.get("uploaded_bgm_id"):
            sess["uploaded_bgm_id"] = str(data["uploaded_bgm_id"])
        if "auto_hook" in data:
            sess["auto_hook"] = bool(data["auto_hook"])

        if len(ACTIVE_JOBS) >= 1:
            return jsonify({"error": "Server is currently rendering another video. Please wait."}), 429

        job_id = f"nx_{uuid.uuid4().hex[:10]}"
        ACTIVE_JOBS.add(job_id)
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 3,
            "current_scene": "Starting Tri-Hybrid 16:9 Cinema Render...",
            "error": None,
        }

    threading.Thread(target=run_final_render, args=(job_id, session_id), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job expired."}), 404
    return jsonify(job)


@app.route("/api/video/<job_id>")
def api_video(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with LOCK:
        job = JOBS.get(job_id)
    if not job or not job.get("file_name"):
        return jsonify({"error": "Video not ready."}), 404
    fname = os.path.basename(job["file_name"])
    target = os.path.join(OUTPUT_DIR, fname)
    if not os.path.exists(target):
        return jsonify({"error": "File purged from storage."}), 404
    return send_file(target, as_attachment=(request.args.get("dl") == "1"), download_name=fname, mimetype="video/mp4")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
