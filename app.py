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
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
from flask import Flask, request, jsonify, send_file

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

# ==============================================================================
# 1. CONFIGURATION & ZERO-BLOAT STORAGE
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
TMP_ROOT = os.path.join(BASE_DIR, "tmp_work")

for d in (OUTPUT_DIR, TMP_ROOT):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_JOB_IDS = set()
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,40}$")


def is_safe_id(val: str) -> bool:
    return bool(val and SAFE_ID_RE.match(val))


def purge_old_files(max_age_sec: int = 600):
    now = time.time()
    try:
        for fname in os.listdir(OUTPUT_DIR):
            fpath = os.path.join(OUTPUT_DIR, fname)
            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_sec:
                os.remove(fpath)
    except Exception:
        pass


HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None


# ==============================================================================
# 2. TYPOGRAPHY & MATH HELPERS
# ==============================================================================
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
# 3. 3-TIER VISUAL ENGINE (AI ART -> WIKIMEDIA -> PROCEDURAL GENRE CINEMA ART)
# ==============================================================================
def fetch_scene_visual(prompt: str, genre: str, out_path: str, width: int, height: int) -> bool:
    """Fetches bespoke cinematic scene artwork with retries and Wikimedia fallback."""
    clean_p = re.sub(r"[^a-zA-Z0-9,\s-]", "", str(prompt or "")).strip()
    if not clean_p:
        return False

    style_suffix = {
        "horror": ", dark eerie horror movie still, volumetric fog, cinematic rim lighting, 8k photorealistic",
        "romantic": ", warm golden hour romantic movie still, bokeh lights, emotional atmosphere, 8k photorealistic",
        "documentary": ", ultra detailed IMAX documentary shot, dramatic lighting, 8k",
    }.get(genre, ", cinematic lighting, 8k photorealistic")

    # Tier 1: Pollinations AI (with 2 attempts so network hiccups don't skip scenes)
    for attempt in range(2):
        try:
            full_prompt = urllib.parse.quote(clean_p + style_suffix)
            seed = (abs(hash(clean_p)) + attempt * 1337) % 99999
            ai_url = (
                f"https://image.pollinations.ai/prompt/{full_prompt}"
                f"?width={width}&height={height}&seed={seed}&model=flux&nologo=true"
            )
            req = urllib.request.Request(ai_url, headers={"User-Agent": "Mozilla/5.0 NEXORAStudio/4.0"})
            with urllib.request.urlopen(req, timeout=22) as resp:
                data = resp.read()
            if len(data) > 12_000:
                with open(out_path, "wb") as f:
                    f.write(data)
                return True
        except Exception:
            time.sleep(0.6)

    # Tier 2: Wikimedia Commons public-domain photography
    try:
        api_url = (
            "https://commons.wikimedia.org/w/api.php?action=query&generator=search"
            f"&gsrsearch={urllib.parse.quote(clean_p)}&gsrnamespace=6&gsrlimit=4"
            "&prop=imageinfo&iiprop=url&iiurlwidth=960&format=json"
        )
        req = urllib.request.Request(api_url, headers={"User-Agent": "NEXORAStudio/4.0 (Educational)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        for _, page in info.get("query", {}).get("pages", {}).items():
            ii = page.get("imageinfo", [{}])[0]
            u = ii.get("thumburl") or ii.get("url", "")
            if u and any(u.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                ireq = urllib.request.Request(u, headers={"User-Agent": "NEXORAStudio/4.0"})
                with urllib.request.urlopen(ireq, timeout=10) as iresp:
                    with open(out_path, "wb") as f:
                        f.write(iresp.read())
                if os.path.exists(out_path) and os.path.getsize(out_path) > 8000:
                    return True
    except Exception:
        pass

    return False


# ==============================================================================
# 4. SCRIPT ENGINE (HINDI DUB + HINGLISH CAPTIONS)
# ==============================================================================
BUILTIN_TEMPLATES: Dict[str, List[Dict[str, Any]]] = {
    "horror": [
        {
            "mode": "photo",
            "visual_prompt": "abandoned dark railway station in dense midnight fog glowing vintage lamp",
            "mood": "intense",
            "hinglish": "Raat ke 1 baje us sunsan railway station par aakhiri train rukne wali thi...",
            "hindi_dub": "रात के ठीक एक बजे, उस सुनसान और घने कोहरे से ढके रेलवे स्टेशन पर आखिरी ट्रेन रुकने वाली थी। चारों तरफ़ ऐसी खामोशी थी, कि हवा की सरसराहट भी किसी की फुसफुसाहट जैसी लग रही थी।",
        },
        {
            "mode": "photo",
            "visual_prompt": "lonely young man walking on dark misty train platform looking back in fear",
            "mood": "dramatic",
            "hinglish": "Jab Rohan platform par utra, to wahan ek bhi insaan maujood nahi tha!",
            "hindi_dub": "जब रोहन अपने भारी बैग के साथ प्लेटफॉर्म पर उतरा, तो उसने देखा कि पूरे स्टेशन पर एक भी इंसान मौजूद नहीं था। सिवाय एक पुराने बेंच पर बैठी, सफेद साड़ी वाली औरत के।",
        },
        {
            "mode": "photo",
            "visual_prompt": "mysterious shadowy woman silhouette standing under flickering street light in fog",
            "mood": "intense",
            "hinglish": "Us aurat ne bina mude kaha: Tumhari train to 10 saal pehle hi hadse ka shikar ho gayi thi!",
            "hindi_dub": "रोहन जैसे ही रास्ता पूछने के लिए उसके करीब गया, उस औरत ने बिना पीछे मुड़े धीमी आवाज़ में कहा। तुम जिस ट्रेन से आए हो, वह तो दस साल पहले ही हादसे का शिकार हो गई थी।",
        },
        {
            "mode": "photo",
            "visual_prompt": "eerie foggy forest road at dawn with mysterious mist fading away",
            "mood": "calm",
            "hinglish": "Subah hone par Rohan zinda to bach gaya, par us station ka raaz aaj bhi ankaha hai.",
            "hindi_dub": "सुबह की पहली किरण के साथ रोहन किसी तरह मुख्य सड़क तक तो पहुंच गया। लेकिन उस भूतिया स्टेशन और उस रात की सच्चाई, आज भी एक अनसुलझा रहस्य बनी हुई है।",
        },
    ],
    "romantic": [
        {
            "mode": "photo",
            "visual_prompt": "two lovers standing under umbrella on rainy Mumbai street warm streetlamps",
            "mood": "calm",
            "hinglish": "Baarish ki us pehli shaam mein unki nazrein mili aur waqt wahin tham gaya.",
            "hindi_dub": "बारिश की उस पहली शाम में, जब शहर की भागदौड़ थम सी गई थी। तब एक ही छतरी के नीचे उनकी नज़रें मिलीं, और मानो वक्त वहीं ठहर गया।",
        },
        {
            "mode": "photo",
            "visual_prompt": "emotional couple saying goodbye at misty train station sunset",
            "mood": "dramatic",
            "hinglish": "Kismat ne unhe alag shehron mein bhej diya, par unke khaton mein pyaar zinda raha.",
            "hindi_dub": "किस्मत के फैसलों ने उन्हें दो अलग शहरों में दूर कर दिया। लेकिन हर शाम ढलने के साथ, उनकी यादें और अनकहे जज़्बात और भी गहरे होते चले गए।",
        },
        {
            "mode": "photo",
            "visual_prompt": "reunion of couple at golden sunset beach silhouettes emotional",
            "mood": "calm",
            "hinglish": "Saalon baad jab woh phir mile, to lafzon ki zaroorat hi nahi padi.",
            "hindi_dub": "सालों के लंबे इंतज़ार के बाद, जब समंदर के किनारे सूरज की सुनहरी रोशनी में वे दोनों फिर से आमने-सामने आए। तो कुछ कहने के लिए, लफ़्ज़ों की ज़रूरत ही नहीं पड़ी।",
        },
    ],
    "documentary": [
        {
            "mode": "solar_system",
            "visual_prompt": "supermassive black hole glowing accretion disk deep space",
            "mood": "intense",
            "hinglish": "Agar Suraj ki jagah ek Black Hole aa jaye to kya Prithvi usmein sama jayegi?",
            "hindi_dub": "दोस्तों, क्या आपने कभी सोचा है कि अगर हमारे सूरज को उतने ही वजन वाले एक ब्लैक होल से बदल दिया जाए, तो क्या हमारी पृथ्वी तुरंत उसके अंदर समा जाएगी?",
        },
        {
            "mode": "dual_reality",
            "visual_prompt": "Earth orbit physics visualization space",
            "mood": "dramatic",
            "hinglish": "Gravity bilkul same rahegi, isliye Prithvi apne orbit mein surakshit ghumti rahegi.",
            "hindi_dub": "हैरानी की बात यह है कि गुरुत्वाकर्षण के नियमों के अनुसार, अगर ब्लैक होल का वजन सूरज के बराबर ही है, तो पृथ्वी की कक्षा में कोई बदलाव नहीं होगा।",
        },
        {
            "mode": "earth_rotation",
            "visual_prompt": "frozen dark Earth in deep space",
            "mood": "intense",
            "hinglish": "8 minute 20 second baad ghup andhera chha jayega aur samandar jam jayenge.",
            "hindi_dub": "लेकिन असली तबाही ठीक आठ मिनट और बीस सेकंड बाद शुरू होगी, जब सूरज की आखिरी किरण धरती पर पहुंचेगी और पूरी पृथ्वी पर घना अंधेरा छा जाएगा।",
        },
    ],
}


def parse_or_build_scenes(topic: str, genre: str, custom_script: str, duration_min: float) -> Tuple[str, List[Dict[str, Any]]]:
    clean_topic = (topic or "").strip() or "Raat Ke 1 Baje Aakhiri Train"
    parsed: List[Dict[str, Any]] = []

    for line in str(custom_script or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 5:
            mode = parts[0].lower()
            if genre in ("horror", "romantic"):
                mode = "photo"  # Never inject space planets into Horror or Romantic stories!
            elif mode not in ("photo", "solar_system", "dual_reality", "earth_rotation"):
                mode = "photo"
            mood = parts[2].lower() if parts[2].lower() in ("intense", "dramatic", "calm") else "dramatic"
            parsed.append({
                "mode": mode,
                "visual_prompt": parts[1],
                "mood": mood,
                "hinglish": parts[3],
                "hindi_dub": parts[4],
            })

    if not parsed:
        tpl = BUILTIN_TEMPLATES.get(genre, BUILTIN_TEMPLATES["horror"])
        parsed = [dict(x) for x in tpl]

    target_sec = max(20.0, min(900.0, float(duration_min) * 60.0))
    base_d = round(target_sec / float(len(parsed)), 2)
    scenes: List[Dict[str, Any]] = []
    for i, sc in enumerate(parsed):
        scenes.append({
            **sc,
            "index": i + 1,
            "duration_sec": max(5.0, base_d),
        })
    return clean_topic, scenes


# ==============================================================================
# 5. SENTENCE-BY-SENTENCE EXPRESSIVE HINDI DUBBING + CINEMA SOUND DESIGN
# ==============================================================================
class NexoraAudioEngine:
    VOICE = "hi-IN-MadhurNeural"

    MOOD_BASE = {
        "intense": {"rate": "+4%", "pitch": "-4Hz"},
        "dramatic": {"rate": "-2%", "pitch": "-6Hz"},
        "calm": {"rate": "-7%", "pitch": "-3Hz"},
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
    def dub_scene_expressive(cls, sc: Dict[str, Any], idx: int, work_dir: str) -> Tuple[str, float]:
        """Splits Hindi narration into sentences and inserts natural dramatic pauses between them."""
        if not HAS_EDGE_TTS:
            raise RuntimeError("edge-tts is not installed on the server.")

        mood_cfg = cls.MOOD_BASE.get(sc.get("mood", "dramatic"), cls.MOOD_BASE["dramatic"])
        full_text = str(sc.get("hindi_dub") or sc.get("hinglish") or "").strip()

        # Split by Hindi danda (।), question mark, exclamation, or period for natural breathing pauses
        raw_sentences = [s.strip() for s in re.split(r"(?<=[।?!.])\s+", full_text) if s.strip()]
        if not raw_sentences:
            raw_sentences = [full_text]

        async def _synth_sentences():
            out_files = []
            for s_i, sent in enumerate(raw_sentences):
                mp3_p = os.path.join(work_dir, f"dub_{idx:03d}_{s_i}.mp3")
                # Slight dynamic variation on final punchline sentence
                rate = mood_cfg["rate"]
                pitch = "-7Hz" if (s_i == len(raw_sentences) - 1 and sc.get("mood") == "intense") else mood_cfg["pitch"]
                comm = edge_tts.Communicate(sent, voice=cls.VOICE, rate=rate, pitch=pitch)
                await comm.save(mp3_p)
                out_files.append(mp3_p)
            return out_files

        loop = asyncio.new_event_loop()
        try:
            mp3_parts = loop.run_until_complete(_synth_sentences())
        finally:
            loop.close()

        # Build silence gap file (0.38s dramatic breath between sentences)
        gap_wav = os.path.join(work_dir, f"gap_{idx:03d}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "0.38", gap_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        wav_parts: List[str] = []
        for s_i, mp3_p in enumerate(mp3_parts):
            w_p = os.path.join(work_dir, f"w_{idx:03d}_{s_i}.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", mp3_p, "-ar", "22050", "-ac", "1", w_p],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            if os.path.exists(w_p) and os.path.getsize(w_p) > 200:
                wav_parts.append(w_p)
                if s_i < len(mp3_parts) - 1:
                    wav_parts.append(gap_wav)
            if os.path.exists(mp3_p):
                os.remove(mp3_p)

        if not wav_parts:
            raise RuntimeError(f"Hindi dubbing failed on scene {idx + 1}.")

        list_txt = os.path.join(work_dir, f"slist_{idx:03d}.txt")
        with open(list_txt, "w", encoding="utf-8") as fh:
            for wp in wav_parts:
                fh.write(f"file '{wp}'\n")

        raw_joined = os.path.join(work_dir, f"raw_dub_{idx:03d}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt, "-c", "copy", raw_joined],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        speech_dur = cls.probe_dur(raw_joined)
        final_dur = round(max(float(sc["duration_sec"]), speech_dur + 0.75), 2)
        final_wav = os.path.join(work_dir, f"dub_final_{idx:03d}.wav")

        # Studio warm voice EQ + compression + trailing pause
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", raw_joined,
                "-af", f"equalizer=f=140:t=q:w=1:g=3,acompressor=threshold=-18dB:ratio=3.2:attack=8:release=120,apad=whole_dur={final_dur:.3f}",
                "-t", f"{final_dur:.3f}",
                "-ar", "22050", "-ac", "1", final_wav,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        for wp in set(wav_parts + [raw_joined]):
            if os.path.exists(wp):
                os.remove(wp)

        return final_wav, final_dur

    @staticmethod
    def build_genre_soundscape(total_dur: float, genre: str, out_wav: str):
        """Generates rich, audible cinema score + atmospheric ambience (rain/wind/sub-bass)."""
        rate = 22050
        rng = np.random.default_rng(108)
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
                    # Dark eerie drone + slow heartbeat + nocturnal wind hiss
                    drone = 0.50 * np.sin(2.0 * np.pi * 41.20 * t) + 0.35 * np.sin(2.0 * np.pi * 43.65 * t)
                    hb = np.exp(-((t % 1.35) * 8.5)) * np.sin(2.0 * np.pi * 55.0 * t) * 0.55
                    wind = rng.normal(0.0, 0.08, n).astype(np.float32) * (0.5 + 0.5 * np.sin(2.0 * np.pi * 0.15 * t))
                    sig = (drone + hb + wind) * 0.16
                elif genre == "romantic":
                    # Warm major-7th piano pad + gentle rain ambience + soft chime
                    pad = (
                        0.40 * np.sin(2.0 * np.pi * 130.81 * t)
                        + 0.30 * np.sin(2.0 * np.pi * 164.81 * t)
                        + 0.25 * np.sin(2.0 * np.pi * 196.00 * t)
                        + 0.22 * np.sin(2.0 * np.pi * 246.94 * t)
                    )
                    chime = np.exp(-((t % 2.8) * 2.4)) * np.sin(2.0 * np.pi * 523.25 * t) * 0.22
                    rain = rng.normal(0.0, 0.05, n).astype(np.float32)
                    sig = (pad + chime + rain) * 0.13
                else:
                    drone = 0.50 * np.sin(2.0 * np.pi * 55.0 * t) + 0.32 * np.sin(2.0 * np.pi * 82.41 * t)
                    pulse = np.exp(-((t % 2.0) * 6.0)) * np.sin(2.0 * np.pi * 440.0 * t) * 0.15
                    sig = (drone + pulse) * 0.13

                wf.writeframes((np.clip(sig, -0.95, 0.95) * 32767.0).astype(np.int16).tobytes())
                cur += dur
                gc.collect()


# ==============================================================================
# 6. FULL-SCREEN CINEMA RENDERER (WORD-BY-WORD KARAOKE CAPTIONS + GENRE ART)
# ==============================================================================
class NexoraCinemaRenderer:
    def __init__(self, width: int, height: int, genre: str = "horror"):
        self.w = width
        self.h = height
        self.genre = genre
        self.sc = min(width, height) / 540.0

        rng = np.random.default_rng(777)
        self.particles = np.column_stack([
            rng.uniform(0, width, 75),
            rng.uniform(0, height, 75),
            rng.uniform(1.0, 3.2, 75),
            rng.uniform(0.4, 1.3, 75),
        ])
        self._genre_artwork_cache = self._build_procedural_genre_art()
        self._sphere_cache: Dict[Tuple[int, bool], Image.Image] = {}

    def _build_procedural_genre_art(self) -> Image.Image:
        """Creates bespoke atmospheric story artwork when offline (NEVER shows space planets in stories)."""
        yy, xx = np.mgrid[0 : self.h, 0 : self.w]
        nx = (xx - self.w * 0.5) / (self.w * 0.65)
        ny = (yy - self.h * 0.38) / (self.h * 0.65)
        rad = np.clip(1.0 - 0.85 * (nx * nx + ny * ny), 0.0, 1.0).astype(np.float32)

        if self.genre == "horror":
            top, bot = (48, 12, 22), (4, 5, 10)
        elif self.genre == "romantic":
            top, bot = (68, 28, 48), (12, 6, 18)
        else:
            top, bot = (12, 26, 56), (3, 5, 13)

        arr = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        for c in range(3):
            arr[:, :, c] = np.clip(bot[c] + (top[c] - bot[c]) * rad, 0, 255).astype(np.uint8)

        img = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(img)

        if self.genre == "horror":
            # Full eerie moon + misty forest silhouettes
            mx, my, mr = int(self.w * 0.5), int(self.h * 0.32), int(64 * self.sc)
            draw.ellipse([mx - mr - 14, my - mr - 14, mx + mr + 14, my + mr + 14], fill=(95, 22, 36))
            draw.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(226, 232, 240))
            rng = np.random.default_rng(99)
            for _ in range(28):
                tx = int(rng.uniform(0, self.w))
                tw = int(rng.uniform(10, 26) * self.sc)
                th = int(rng.uniform(self.h * 0.35, self.h * 0.72))
                draw.polygon([(tx, self.h), (tx + tw // 2, self.h - th), (tx + tw, self.h)], fill=(6, 7, 12))
        elif self.genre == "romantic":
            # Glowing sunset orb + golden bokeh circles
            sx, sy, sr = int(self.w * 0.5), int(self.h * 0.36), int(78 * self.sc)
            draw.ellipse([sx - sr - 20, sy - sr - 20, sx + sr + 20, sy + sr + 20], fill=(135, 58, 72))
            draw.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(253, 186, 116))
            draw.rectangle([0, int(self.h * 0.62), self.w, self.h], fill=(18, 10, 24))

        return img

    def _get_3d_sphere(self, radius: int, perturbed: bool) -> Image.Image:
        key = (radius, perturbed)
        if key in self._sphere_cache:
            return self._sphere_cache[key]
        pad = int(radius * 0.25)
        size = (radius + pad) * 2
        y, x = np.mgrid[-radius - pad : radius + pad, -radius - pad : radius + pad]
        dist = np.sqrt((x * x + y * y).astype(np.float32)) / float(radius)

        rgba = np.zeros((size, size, 4), dtype=np.uint8)
        halo = (dist >= 0.96) & (dist <= 1.24)
        ha = np.clip(1.0 - (dist[halo] - 0.96) / 0.28, 0.0, 1.0) ** 2.2
        hcol = (244, 63, 94) if perturbed else (34, 211, 238)
        for c in range(3):
            rgba[halo, c] = hcol[c]
        rgba[halo, 3] = (ha * 165).astype(np.uint8)

        inside = dist <= 1.0
        nx = x[inside].astype(np.float32) / float(radius)
        ny = y[inside].astype(np.float32) / float(radius)
        nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))
        ndotl = np.clip(nx * -0.55 + ny * -0.45 + nz * 0.70, 0.05, 1.0)
        fresnel = (1.0 - nz) ** 2.5

        base = np.array([150, 38, 62] if perturbed else [16, 102, 185], dtype=np.float32)
        rim = np.array([251, 146, 60] if perturbed else [56, 225, 255], dtype=np.float32)
        for c in range(3):
            rgba[inside, c] = np.clip(base[c] * (ndotl ** 0.85) + rim[c] * fresnel * 0.55, 0, 255).astype(np.uint8)
        rgba[inside, 3] = 255

        img = Image.fromarray(rgba, mode="RGBA")
        self._sphere_cache[key] = img
        return img

    def prepare_fullscreen_photo(self, img_path: Optional[str]) -> Optional[Image.Image]:
        if not img_path or not os.path.exists(img_path):
            return None
        try:
            im = Image.open(img_path).convert("RGB")
            tw, th = int(self.w * 1.18), int(self.h * 1.18)
            im = im.resize((tw, th), Image.Resampling.BILINEAR)
            im = ImageEnhance.Contrast(im).enhance(1.14)
            im = ImageEnhance.Color(im).enhance(1.16)
            return im
        except Exception:
            return None

    def _draw_karaoke_captions(self, draw: ImageDraw.Draw, text: str, sc_prog: float):
        """Renders modern animated word-by-word highlighted Hinglish subtitles."""
        words = str(text or "").split()
        if not words:
            return

        f_sub = get_font(max(15, int(20 * self.sc)), bold=True)
        max_w = int(self.w * 0.84)

        # Group words into max 2 balanced lines
        lines: List[List[str]] = [[]]
        cur_w = 0
        space_w = draw.textbbox((0, 0), " ", font=f_sub)[2]
        for w in words:
            ww = draw.textbbox((0, 0), w, font=f_sub)[2]
            if cur_w + ww > max_w and len(lines) < 2 and lines[-1]:
                lines.append([w])
                cur_w = ww + space_w
            else:
                lines[-1].append(w)
                cur_w += ww + space_w

        active_word_idx = min(len(words) - 1, int(sc_prog * len(words) * 1.08))
        hi_col = (251, 191, 36) if self.genre != "romantic" else (244, 114, 182)
        border_c = (244, 63, 94) if self.genre == "horror" else ((244, 114, 182) if self.genre == "romantic" else (34, 211, 238))

        line_h = int(26 * self.sc)
        box_h = len(lines) * line_h + int(22 * self.sc)
        box_w = int(self.w * 0.90)
        bx0 = (self.w - box_w) // 2
        by1 = self.h - int(22 * self.sc)
        by0 = by1 - box_h

        # Slide-up entry animation in first 8% of scene
        slide_offset = int((1.0 - min(1.0, sc_prog / 0.08)) * 18 * self.sc)
        by0 += slide_offset
        by1 += slide_offset

        draw.rounded_rectangle([bx0, by0, bx0 + box_w, by1], radius=13, fill=(6, 9, 18), outline=border_c, width=2)

        ty = by0 + int(10 * self.sc)
        global_w_idx = 0
        for line_words in lines:
            line_str = " ".join(line_words)
            total_lw = draw.textbbox((0, 0), line_str, font=f_sub)[2]
            tx = (self.w - total_lw) // 2

            for w in line_words:
                is_active = global_w_idx <= active_word_idx
                is_current = global_w_idx == active_word_idx
                col = hi_col if is_current else ((255, 255, 255) if is_active else (148, 163, 184))
                # Drop shadow for crisp readability
                draw.text((tx + 2, ty + 2), w, font=f_sub, fill=(0, 0, 0))
                draw.text((tx, ty), w, font=f_sub, fill=col)
                tx += draw.textbbox((0, 0), w + " ", font=f_sub)[2]
                global_w_idx += 1
            ty += line_h

    def render_frame(
        self,
        scene: Dict[str, Any],
        sc_prog: float,
        global_t: float,
        ov_prog: float,
        photo_img: Optional[Image.Image] = None,
    ) -> bytes:
        mode = scene.get("mode", "photo")

        if photo_img is not None:
            pw, ph = photo_img.size
            max_dx = max(1, pw - self.w)
            max_dy = max(1, ph - self.h)
            e = ease_in_out(sc_prog)
            ox = int(max_dx * 0.5 * e)
            oy = int(max_dy * 0.4 * e)
            frame = photo_img.crop((ox, oy, ox + self.w, oy + self.h))
            draw = ImageDraw.Draw(frame)
        elif self.genre == "documentary" and mode != "photo":
            frame = self._genre_artwork_cache.copy()
            draw = ImageDraw.Draw(frame)
            cx, cy = self.w * 0.5, self.h * 0.44
            zoom = 1.0 + 0.08 * ease_in_out(sc_prog)
            if mode == "dual_reality":
                rp = min(self.w * 0.19, self.h * 0.28) * zoom
                sp1 = self._get_3d_sphere(int(rp), False)
                sp2 = self._get_3d_sphere(int(rp), True)
                frame.paste(sp1, (int(self.w * 0.28 - sp1.width // 2), int(cy - sp1.height // 2)), sp1)
                frame.paste(sp2, (int(self.w * 0.72 - sp2.width // 2), int(cy - sp2.height // 2)), sp2)
            elif mode == "solar_system":
                sr = min(self.w, self.h) * 0.10 * zoom
                draw.ellipse([cx - sr, cy - sr, cx + sr, cy + sr], fill=(251, 191, 36))
                for oi, m in enumerate([0.22, 0.34, 0.46], start=1):
                    rx, ry = self.w * m, self.h * m * 0.58
                    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(65, 85, 130), width=1)
                    ang = global_t * (1.7 / oi) + oi
                    px, py = cx + math.cos(ang) * rx, cy + math.sin(ang) * ry
                    pr = max(5, int((9 - oi) * self.sc))
                    draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=(34, 211, 238) if oi == 2 else (168, 85, 247))
            else:
                rp = min(self.w, self.h) * 0.29 * zoom
                sp = self._get_3d_sphere(int(rp), True)
                frame.paste(sp, (int(cx - sp.width // 2), int(cy - sp.height // 2)), sp)
        else:
            # Genre-matched procedural cinema art (Misty Moonlit Forest for Horror / Sunset Bokeh for Romantic)
            frame = self._genre_artwork_cache.copy()
            draw = ImageDraw.Draw(frame)

        # Floating atmospheric particles (Embers / Golden Firefly Bokeh / Starlight)
        p_col = (244, 63, 94) if self.genre == "horror" else ((253, 224, 71) if self.genre == "romantic" else (186, 230, 253))
        for px0, py0, pr, spd in self.particles:
            px = (px0 + global_t * 16.0 * spd) % self.w
            py = (py0 - global_t * 14.0 * spd) % self.h
            r_px = max(1, int(pr * self.sc * 0.75))
            draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=p_col)

        # Animated Word-by-Word Karaoke Subtitles
        self._draw_karaoke_captions(draw, scene["hinglish"], sc_prog)

        # Thin cinema progress line at bottom edge
        bar_w = int(self.w * ov_prog)
        accent_c = (244, 63, 94) if self.genre == "horror" else ((244, 114, 182) if self.genre == "romantic" else (34, 211, 238))
        draw.rectangle([0, self.h - 4, bar_w, self.h], fill=accent_c)

        return frame.tobytes()


# ==============================================================================
# 7. STREAMING RENDER PIPELINE
# ==============================================================================
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

        topic, scenes = parse_or_build_scenes(
            payload.get("topic", ""),
            genre,
            payload.get("custom_script", ""),
            float(payload.get("duration_min", 1.0)),
        )
        aspect = payload.get("aspect_ratio", "16:9")
        out_w, out_h = (540, 960) if aspect == "9:16" else (960, 540)
        fps = 14

        renderer = NexoraCinemaRenderer(out_w, out_h, genre=genre)
        total_sc = len(scenes)
        est_total_dur = sum(float(s["duration_sec"]) for s in scenes)
        global_t = 0.0
        dt = 1.0 / float(fps)

        av_chunks: List[str] = []
        cinema_vf = (
            "eq=contrast=1.10:brightness=-0.02:saturation=1.15,unsharp=3:3:0.5:3:3:0.0,vignette=PI/5"
            if genre == "horror"
            else "eq=contrast=1.06:brightness=0.01:saturation=1.22,unsharp=3:3:0.45:3:3:0.0,vignette=PI/6"
        )

        for idx, sc in enumerate(scenes):
            update_job(
                job_id,
                status="running",
                progress=5 + int((idx / float(total_sc)) * 85),
                current_scene=f"Dubbing & Rendering Scene {idx + 1}/{total_sc}...",
            )

            dub_wav, actual_dur = NexoraAudioEngine.dub_scene_expressive(sc, idx, work_dir)
            sc["duration_sec"] = actual_dur

            photo_img = None
            if sc.get("mode") == "photo" and sc.get("visual_prompt"):
                img_file = os.path.join(work_dir, f"vis_{idx:03d}.jpg")
                if fetch_scene_visual(sc["visual_prompt"], genre, img_file, out_w, out_h):
                    photo_img = renderer.prepare_fullscreen_photo(img_file)
                if os.path.exists(img_file):
                    os.remove(img_file)

            n_frames = max(1, int(round(actual_dur * fps)))
            av_chunk = os.path.join(work_dir, f"av_{idx:03d}.mp4")

            proc = subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
                    "-i", dub_wav,
                    "-vf", cinema_vf,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest", "-pix_fmt", "yuv420p", av_chunk,
                ],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )

            for f_i in range(n_frames):
                raw = renderer.render_frame(
                    sc,
                    f_i / float(max(1, n_frames - 1)),
                    global_t,
                    min(1.0, global_t / max(1.0, est_total_dur)),
                    photo_img=photo_img,
                )
                proc.stdin.write(raw)
                global_t += dt

            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f"FFmpeg failed on Scene {idx + 1}.")

            av_chunks.append(av_chunk)
            if os.path.exists(dub_wav):
                os.remove(dub_wav)
            photo_img = None
            gc.collect()

        update_job(job_id, progress=92, current_scene="Mixing Genre Soundscape & Finalizing Cinema MP4...")
        concat_txt = os.path.join(work_dir, "av_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as cf:
            for c in av_chunks:
                cf.write(f"file '{c}'\n")

        joined_av = os.path.join(work_dir, "joined_av.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", joined_av],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        actual_total_dur = NexoraAudioEngine.probe_dur(joined_av) or global_t
        music_wav = os.path.join(work_dir, "genre_music.wav")
        NexoraAudioEngine.build_genre_soundscape(actual_total_dur, genre, music_wav)

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", topic)[:28].strip("_") or "NEXORA_STORY"
        out_name = f"NEXORA_{genre.upper()}_{slug}_{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", joined_av,
                "-i", music_wav,
                "-filter_complex", "[0:a]volume=1.18[v];[1:a]volume=0.55[m];[v][m]amix=inputs=2:duration=first[aout]",
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


# ==============================================================================
# 8. REST API
# ==============================================================================
@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "ffmpeg": HAS_FFMPEG, "hindi_dubbing": HAS_EDGE_TTS})


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
            "current_scene": "Initializing NEXORA Ultra Pro Cinema Engine...",
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
