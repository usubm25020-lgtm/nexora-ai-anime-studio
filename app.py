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
from typing import Dict, List, Any, Optional, Tuple

import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from flask import Flask, request, jsonify, send_file

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

# ==============================================================================
# 1. ROMANCE APEX STUDIO CONFIGURATION & MEMORY-SAFE WORKSPACE
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


def is_safe_id(val: str) -> bool:
    return bool(val and SAFE_ID_RE.match(val))


def purge_expired(max_age_sec: int = 3600):
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


_FONT_CACHE: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}


def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    key = ("bold" if bold else "reg", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    paths = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for path in paths:
        if os.path.exists(path):
            try:
                fnt = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = fnt
                return fnt
            except Exception:
                pass
    return ImageFont.load_default()


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


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
# 3. ROMANTIC SCRIPT PARSER + B-ROLL KEYWORD + PHONE OVERLAY DETECTOR
# ==============================================================================
ANIM_MODES = [
    "3D_Zoom_Punch",
    "Cinema_Pan_Right",
    "Dramatic_Zoom_Out",
    "Cinema_Pan_Left",
    "Heartbeat_Pulse",
    "Tilt_Rotate_Drift",
    "Flash_Impact_Zoom",
    "Whip_Slide_In",
]

_STOP_WORDS = {
    "24-year-old", "26-year-old", "27-year-old", "28-year-old", "indian", "man", "woman",
    "aarav", "meera", "riya", "kabir", "photorealistic", "8k", "cinematic", "dramatic",
    "lighting", "no", "text", "with", "and", "the", "in", "on", "of", "a", "an", "to", "from",
}


def extract_broll_query(visual_prompt: str) -> str:
    clean = re.sub(r"[^a-zA-Z\s]", " ", visual_prompt.lower())
    words = [w for w in clean.split() if len(w) > 2 and w not in _STOP_WORDS]
    picked = words[-4:] if len(words) >= 4 else words
    return " ".join(picked[:3]) or "romantic night rain couple"


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


def detect_phone_popup(hinglish_txt: str, speaker: str) -> Optional[Dict[str, str]]:
    """Detects if a scene involves a late-night text message or incoming call for animated UI overlay."""
    low = hinglish_txt.lower()
    if any(k in low for k in ("message", "msg", "likha tha", "text aaya", "whatsapp")):
        sender = "Riya ❤️" if speaker == "male" else "Kabir 🖤"
        # Extract quoted part if present, otherwise use crisp snippet
        m = re.search(r"[—:-]\s*(.+)$", hinglish_txt)
        snippet = (m.group(1) if m else hinglish_txt)[:44]
        return {"type": "message", "sender": sender, "text": snippet}
    if any(k in low for k in ("phone baja", "call aaya", "incoming call", "ring")):
        sender = "Unknown Night Call" if "unknown" in low else ("Riya ❤️" if speaker == "male" else "Kabir 🖤")
        return {"type": "call", "sender": sender, "text": "Incoming Audio Call • 02:14 AM"}
    return None


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
                "broll_query": extract_broll_query(visual_prompt),
                "hindi_dub": spoken_hindi,
                "auto_caption": auto_caption,
                "phone_popup": detect_phone_popup(auto_caption, sp),
                "anim_type": "3D_Zoom_Punch" if idx == 0 else ANIM_MODES[idx % len(ANIM_MODES)],
                "sfx_type": detect_romantic_sfx(combined_txt, mood),
                "media_ready": False,
                "media_type": "image",  # "image", "video", or "remote_video"
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
# 4. 0.5-SECOND ACOUSTIC SYNC ENGINE
# ==============================================================================
class FastAcousticSyncEngine:
    @staticmethod
    def detect_speech_segments(wav_path: str, total_dur: float) -> List[Tuple[float, float]]:
        try:
            with wave.open(wav_path, "rb") as wf:
                sr = wf.getframerate()
                n_frames = wf.getnframes()
                raw = wf.readframes(n_frames)
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if wf.getnchannels() > 1:
                    samples = samples[::wf.getnchannels()]

            win = max(1, int(sr * 0.04))
            trim_len = (len(samples) // win) * win
            if trim_len == 0:
                return []
            frames = samples[:trim_len].reshape(-1, win)
            rms = np.sqrt(np.mean(frames * frames, axis=1))
            max_e = float(np.max(rms)) if len(rms) else 0.0
            thresh = max(0.012, max_e * 0.075)

            segs: List[Tuple[float, float]] = []
            in_speech = False
            st = 0.0
            sil_run = 0.0
            for k, e in enumerate(rms):
                t = k * 0.04
                if e > thresh:
                    if not in_speech:
                        in_speech = True
                        st = max(0.0, t - 0.03)
                    sil_run = 0.0
                elif in_speech:
                    sil_run += 0.04
                    if sil_run >= 0.22:
                        segs.append((st, max(st + 0.08, t - 0.10)))
                        in_speech = False
            if in_speech:
                segs.append((st, total_dur))
            return segs
        except Exception:
            return []

    @classmethod
    def align_scenes_and_words(cls, scenes: List[Dict[str, Any]], master_wav: str, total_dur: float):
        speech_segs = cls.detect_speech_segments(master_wav, total_dur)
        pauses = [en for (_, en) in speech_segs]

        weights = []
        for s in scenes:
            txt = s.get("auto_caption") or ""
            punct = len(re.findall(r"[.!?।]", txt)) * 4.5 + len(re.findall(r"[,—;:]", txt)) * 2.5
            weights.append(max(4.0, float(len(re.sub(r"\s+", "", txt))) + punct))
        total_w = sum(weights) or 1.0

        cursor = 0.0
        for idx, sc in enumerate(scenes):
            raw_dur = (weights[idx] / total_w) * total_dur
            st = cursor
            en = cursor + raw_dur

            if pauses and idx < len(scenes) - 1:
                best_p = en
                min_diff = 0.75
                for p in pauses:
                    d = abs(p - en)
                    if d < min_diff:
                        min_diff = d
                        best_p = p
                en = best_p

            cursor = en
            sc["start_sec"] = st
            sc["end_sec"] = en
            sc["duration_sec"] = max(0.5, round(en - st, 3))

        if scenes:
            scenes[0]["start_sec"] = 0.0
            scenes[-1]["end_sec"] = total_dur
            scenes[-1]["duration_sec"] = max(0.5, round(total_dur - scenes[-1]["start_sec"], 3))

        for sc in scenes:
            st = sc["start_sec"]
            en = sc["end_sec"]
            span = max(0.1, en - st)
            active_start = 0.0
            active_end = 1.0
            if speech_segs:
                inside = [(max(st, s0), min(en, s1)) for (s0, s1) in speech_segs if s1 > st and s0 < en]
                if inside:
                    active_start = max(0.0, (inside[0][0] - st) / span)
                    active_end = min(1.0, max(active_start + 0.15, (inside[-1][1] - st) / span))
            sc["word_timings"] = NexoraRomanceRenderer.build_word_timings(
                sc.get("auto_caption", ""),
                win_start=active_start,
                win_end=active_end,
            )


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
                    return {"voice": "hi-IN-SwaraNeural", "rate": "-3%", "pitch": "-3Hz"}
                if emo == "emotional_ache":
                    return {"voice": "hi-IN-SwaraNeural", "rate": "-1%", "pitch": "-2Hz"}
                return {"voice": "hi-IN-SwaraNeural", "rate": "+5%", "pitch": "-1Hz"}
            if sp == "male":
                if emo == "intimate_whisper":
                    return {"voice": "hi-IN-MadhurNeural", "rate": "-4%", "pitch": "-9Hz"}
                if emo == "emotional_ache":
                    return {"voice": "hi-IN-MadhurNeural", "rate": "-1%", "pitch": "-7Hz"}
                return {"voice": "hi-IN-MadhurNeural", "rate": "+5%", "pitch": "-6Hz"}
            if sp == "child":
                return {"voice": "hi-IN-SwaraNeural", "rate": "+9%", "pitch": "+14Hz"}

        # Late-Night FM Romantic Whisper Narrator
        if emo == "intimate_whisper":
            return {"voice": "hi-IN-MadhurNeural", "rate": "-4%", "pitch": "-11Hz"}
        if emo == "emotional_ache":
            return {"voice": "hi-IN-MadhurNeural", "rate": "-2%", "pitch": "-9Hz"}
        if emo == "urgent_passion":
            return {"voice": "hi-IN-MadhurNeural", "rate": "+10%", "pitch": "-7Hz"}
        return {"voice": "hi-IN-MadhurNeural", "rate": "+3%", "pitch": "-9Hz"}

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
                time.sleep(0.5 * (attempt + 1))

        speech_dur = cls.probe_dur(mp3_p)
        if speech_dur <= 0.20:
            raise RuntimeError(f"Voice timeout on Scene {idx + 1} ({last_err}).")

        # Emotional breath pause (0.28s) for intimate FM storytelling feel
        final_dur = round(speech_dur + 0.28, 2)

        # Warm Close-Mic FM Mastering: Silky Sub-Bass (+4.5dB @ 120Hz), Soft Air (+2dB @ 3.5kHz), Gentle Velvet Reverb
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
        sfx_len = min(n_samples, int(rate * 1.90))
        if sfx_len <= 10:
            return out
        t = np.linspace(0, sfx_len / float(rate), sfx_len, endpoint=False, dtype=np.float32)
        rng = np.random.default_rng(202)

        if sfx_type == "phone_chime":
            # Soft romantic message chime (E5 -> B5)
            c1 = np.exp(-t * 8.0) * np.sin(2.0 * np.pi * 659.25 * t)
            c2 = np.exp(-np.maximum(0.0, t - 0.14) * 7.0) * np.sin(2.0 * np.pi * 987.77 * t) * (t >= 0.14)
            out[:sfx_len] = (c1 + c2) * 0.28

        elif sfx_type == "soft_rain":
            rain = rng.normal(0.0, 0.22, sfx_len).astype(np.float32)
            kernel = np.ones(18, dtype=np.float32) / 18.0
            rain = np.convolve(rain, kernel, mode="same")
            env = np.sin(np.pi * (t / (sfx_len / float(rate))))
            out[:sfx_len] = rain * env * 0.55

        elif sfx_type == "soft_heartbeat":
            b1 = np.exp(-t * 13.0) * np.sin(2.0 * np.pi * 54.0 * t)
            b2 = np.exp(-np.maximum(0.0, t - 0.30) * 13.0) * np.sin(2.0 * np.pi * 48.0 * t) * (t >= 0.30)
            out[:sfx_len] = (b1 + b2) * 0.65

        elif sfx_type == "emotional_swell":
            # Warm cello/piano harmonic swell
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
                    # Deep emotional Dm7/Am piano-cello pad
                    pad = (
                        0.45 * np.sin(2.0 * np.pi * 146.83 * t)
                        + 0.35 * np.sin(2.0 * np.pi * 174.61 * t)
                        + 0.28 * np.sin(2.0 * np.pi * 220.00 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 73.42 * t)
                    )
                    bgm = pad * 0.13 * swell
                elif mood == "secret_suspense":
                    # Intimate Am7 + subtle midnight tension pulse
                    pulse = 0.8 + 0.2 * np.sin(2.0 * np.pi * 0.6 * t)
                    pad = (
                        0.42 * np.sin(2.0 * np.pi * 110.00 * t)
                        + 0.32 * np.sin(2.0 * np.pi * 130.81 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 164.81 * t)
                        + 0.22 * np.sin(2.0 * np.pi * 55.00 * t)
                    )
                    bgm = pad * 0.13 * swell * pulse
                else:
                    # Warm Candlelight Fmaj7/Am romantic velvet chord
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
# 6. ROMANCE APEX VISUAL ENGINE (CROSS-DISSOLVE + PHONE POPUP + BOKEH + CLIFFHANGER)
# ==============================================================================
class NexoraRomanceRenderer:
    POWER_WORDS = {
        "pyaar", "ishq", "mohabbat", "raaz", "dil", "saansein", "kareeb",
        "intezar", "dard", "aansu", "kasam", "junoon", "dhokha", "sach",
        "chhua", "baahon", "kiss", "secret", "adhura", "tanha", "alvida", "raat",
    }

    def __init__(self, width: int, height: int, mood: str = "deep_romance"):
        self.w = width
        self.h = height
        self.mood = mood
        self.sc = min(width, height) / 540.0

        rng = np.random.default_rng(999)
        self.particles = np.column_stack([
            rng.uniform(0, width, 65),
            rng.uniform(0, height, 65),
            rng.uniform(1.5, 3.6, 65),
            rng.uniform(0.4, 1.3, 65),
        ])

    def prepare_scaled_image(self, raw_im: Image.Image) -> Image.Image:
        im = raw_im.convert("RGB")
        tw, th = int(self.w * 1.32), int(self.h * 1.32)
        im = im.resize((tw, th), Image.Resampling.BILINEAR)
        im = ImageEnhance.Contrast(im).enhance(1.13)
        im = ImageEnhance.Color(im).enhance(1.22)
        return im

    def extract_video_frames_for_scene(self, video_path: str, dur_sec: float, fps: int, out_dir: str) -> List[str]:
        os.makedirs(out_dir, exist_ok=True)
        tw, th = int(self.w * 1.32), int(self.h * 1.32)
        tw += tw % 2
        th += th % 2
        pattern = os.path.join(out_dir, "f_%05d.jpg")
        vf = (
            f"scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},"
            "eq=contrast=1.12:saturation=1.20"
        )
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-stream_loop", "-1",
                "-i", video_path,
                "-t", f"{dur_sec + 0.35:.3f}",
                "-r", str(fps),
                "-vf", vf,
                "-q:v", "3",
                pattern,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        return sorted(os.path.join(out_dir, fn) for fn in os.listdir(out_dir) if fn.endswith(".jpg"))

    @staticmethod
    def build_word_timings(text: str, win_start: float = 0.0, win_end: float = 1.0) -> List[Tuple[str, float, float]]:
        words = str(text or "").split()
        if not words:
            return []
        weights: List[float] = []
        for w in words:
            base_w = max(2.0, float(len(re.sub(r"[^a-zA-Z0-9]", "", w))))
            if w.endswith((",", ";", ":")):
                base_w += 3.0
            elif w.endswith((".", "!", "?", "।")):
                base_w += 4.2
            weights.append(base_w)
        total_w = sum(weights) or 1.0
        span = max(0.05, win_end - win_start)
        timings: List[Tuple[str, float, float]] = []
        acc = 0.0
        for w, wt in zip(words, weights):
            st = win_start + (acc / total_w) * span
            acc += wt
            en = win_start + (acc / total_w) * span
            timings.append((w, st, en))
        return timings

    def _draw_top_hud(self, draw: ImageDraw.Draw, is_teaser: bool, climax_ts_label: str, ov_prog: float, bar_h: int):
        f_hud = get_font(max(11, int(12 * self.sc)), bold=True)
        pad_x = int(14 * self.sc)
        pad_y = bar_h + int(10 * self.sc)

        if is_teaser:
            badge = f"🔥 AAGE DEKHIYE ({climax_ts_label} PE ASLI MOD)..."
            bb = draw.textbbox((0, 0), badge, font=f_hud)
            bw = (bb[2] - bb[0]) + int(24 * self.sc)
            bh = (bb[3] - bb[1]) + int(12 * self.sc)
            draw.rounded_rectangle([pad_x, pad_y, pad_x + bw, pad_y + bh], radius=8, fill=(219, 39, 119), outline=(255, 255, 255), width=2)
            draw.text((pad_x + int(12 * self.sc), pad_y + int(5 * self.sc)), badge, font=f_hud, fill=(255, 255, 255))
        else:
            stage = "01. PEHLI MULAKAT" if ov_prog < 0.34 else ("02. GEHRA RAAZ" if ov_prog < 0.72 else "03. ASLI ANJAAM")
            bb = draw.textbbox((0, 0), stage, font=f_hud)
            bw = (bb[2] - bb[0]) + int(20 * self.sc)
            bh = (bb[3] - bb[1]) + int(10 * self.sc)
            draw.rounded_rectangle([pad_x, pad_y, pad_x + bw, pad_y + bh], radius=8, fill=(10, 12, 24), outline=(244, 114, 182), width=1)
            draw.text((pad_x + int(10 * self.sc), pad_y + int(4 * self.sc)), stage, font=f_hud, fill=(244, 114, 182))

    def _draw_phone_notification_popup(self, draw: ImageDraw.Draw, popup: Dict[str, str], sc_prog: float, bar_h: int):
        """Renders an animated late-night smartphone message/call notification card."""
        if sc_prog < 0.08 or sc_prog > 0.92:
            return
        slide = min(1.0, (sc_prog - 0.08) / 0.14)
        e = ease_in_out(slide)

        f_sender = get_font(max(12, int(13 * self.sc)), bold=True)
        f_msg = get_font(max(11, int(12 * self.sc)), bold=False)

        card_w = int(275 * self.sc)
        card_h = int(56 * self.sc)
        cx1 = self.w - int(16 * self.sc)
        cx0 = cx1 - card_w
        cy0 = bar_h + int((10 + (1.0 - e) * -45) * self.sc)
        cy1 = cy0 + card_h

        border_c = (16, 185, 129) if popup["type"] == "call" else (244, 114, 182)
        draw.rounded_rectangle([cx0, cy0, cx1, cy1], radius=12, fill=(12, 16, 30), outline=border_c, width=2)

        icon = "📞" if popup["type"] == "call" else "💬"
        draw.text((cx0 + int(12 * self.sc), cy0 + int(8 * self.sc)), f"{icon} {popup['sender']}", font=f_sender, fill=border_c)
        draw.text((cx0 + int(12 * self.sc), cy0 + int(30 * self.sc)), popup["text"], font=f_msg, fill=(241, 245, 249))

    def _draw_punchy_3word_captions(self, draw: ImageDraw.Draw, timings: List[Tuple[str, float, float]], sc_prog: float, voice_mode: str, speaker: str, bar_h: int):
        if not timings:
            return

        cur_w_idx = len(timings) - 1
        for i, (_, st, en) in enumerate(timings):
            if sc_prog <= en:
                cur_w_idx = i
                break

        group_size = 3
        grp_idx = cur_w_idx // group_size
        grp_start = grp_idx * group_size
        grp_words = timings[grp_start : grp_start + group_size]

        f_sub = get_font(max(22, int(29 * self.sc)), bold=True)
        f_tag = get_font(max(10, int(11 * self.sc)), bold=True)

        full_grp_str = " ".join(w[0] for w in grp_words)
        bb = draw.textbbox((0, 0), full_grp_str, font=f_sub)
        pw = bb[2] - bb[0]
        ph = bb[3] - bb[1]

        accent = (244, 114, 182) if self.mood == "deep_romance" else ((56, 189, 248) if self.mood == "sad_heartbreak" else (251, 113, 133))

        show_tag = voice_mode == "builtin_multi"
        box_w = min(int(self.w * 0.92), pw + int(48 * self.sc))
        box_h = ph + (int(34 * self.sc) if show_tag else int(24 * self.sc))
        bx0 = (self.w - box_w) // 2
        by1 = self.h - bar_h - int(14 * self.sc)
        by0 = by1 - box_h

        draw.rounded_rectangle([bx0, by0, bx0 + box_w, by1], radius=14, fill=(8, 10, 20), outline=accent, width=2)

        if show_tag:
            sp_map = {"narrator": "LATE-NIGHT STORY", "male": "HE", "female": "SHE", "child": "CHILD"}
            draw.text((bx0 + int(14 * self.sc), by0 + int(5 * self.sc)), sp_map.get(speaker, "STORY"), font=f_tag, fill=accent)

        tx = (self.w - pw) // 2
        ty = by0 + (int(18 * self.sc) if show_tag else int(11 * self.sc))

        for offset_i, (word_str, _, _) in enumerate(grp_words):
            abs_i = grp_start + offset_i
            is_spoken_now = abs_i == cur_w_idx
            is_already_spoken = abs_i < cur_w_idx
            clean_low = re.sub(r"[^a-z]", "", word_str.lower())
            is_power = clean_low in self.POWER_WORDS

            if is_spoken_now:
                w_col = (253, 224, 71) if is_power else accent
            elif is_already_spoken:
                w_col = (255, 255, 255)
            else:
                w_col = (148, 163, 184)

            draw.text((tx + 2, ty + 2), word_str, font=f_sub, fill=(0, 0, 0))
            draw.text((tx, ty), word_str, font=f_sub, fill=w_col)
            tx += draw.textbbox((0, 0), word_str + " ", font=f_sub)[2]

    def compose_camera_crop(self, base_img: Image.Image, anim: str, sc_prog: float, global_t: float, is_video: bool, is_teaser: bool, sfx_type: str) -> Image.Image:
        pw, ph = base_img.size
        e = ease_in_out(sc_prog)
        float_x = 0.03 * math.sin(global_t * 2.2)
        float_y = 0.03 * math.cos(global_t * 1.8)
        rot_deg = 0.0
        motion_mult = 0.52 if is_video else 1.0

        if anim == "3D_Zoom_Punch":
            scale = 1.02 + (0.22 * motion_mult) * min(1.0, sc_prog * 1.45)
            pan_x, pan_y = 0.5 + float_x, 0.44 + float_y
        elif anim == "Dramatic_Zoom_Out":
            scale = 1.22 - (0.18 * motion_mult) * e
            pan_x, pan_y = 0.5 + float_x, 0.5 + float_y
        elif anim == "Cinema_Pan_Right":
            scale = 1.13
            pan_x, pan_y = 0.5 + (e - 0.5) * motion_mult, 0.48 + float_y
        elif anim == "Cinema_Pan_Left":
            scale = 1.13
            pan_x, pan_y = 0.5 + (0.5 - e) * motion_mult, 0.48 + float_y
        elif anim == "Heartbeat_Pulse":
            scale = 1.08 + 0.07 * abs(math.sin(sc_prog * math.pi * 3.0))
            pan_x, pan_y = 0.5 + 0.12 * math.sin(sc_prog * math.pi) * motion_mult, 0.5 + float_y
        elif anim == "Tilt_Rotate_Drift":
            scale = 1.15 + 0.06 * e
            pan_x, pan_y = 0.5 + (e - 0.5) * motion_mult, 0.5 + (0.5 - e) * motion_mult
            rot_deg = math.sin(sc_prog * math.pi) * 1.9 * motion_mult
        elif anim == "Flash_Impact_Zoom":
            scale = 1.04 + 0.20 * e
            pan_x, pan_y = 0.5 + float_x, 0.5 + float_y
        elif anim == "Whip_Slide_In":
            scale = 1.13
            whip = min(1.0, sc_prog * 4.5)
            pan_x, pan_y = 0.15 + 0.70 * ease_in_out(whip), 0.5 + float_y
        else:
            scale = 1.06 + 0.11 * e
            pan_x, pan_y = e, e

        crop_w = int(pw / scale)
        crop_h = int(ph / scale)
        max_dx = max(1, pw - crop_w)
        max_dy = max(1, ph - crop_h)

        ox = int(max_dx * max(0.0, min(1.0, pan_x)))
        oy = int(max_dy * max(0.0, min(1.0, pan_y)))

        if (is_teaser or anim in ("Flash_Impact_Zoom", "3D_Zoom_Punch") or sfx_type == "thunder") and sc_prog < 0.22:
            ox = max(0, min(max_dx, ox + int(math.sin(global_t * 50.0) * 9 * self.sc)))
            oy = max(0, min(max_dy, oy + int(math.cos(global_t * 45.0) * 7 * self.sc)))

        frame = base_img.crop((ox, oy, ox + crop_w, oy + crop_h)).resize((self.w, self.h), Image.Resampling.BILINEAR)
        if abs(rot_deg) > 0.05:
            frame = frame.rotate(rot_deg, resample=Image.Resampling.BILINEAR, expand=False)
        return frame

    def render_frame(
        self,
        scene: Dict[str, Any],
        timings: List[Tuple[str, float, float]],
        sc_idx: int,
        sc_prog: float,
        global_t: float,
        ov_prog: float,
        voice_mode: str,
        base_img: Image.Image,
        prev_frame_img: Optional[Image.Image] = None,
    ) -> Tuple[bytes, Image.Image]:
        is_teaser = bool(scene.get("is_teaser", False))
        is_video = scene.get("media_type") in ("video", "remote_video")
        anim = "Flash_Impact_Zoom" if is_teaser else scene.get("anim_type", "Cinema_Pan_Right")

        raw_crop = self.compose_camera_crop(base_img, anim, sc_prog, global_t, is_video, is_teaser, scene.get("sfx_type", ""))

        # Smooth 0.35s Cross-Dissolve Transition from previous scene!
        if prev_frame_img is not None and sc_prog < 0.10:
            blend_alpha = ease_in_out(sc_prog / 0.10)
            frame = Image.blend(prev_frame_img, raw_crop, blend_alpha)
        else:
            frame = raw_crop

        if (is_teaser or anim in ("Flash_Impact_Zoom", "3D_Zoom_Punch")) and sc_prog < 0.07:
            frame = ImageEnhance.Brightness(frame).enhance(1.0 + 0.55 * (1.0 - sc_prog / 0.07))

        draw = ImageDraw.Draw(frame)

        # Romantic Golden Bokeh / Midnight Rain Particles
        if self.mood == "sad_heartbreak" or scene.get("sfx_type") == "soft_rain":
            for px0, py0, pr, spd in self.particles:
                px = (px0 - global_t * 18.0 * spd) % self.w
                py = (py0 + global_t * 95.0 * spd) % self.h
                draw.line([(px, py), (px - 2 * self.sc, py + 11 * self.sc)], fill=(186, 230, 253), width=max(1, int(self.sc)))
        else:
            for px0, py0, pr, spd in self.particles:
                px = (px0 + math.sin(global_t * 1.3 + py0) * 20.0) % self.w
                py = (py0 - global_t * 14.0 * spd) % self.h
                r_px = max(2, int(pr * self.sc * 1.25))
                col = (253, 224, 71) if self.mood == "deep_romance" else (244, 114, 182)
                draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=col)

        bar_h = int(self.h * 0.055) if self.w > self.h else 0
        if bar_h > 0:
            draw.rectangle([0, 0, self.w, bar_h], fill=(4, 6, 12))
            draw.rectangle([0, self.h - bar_h, self.w, self.h], fill=(4, 6, 12))

        self._draw_top_hud(draw, is_teaser, scene.get("climax_ts_label", "01:45"), ov_prog, bar_h)

        if scene.get("phone_popup"):
            self._draw_phone_notification_popup(draw, scene["phone_popup"], sc_prog, bar_h)

        self._draw_punchy_3word_captions(draw, timings, sc_prog, voice_mode, scene.get("speaker", "narrator"), bar_h)

        # Cliffhanger End-Card overlay on the final 2.5 seconds of the video!
        if ov_prog > 0.965 and not is_teaser:
            fade = min(1.0, (ov_prog - 0.965) / 0.035)
            dark_ov = Image.new("RGB", (self.w, self.h), (5, 7, 15))
            frame = Image.blend(frame, dark_ov, fade * 0.82)
            d2 = ImageDraw.Draw(frame)
            f_end = get_font(max(20, int(28 * self.sc)), bold=True)
            f_sub2 = get_font(max(13, int(16 * self.sc)), bold=True)
            t1 = "KAHANI ABHI BAAKI HAI..."
            t2 = "Agle Part Ke Liye Subscribe Zaroor Karein ❤️"
            w1 = d2.textbbox((0, 0), t1, font=f_end)[2]
            w2 = d2.textbbox((0, 0), t2, font=f_sub2)[2]
            d2.text(((self.w - w1) // 2, int(self.h * 0.42)), t1, font=f_end, fill=(244, 114, 182))
            d2.text(((self.w - w2) // 2, int(self.h * 0.53)), t2, font=f_sub2, fill=(253, 224, 71))

        bar_w = int(self.w * ov_prog)
        accent_c = (244, 114, 182) if self.mood == "deep_romance" else ((56, 189, 248) if self.mood == "sad_heartbreak" else (251, 113, 133))
        draw.rectangle([0, self.h - 4, bar_w, self.h], fill=accent_c)

        return frame.tobytes(), raw_crop


# ==============================================================================
# 7. APEX RENDER PIPELINE (STREAMING B-ROLL DOWNLOAD + CROSS-DISSOLVE + SIDECHAIN)
# ==============================================================================
def update_job(job_id: str, **kwargs):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def fmt_mmss(sec: float) -> str:
    s = max(0, int(round(sec)))
    return f"{s // 60:02d}:{s % 60:02d}"


def download_remote_broll_to_temp(url: str, out_path: str):
    with requests.get(url, stream=True, timeout=25) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)


def run_final_render(job_id: str, session_id: str):
    work_dir = tempfile.mkdtemp(prefix=f"nxr_{job_id}_", dir=TMP_ROOT)
    try:
        purge_expired(3600)
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
                raise RuntimeError(f"Scene {sc['index'] + 1} has no video or image yet. Use Auto-Fill HD Videos or upload one!")
            if sc.get("media_type") != "remote_video":
                med_p = os.path.join(sess_dir, sc.get("media_file", ""))
                if not os.path.exists(med_p):
                    raise RuntimeError(f"Scene {sc['index'] + 1} file missing. Re-upload or Auto-Fill.")

        mood = sess["genre"]
        topic = sess["topic"]
        aspect = sess["aspect_ratio"]
        voice_mode = sess["voice_mode"]
        auto_hook = bool(sess.get("auto_hook", True))
        uploaded_audio_id = sess.get("uploaded_audio_id", "")
        uploaded_bgm_id = sess.get("uploaded_bgm_id", "")

        out_w, out_h = (540, 960) if aspect == "9:16" else (960, 540)
        fps = 15

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

            update_job(job_id, status="running", progress=6, current_scene="Instant Acoustic Sync & Auto-Captioning...")
            subprocess.run(
                ["ffmpeg", "-y", "-i", uploaded_audio_path, "-ar", "22050", "-ac", "1", master_voice],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            total_el_dur = NexoraRomanceAudioEngine.probe_dur(master_voice)
            if total_el_dur <= 1.0:
                raise RuntimeError("Uploaded audio file is invalid or empty.")
            FastAcousticSyncEngine.align_scenes_and_words(raw_scenes, master_voice, total_el_dur)
        else:
            running_t = 0.0
            for idx, sc in enumerate(raw_scenes):
                update_job(
                    job_id,
                    status="running",
                    progress=4 + int((idx / float(len(raw_scenes))) * 15),
                    current_scene=f"Dubbing Intimate Voice ({idx + 1}/{len(raw_scenes)}) [{sc['emotion']}]...",
                )
                dub_wav, actual_dur = NexoraRomanceAudioEngine.dub_scene_romantic(sc, idx, voice_mode, work_dir)
                sc["start_sec"] = running_t
                sc["end_sec"] = running_t + actual_dur
                sc["duration_sec"] = actual_dur
                sc["wav_file"] = dub_wav
                running_t += actual_dur
                sc["word_timings"] = NexoraRomanceRenderer.build_word_timings(sc.get("auto_caption", ""), 0.02, 0.94)
                voice_chunks.append(dub_wav)

            a_list = os.path.join(work_dir, "a_list.txt")
            with open(a_list, "w", encoding="utf-8") as af:
                for ac in voice_chunks:
                    af.write(f"file '{ac}'\n")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", a_list, "-c", "copy", master_voice],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )

        render_scenes: List[Dict[str, Any]] = []
        hook_dur = 0.0

        if auto_hook and len(raw_scenes) >= 4:
            climax_idx = min(len(raw_scenes) - 2, max(2, int(len(raw_scenes) * 0.75)))
            climax_sc = raw_scenes[climax_idx]
            hook_sc = dict(climax_sc)
            hook_sc["is_teaser"] = True
            hook_sc["anim_type"] = "Flash_Impact_Zoom"
            hook_sc["sfx_type"] = "soft_heartbeat"
            hook_dur = min(4.5, float(climax_sc.get("duration_sec", 4.0)))
            hook_sc["duration_sec"] = hook_dur
            hook_sc["climax_ts_label"] = fmt_mmss(float(climax_sc.get("start_sec", 60.0)) + hook_dur)
            render_scenes.append(hook_sc)

            hook_wav = os.path.join(work_dir, "hook_voice.wav")
            joined_voice = os.path.join(work_dir, "joined_voice.wav")
            if voice_mode != "uploaded" and climax_sc.get("wav_file") and os.path.exists(climax_sc["wav_file"]):
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", climax_sc["wav_file"],
                        "-t", f"{hook_dur:.3f}", "-ar", "22050", "-ac", "1", hook_wav,
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                )
            else:
                climax_start = float(climax_sc.get("start_sec", 0.0))
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-ss", f"{climax_start:.3f}", "-t", f"{hook_dur:.3f}",
                        "-i", master_voice, "-ar", "22050", "-ac", "1", hook_wav,
                    ],
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

        renderer = NexoraRomanceRenderer(out_w, out_h, mood=mood)
        total_sc = len(render_scenes)
        est_total_dur = sum(float(s.get("duration_sec", 4.0)) for s in render_scenes)
        global_t = 0.0
        dt = 1.0 / float(fps)

        video_chunks: List[str] = []
        # Warm Candlelight / Midnight Rain Romantic Color Grading + Subtle 35mm Film Grain
        cinema_vf = (
            "eq=contrast=1.08:brightness=-0.01:saturation=1.22,"
            "colorbalance=rs=0.04:gs=0.01:bs=-0.03,"
            "noise=alls=4:allf=t+u,"
            "unsharp=3:3:0.45:3:3:0.0,"
            "vignette=PI/5"
        )

        last_scene_end_frame: Optional[Image.Image] = None

        for idx, sc in enumerate(render_scenes):
            lbl = "Climax Hook Teaser" if sc.get("is_teaser") else f"Scene {idx + 1}/{total_sc}"
            update_job(
                job_id,
                status="running",
                progress=19 + int((idx / float(total_sc)) * 73),
                current_scene=f"Rendering {lbl} ({sc['anim_type']} + Cross-Dissolve)...",
            )

            actual_dur = float(sc.get("duration_sec", 4.0))
            timings = sc.get("word_timings") or NexoraRomanceRenderer.build_word_timings(sc.get("auto_caption", ""))

            n_frames = max(1, int(round(actual_dur * fps)))
            v_chunk = os.path.join(work_dir, f"v_{idx:03d}.mp4")

            extracted_frames: List[str] = []
            static_img: Optional[Image.Image] = None
            sc_frame_dir = os.path.join(work_dir, f"vidf_{idx:03d}")
            temp_dl_vid = os.path.join(work_dir, f"dl_{idx:03d}.mp4")

            mtype = sc.get("media_type", "image")
            if mtype == "remote_video":
                download_remote_broll_to_temp(sc["remote_video_url"], temp_dl_vid)
                extracted_frames = renderer.extract_video_frames_for_scene(temp_dl_vid, actual_dur, fps, sc_frame_dir)
                if os.path.exists(temp_dl_vid):
                    os.remove(temp_dl_vid)
            elif mtype == "video":
                med_path = os.path.join(sess_dir, sc["media_file"])
                extracted_frames = renderer.extract_video_frames_for_scene(med_path, actual_dur, fps, sc_frame_dir)
            else:
                med_path = os.path.join(sess_dir, sc["media_file"])
                with Image.open(med_path) as raw_im:
                    static_img = renderer.prepare_scaled_image(raw_im)

            proc = subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
                    "-vf", cinema_vf,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
                    "-pix_fmt", "yuv420p", v_chunk,
                ],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )

            latest_crop: Optional[Image.Image] = None
            for f_i in range(n_frames):
                sc_prog = f_i / float(max(1, n_frames - 1))
                ov_prog = min(1.0, global_t / max(1.0, est_total_dur))
                if extracted_frames:
                    fp = extracted_frames[f_i % len(extracted_frames)]
                    with Image.open(fp) as vf_im:
                        cur_base = vf_im.convert("RGB")
                        raw, latest_crop = renderer.render_frame(
                            sc, timings, idx, sc_prog, global_t, ov_prog, voice_mode,
                            base_img=cur_base, prev_frame_img=last_scene_end_frame,
                        )
                else:
                    raw, latest_crop = renderer.render_frame(
                        sc, timings, idx, sc_prog, global_t, ov_prog, voice_mode,
                        base_img=static_img, prev_frame_img=last_scene_end_frame,
                    )
                proc.stdin.write(raw)
                global_t += dt

            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f"FFmpeg encoding error on {lbl}.")

            last_scene_end_frame = latest_crop
            video_chunks.append(v_chunk)
            if static_img is not None:
                static_img.close()
                static_img = None
            if os.path.isdir(sc_frame_dir):
                shutil.rmtree(sc_frame_dir, ignore_errors=True)
            gc.collect()

        update_job(job_id, progress=93, current_scene="Mastering Romantic Soundscape, Sidechain Audio & MP4...")
        v_list = os.path.join(work_dir, "v_list.txt")
        with open(v_list, "w", encoding="utf-8") as vf:
            for vc in video_chunks:
                vf.write(f"file '{vc}'\n")

        joined_v = os.path.join(work_dir, "joined_v.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", v_list, "-c", "copy", joined_v],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

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

        filter_audio = (
            "[1:a]asplit=2[v_main][v_sc];"
            "[2:a][v_sc]sidechaincompress=threshold=0.03:ratio=4:attack=15:release=250[bg_ducked];"
            "[v_main]volume=1.38[v_loud];"
            "[bg_ducked]volume=0.66[bg_loud];"
            "[v_loud][bg_loud]amix=inputs=2:duration=first[aout]"
        )

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", joined_v,
                "-i", master_voice,
                "-i", sfx_bgm_wav,
                "-filter_complex", filter_audio,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-shortest", out_path,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        actual_total_dur = NexoraRomanceAudioEngine.probe_dur(out_path) or global_t
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
# 8. REST API ENDPOINTS (AUTO PEXELS B-ROLL FETCHER + HYBRID UPLOAD)
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
    return jsonify({"ok": True, "ffmpeg": HAS_FFMPEG, "voice_ready": HAS_EDGE_TTS, "romance_v16": True})


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
        return jsonify({"error": "Paste a valid 3-column script first (Speaker | Visual Prompt | Spoken Hindi)."}), 400

    session_id = f"ses_{uuid.uuid4().hex[:10]}"
    os.makedirs(os.path.join(SESSIONS_DIR, session_id), exist_ok=True)
    el_script = build_elevenlabs_script(scenes, voice_mode)

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


@app.route("/api/session/<session_id>/autofill_pexels", methods=["POST"])
def api_autofill_pexels(session_id: str):
    """Automatically searches Pexels API for every empty scene and assigns matching HD B-Roll video clips!"""
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session ID."}), 400
    data = request.get_json(silent=True) or {}
    api_key = str(data.get("pexels_key", "")).strip()
    if not api_key:
        return jsonify({"error": "Enter your free Pexels API Key first!"}), 400

    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess:
            return jsonify({"error": "Session expired."}), 404
        scenes_copy = [dict(s) for s in sess["scenes"]]
        orientation = "portrait" if sess.get("aspect_ratio") == "9:16" else "landscape"

    headers = {"Authorization": api_key}
    used_ids = set()
    filled_count = 0

    for sc in scenes_copy:
        if sc.get("media_ready"):
            continue
        query = sc.get("broll_query") or "romantic night couple"
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": query, "orientation": orientation, "per_page": 6, "size": "medium"},
                timeout=10,
            )
            if resp.status_code == 401:
                return jsonify({"error": "Invalid Pexels API Key. Please check your key."}), 401
            if resp.status_code != 200:
                continue
            vids = resp.json().get("videos", [])
            chosen = None
            for v in vids:
                if v.get("id") not in used_ids:
                    chosen = v
                    break
            if not chosen and vids:
                chosen = vids[0]
            if not chosen:
                continue

            used_ids.add(chosen.get("id"))
            # Pick clean ~720p/540p MP4 stream for fast zero-bloat rendering
            vfiles = chosen.get("video_files", [])
            mp4s = [vf for vf in vfiles if vf.get("file_type") == "video/mp4" and vf.get("link")]
            mp4s.sort(key=lambda x: abs((x.get("width") or 960) - 960))
            if not mp4s:
                continue

            best_link = mp4s[0]["link"]
            thumb_pic = chosen.get("image") or ""
            idx = sc["index"]
            with LOCK:
                if session_id in SESSIONS:
                    SESSIONS[session_id]["scenes"][idx]["media_ready"] = True
                    SESSIONS[session_id]["scenes"][idx]["media_type"] = "remote_video"
                    SESSIONS[session_id]["scenes"][idx]["remote_video_url"] = best_link
                    SESSIONS[session_id]["scenes"][idx]["thumb_url"] = thumb_pic
                    filled_count += 1
        except Exception:
            continue

    return jsonify({"ok": True, "filled": filled_count})


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
def api_replace_media(session_id: str, idx: int):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess or idx < 0 or idx >= len(sess["scenes"]):
        return jsonify({"error": "Scene not found."}), 404

    f = request.files.get("media")
    if not f or not f.filename:
        return jsonify({"error": "Select a video or image file first."}), 400

    sess_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(sess_dir, exist_ok=True)
    mtype, mfile, tfile = save_uploaded_media(f, sess_dir, idx)

    with LOCK:
        sess["scenes"][idx]["media_ready"] = True
        sess["scenes"][idx]["media_type"] = mtype
        sess["scenes"][idx]["media_file"] = mfile
        sess["scenes"][idx]["thumb_file"] = tfile
        sess["scenes"][idx]["remote_video_url"] = ""
        sess["scenes"][idx]["thumb_url"] = ""
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

    u_ptr = 0
    for idx in range(total_scenes):
        target_f = explicit_map.get(idx)
        if target_f is None and u_ptr < len(unmapped_files):
            target_f = unmapped_files[u_ptr]
            u_ptr += 1
        if target_f is not None:
            mtype, mfile, tfile = save_uploaded_media(target_f, sess_dir, idx)
            with LOCK:
                sess["scenes"][idx]["media_ready"] = True
                sess["scenes"][idx]["media_type"] = mtype
                sess["scenes"][idx]["media_file"] = mfile
                sess["scenes"][idx]["thumb_file"] = tfile
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
        for k in ("media_ready", "media_type", "media_file", "thumb_file", "remote_video_url", "thumb_url"):
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
        return jsonify({"error": "Load scenes and upload media first."}), 400

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
            return jsonify({"error": f"Scene(s) {', '.join(map(str, missing))} are empty. Use Auto-Fill HD Videos or upload media!"}), 400
        if len(ACTIVE_JOBS) >= 1:
            return jsonify({"error": "Server is currently rendering another video. Please wait."}), 429

        job_id = f"nx_{uuid.uuid4().hex[:10]}"
        ACTIVE_JOBS.add(job_id)
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 3,
            "current_scene": "Starting Romance APEX Cinema Render...",
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
