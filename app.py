import os
import re
import gc
import json
import math
import time
import uuid
import wave
import shutil
import asyncio
import urllib.parse
import urllib.request
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
TMP_ROOT = os.path.join(BASE_DIR, "tmp_work")

for d in (OUTPUT_DIR, UPLOAD_DIR, TMP_ROOT):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_JOB_IDS = set()
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,40}$")


def is_safe_id(val: str) -> bool:
    return bool(val and SAFE_ID_RE.match(val))


def purge_old_files(max_age_sec: int = 600):
    now = time.time()
    for folder in (OUTPUT_DIR, UPLOAD_DIR):
        try:
            for fname in os.listdir(folder):
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_sec:
                    os.remove(fpath)
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


def fetch_scene_visual(prompt: str, genre: str, out_path: str, width: int, height: int) -> bool:
    """Rate-limit safe visual fetcher with 3 retries + Wikimedia backup so scenes never stay empty."""
    clean_p = re.sub(r"[^a-zA-Z0-9,\s-]", "", str(prompt or "")).strip()
    if not clean_p:
        return False

    style_suffix = {
        "horror": ", dark eerie horror movie still, volumetric fog, dramatic shadows, 8k photorealistic",
        "romantic": ", warm golden hour romantic movie still, soft bokeh, emotional lighting, 8k photorealistic",
        "documentary": ", ultra detailed IMAX documentary still, dramatic lighting, 8k",
    }.get(genre, ", cinematic lighting, 8k photorealistic")

    # 3 attempts with polite pacing so Pollinations never triggers 429 rate-limit
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2.5 * attempt)
            full_prompt = urllib.parse.quote(clean_p + style_suffix)
            seed = (abs(hash(clean_p)) + attempt * 3137) % 99999
            model_name = "flux" if attempt < 2 else "turbo"
            ai_url = (
                f"https://image.pollinations.ai/prompt/{full_prompt}"
                f"?width={width}&height={height}&seed={seed}&model={model_name}&nologo=true"
            )
            req = urllib.request.Request(ai_url, headers={"User-Agent": "Mozilla/5.0 NEXORAStudio/5.2"})
            with urllib.request.urlopen(req, timeout=28) as resp:
                data = resp.read()
            if len(data) > 12_000:
                with open(out_path, "wb") as f:
                    f.write(data)
                time.sleep(1.5)  # Polite gap before next scene
                return True
        except Exception:
            pass

    # Backup: Wikimedia Commons search using top 3 keywords
    kw = " ".join(clean_p.split()[:4])
    try:
        api_url = (
            "https://commons.wikimedia.org/w/api.php?action=query&generator=search"
            f"&gsrsearch={urllib.parse.quote(kw)}&gsrnamespace=6&gsrlimit=5"
            "&prop=imageinfo&iiprop=url&iiurlwidth=960&format=json"
        )
        req = urllib.request.Request(api_url, headers={"User-Agent": "NEXORAStudio/5.2 (Educational)"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        for _, page in info.get("query", {}).get("pages", {}).items():
            ii = page.get("imageinfo", [{}])[0]
            u = ii.get("thumburl") or ii.get("url", "")
            if u and any(u.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                ireq = urllib.request.Request(u, headers={"User-Agent": "NEXORAStudio/5.2"})
                with urllib.request.urlopen(ireq, timeout=10) as iresp:
                    with open(out_path, "wb") as f:
                        f.write(iresp.read())
                if os.path.exists(out_path) and os.path.getsize(out_path) > 8000:
                    return True
    except Exception:
        pass

    return False


def parse_scenes(topic: str, genre: str, custom_script: str, duration_min: float) -> Tuple[str, List[Dict[str, Any]]]:
    clean_topic = (topic or "").strip() or "Purani Haveli Ka Rahasya"
    parsed: List[Dict[str, Any]] = []

    for line in str(custom_script or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            sp = parts[0].lower()
            if sp not in ("narrator", "male", "female", "child"):
                sp = "narrator"
            parsed.append({
                "speaker": sp,
                "visual_prompt": parts[1],
                "hinglish": parts[2],
                "hindi_dub": parts[3],
            })

    if not parsed:
        parsed = [
            {
                "speaker": "narrator",
                "visual_prompt": "abandoned dark Indian mansion gate at midnight in thick fog",
                "hinglish": "Raat ke 12 baje us purani haveli ka darwaza apne aap khul gaya.",
                "hindi_dub": "रात के ठीक बारह बजे, उस पुरानी हवेली का भारी दरवाज़ा अपने आप खुल गया।",
            }
        ]

    target_sec = max(15.0, min(900.0, float(duration_min) * 60.0))
    base_d = round(target_sec / float(len(parsed)), 2)
    scenes: List[Dict[str, Any]] = []
    for i, sc in enumerate(parsed):
        scenes.append({**sc, "index": i + 1, "duration_sec": max(3.0, base_d)})
    return clean_topic, scenes


class NexoraVoiceAndMusicEngine:
    SPEAKER_PROFILES = {
        "narrator": {"voice": "hi-IN-MadhurNeural", "rate": "-2%", "pitch": "-4Hz"},
        "male": {"voice": "hi-IN-MadhurNeural", "rate": "+3%", "pitch": "+2Hz"},
        "female": {"voice": "hi-IN-SwaraNeural", "rate": "+0%", "pitch": "+1Hz"},
        "child": {"voice": "hi-IN-SwaraNeural", "rate": "+6%", "pitch": "+16Hz"},
    }

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
    def dub_scene_character(cls, sc: Dict[str, Any], idx: int, work_dir: str) -> Tuple[str, float]:
        if not HAS_EDGE_TTS:
            raise RuntimeError("edge-tts is not installed on the server.")

        sp = sc.get("speaker", "narrator").lower()
        prof = cls.SPEAKER_PROFILES.get(sp, cls.SPEAKER_PROFILES["narrator"])
        text = str(sc.get("hindi_dub") or sc.get("hinglish") or "").strip()

        mp3_p = os.path.join(work_dir, f"char_{idx:03d}.mp3")
        wav_p = os.path.join(work_dir, f"char_{idx:03d}.wav")

        async def _synth():
            comm = edge_tts.Communicate(text, voice=prof["voice"], rate=prof["rate"], pitch=prof["pitch"])
            await comm.save(mp3_p)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_synth())
        finally:
            loop.close()

        speech_dur = cls.probe_dur(mp3_p)
        if speech_dur <= 0.25:
            raise RuntimeError(f"Voice generation failed on scene {idx + 1}.")

        final_dur = round(speech_dur + 0.40, 2)
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_p,
                "-af", f"highpass=f=75,equalizer=f=150:t=q:w=1:g=3,acompressor=threshold=-18dB:ratio=3:attack=8:release=110,apad=whole_dur={final_dur:.3f}",
                "-t", f"{final_dur:.3f}",
                "-ar", "22050", "-ac", "1", wav_p,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        if os.path.exists(mp3_p):
            os.remove(mp3_p)
        return wav_p, final_dur

    @staticmethod
    def build_clean_cinema_bgm(total_dur: float, genre: str, out_wav: str):
        rate = 22050
        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            chunk = 25.0
            cur = 0.0
            while cur < total_dur:
                dur = min(chunk, total_dur - cur)
                n = int(round(dur * rate))
                if n <= 0:
                    break
                t = np.linspace(cur, cur + dur, n, endpoint=False, dtype=np.float32)

                if genre == "horror":
                    drone = 0.55 * np.sin(2.0 * np.pi * 41.20 * t) + 0.30 * np.sin(2.0 * np.pi * 61.74 * t)
                    beat1 = np.exp(-((t % 1.6) * 12.0)) * np.sin(2.0 * np.pi * 52.0 * t) * 0.65
                    beat2 = np.exp(-(((t - 0.24) % 1.6) * 14.0)) * np.sin(2.0 * np.pi * 48.0 * t) * 0.45
                    sig = (drone + beat1 + beat2) * 0.14
                elif genre == "romantic":
                    pad = (
                        0.40 * np.sin(2.0 * np.pi * 130.81 * t)
                        + 0.30 * np.sin(2.0 * np.pi * 164.81 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 196.00 * t)
                        + 0.20 * np.sin(2.0 * np.pi * 246.94 * t)
                    )
                    bell = np.exp(-((t % 3.0) * 2.5)) * np.sin(2.0 * np.pi * 523.25 * t) * 0.22
                    sig = (pad + bell) * 0.12
                else:
                    drone = 0.50 * np.sin(2.0 * np.pi * 55.0 * t) + 0.30 * np.sin(2.0 * np.pi * 82.41 * t)
                    pulse = np.exp(-((t % 2.0) * 6.5)) * np.sin(2.0 * np.pi * 440.0 * t) * 0.14
                    sig = (drone + pulse) * 0.12

                wf.writeframes((np.clip(sig, -0.95, 0.95) * 32767.0).astype(np.int16).tobytes())
                cur += dur
                gc.collect()


class NexoraCinemaRenderer:
    def __init__(self, width: int, height: int, genre: str = "horror"):
        self.w = width
        self.h = height
        self.genre = genre
        self.sc = min(width, height) / 540.0

        rng = np.random.default_rng(777)
        self.particles = np.column_stack([
            rng.uniform(0, width, 60),
            rng.uniform(0, height, 60),
            rng.uniform(1.0, 2.8, 60),
            rng.uniform(0.4, 1.2, 60),
        ])

    def _build_scene_fallback(self, sc_idx: int) -> Image.Image:
        """Generates unique per-scene architectural/atmospheric fallback art so two scenes never look identical."""
        yy, xx = np.mgrid[0 : self.h, 0 : self.w]
        nx = (xx - self.w * 0.5) / (self.w * 0.65)
        ny = (yy - self.h * 0.38) / (self.h * 0.65)
        rad = np.clip(1.0 - 0.85 * (nx * nx + ny * ny), 0.0, 1.0).astype(np.float32)

        palettes = [
            ((48, 12, 22), (4, 5, 10)),
            ((14, 24, 46), (3, 5, 10)),
            ((38, 16, 44), (5, 4, 12)),
            ((42, 22, 14), (6, 4, 8)),
        ]
        top, bot = palettes[sc_idx % len(palettes)] if self.genre == "horror" else ((68, 28, 48), (12, 6, 18))

        arr = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        for c in range(3):
            arr[:, :, c] = np.clip(bot[c] + (top[c] - bot[c]) * rad, 0, 255).astype(np.uint8)

        img = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(img)

        variant = sc_idx % 3
        if variant == 0:
            # Mansion arch & glowing windows
            cx = self.w // 2
            draw.rectangle([cx - int(140 * self.sc), int(self.h * 0.25), cx + int(140 * self.sc), self.h], fill=(8, 10, 16))
            for wx in (-70, 0, 70):
                draw.rectangle([cx + int((wx - 18) * self.sc), int(self.h * 0.34), cx + int((wx + 18) * self.sc), int(self.h * 0.48)], fill=(251, 191, 36))
        elif variant == 1:
            # Dark corridor perspective lines + distant doorway light
            cx, cy = self.w // 2, int(self.h * 0.45)
            for corner in [(0, 0), (self.w, 0), (0, self.h), (self.w, self.h)]:
                draw.line([corner, (cx, cy)], fill=(30, 41, 59), width=2)
            draw.rectangle([cx - int(36 * self.sc), cy - int(65 * self.sc), cx + int(36 * self.sc), cy + int(65 * self.sc)], fill=(226, 232, 240))
        else:
            # Moonlit forest silhouettes
            mx, my, mr = int(self.w * 0.5), int(self.h * 0.30), int(56 * self.sc)
            draw.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(226, 232, 240))
            rng = np.random.default_rng(100 + sc_idx)
            for _ in range(24):
                tx = int(rng.uniform(0, self.w))
                tw = int(rng.uniform(12, 28) * self.sc)
                th = int(rng.uniform(self.h * 0.35, self.h * 0.72))
                draw.polygon([(tx, self.h), (tx + tw // 2, self.h - th), (tx + tw, self.h)], fill=(6, 7, 12))

        return img.resize((int(self.w * 1.22), int(self.h * 1.22)), Image.Resampling.BILINEAR)

    def prepare_photo(self, img_path: Optional[str]) -> Optional[Image.Image]:
        if not img_path or not os.path.exists(img_path):
            return None
        try:
            im = Image.open(img_path).convert("RGB")
            tw, th = int(self.w * 1.22), int(self.h * 1.22)
            im = im.resize((tw, th), Image.Resampling.BILINEAR)
            im = ImageEnhance.Contrast(im).enhance(1.14)
            im = ImageEnhance.Color(im).enhance(1.16)
            return im
        except Exception:
            return None

    def _draw_synced_phrase_caption(self, draw: ImageDraw.Draw, text: str, speaker: str, sc_prog: float):
        words = str(text or "").split()
        if not words:
            return

        chunk_size = 5
        chunks = [" ".join(words[i : i + chunk_size]) for i in range(0, len(words), chunk_size)]
        active_idx = min(len(chunks) - 1, int(sc_prog * len(chunks)))
        phrase = chunks[active_idx]

        sp_labels = {
            "narrator": ("KATHAVACHAK", (251, 191, 36)),
            "male": ("MALE VOICE", (56, 189, 248)),
            "female": ("FEMALE VOICE", (244, 114, 182)),
            "child": ("CHILD VOICE", (52, 211, 153)),
        }
        sp_tag, sp_col = sp_labels.get(speaker, ("NARRATOR", (251, 191, 36)))

        f_sub = get_font(max(18, int(24 * self.sc)), bold=True)
        f_tag = get_font(max(10, int(11 * self.sc)), bold=True)

        bb = draw.textbbox((0, 0), phrase, font=f_sub)
        pw = bb[2] - bb[0]
        ph = bb[3] - bb[1]

        box_w = min(int(self.w * 0.92), pw + int(44 * self.sc))
        box_h = ph + int(34 * self.sc)
        bx0 = (self.w - box_w) // 2
        by1 = self.h - int(22 * self.sc)
        by0 = by1 - box_h

        phrase_local_t = (sc_prog * len(chunks)) - active_idx
        pop = int((1.0 - min(1.0, phrase_local_t / 0.15)) * 10 * self.sc)
        by0 += pop
        by1 += pop

        draw.rounded_rectangle([bx0, by0, bx0 + box_w, by1], radius=14, fill=(6, 9, 18), outline=sp_col, width=2)
        draw.text((bx0 + int(14 * self.sc), by0 + int(5 * self.sc)), sp_tag, font=f_tag, fill=sp_col)

        tx = (self.w - pw) // 2
        ty = by0 + int(18 * self.sc)
        draw.text((tx + 2, ty + 2), phrase, font=f_sub, fill=(0, 0, 0))
        draw.text((tx, ty), phrase, font=f_sub, fill=(255, 255, 255))

    def render_frame(
        self,
        scene: Dict[str, Any],
        sc_idx: int,
        sc_prog: float,
        global_t: float,
        ov_prog: float,
        base_img: Image.Image,
    ) -> bytes:
        pw, ph = base_img.size
        e = ease_in_out(sc_prog)
        if sc_idx == 0:
            scale = 1.0 + 0.14 * min(1.0, sc_prog * 1.6)
        elif sc_idx % 2 == 0:
            scale = 1.02 + 0.10 * e
        else:
            scale = 1.12 - 0.10 * e

        crop_w = int(pw / scale)
        crop_h = int(ph / scale)
        max_dx = max(1, pw - crop_w)
        max_dy = max(1, ph - crop_h)

        ox = int(max_dx * (e if sc_idx % 2 == 0 else (1.0 - e)))
        oy = int(max_dy * 0.45)

        if self.genre == "horror" and sc_idx == 0 and sc_prog < 0.25:
            ox = max(0, min(max_dx, ox + int(math.sin(global_t * 38.0) * 6 * self.sc)))

        frame = base_img.crop((ox, oy, ox + crop_w, oy + crop_h)).resize((self.w, self.h), Image.Resampling.BILINEAR)
        draw = ImageDraw.Draw(frame)

        p_col = (244, 63, 94) if self.genre == "horror" else ((253, 224, 71) if self.genre == "romantic" else (186, 230, 253))
        for px0, py0, pr, spd in self.particles:
            px = (px0 + global_t * 16.0 * spd) % self.w
            py = (py0 - global_t * 14.0 * spd) % self.h
            r_px = max(1, int(pr * self.sc * 0.7))
            draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=p_col)

        self._draw_synced_phrase_caption(draw, scene["hinglish"], scene.get("speaker", "narrator"), sc_prog)

        bar_w = int(self.w * ov_prog)
        accent_c = (244, 63, 94) if self.genre == "horror" else ((244, 114, 182) if self.genre == "romantic" else (34, 211, 238))
        draw.rectangle([0, self.h - 4, bar_w, self.h], fill=accent_c)

        return frame.tobytes()


def update_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def run_nexora_job(job_id: str, payload: Dict[str, Any]):
    work_dir = tempfile.mkdtemp(prefix=f"nxr_{job_id}_", dir=TMP_ROOT)
    try:
        purge_old_files(600)
        if not HAS_FFMPEG:
            raise RuntimeError("FFmpeg not installed on server.")

        genre = str(payload.get("genre", "horror")).lower()
        if genre not in ("horror", "romantic", "documentary"):
            genre = "horror"

        topic, scenes = parse_scenes(
            payload.get("topic", ""),
            genre,
            payload.get("custom_script", ""),
            float(payload.get("duration_min", 1.0)),
        )
        aspect = payload.get("aspect_ratio", "16:9")
        out_w, out_h = (540, 960) if aspect == "9:16" else (960, 540)
        fps = 15

        voice_source = str(payload.get("voice_source", "builtin_multi")).lower()
        uploaded_audio_id = str(payload.get("uploaded_audio_id", "")).strip()

        uploaded_audio_path: Optional[str] = None
        if voice_source == "uploaded" and is_safe_id(uploaded_audio_id):
            for fn in os.listdir(UPLOAD_DIR):
                if fn.startswith(uploaded_audio_id):
                    uploaded_audio_path = os.path.join(UPLOAD_DIR, fn)
                    break
            if not uploaded_audio_path:
                raise FileNotFoundError("Uploaded ElevenLabs audio file not found.")

            total_el_dur = NexoraVoiceAndMusicEngine.probe_dur(uploaded_audio_path)
            if total_el_dur <= 1.0:
                raise RuntimeError("Uploaded audio file is invalid or empty.")
            total_words = sum(max(1, len(s["hinglish"].split())) for s in scenes)
            for sc in scenes:
                w_cnt = max(1, len(sc["hinglish"].split()))
                sc["duration_sec"] = round((w_cnt / float(total_words)) * total_el_dur, 2)

        renderer = NexoraCinemaRenderer(out_w, out_h, genre=genre)
        total_sc = len(scenes)
        est_total_dur = sum(float(s["duration_sec"]) for s in scenes)
        global_t = 0.0
        dt = 1.0 / float(fps)

        video_chunks: List[str] = []
        voice_chunks: List[str] = []
        cinema_vf = (
            "eq=contrast=1.10:brightness=-0.02:saturation=1.15,unsharp=3:3:0.5:3:3:0.0,vignette=PI/5"
            if genre == "horror"
            else "eq=contrast=1.06:brightness=0.01:saturation=1.22,unsharp=3:3:0.45:3:3:0.0,vignette=PI/6"
        )

        for idx, sc in enumerate(scenes):
            update_job(
                job_id,
                status="running",
                progress=5 + int((idx / float(total_sc)) * 84),
                current_scene=f"Producing Scene {idx + 1}/{total_sc} ({sc.get('speaker', 'narrator').upper()})...",
            )

            if voice_source == "uploaded":
                actual_dur = float(sc["duration_sec"])
            else:
                dub_wav, actual_dur = NexoraVoiceAndMusicEngine.dub_scene_character(sc, idx, work_dir)
                sc["duration_sec"] = actual_dur
                voice_chunks.append(dub_wav)

            photo_img = None
            if sc.get("visual_prompt"):
                img_file = os.path.join(work_dir, f"vis_{idx:03d}.jpg")
                if fetch_scene_visual(sc["visual_prompt"], genre, img_file, out_w, out_h):
                    photo_img = renderer.prepare_photo(img_file)
                if os.path.exists(img_file):
                    os.remove(img_file)

            base_img = photo_img if photo_img is not None else renderer._build_scene_fallback(idx)
            n_frames = max(1, int(round(actual_dur * fps)))
            v_chunk = os.path.join(work_dir, f"v_{idx:03d}.mp4")

            proc = subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
                    "-vf", cinema_vf,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-pix_fmt", "yuv420p", v_chunk,
                ],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )

            for f_i in range(n_frames):
                raw = renderer.render_frame(
                    sc,
                    idx,
                    f_i / float(max(1, n_frames - 1)),
                    global_t,
                    min(1.0, global_t / max(1.0, est_total_dur)),
                    base_img=base_img,
                )
                proc.stdin.write(raw)
                global_t += dt

            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f"FFmpeg failed on Scene {idx + 1}.")

            video_chunks.append(v_chunk)
            photo_img = None
            base_img = None
            gc.collect()

        update_job(job_id, progress=91, current_scene="Mastering Clean Cinema Audio & Final MP4...")
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
        if voice_source == "uploaded" and uploaded_audio_path:
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

        actual_total_dur = NexoraVoiceAndMusicEngine.probe_dur(joined_v) or global_t
        bgm_wav = os.path.join(work_dir, "clean_bgm.wav")
        NexoraVoiceAndMusicEngine.build_clean_cinema_bgm(actual_total_dur, genre, bgm_wav)

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", topic)[:28].strip("_") or "NEXORA_STORY"
        out_name = f"NEXORA_{genre.upper()}_{slug}_{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", joined_v,
                "-i", master_voice,
                "-i", bgm_wav,
                "-filter_complex", "[1:a]volume=1.25[v];[2:a]volume=0.45[m];[v][m]amix=inputs=2:duration=first[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-shortest", out_path,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

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
        with JOBS_LOCK:
            ACTIVE_JOB_IDS.discard(job_id)
        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()


@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "ffmpeg": HAS_FFMPEG, "multi_voice": HAS_EDGE_TTS})


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


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()
    custom_script = str(data.get("custom_script", "")).strip()
    if len(topic) < 3 and not custom_script:
        return jsonify({"error": "Enter a story/video topic or paste your script."}), 400

    with JOBS_LOCK:
        if len(ACTIVE_JOB_IDS) >= 1:
            return jsonify({"error": "Server is currently rendering another video. Wait a moment."}), 429
        job_id = f"nx_{uuid.uuid4().hex[:10]}"
        ACTIVE_JOB_IDS.add(job_id)
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 2,
            "current_scene": "Initializing NEXORA Multi-Voice Cinema Engine...",
            "error": None,
        }

    threading.Thread(target=run_nexora_job, args=(job_id, data), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job expired or server restarted."}), 404
    return jsonify(job)


@app.route("/api/video/<job_id>")
def api_video(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or not job.get("file_name"):
        return jsonify({"error": "Video not ready."}), 404
    fname = os.path.basename(job["file_name"])
    target = os.path.join(OUTPUT_DIR, fname)
    if not os.path.exists(target):
        return jsonify({"error": "File purged from temporary storage."}), 404
    return send_file(target, as_attachment=(request.args.get("dl") == "1"), download_name=fname, mimetype="video/mp4")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
