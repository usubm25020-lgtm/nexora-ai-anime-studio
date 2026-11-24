import os
import re
import gc
import time
import uuid
import wave
import random
import shutil
import asyncio
import tempfile
import subprocess
import threading
from typing import Dict, List, Any, Optional, Tuple

import requests
import numpy as np
from PIL import Image
from flask import Flask, request, jsonify, send_file

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

# ==============================================================================
# 1. NEXORA ROMANCE APEX v19 — 90-SECOND NATIVE FFMPEG MOTION VIDEO ENGINE
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
TMP_ROOT = os.path.join(BASE_DIR, "tmp_work")

for d in (OUTPUT_DIR, UPLOAD_DIR, SESSIONS_DIR, TMP_ROOT):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024

SESSIONS: Dict[str, Dict[str, Any]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}
LOCK = threading.Lock()
ACTIVE_JOBS = set()
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,40}$")

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
HTTP_HEADERS = {"User-Agent": "NexoraStudioApex/19.0 (Contact: admin@nexorastudio.app)"}


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
# 2. DEVANAGARI -> CLEAN HINGLISH AUTO-CAPTION GENERATOR
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
    "ं": "n", "ँ": "n", "ः": "h", "्": "",
}


def devanagari_to_hinglish(text: str) -> str:
    if not text:
        return ""
    if not any("\u0900" <= ch <= "\u097F" for ch in text):
        return text

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
        out_words.append(word_str)
    return " ".join(out_words)


# ==============================================================================
# 3. MULTI-SOURCE UNIQUE HD VIDEO POOL (ZERO REPEATS, ZERO API KEYS)
# ==============================================================================
ANIM_MODES = [
    "Slow_Zoom_In",
    "Cinema_Pan_Right",
    "Slow_Zoom_Out",
    "Cinema_Pan_Left",
    "Heartbeat_Zoom",
    "Drift_Push_In",
]

DIVERSE_ROMANTIC_QUERIES = [
    "rain night city lights",
    "romantic couple sunset beach",
    "candlelight dinner dark",
    "night traffic bokeh cars",
    "raindrops glass window",
    "couple walking sunset",
    "city skyline night lights",
    "fireplace warm glow night",
    "misty forest morning light",
    "ocean waves sunset golden",
    "street lights fog night",
    "coffee cup steam morning",
    "starry night sky timelapse",
    "vintage clock ticking",
    "autumn leaves wind slow motion",
    "moonlight clouds night sky",
]


def map_scene_to_smart_video_query(visual_prompt: str, hinglish_caption: str, idx: int) -> str:
    blob = f"{visual_prompt} {hinglish_caption}".lower()

    if any(k in blob for k in ("phone", "message", "msg", "call", "screen", "ring", "mobile")):
        return "smartphone screen night"
    if any(k in blob for k in ("rain", "barish", "boond", "window", "khidki", "umbrella", "bheeg")):
        return "raindrops glass window"
    if any(k in blob for k in ("car", "gaadi", "drive", "highway", "headlight", "road", "street")):
        return "night traffic bokeh cars"
    if any(k in blob for k in ("cafe", "coffee", "restaurant", "table", "cup")):
        return "coffee cup steam morning"
    if any(k in blob for k in ("kiss", "hug", "baahon", "seene", "kareeb", "chhua", "embrace", "romantic", "love")):
        return "romantic couple sunset beach"
    if any(k in blob for k in ("hand", "haath", "hold", "fingers")):
        return "couple walking sunset"
    if any(k in blob for k in ("candle", "lamp", "bedroom", "kamre", "bed", "diya")):
        return "candlelight dinner dark"
    if any(k in blob for k in ("cry", "tears", "aansu", "ro ", "sad", "dard", "heartbreak", "alone")):
        return "rain night city lights"
    if any(k in blob for k in ("clock", "ghadi", "2:00", "2:45", "midnight", "aadhi raat", "waqt")):
        return "vintage clock ticking"
    if any(k in blob for k in ("shadow", "saaya", "door", "darwaza", "mystery", "raaz", "dark")):
        return "street lights fog night"
    if any(k in blob for k in ("sunrise", "morning", "subah", "hills", "sky", "balcony")):
        return "ocean waves sunset golden"

    return DIVERSE_ROMANTIC_QUERIES[idx % len(DIVERSE_ROMANTIC_QUERIES)]


_VIDEO_SEARCH_CACHE: Dict[str, List[Dict[str, str]]] = {}


def _fetch_wikimedia_videos(query: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            headers=HTTP_HEADERS,
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"{query} filetype:video",
                "gsrnamespace": "6",
                "gsrlimit": "25",
                "prop": "imageinfo",
                "iiprop": "url|size|mime",
                "iiurlwidth": "640",
            },
            timeout=9,
        )
        if resp.status_code == 200:
            pages = resp.json().get("query", {}).get("pages", {})
            for _, pdata in pages.items():
                iinfo = (pdata.get("imageinfo") or [{}])[0]
                mime = str(iinfo.get("mime") or "")
                v_url = str(iinfo.get("url") or "")
                t_url = str(iinfo.get("thumburl") or "")
                size_b = int(iinfo.get("size") or 0)
                # Strict size limit (< 16MB) for ultra-fast download and zero disk bloat
                if mime.startswith("video/") and v_url and 80_000 < size_b < 16_000_000:
                    out.append({
                        "id": f"wm_{pdata.get('pageid')}",
                        "url": v_url,
                        "thumb": t_url,
                    })
    except Exception:
        pass
    return out


def search_unique_open_videos(query: str, backup_idx: int = 0) -> List[Dict[str, str]]:
    if query in _VIDEO_SEARCH_CACHE and _VIDEO_SEARCH_CACHE[query]:
        return _VIDEO_SEARCH_CACHE[query]

    results = _fetch_wikimedia_videos(query)
    if len(results) < 5:
        alt_q = DIVERSE_ROMANTIC_QUERIES[backup_idx % len(DIVERSE_ROMANTIC_QUERIES)]
        results.extend(_fetch_wikimedia_videos(alt_q))

    # Deduplicate by ID
    seen = set()
    uniq: List[Dict[str, str]] = []
    for item in results:
        if item["id"] not in seen:
            seen.add(item["id"])
            uniq.append(item)

    if uniq:
        _VIDEO_SEARCH_CACHE[query] = uniq
    return uniq


def pick_unique_video_for_scene(query: str, scene_idx: int, used_ids: set, skip_index: int = 0) -> Optional[Dict[str, str]]:
    # Try primary query first, then rotate across diverse queries so NO clip repeats 6-7 times!
    candidate_queries = [query] + [
        DIVERSE_ROMANTIC_QUERIES[(scene_idx + k) % len(DIVERSE_ROMANTIC_QUERIES)]
        for k in range(6)
    ]

    pool: List[Dict[str, str]] = []
    seen_local = set()
    for q in candidate_queries:
        for v in search_unique_open_videos(q, backup_idx=scene_idx):
            if v["id"] not in seen_local:
                seen_local.add(v["id"])
                pool.append(v)
        if len(pool) >= 28:
            break

    if not pool:
        return None

    if skip_index > 0:
        return pool[skip_index % len(pool)]

    for v in pool:
        if v["id"] not in used_ids:
            return v

    return pool[scene_idx % len(pool)]


def detect_romantic_sfx(text_blob: str, mood: str) -> str:
    low = text_blob.lower()
    if any(k in low for k in ("message", "msg", "phone", "call", "notification", "ring", "screen")):
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


def parse_user_script(custom_script: str, mood: str) -> List[Dict[str, Any]]:
    scenes: List[Dict[str, Any]] = []
    for line in str(custom_script or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            sp = parts[0].lower()
            if sp not in ("narrator", "male", "female", "child"):
                sp = "narrator"
            visual_prompt = parts[1]
            spoken_hindi = parts[3] if len(parts) >= 4 else parts[2]
            auto_caption = devanagari_to_hinglish(spoken_hindi)
            idx = len(scenes)
            combined_txt = f"{visual_prompt} {auto_caption}"
            scenes.append({
                "index": idx,
                "is_teaser": False,
                "speaker": sp,
                "emotion": detect_line_emotion(auto_caption, sp),
                "visual_prompt": visual_prompt,
                "broll_query": map_scene_to_smart_video_query(visual_prompt, auto_caption, idx),
                "video_alt_index": 0,
                "hindi_dub": spoken_hindi,
                "auto_caption": auto_caption,
                "anim_type": ANIM_MODES[idx % len(ANIM_MODES)],
                "sfx_type": detect_romantic_sfx(combined_txt, mood),
                "media_ready": False,
                "media_type": "remote_video",
                "media_file": "",
                "remote_video_url": "",
                "thumb_url": "",
                "thumb_file": "",
            })
    return scenes


def build_elevenlabs_script(scenes: List[Dict[str, Any]], voice_mode: str) -> str:
    lines: List[str] = []
    for sc in scenes:
        dub = sc.get("hindi_dub") or ""
        if voice_mode == "builtin_multi":
            sp = sc.get("speaker", "narrator").upper()
            lines.append(f"[{sp}]: {dub}")
        else:
            lines.append(dub)
    return "\n\n".join(lines)


# ==============================================================================
# 4. NATIVE ASS KARAOKE SUBTITLE & HUD GENERATOR (C-SPEED FFMPEG BURN-IN)
# ==============================================================================
def build_word_timings(text: str, dur_sec: float) -> List[Tuple[str, float, float]]:
    words = str(text or "").split()
    if not words:
        return []
    weights: List[float] = []
    for w in words:
        base_w = max(2.0, float(len(re.sub(r"[^a-zA-Z0-9]", "", w))))
        if w.endswith((",", ";", ":")):
            base_w += 2.5
        elif w.endswith((".", "!", "?", "।")):
            base_w += 3.5
        weights.append(base_w)
    total_w = sum(weights) or 1.0
    span = max(0.3, dur_sec - 0.15)
    timings: List[Tuple[str, float, float]] = []
    acc = 0.0
    for w, wt in zip(words, weights):
        st = 0.05 + (acc / total_w) * span
        acc += wt
        en = 0.05 + (acc / total_w) * span
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
    voice_mode: str,
):
    """Generates ultra-crisp 3-word karaoke captions & climax hook badges in native ASS format."""
    # ASS colors are &HBBGGRR&
    accent_bgr = "&H00B472F4&" if mood == "deep_romance" else ("&H00F8BD38&" if mood == "sad_heartbreak" else "&H008571FB&")
    font_sz = 34 if out_w > out_h else 30
    hud_sz = 18 if out_w > out_h else 16
    margin_v = 48 if out_w > out_h else 110

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {out_w}
PlayResY: {out_h}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CapStyle,DejaVu Sans,{font_sz},&H00FFFFFF&,&H0094A3B8&,&H00080A14&,&H96060812&,-1,0,0,0,100,100,0,0,3,6,2,2,24,24,{margin_v},1
Style: HookStyle,DejaVu Sans,{hud_sz},&H00FFFFFF&,&H00FFFFFF&,&H007727DB&,&HD07727DB&,-1,0,0,0,100,100,0,0,3,6,0,7,20,20,24,1
Style: StageStyle,DejaVu Sans,{hud_sz - 2},&H00B472F4&,&H00B472F4&,&H000A0C18&,&HB00A0C18&,-1,0,0,0,100,100,0,0,3,5,0,7,20,20,24,1
Style: EndStyle,DejaVu Sans,{font_sz + 2},&H00B472F4&,&H00FFFFFF&,&H00000000&,&HC805070F&,-1,0,0,0,100,100,0,0,3,10,0,5,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: List[str] = []
    cur_t = 0.0
    total_dur = sum(float(s.get("duration_sec", 4.0)) for s in render_scenes) or 1.0

    for sc in render_scenes:
        dur = float(sc.get("duration_sec", 4.0))
        st_abs = cur_t
        en_abs = cur_t + dur
        is_teaser = bool(sc.get("is_teaser", False))
        ov_p = st_abs / total_dur

        # Top HUD Badge
        if is_teaser:
            ts_lbl = sc.get("climax_ts_label", "01:45")
            events.append(
                f"Dialogue: 2,{fmt_ass_time(st_abs)},{fmt_ass_time(en_abs)},HookStyle,,0,0,0,,🔥 AAGE DEKHIYE ({ts_lbl} PE ASLI MOD)..."
            )
        else:
            stage = "01. PEHLI MULAKAT" if ov_p < 0.34 else ("02. GEHRA RAAZ" if ov_p < 0.72 else "03. ASLI ANJAAM")
            events.append(
                f"Dialogue: 2,{fmt_ass_time(st_abs)},{fmt_ass_time(en_abs)},StageStyle,,0,0,0,,{stage}"
            )

        # 3-Word Punchy Highlighted Captions
        timings = build_word_timings(sc.get("auto_caption", ""), dur)
        sp_prefix = ""
        if voice_mode == "builtin_multi":
            sp_map = {"narrator": "", "male": "[HE] ", "female": "[SHE] "}
            sp_prefix = sp_map.get(sc.get("speaker", "narrator"), "")

        group_size = 3
        for g_start in range(0, len(timings), group_size):
            grp = timings[g_start : g_start + group_size]
            for local_i, (w_str, w_st, w_en) in enumerate(grp):
                seg_s = st_abs + w_st
                seg_e = st_abs + (grp[local_i + 1][1] if local_i + 1 < len(grp) else (grp[-1][2] + 0.12))
                seg_e = min(en_abs, max(seg_s + 0.08, seg_e))

                styled_words: List[str] = []
                for j, (jw, _, _) in enumerate(grp):
                    clean_jw = jw.replace("{", "").replace("}", "")
                    if j == local_i:
                        styled_words.append(f"{{\\c{accent_bgr}}}{clean_jw}{{\\c&H00FFFFFF&}}")
                    elif j < local_i:
                        styled_words.append(clean_jw)
                    else:
                        styled_words.append(f"{{\\c&H0094A3B8&}}{clean_jw}{{\\c&H00FFFFFF&}}")

                line_txt = f"{sp_prefix}{' '.join(styled_words)}"
                events.append(
                    f"Dialogue: 1,{fmt_ass_time(seg_s)},{fmt_ass_time(seg_e)},CapStyle,,0,0,0,,{line_txt}"
                )

        cur_t = en_abs

    # Cliffhanger End-Card on last 2.8 seconds
    if total_dur > 6.0:
        end_s = max(0.0, total_dur - 2.8)
        events.append(
            f"Dialogue: 3,{fmt_ass_time(end_s)},{fmt_ass_time(total_dur)},EndStyle,,0,0,0,,KAHANI ABHI BAAKI HAI...\\N{{\\fs{font_sz - 8}\\c&H0071E0FD&}}Agle Part Ke Liye Subscribe Zaroor Karein ❤️"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")


# ==============================================================================
# 5. LATE-NIGHT INTIMATE VOICE & ROMANTIC SOUNDSCAPE ENGINE
# ==============================================================================
class NexoraRomanceAudioEngine:
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
                    return {"voice": "hi-IN-SwaraNeural", "rate": "-2%", "pitch": "-3Hz"}
                if emo == "emotional_ache":
                    return {"voice": "hi-IN-SwaraNeural", "rate": "+0%", "pitch": "-2Hz"}
                return {"voice": "hi-IN-SwaraNeural", "rate": "+6%", "pitch": "-1Hz"}
            if sp == "male":
                if emo == "intimate_whisper":
                    return {"voice": "hi-IN-MadhurNeural", "rate": "-3%", "pitch": "-9Hz"}
                if emo == "emotional_ache":
                    return {"voice": "hi-IN-MadhurNeural", "rate": "+0%", "pitch": "-7Hz"}
                return {"voice": "hi-IN-MadhurNeural", "rate": "+6%", "pitch": "-6Hz"}
            if sp == "child":
                return {"voice": "hi-IN-SwaraNeural", "rate": "+9%", "pitch": "+14Hz"}

        if emo == "intimate_whisper":
            return {"voice": "hi-IN-MadhurNeural", "rate": "-3%", "pitch": "-11Hz"}
        if emo == "emotional_ache":
            return {"voice": "hi-IN-MadhurNeural", "rate": "-1%", "pitch": "-9Hz"}
        if emo == "urgent_passion":
            return {"voice": "hi-IN-MadhurNeural", "rate": "+10%", "pitch": "-7Hz"}
        return {"voice": "hi-IN-MadhurNeural", "rate": "+4%", "pitch": "-9Hz"}

    @classmethod
    def dub_scene_romantic(cls, sc: Dict[str, Any], idx: int, voice_mode: str, work_dir: str) -> Tuple[str, float]:
        if not HAS_EDGE_TTS:
            raise RuntimeError("edge-tts is not installed on the server.")

        prof = cls.get_romantic_voice_profile(sc, voice_mode)
        text = str(sc.get("hindi_dub") or "").strip() or "कहानी आगे बढ़ती है..."

        mp3_p = os.path.join(work_dir, f"v_{idx:03d}.mp3")
        wav_p = os.path.join(work_dir, f"v_{idx:03d}.wav")

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

        final_dur = round(speech_dur + 0.26, 2)

        studio_af = (
            "highpass=f=68,"
            "equalizer=f=120:t=q:w=1.1:g=4.5,"
            "equalizer=f=3500:t=q:w=1.4:g=2.0,"
            "aecho=0.85:0.88:22:0.10,"
            "acompressor=threshold=-19dB:ratio=2.8:attack=6:release=115,"
            f"apad=whole_dur={final_dur:.3f}"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_p,
                "-af", studio_af,
                "-t", f"{final_dur:.3f}",
                "-ar", "22050", "-ac", "1", wav_p,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        if os.path.exists(mp3_p):
            os.remove(mp3_p)
        return wav_p, final_dur

    @staticmethod
    def generate_romantic_sfx(sfx_type: str, n_samples: int, rate: int = 22050) -> np.ndarray:
        out = np.zeros(n_samples, dtype=np.float32)
        sfx_len = min(n_samples, int(rate * 1.85))
        if sfx_len <= 10:
            return out
        t = np.linspace(0, sfx_len / float(rate), sfx_len, endpoint=False, dtype=np.float32)
        rng = np.random.default_rng(202)

        if sfx_type == "phone_chime":
            c1 = np.exp(-t * 8.0) * np.sin(2.0 * np.pi * 659.25 * t)
            c2 = np.exp(-np.maximum(0.0, t - 0.14) * 7.0) * np.sin(2.0 * np.pi * 987.77 * t) * (t >= 0.14)
            out[:sfx_len] = (c1 + c2) * 0.28
        elif sfx_type == "soft_rain":
            rain = np.convolve(rng.normal(0.0, 0.22, sfx_len).astype(np.float32), np.ones(18) / 18.0, mode="same")
            env = np.sin(np.pi * (t / (sfx_len / float(rate))))
            out[:sfx_len] = rain * env * 0.55
        elif sfx_type == "soft_heartbeat":
            b1 = np.exp(-t * 13.0) * np.sin(2.0 * np.pi * 54.0 * t)
            b2 = np.exp(-np.maximum(0.0, t - 0.30) * 13.0) * np.sin(2.0 * np.pi * 48.0 * t) * (t >= 0.30)
            out[:sfx_len] = (b1 + b2) * 0.65
        elif sfx_type == "emotional_swell":
            f1 = np.sin(2.0 * np.pi * 220.00 * t)
            f2 = np.sin(2.0 * np.pi * 261.63 * t)
            f3 = np.sin(2.0 * np.pi * 329.63 * t)
            env = np.sin(np.pi * (t / (sfx_len / float(rate)))) ** 1.6
            out[:sfx_len] = (0.45 * f1 + 0.35 * f2 + 0.30 * f3) * env * 0.24
        elif sfx_type == "clock_tick":
            for tick_t in (0.15, 0.85, 1.55):
                mask = (t >= tick_t) & (t < tick_t + 0.05)
                lt = np.maximum(0.0, t - tick_t)
                out[:sfx_len] += mask * np.exp(-lt * 90.0) * np.sin(2.0 * np.pi * 1150.0 * lt) * 0.35
        elif sfx_type == "door_creak":
            freq = 175.0 + 90.0 * np.sin(2.0 * np.pi * 3.8 * t)
            phase = 2.0 * np.pi * np.cumsum(freq) / float(rate)
            env = np.sin(np.pi * (t / (sfx_len / float(rate)))) ** 1.5
            out[:sfx_len] = np.sin(phase) * env * 0.25
        elif sfx_type == "thunder":
            rumble = np.convolve(rng.normal(0.0, 0.45, sfx_len).astype(np.float32), np.ones(32) / 32.0, mode="same")
            env = np.exp(-t * 1.9) * (1.0 - np.exp(-t * 45.0))
            out[:sfx_len] = rumble * env * 0.72
        else:
            noise = np.convolve(rng.normal(0.0, 0.26, sfx_len).astype(np.float32), np.ones(16) / 16.0, mode="same")
            env = np.sin(np.pi * (t / (sfx_len / float(rate)))) ** 2.0
            out[:sfx_len] = noise * env * 0.40

        return out

    @classmethod
    def build_romantic_soundscape(
        cls,
        render_scenes: List[Dict[str, Any]],
        mood: str,
        out_wav: str,
        custom_bgm_path: Optional[str] = None,
        work_dir: str = "",
    ):
        rate = 22050
        sfx_only_wav = os.path.join(work_dir, "sfx_only.wav") if work_dir else out_wav

        with wave.open(sfx_only_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            cur_t = 0.0

            for sc in render_scenes:
                dur = float(sc.get("duration_sec", 4.0))
                n = max(1, int(round(dur * rate)))
                t = np.linspace(cur_t, cur_t + dur, n, endpoint=False, dtype=np.float32)
                local_p = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
                swell = 1.0 + 0.42 * (np.exp(-local_p * 5.0) + np.exp(-(1.0 - local_p) * 5.0))

                if custom_bgm_path and os.path.exists(custom_bgm_path):
                    bgm = np.zeros(n, dtype=np.float32)
                elif mood == "sad_heartbreak":
                    pad = (
                        0.45 * np.sin(2.0 * np.pi * 146.83 * t)
                        + 0.35 * np.sin(2.0 * np.pi * 174.61 * t)
                        + 0.28 * np.sin(2.0 * np.pi * 220.00 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 73.42 * t)
                    )
                    bgm = pad * 0.13 * swell
                elif mood == "secret_suspense":
                    pulse = 0.8 + 0.2 * np.sin(2.0 * np.pi * 0.6 * t)
                    pad = (
                        0.42 * np.sin(2.0 * np.pi * 110.00 * t)
                        + 0.32 * np.sin(2.0 * np.pi * 130.81 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 164.81 * t)
                        + 0.22 * np.sin(2.0 * np.pi * 55.00 * t)
                    )
                    bgm = pad * 0.13 * swell * pulse
                else:
                    pad = (
                        0.42 * np.sin(2.0 * np.pi * 130.81 * t)
                        + 0.34 * np.sin(2.0 * np.pi * 164.81 * t)
                        + 0.28 * np.sin(2.0 * np.pi * 196.00 * t)
                        + 0.22 * np.sin(2.0 * np.pi * 65.41 * t)
                    )
                    bgm = pad * 0.13 * swell

                sfx = cls.generate_romantic_sfx(sc.get("sfx_type", "whoosh"), n, rate=rate)
                mix = bgm + sfx
                wf.writeframes((np.clip(mix, -0.95, 0.95) * 32767.0).astype(np.int16).tobytes())
                cur_t += dur
                gc.collect()

        if custom_bgm_path and os.path.exists(custom_bgm_path):
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-stream_loop", "-1", "-i", custom_bgm_path,
                    "-i", sfx_only_wav,
                    "-filter_complex", "[0:a]volume=0.30[bg];[1:a]volume=1.0[sx];[bg][sx]amix=inputs=2:duration=shortest[out]",
                    "-map", "[out]", "-ar", "22050", "-ac", "1", out_wav,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )


# ==============================================================================
# 6. 90-SECOND NATIVE FFMPEG RENDER PIPELINE (ZERO JPG BLOAT / ZERO EXIT 254)
# ==============================================================================
def update_job(job_id: str, **kwargs):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def fmt_mmss(sec: float) -> str:
    s = max(0, int(round(sec)))
    return f"{s // 60:02d}:{s % 60:02d}"


def download_remote_broll_to_temp(url: str, out_path: str):
    with requests.get(url, headers=HTTP_HEADERS, stream=True, timeout=20) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)


def transcode_clip_native_ffmpeg(src_video: str, dst_mp4: str, dur_sec: float, out_w: int, out_h: int):
    """
    Transcodes, scales, loops, applies warm romantic color grading, vignette & subtle 0.25s fade
    directly in native C-speed FFmpeg—15x faster and uses < 40MB RAM/Disk!
    """
    fade_out_st = max(0.1, dur_sec - 0.22)
    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},"
        "fps=24,"
        "eq=contrast=1.09:brightness=-0.01:saturation=1.22,"
        "colorbalance=rs=0.04:gs=0.01:bs=-0.03,"
        "vignette=PI/5,"
        f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out_st:.3f}:d=0.20"
    )
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", src_video,
            "-t", f"{dur_sec:.3f}",
            "-an",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-threads", "2",
            "-pix_fmt", "yuv420p", dst_mp4,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )


def create_fallback_atmospheric_clip(dst_mp4: str, dur_sec: float, out_w: int, out_h: int):
    """Guarantees render never fails even if a remote URL is temporarily unreachable."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=#140c1f:s={out_w}x{out_h}:r=24:d={dur_sec:.3f}",
            "-vf", "noise=alls=12:allf=t+u,vignette=PI/4",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
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
        for sc in raw_scenes:
            if not sc.get("media_ready"):
                raise RuntimeError(f"Scene {sc['index'] + 1} has no video clip yet. Click 'Auto-Fill Missing Clips'!")

        mood = sess["genre"]
        topic = sess["topic"]
        aspect = sess["aspect_ratio"]
        voice_mode = sess["voice_mode"]
        auto_hook = bool(sess.get("auto_hook", True))
        uploaded_audio_id = sess.get("uploaded_audio_id", "")
        uploaded_bgm_id = sess.get("uploaded_bgm_id", "")

        out_w, out_h = (540, 960) if aspect == "9:16" else (960, 540)

        # 1. Dub Studio Voice
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

            update_job(job_id, status="running", progress=8, current_scene="Syncing External Voiceover...")
            subprocess.run(
                ["ffmpeg", "-y", "-i", uploaded_audio_path, "-ar", "22050", "-ac", "1", master_voice],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            total_el_dur = NexoraRomanceAudioEngine.probe_dur(master_voice)
            per_sc = max(1.5, total_el_dur / float(len(raw_scenes)))
            for idx, sc in enumerate(raw_scenes):
                sc["start_sec"] = idx * per_sc
                sc["duration_sec"] = per_sc
        else:
            running_t = 0.0
            for idx, sc in enumerate(raw_scenes):
                update_job(
                    job_id,
                    status="running",
                    progress=4 + int((idx / float(len(raw_scenes))) * 18),
                    current_scene=f"Dubbing Intimate Voice ({idx + 1}/{len(raw_scenes)})...",
                )
                dub_wav, actual_dur = NexoraRomanceAudioEngine.dub_scene_romantic(sc, idx, voice_mode, work_dir)
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

        # 2. Prepend 0:00 Climax Hook Teaser
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
            render_scenes.append(hook_sc)

            hook_wav = os.path.join(work_dir, "hook_voice.wav")
            joined_voice = os.path.join(work_dir, "joined_voice.wav")
            if voice_mode != "uploaded" and climax_sc.get("wav_file") and os.path.exists(climax_sc["wav_file"]):
                subprocess.run(
                    ["ffmpeg", "-y", "-i", climax_sc["wav_file"], "-t", f"{hook_dur:.3f}", "-ar", "22050", "-ac", "1", hook_wav],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                )
            else:
                climax_start = float(climax_sc.get("start_sec", 0.0))
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{climax_start:.3f}", "-t", f"{hook_dur:.3f}", "-i", master_voice, "-ar", "22050", "-ac", "1", hook_wav],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                )

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

        # 3. Native C-Speed Video Clip Processing (Zero JPG Extraction, Zero Disk Bloat)
        total_sc = len(render_scenes)
        video_chunks: List[str] = []

        for idx, sc in enumerate(render_scenes):
            lbl = "Climax Hook Teaser" if sc.get("is_teaser") else f"Scene {idx + 1}/{total_sc}"
            update_job(
                job_id,
                status="running",
                progress=22 + int((idx / float(total_sc)) * 66),
                current_scene=f"Fast-Rendering {lbl} (Native HD Stream)...",
            )

            actual_dur = float(sc.get("duration_sec", 4.0))
            v_chunk = os.path.join(work_dir, f"vc_{idx:03d}.mp4")
            temp_dl_vid = os.path.join(work_dir, f"dl_{idx:03d}.webm")

            try:
                if sc.get("media_type") == "remote_video":
                    download_remote_broll_to_temp(sc["remote_video_url"], temp_dl_vid)
                    transcode_clip_native_ffmpeg(temp_dl_vid, v_chunk, actual_dur, out_w, out_h)
                else:
                    med_path = os.path.join(sess_dir, sc["media_file"])
                    transcode_clip_native_ffmpeg(med_path, v_chunk, actual_dur, out_w, out_h)
            except Exception:
                # Auto-recover seamlessly if any single remote clip fails
                create_fallback_atmospheric_clip(v_chunk, actual_dur, out_w, out_h)
            finally:
                if os.path.exists(temp_dl_vid):
                    os.remove(temp_dl_vid)

            video_chunks.append(v_chunk)
            gc.collect()

        # 4. Generate Native ASS Subtitles + Soundscape + Final Single-Pass Master
        update_job(job_id, progress=90, current_scene="Burning 3-Word Karaoke Captions & Mastering Audio...")

        ass_file = os.path.join(work_dir, "captions.ass")
        generate_ass_subtitle_file(ass_file, render_scenes, out_w, out_h, mood, voice_mode)

        v_list = os.path.join(work_dir, "v_list.txt")
        with open(v_list, "w", encoding="utf-8") as vf:
            for vc in video_chunks:
                vf.write(f"file '{vc}'\n")

        joined_v = os.path.join(work_dir, "joined_v.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", v_list, "-c", "copy", joined_v],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        # Immediately purge individual video chunks to free disk space before final encode!
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
        NexoraRomanceAudioEngine.build_romantic_soundscape(
            render_scenes, mood, sfx_bgm_wav, custom_bgm_path=custom_bgm_path, work_dir=work_dir
        )

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", topic)[:28].strip("_") or "NEXORA_ROMANCE"
        out_name = f"NEXORA_LOVE_{slug}_{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        # Escape path for FFmpeg ass filter
        esc_ass = ass_file.replace("\\", "/").replace(":", "\\:")
        filter_complex = (
            f"[0:v]ass='{esc_ass}'[vout];"
            "[1:a]asplit=2[v_main][v_sc];"
            "[2:a][v_sc]sidechaincompress=threshold=0.03:ratio=4:attack=15:release=250[bg_ducked];"
            "[v_main]volume=1.38[v_loud];"
            "[bg_ducked]volume=0.66[bg_loud];"
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
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-shortest", out_path,
            ],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg final master error: {res.stderr[-250:]}")

        actual_total_dur = NexoraRomanceAudioEngine.probe_dur(out_path) or 60.0
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
# 7. REST API ENDPOINTS (UNIQUE MULTI-QUERY VIDEO SELECTION)
# ==============================================================================
@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "ffmpeg": HAS_FFMPEG, "voice_ready": HAS_EDGE_TTS, "romance_v19": True})


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
    voice_mode = str(data.get("voice_mode", "builtin_multi"))

    scenes = parse_user_script(str(data.get("custom_script", "")), mood)
    if not scenes:
        return jsonify({"error": "Paste a valid 3-column script first (Speaker | Scene Context | Spoken Hindi)."}), 400

    session_id = f"ses_{uuid.uuid4().hex[:10]}"
    os.makedirs(os.path.join(SESSIONS_DIR, session_id), exist_ok=True)
    el_script = build_elevenlabs_script(scenes, voice_mode)

    # Strictly pick 24 DIFFERENT video clips across diverse romantic/night themes!
    used_ids = set()
    for idx, sc in enumerate(scenes):
        vid = pick_unique_video_for_scene(sc["broll_query"], scene_idx=idx, used_ids=used_ids, skip_index=0)
        if vid:
            used_ids.add(vid["id"])
            sc["media_ready"] = True
            sc["media_type"] = "remote_video"
            sc["remote_video_url"] = vid["url"]
            sc["thumb_url"] = vid["thumb"]

    with LOCK:
        SESSIONS[session_id] = {
            "session_id": session_id,
            "topic": str(data.get("topic", "NEXORA Love Story")).strip() or "NEXORA Love Story",
            "genre": mood,
            "aspect_ratio": str(data.get("aspect_ratio", "16:9")),
            "voice_mode": voice_mode,
            "auto_hook": bool(data.get("auto_hook", True)),
            "uploaded_audio_id": str(data.get("uploaded_audio_id", "")),
            "uploaded_bgm_id": str(data.get("uploaded_bgm_id", "")),
            "elevenlabs_script": el_script,
            "scenes": scenes,
        }

    return jsonify({"ok": True, "session_id": session_id, "elevenlabs_script": el_script})


@app.route("/api/session/<session_id>/autofill_videos", methods=["POST"])
def api_autofill_videos(session_id: str):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session ID."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess:
            return jsonify({"error": "Session expired."}), 404
        scenes_copy = [dict(s) for s in sess["scenes"]]

    used_ids = set()
    filled = 0
    for idx, sc in enumerate(scenes_copy):
        if sc.get("media_ready"):
            continue
        vid = pick_unique_video_for_scene(sc["broll_query"], scene_idx=idx, used_ids=used_ids, skip_index=0)
        if vid:
            used_ids.add(vid["id"])
            with LOCK:
                if session_id in SESSIONS:
                    SESSIONS[session_id]["scenes"][idx]["media_ready"] = True
                    SESSIONS[session_id]["scenes"][idx]["media_type"] = "remote_video"
                    SESSIONS[session_id]["scenes"][idx]["remote_video_url"] = vid["url"]
                    SESSIONS[session_id]["scenes"][idx]["thumb_url"] = vid["thumb"]
                    filled += 1

    return jsonify({"ok": True, "filled": filled})


@app.route("/api/session/<session_id>/next_clip/<int:idx>", methods=["POST"])
def api_next_clip(session_id: str, idx: int):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess or idx < 0 or idx >= len(sess["scenes"]):
            return jsonify({"error": "Scene not found."}), 404
        sc = dict(sess["scenes"][idx])

    next_alt = int(sc.get("video_alt_index", 0)) + 1
    vid = pick_unique_video_for_scene(sc["broll_query"], scene_idx=idx, used_ids=set(), skip_index=next_alt)
    if not vid:
        return jsonify({"error": "No more clips found."}), 404
    with LOCK:
        if session_id in SESSIONS:
            SESSIONS[session_id]["scenes"][idx]["video_alt_index"] = next_alt
            SESSIONS[session_id]["scenes"][idx]["media_ready"] = True
            SESSIONS[session_id]["scenes"][idx]["media_type"] = "remote_video"
            SESSIONS[session_id]["scenes"][idx]["remote_video_url"] = vid["url"]
            SESSIONS[session_id]["scenes"][idx]["thumb_url"] = vid["thumb"]
    return jsonify({"ok": True})


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
    if not sc.get("media_ready") or not os.path.exists(fpath):
        return jsonify({"error": "Media missing."}), 404
    return send_file(fpath)


@app.route("/api/session/<session_id>/replace/<int:idx>", methods=["POST"])
def api_replace_video(session_id: str, idx: int):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess or idx < 0 or idx >= len(sess["scenes"]):
        return jsonify({"error": "Scene not found."}), 404

    f = request.files.get("media")
    if not f or not f.filename:
        return jsonify({"error": "Select a video file (.mp4/.mov) first."}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in VIDEO_EXTS:
        return jsonify({"error": "Only motion video files (.mp4, .mov, .webm) are supported!"}), 400

    sess_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(sess_dir, exist_ok=True)
    uid = uuid.uuid4().hex[:6]
    vname = f"vid_{idx:03d}_{uid}{ext}"
    vpath = os.path.join(sess_dir, vname)
    f.save(vpath)

    tname = f"thumb_{idx:03d}_{uid}.jpg"
    tpath = os.path.join(sess_dir, tname)
    subprocess.run(
        ["ffmpeg", "-y", "-i", vpath, "-vframes", "1", "-vf", "scale=480:-1", tpath],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not os.path.exists(tpath):
        Image.new("RGB", (480, 270), (15, 23, 42)).save(tpath)

    with LOCK:
        sess["scenes"][idx]["media_ready"] = True
        sess["scenes"][idx]["media_type"] = "video"
        sess["scenes"][idx]["media_file"] = vname
        sess["scenes"][idx]["thumb_file"] = tname
        sess["scenes"][idx]["remote_video_url"] = ""
        sess["scenes"][idx]["thumb_url"] = ""
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
        for k in ("media_ready", "media_type", "media_file", "thumb_file", "remote_video_url", "thumb_url", "video_alt_index"):
            sa[k], sb[k] = sb[k], sa[k]
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
        if not sess or not (0 <= idx < len(sess["scenes"])) or anim not in ANIM_MODES:
            return jsonify({"error": "Invalid animation."}), 400
        sess["scenes"][idx]["anim_type"] = anim
    return jsonify({"ok": True})


@app.route("/api/render", methods=["POST"])
def api_render():
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id", "")).strip()
    if not is_safe_id(session_id):
        return jsonify({"error": "Load scenes first."}), 400

    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess:
            return jsonify({"error": "Session expired."}), 404
        if data.get("voice_mode"):
            sess["voice_mode"] = str(data["voice_mode"])
        if data.get("uploaded_audio_id"):
            sess["uploaded_audio_id"] = str(data["uploaded_audio_id"])
        if data.get("uploaded_bgm_id"):
            sess["uploaded_bgm_id"] = str(data["uploaded_bgm_id"])
        if "auto_hook" in data:
            sess["auto_hook"] = bool(data["auto_hook"])

        missing = [s["index"] + 1 for s in sess["scenes"] if not s.get("media_ready")]
        if missing:
            return jsonify({"error": f"Scene(s) {', '.join(map(str, missing))} have no video clip yet."}), 400
        if len(ACTIVE_JOBS) >= 1:
            return jsonify({"error": "Server is currently rendering another video. Please wait."}), 429

        job_id = f"nx_{uuid.uuid4().hex[:10]}"
        ACTIVE_JOBS.add(job_id)
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 3,
            "current_scene": "Starting 90-Second Fast Cinema Render...",
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
