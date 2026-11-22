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

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from flask import Flask, request, jsonify, send_file

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

# ==============================================================================
# 1. CONFIGURATION & MEMORY-SAFE WORKSPACE
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
TMP_ROOT = os.path.join(BASE_DIR, "tmp_work")

for d in (OUTPUT_DIR, UPLOAD_DIR, SESSIONS_DIR, TMP_ROOT):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 180 * 1024 * 1024

SESSIONS: Dict[str, Dict[str, Any]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}
LOCK = threading.Lock()
ACTIVE_JOBS = set()
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,40}$")


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


HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None

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
# 2. SCRIPT PARSER + ELEVENLABS CLEAN VOICEOVER GENERATOR
# ==============================================================================
ANIM_MODES = [
    "hook_zoom_in",
    "smooth_pan_right",
    "dramatic_zoom_out",
    "smooth_pan_left",
    "pulse_zoom",
    "diagonal_drift",
]


def detect_scene_sfx(text_blob: str, genre: str) -> str:
    low = text_blob.lower()
    if any(k in low for k in ("thunder", "bijli", "lightning", "toofan", "storm", "barish")):
        return "thunder"
    if any(k in low for k in ("door", "darwaza", "gate", "khula", "band", "kamre")):
        return "door_creak"
    if any(k in low for k in ("funny", "meme", "joke", "hasne", "pagal", "mazak", "comedy")):
        return "meme_boing"
    if any(k in low for k in ("scream", "chilla", "bhoot", "khauf", "dar", "ghost", "blood", "khoon", "raaz", "dhokha")):
        return "horror_sting"
    if any(k in low for k in ("heartbeat", "dil", "saansein", "dhadkan", "kareeb", "pyaar", "aankhon")):
        return "heartbeat"
    if genre == "funny":
        return "meme_pop"
    return "whoosh"


def parse_user_script(custom_script: str, genre: str) -> List[Dict[str, Any]]:
    scenes: List[Dict[str, Any]] = []
    for line in str(custom_script or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            sp = parts[0].lower()
            if sp not in ("narrator", "male", "female", "child"):
                sp = "narrator"
            idx = len(scenes)
            combined_txt = f"{parts[1]} {parts[2]}"
            scenes.append({
                "index": idx,
                "is_teaser": False,
                "speaker": sp,
                "visual_prompt": parts[1],
                "hinglish": parts[2],
                "hindi_dub": parts[3],
                "anim_type": "hook_zoom_in" if idx == 0 else ANIM_MODES[idx % len(ANIM_MODES)],
                "sfx_type": detect_scene_sfx(combined_txt, genre),
                "image_ready": False,
                "image_file": "",
            })
    return scenes


def build_elevenlabs_script(scenes: List[Dict[str, Any]], voice_mode: str) -> str:
    """Generates a clean, ready-to-paste Hindi voiceover script for ElevenLabs."""
    lines: List[str] = []
    for sc in scenes:
        dub = sc.get("hindi_dub") or sc.get("hinglish") or ""
        if voice_mode == "builtin_multi":
            sp = sc.get("speaker", "narrator").upper()
            lines.append(f"[{sp}]: {dub}")
        else:
            lines.append(dub)
    return "\n\n".join(lines)


# ==============================================================================
# 3. CRASH-PROOF VOICE ENGINE (WITH 3X AUTO-RETRY) & PROCEDURAL SFX/BGM
# ==============================================================================
class NexoraSoundAndVoiceEngine:
    MULTI_PROFILES = {
        "narrator": {"voice": "hi-IN-MadhurNeural", "rate": "+12%", "pitch": "-5Hz"},
        "male": {"voice": "hi-IN-MadhurNeural", "rate": "+14%", "pitch": "+1Hz"},
        "female": {"voice": "hi-IN-SwaraNeural", "rate": "+13%", "pitch": "+0Hz"},
        "child": {"voice": "hi-IN-SwaraNeural", "rate": "+15%", "pitch": "+14Hz"},
    }
    SINGLE_STORYTELLER = {"voice": "hi-IN-MadhurNeural", "rate": "+12%", "pitch": "-5Hz"}

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
    def dub_scene_with_retry(cls, sc: Dict[str, Any], idx: int, voice_mode: str, work_dir: str) -> Tuple[str, float]:
        if not HAS_EDGE_TTS:
            raise RuntimeError("edge-tts is not installed on the server.")

        if voice_mode == "single_storyteller":
            prof = cls.SINGLE_STORYTELLER
        else:
            sp = sc.get("speaker", "narrator").lower()
            prof = cls.MULTI_PROFILES.get(sp, cls.MULTI_PROFILES["narrator"])

        text = str(sc.get("hindi_dub") or sc.get("hinglish") or "").strip()
        if not text:
            text = "कहानी आगे बढ़ती है..."

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
                time.sleep(0.7 * (attempt + 1))

        speech_dur = cls.probe_dur(mp3_p)
        if speech_dur <= 0.20:
            raise RuntimeError(f"Voice engine timeout on Scene {idx + 1} ({last_err or 'empty audio'}). Tip: Try ElevenLabs upload or retry render.")

        final_dur = round(speech_dur + 0.12, 2)
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_p,
                "-af", f"highpass=f=75,equalizer=f=135:t=q:w=1:g=3.5,acompressor=threshold=-18dB:ratio=3.0:attack=6:release=95,apad=whole_dur={final_dur:.3f}",
                "-t", f"{final_dur:.3f}",
                "-ar", "22050", "-ac", "1", wav_p,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        if os.path.exists(mp3_p):
            os.remove(mp3_p)
        return wav_p, final_dur

    @staticmethod
    def generate_sfx_wave(sfx_type: str, n_samples: int, rate: int = 22050) -> np.ndarray:
        out = np.zeros(n_samples, dtype=np.float32)
        sfx_len = min(n_samples, int(rate * 1.40))
        if sfx_len <= 10:
            return out
        t = np.linspace(0, sfx_len / float(rate), sfx_len, endpoint=False, dtype=np.float32)
        rng = np.random.default_rng(42)

        if sfx_type == "thunder":
            rumble = rng.normal(0.0, 0.5, sfx_len).astype(np.float32)
            kernel = np.ones(28, dtype=np.float32) / 28.0
            rumble = np.convolve(rumble, kernel, mode="same")
            env = np.exp(-t * 2.2) * (1.0 - np.exp(-t * 40.0))
            out[:sfx_len] = rumble * env * 0.85
        elif sfx_type == "horror_sting":
            f1 = np.sin(2.0 * np.pi * 220.0 * t)
            f2 = np.sin(2.0 * np.pi * 233.08 * t)
            f3 = np.sin(2.0 * np.pi * 622.25 * t)
            env = np.exp(-t * 3.0) * (1.0 - np.exp(-t * 60.0))
            out[:sfx_len] = (f1 + f2 + 0.6 * f3) * env * 0.30
        elif sfx_type == "door_creak":
            freq = 180.0 + 95.0 * np.sin(2.0 * np.pi * 4.5 * t)
            phase = 2.0 * np.pi * np.cumsum(freq) / float(rate)
            env = np.sin(np.pi * (t / (sfx_len / float(rate)))) ** 1.5
            out[:sfx_len] = np.sin(phase) * env * 0.25
        elif sfx_type in ("meme_boing", "meme_pop"):
            freq = 190.0 + 380.0 * (t / (sfx_len / float(rate))) + 70.0 * np.sin(2.0 * np.pi * 18.0 * t)
            phase = 2.0 * np.pi * np.cumsum(freq) / float(rate)
            env = np.exp(-t * 4.8)
            out[:sfx_len] = np.sin(phase) * env * 0.35
        elif sfx_type == "heartbeat":
            b1 = np.exp(-t * 14.0) * np.sin(2.0 * np.pi * 55.0 * t)
            b2 = np.exp(-np.maximum(0.0, t - 0.25) * 14.0) * np.sin(2.0 * np.pi * 50.0 * t) * (t >= 0.25)
            out[:sfx_len] = (b1 + b2) * 0.60
        else:
            noise = rng.normal(0.0, 0.3, sfx_len).astype(np.float32)
            kernel = np.ones(14, dtype=np.float32) / 14.0
            filtered = np.convolve(noise, kernel, mode="same")
            env = np.sin(np.pi * (t / (sfx_len / float(rate)))) ** 2.0
            out[:sfx_len] = filtered * env * 0.48

        return out

    @classmethod
    def build_full_sfx_and_bgm_track(cls, render_scenes: List[Dict[str, Any]], genre: str, out_wav: str):
        rate = 22050
        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            cur_t = 0.0

            for sc in render_scenes:
                dur = float(sc.get("duration_sec", 4.0))
                n = max(1, int(round(dur * rate)))
                t = np.linspace(cur_t, cur_t + dur, n, endpoint=False, dtype=np.float32)
                local_p = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
                swell = 1.0 + 0.35 * (np.exp(-local_p * 5.0) + np.exp(-(1.0 - local_p) * 5.0))

                if genre == "horror":
                    drone = 0.50 * np.sin(2.0 * np.pi * 41.20 * t) + 0.30 * np.sin(2.0 * np.pi * 61.74 * t)
                    bgm = drone * 0.11 * swell
                elif genre == "romantic":
                    pad = (
                        0.40 * np.sin(2.0 * np.pi * 130.81 * t)
                        + 0.30 * np.sin(2.0 * np.pi * 164.81 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 196.00 * t)
                    )
                    bgm = pad * 0.11 * swell
                elif genre == "funny":
                    beat = np.exp(-((t % 0.45) * 10.0)) * np.sin(2.0 * np.pi * 196.0 * t)
                    bgm = beat * 0.12
                else:
                    drone = 0.50 * np.sin(2.0 * np.pi * 55.0 * t) + 0.30 * np.sin(2.0 * np.pi * 82.41 * t)
                    bgm = drone * 0.10 * swell

                sfx = cls.generate_sfx_wave(sc.get("sfx_type", "whoosh"), n, rate=rate)
                mix = bgm + sfx
                wf.writeframes((np.clip(mix, -0.95, 0.95) * 32767.0).astype(np.int16).tobytes())
                cur_t += dur
                gc.collect()


# ==============================================================================
# 4. RETENTION-LOCK VFX RENDERER
# ==============================================================================
class NexoraVfxRenderer:
    POWER_WORDS = {
        "raaz", "dhokha", "pyaar", "achanak", "khatra", "bhoot", "khoon",
        "sach", "ishq", "mohabbat", "intezar", "dard", "aansu", "kasam",
        "secret", "shock", "murder", "mystery", "junoon", "barbaad",
    }

    def __init__(self, width: int, height: int, genre: str = "romantic"):
        self.w = width
        self.h = height
        self.genre = genre
        self.sc = min(width, height) / 540.0

        rng = np.random.default_rng(777)
        self.particles = np.column_stack([
            rng.uniform(0, width, 55),
            rng.uniform(0, height, 55),
            rng.uniform(1.0, 2.6, 55),
            rng.uniform(0.5, 1.4, 55),
        ])

    def load_scene_image(self, img_path: str) -> Image.Image:
        with Image.open(img_path) as raw_im:
            im = raw_im.convert("RGB")
            tw, th = int(self.w * 1.26), int(self.h * 1.26)
            im = im.resize((tw, th), Image.Resampling.BILINEAR)
            im = ImageEnhance.Contrast(im).enhance(1.13)
            im = ImageEnhance.Color(im).enhance(1.16)
            return im

    @staticmethod
    def build_word_timings(text: str) -> List[Tuple[str, float, float]]:
        words = str(text or "").split()
        if not words:
            return []
        weights: List[float] = []
        for w in words:
            base_w = max(2.0, float(len(re.sub(r"[^a-zA-Z0-9]", "", w))))
            if w.endswith((",", ";", ":")):
                base_w += 3.2
            elif w.endswith((".", "!", "?", "।")):
                base_w += 4.5
            weights.append(base_w)

        total_w = sum(weights)
        timings: List[Tuple[str, float, float]] = []
        acc = 0.0
        for w, wt in zip(words, weights):
            st = acc / total_w
            acc += wt
            en = acc / total_w
            timings.append((w, st, en))
        return timings

    def _draw_top_hud(self, draw: ImageDraw.Draw, is_teaser: bool, ov_prog: float):
        f_hud = get_font(max(11, int(12 * self.sc)), bold=True)
        pad_x = int(14 * self.sc)
        pad_y = int(12 * self.sc)

        if is_teaser:
            badge = "🔥 AAGE DEKHIYE... (CLIMAX PREVIEW)"
            bb = draw.textbbox((0, 0), badge, font=f_hud)
            bw = (bb[2] - bb[0]) + int(24 * self.sc)
            bh = (bb[3] - bb[1]) + int(12 * self.sc)
            draw.rounded_rectangle([pad_x, pad_y, pad_x + bw, pad_y + bh], radius=8, fill=(225, 29, 72), outline=(255, 255, 255), width=2)
            draw.text((pad_x + int(12 * self.sc), pad_y + int(5 * self.sc)), badge, font=f_hud, fill=(255, 255, 255))
        else:
            stage = "01. SHURUAT" if ov_prog < 0.34 else ("02. GEHRA RAAZ" if ov_prog < 0.72 else "03. ASLI ANJAAM")
            bb = draw.textbbox((0, 0), stage, font=f_hud)
            bw = (bb[2] - bb[0]) + int(20 * self.sc)
            bh = (bb[3] - bb[1]) + int(10 * self.sc)
            draw.rounded_rectangle([pad_x, pad_y, pad_x + bw, pad_y + bh], radius=8, fill=(8, 13, 26), outline=(34, 211, 238), width=1)
            draw.text((pad_x + int(10 * self.sc), pad_y + int(4 * self.sc)), stage, font=f_hud, fill=(34, 211, 238))

    def _draw_exact_synced_captions(self, draw: ImageDraw.Draw, timings: List[Tuple[str, float, float]], sc_prog: float, voice_mode: str, speaker: str):
        if not timings:
            return

        cur_w_idx = len(timings) - 1
        for i, (_, st, en) in enumerate(timings):
            if sc_prog <= en:
                cur_w_idx = i
                break

        group_size = 5
        grp_idx = cur_w_idx // group_size
        grp_start = grp_idx * group_size
        grp_words = timings[grp_start : grp_start + group_size]

        f_sub = get_font(max(20, int(26 * self.sc)), bold=True)
        f_tag = get_font(max(10, int(11 * self.sc)), bold=True)

        full_grp_str = " ".join(w[0] for w in grp_words)
        bb = draw.textbbox((0, 0), full_grp_str, font=f_sub)
        pw = bb[2] - bb[0]
        ph = bb[3] - bb[1]

        accent = (
            (251, 191, 36) if self.genre == "funny"
            else ((244, 114, 182) if self.genre == "romantic"
                  else ((244, 63, 94) if self.genre == "horror" else (34, 211, 238)))
        )

        show_tag = voice_mode == "builtin_multi"
        box_w = min(int(self.w * 0.92), pw + int(46 * self.sc))
        box_h = ph + (int(34 * self.sc) if show_tag else int(24 * self.sc))
        bx0 = (self.w - box_w) // 2
        by1 = self.h - int(22 * self.sc)
        by0 = by1 - box_h

        draw.rounded_rectangle([bx0, by0, bx0 + box_w, by1], radius=14, fill=(6, 9, 18), outline=accent, width=2)

        if show_tag:
            sp_map = {"narrator": "KATHAVACHAK", "male": "MALE", "female": "FEMALE", "child": "CHILD"}
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
    ) -> bytes:
        pw, ph = base_img.size
        e = ease_in_out(sc_prog)
        is_teaser = bool(scene.get("is_teaser", False))
        anim = "hook_zoom_in" if is_teaser else scene.get("anim_type", "smooth_pan_right")

        float_x = 0.03 * math.sin(global_t * 2.2)
        float_y = 0.03 * math.cos(global_t * 1.8)

        if anim == "hook_zoom_in":
            scale = 1.0 + 0.20 * min(1.0, sc_prog * 1.45)
            pan_x, pan_y = 0.5 + float_x, 0.45 + float_y
        elif anim == "dramatic_zoom_out":
            scale = 1.20 - 0.17 * e
            pan_x, pan_y = 0.5 + float_x, 0.5 + float_y
        elif anim == "smooth_pan_right":
            scale = 1.12
            pan_x, pan_y = e, 0.48 + float_y
        elif anim == "smooth_pan_left":
            scale = 1.12
            pan_x, pan_y = 1.0 - e, 0.48 + float_y
        elif anim == "pulse_zoom":
            scale = 1.08 + 0.05 * math.sin(sc_prog * math.pi * 3.0)
            pan_x, pan_y = 0.5 + 0.15 * math.sin(sc_prog * math.pi), 0.5 + float_y
        else:
            scale = 1.05 + 0.11 * e
            pan_x, pan_y = e, e

        crop_w = int(pw / scale)
        crop_h = int(ph / scale)
        max_dx = max(1, pw - crop_w)
        max_dy = max(1, ph - crop_h)

        ox = int(max_dx * max(0.0, min(1.0, pan_x)))
        oy = int(max_dy * max(0.0, min(1.0, pan_y)))

        if (is_teaser or scene.get("sfx_type") in ("horror_sting", "thunder")) and sc_prog < 0.25:
            ox = max(0, min(max_dx, ox + int(math.sin(global_t * 48.0) * 9 * self.sc)))
            oy = max(0, min(max_dy, oy + int(math.cos(global_t * 42.0) * 6 * self.sc)))

        frame = base_img.crop((ox, oy, ox + crop_w, oy + crop_h)).resize((self.w, self.h), Image.Resampling.BILINEAR)

        if (is_teaser or scene.get("sfx_type") in ("thunder", "horror_sting")) and sc_prog < 0.08:
            flash_strength = 1.0 + 0.55 * (1.0 - sc_prog / 0.08)
            frame = ImageEnhance.Brightness(frame).enhance(flash_strength)
        elif sc_prog < 0.06:
            frame = ImageEnhance.Brightness(frame).enhance(max(0.20, sc_prog / 0.06))

        draw = ImageDraw.Draw(frame)

        if self.genre == "horror":
            for px0, py0, pr, spd in self.particles:
                px = (px0 + global_t * 22.0 * spd) % self.w
                py = (py0 - global_t * 18.0 * spd) % self.h
                r_px = max(1, int(pr * self.sc * 0.75))
                draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=(244, 63, 94))
        elif self.genre == "romantic":
            for px0, py0, pr, spd in self.particles:
                px = (px0 + math.sin(global_t + py0) * 14.0) % self.w
                py = (py0 - global_t * 12.0 * spd) % self.h
                r_px = max(2, int(pr * self.sc * 1.1))
                draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=(253, 224, 71))
        elif self.genre == "funny":
            for px0, py0, pr, spd in self.particles[:25]:
                px = (px0 + global_t * 25.0 * spd) % self.w
                py = (py0 + math.sin(global_t * 4.0 + px0) * 15.0) % self.h
                r_px = max(2, int(pr * self.sc))
                draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=(56, 189, 248))

        self._draw_top_hud(draw, is_teaser, ov_prog)
        self._draw_exact_synced_captions(draw, timings, sc_prog, voice_mode, scene.get("speaker", "narrator"))

        bar_w = int(self.w * ov_prog)
        accent_c = (244, 63, 94) if self.genre == "horror" else ((244, 114, 182) if self.genre == "romantic" else (34, 211, 238))
        draw.rectangle([0, self.h - 4, bar_w, self.h], fill=accent_c)

        return frame.tobytes()


# ==============================================================================
# 5. CRASH-PROOF & MEMORY-SAFE RENDER PIPELINE (HANDLES 50+ SCENES CLEANLY)
# ==============================================================================
def update_job(job_id: str, **kwargs):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def build_render_timeline(scenes: List[Dict[str, Any]], auto_hook: bool, voice_mode: str) -> List[Dict[str, Any]]:
    if not auto_hook or len(scenes) < 4 or voice_mode == "uploaded":
        return [dict(s) for s in scenes]

    climax_idx = min(len(scenes) - 2, max(2, int(len(scenes) * 0.75)))
    hook_sc = dict(scenes[climax_idx])
    hook_sc["is_teaser"] = True
    hook_sc["anim_type"] = "hook_zoom_in"
    hook_sc["sfx_type"] = "heartbeat"

    timeline = [hook_sc]
    for s in scenes:
        timeline.append(dict(s))
    return timeline


def run_final_render(job_id: str, session_id: str):
    work_dir = tempfile.mkdtemp(prefix=f"nxr_{job_id}_", dir=TMP_ROOT)
    try:
        purge_expired(1800)
        if not HAS_FFMPEG:
            raise RuntimeError("FFmpeg not available on server.")

        with LOCK:
            sess = SESSIONS.get(session_id)
        if not sess:
            raise RuntimeError("Session expired. Please load scenes again.")

        raw_scenes = sess["scenes"]
        sess_dir = os.path.join(SESSIONS_DIR, session_id)
        for sc in raw_scenes:
            img_p = os.path.join(sess_dir, sc.get("image_file", ""))
            if not sc.get("image_ready") or not os.path.exists(img_p):
                raise RuntimeError(f"Scene {sc['index'] + 1} is missing an image. Upload it first!")

        genre = sess["genre"]
        topic = sess["topic"]
        aspect = sess["aspect_ratio"]
        voice_mode = sess["voice_mode"]
        auto_hook = bool(sess.get("auto_hook", True))
        uploaded_audio_id = sess.get("uploaded_audio_id", "")

        render_scenes = build_render_timeline(raw_scenes, auto_hook, voice_mode)

        out_w, out_h = (540, 960) if aspect == "9:16" else (960, 540)
        # 14 FPS cuts RAM & CPU usage by 35% while maintaining ultra-smooth motion
        fps = 14

        uploaded_audio_path: Optional[str] = None
        if voice_mode == "uploaded":
            if not is_safe_id(uploaded_audio_id):
                raise RuntimeError("Please upload your ElevenLabs audio file first.")
            for fn in os.listdir(UPLOAD_DIR):
                if fn.startswith(uploaded_audio_id):
                    uploaded_audio_path = os.path.join(UPLOAD_DIR, fn)
                    break
            if not uploaded_audio_path:
                raise FileNotFoundError("Uploaded ElevenLabs audio file not found.")

            total_el_dur = NexoraSoundAndVoiceEngine.probe_dur(uploaded_audio_path)
            if total_el_dur <= 1.0:
                raise RuntimeError("Uploaded audio file is invalid or empty.")
            total_weights = sum(max(1, len(s["hinglish"])) for s in render_scenes)
            for sc in render_scenes:
                w_cnt = max(1, len(sc["hinglish"]))
                sc["duration_sec"] = round((w_cnt / float(total_weights)) * total_el_dur, 2)

        renderer = NexoraVfxRenderer(out_w, out_h, genre=genre)
        total_sc = len(render_scenes)
        est_total_dur = sum(float(s.get("duration_sec", 4.0)) for s in render_scenes)
        global_t = 0.0
        dt = 1.0 / float(fps)

        video_chunks: List[str] = []
        voice_chunks: List[str] = []
        cinema_vf = (
            "eq=contrast=1.10:brightness=-0.02:saturation=1.15,unsharp=3:3:0.5:3:3:0.0,vignette=PI/5"
            if genre == "horror"
            else "eq=contrast=1.06:brightness=0.01:saturation=1.22,unsharp=3:3:0.45:3:3:0.0,vignette=PI/6"
        )

        for idx, sc in enumerate(render_scenes):
            lbl = "Auto-Hook Teaser" if sc.get("is_teaser") else f"Scene {idx + 1}/{total_sc}"
            update_job(
                job_id,
                status="running",
                progress=4 + int((idx / float(total_sc)) * 85),
                current_scene=f"Rendering {lbl} ({sc['anim_type']})...",
            )

            if voice_mode == "uploaded":
                actual_dur = float(sc["duration_sec"])
            else:
                dub_wav, actual_dur = NexoraSoundAndVoiceEngine.dub_scene_with_retry(sc, idx, voice_mode, work_dir)
                sc["duration_sec"] = actual_dur
                voice_chunks.append(dub_wav)

            timings = renderer.build_word_timings(sc["hinglish"])
            img_path = os.path.join(sess_dir, sc["image_file"])
            base_img = renderer.load_scene_image(img_path)

            n_frames = max(1, int(round(actual_dur * fps)))
            v_chunk = os.path.join(work_dir, f"v_{idx:03d}.mp4")

            proc = subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
                    "-vf", cinema_vf,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-threads", "2",
                    "-pix_fmt", "yuv420p", v_chunk,
                ],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )

            for f_i in range(n_frames):
                raw = renderer.render_frame(
                    sc,
                    timings,
                    idx,
                    f_i / float(max(1, n_frames - 1)),
                    global_t,
                    min(1.0, global_t / max(1.0, est_total_dur)),
                    voice_mode,
                    base_img=base_img,
                )
                proc.stdin.write(raw)
                global_t += dt

            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f"FFmpeg encoding error on {lbl}.")

            video_chunks.append(v_chunk)
            base_img.close()
            base_img = None
            gc.collect()

        update_job(job_id, progress=91, current_scene="Joining Scenes, Mixing SFX & Finalizing MP4...")
        v_list = os.path.join(work_dir, "v_list.txt")
        with open(v_list, "w", encoding="utf-8") as vf:
            for vc in video_chunks:
                vf.write(f"file '{vc}'\n")

        joined_v = os.path.join(work_dir, "joined_v.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", v_list, "-c", "copy", joined_v],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        master_voice = os.path.join(work_dir, "master_voice.wav")
        if voice_mode == "uploaded" and uploaded_audio_path:
            subprocess.run(
                ["ffmpeg", "-y", "-i", uploaded_audio_path, "-ar", "22050", "-ac", "1", master_voice],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
        else:
            a_list = os.path.join(work_dir, "a_list.txt")
            with open(a_list, "w", encoding="utf-8") as af:
                for ac in voice_chunks:
                    af.write(f"file '{ac}'\n")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", a_list, "-c", "copy", master_voice],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )

        sfx_bgm_wav = os.path.join(work_dir, "sfx_bgm.wav")
        NexoraSoundAndVoiceEngine.build_full_sfx_and_bgm_track(render_scenes, genre, sfx_bgm_wav)

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", topic)[:28].strip("_") or "NEXORA_V9"
        out_name = f"NEXORA_{genre.upper()}_{slug}_{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", joined_v,
                "-i", master_voice,
                "-i", sfx_bgm_wav,
                "-filter_complex", "[1:a]volume=1.32[v];[2:a]volume=0.55[m];[v][m]amix=inputs=2:duration=first[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-shortest", out_path,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        actual_total_dur = NexoraSoundAndVoiceEngine.probe_dur(out_path) or global_t
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
# 6. REST API ENDPOINTS (BATCH SORTER + SCENE SWAPPER + ELEVENLABS SCRIPT)
# ==============================================================================
@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "ffmpeg": HAS_FFMPEG, "voice_ready": HAS_EDGE_TTS})


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


@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    data = request.get_json(silent=True) or {}
    genre = str(data.get("genre", "romantic")).lower()
    voice_mode = str(data.get("voice_mode", "single_storyteller"))
    scenes = parse_user_script(str(data.get("custom_script", "")), genre)
    if not scenes:
        return jsonify({"error": "Paste a valid 4-column script first (Speaker | Image Prompt | Hinglish | Hindi)."}), 400

    session_id = f"ses_{uuid.uuid4().hex[:10]}"
    os.makedirs(os.path.join(SESSIONS_DIR, session_id), exist_ok=True)
    el_script = build_elevenlabs_script(scenes, voice_mode)

    with LOCK:
        SESSIONS[session_id] = {
            "session_id": session_id,
            "topic": str(data.get("topic", "NEXORA Story")).strip() or "NEXORA Story",
            "genre": genre,
            "aspect_ratio": str(data.get("aspect_ratio", "16:9")),
            "voice_mode": voice_mode,
            "auto_hook": bool(data.get("auto_hook", True)),
            "uploaded_audio_id": str(data.get("uploaded_audio_id", "")),
            "elevenlabs_script": el_script,
            "scenes": scenes,
        }

    return jsonify({"ok": True, "session_id": session_id, "elevenlabs_script": el_script})


@app.route("/api/session/<session_id>")
def api_session(session_id: str):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session ID."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess:
        return jsonify({"error": "Session expired."}), 404
    return jsonify(sess)


@app.route("/api/session/<session_id>/image/<int:idx>")
def api_session_image(session_id: str, idx: int):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess or idx < 0 or idx >= len(sess["scenes"]):
        return jsonify({"error": "Not found."}), 404
    sc = sess["scenes"][idx]
    fpath = os.path.join(SESSIONS_DIR, session_id, sc.get("image_file", ""))
    if not sc.get("image_ready") or not os.path.exists(fpath):
        return jsonify({"error": "Image missing."}), 404
    return send_file(fpath)


@app.route("/api/session/<session_id>/replace/<int:idx>", methods=["POST"])
def api_replace_image(session_id: str, idx: int):
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess or idx < 0 or idx >= len(sess["scenes"]):
        return jsonify({"error": "Scene not found."}), 404

    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"error": "Select an image file first."}), 400

    sess_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(sess_dir, exist_ok=True)
    fname = f"img_{idx:03d}_{uuid.uuid4().hex[:6]}.jpg"
    fpath = os.path.join(sess_dir, fname)

    with Image.open(f.stream) as raw_im:
        im = raw_im.convert("RGB")
        im.thumbnail((1280, 1280), Image.Resampling.BILINEAR)
        im.save(fpath, format="JPEG", quality=88)

    with LOCK:
        sess["scenes"][idx]["image_ready"] = True
        sess["scenes"][idx]["image_file"] = fname
    return jsonify({"ok": True})


@app.route("/api/session/<session_id>/batch_upload", methods=["POST"])
def api_batch_upload(session_id: str):
    """Smartly detects scene numbers from filenames (e.g., scene_12.jpg, 12.png) or assigns sequentially."""
    if not is_safe_id(session_id):
        return jsonify({"error": "Invalid session."}), 400
    with LOCK:
        sess = SESSIONS.get(session_id)
    if not sess:
        return jsonify({"error": "Session expired."}), 404

    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "No images selected."}), 400

    sess_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(sess_dir, exist_ok=True)
    total_scenes = len(sess["scenes"])

    # Check if filenames explicitly carry 1-based scene numbers like "1.jpg", "scene_15.png"
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

    # Sort remaining files naturally
    unmapped_files.sort(key=lambda x: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", x.filename or "")])

    u_ptr = 0
    for idx in range(total_scenes):
        target_f = explicit_map.get(idx)
        if target_f is None and u_ptr < len(unmapped_files):
            target_f = unmapped_files[u_ptr]
            u_ptr += 1
        if target_f is not None:
            fname = f"img_{idx:03d}_{uuid.uuid4().hex[:6]}.jpg"
            fpath = os.path.join(sess_dir, fname)
            with Image.open(target_f.stream) as raw_im:
                im = raw_im.convert("RGB")
                im.thumbnail((1280, 1280), Image.Resampling.BILINEAR)
                im.save(fpath, format="JPEG", quality=88)
            with LOCK:
                sess["scenes"][idx]["image_ready"] = True
                sess["scenes"][idx]["image_file"] = fname

    return jsonify({"ok": True})


@app.route("/api/session/<session_id>/swap", methods=["POST"])
def api_swap_scenes(session_id: str):
    """Lets you swap images between any two scenes in 1 click if order needs adjustment."""
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
        sa["image_ready"], sb["image_ready"] = sb["image_ready"], sa["image_ready"]
        sa["image_file"], sb["image_file"] = sb["image_file"], sa["image_file"]
    return jsonify({"ok": True})


@app.route("/api/render", methods=["POST"])
def api_render():
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id", "")).strip()
    if not is_safe_id(session_id):
        return jsonify({"error": "Load scenes and upload images first."}), 400

    with LOCK:
        sess = SESSIONS.get(session_id)
        if not sess:
            return jsonify({"error": "Session expired."}), 404
        missing = [s["index"] + 1 for s in sess["scenes"] if not s.get("image_ready")]
        if missing:
            return jsonify({"error": f"Upload images for Scene(s): {', '.join(map(str, missing))} before rendering!"}), 400
        if len(ACTIVE_JOBS) >= 1:
            return jsonify({"error": "Server is currently rendering another video. Please wait."}), 429

        job_id = f"nx_{uuid.uuid4().hex[:10]}"
        ACTIVE_JOBS.add(job_id)
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 2,
            "current_scene": "Starting Crash-Proof Cinema Render...",
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
