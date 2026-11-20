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

# ==============================================================================
# 1. CONFIGURATION & ZERO-BLOAT EPHEMERAL STORAGE
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
    """Automatically deletes old MP4s so Render storage never fills up."""
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
# 2. TYPOGRAPHY & TEXT HELPERS
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


def wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    words = str(text or "").split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


# ==============================================================================
# 3. WIKIMEDIA COPYRIGHT-SAFE HD VISUAL FETCHER (ZERO API KEY)
# ==============================================================================
def fetch_wikimedia_image(query: str, out_path: str) -> bool:
    """Fetches high-res copyright-safe educational/historical photos from Wikimedia Commons."""
    clean_q = re.sub(r"[^a-zA-Z0-9\s]", "", str(query or "")).strip()
    if not clean_q:
        return False
    try:
        api_url = (
            "https://commons.wikimedia.org/w/api.php?action=query&generator=search"
            f"&gsrsearch={urllib.parse.quote(clean_q)}&gsrnamespace=6&gsrlimit=4"
            "&prop=imageinfo&iiprop=url&iiurlwidth=960&format=json"
        )
        req = urllib.request.Request(api_url, headers={"User-Agent": "NEXUSRealityStudio/2.0 (Educational)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pages = data.get("query", {}).get("pages", {})
        for _, page in pages.items():
            ii = page.get("imageinfo", [{}])[0]
            thumb_url = ii.get("thumburl") or ii.get("url", "")
            if thumb_url and any(thumb_url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                img_req = urllib.request.Request(thumb_url, headers={"User-Agent": "NEXUSRealityStudio/2.0"})
                with urllib.request.urlopen(img_req, timeout=8) as img_resp:
                    with open(out_path, "wb") as f:
                        f.write(img_resp.read())
                if os.path.exists(out_path) and os.path.getsize(out_path) > 8000:
                    return True
    except Exception:
        pass
    return False


# ==============================================================================
# 4. HINDI + HINGLISH SCRIPT PARSER & LONG-FORM GENERATOR
# ==============================================================================
BUILTIN_HINDI_SCENARIOS: Dict[str, List[Dict[str, Any]]] = {
    "earth_stop": [
        {
            "stage": "01 // INITIAL SHOCK",
            "clock": "T + 00:00:01",
            "mode": "earth_rotation",
            "search": "Earth from space",
            "mood": "intense",
            "headline": "Agar Prithvi Achanak Ghumna Band Kar De?",
            "hinglish": "Equator par Prithvi 1,670 km/h ki speed se ghumti hai. Ek second mein rukne par sab kuch hawa mein ud jayega!",
            "hindi_dub": "भूमध्य रेखा पर हमारी पृथ्वी सोलह सौ सत्तर किलोमीटर प्रति घंटे की रफ्तार से घूम रही है। अगर पृथ्वी एक ही सेकंड में घूमना बंद कर दे, तो जड़त्व के नियम के कारण हवा और समंदर इसी भयानक रफ्तार से पूर्व दिशा की ओर बहने लगेंगे।",
            "telemetry": {"Crust Speed": "0 km/h", "Wind Shear": "1,670 km/h", "Danger": "Extreme"},
        },
        {
            "stage": "02 // SUPERSONIC WINDS",
            "clock": "T + 00:04:00",
            "mode": "dual_reality",
            "search": "Hurricane storm from space",
            "mood": "dramatic",
            "headline": "Super-Hurricane Toofan Aur Aag Ki Aandhi",
            "hinglish": "Duniya ke sabse khatarnak tornado se 4 guna tez hawayein zameen ki har cheez ko mita dengi.",
            "hindi_dub": "दुनिया के सबसे खतरनाक तूफान से भी चार गुना तेज हवाएं पूरी धरती की सतह को छील देंगी। हवा के घर्षण से इतनी भयानक गर्मी पैदा होगी कि पूरे महाद्वीपों में आग के तूफान उठ खड़े होंगे।",
            "telemetry": {"Wind Speed": "> 450 m/s", "Pressure": "130 kPa", "Heat": "Critical"},
        },
        {
            "stage": "03 // MEGA TSUNAMI",
            "clock": "T + 12:00:00",
            "mode": "consequence_map",
            "search": "Ocean waves tsunami",
            "mood": "intense",
            "headline": "Samandar Ka Pani Poles Ki Taraf Bhagega",
            "hinglish": "Centrifugal force khatam hote hi samandar North aur South Pole ki taraf daudenge aur mega-tsunami aayegi.",
            "hindi_dub": "पृथ्वी के घूमने से पैदा होने वाला अपकेंद्रीय बल खत्म होते ही, भूमध्य रेखा पर जमा समंदर का पानी तेजी से उत्तरी और दक्षिणी ध्रुवों की ओर भागेगा, जिससे महाद्वीपों में कई किलोमीटर ऊंची सुनामी आ जाएगी।",
            "telemetry": {"Equator Drop": "-21 km", "New Oceans": "2 Polar Seas", "Tsunami": "Planetary"},
        },
        {
            "stage": "04 // 6 MONTHS DAY & NIGHT",
            "clock": "T + 180 DAYS",
            "mode": "dual_reality",
            "search": "Desert heat and arctic ice",
            "mood": "calm",
            "headline": "6 Mahine Din Aur 6 Mahine Raat Ka Keher",
            "hinglish": "Ab ek din 24 ghante ka nahi balki poore 1 saal ka hoga. Ek taraf aag barsegi aur doosri taraf barf jamegi.",
            "hindi_dub": "अब पृथ्वी पर एक दिन चौबीस घंटे का नहीं, बल्कि पूरे एक साल का होगा। धरती के एक हिस्से में छह महीने तक लगातार सूरज की आग बरसेगी, जबकि दूसरे अंधेरे हिस्से में तापमान माइनस पचास डिग्री से भी नीचे चला जाएगा।",
            "telemetry": {"Day Length": "365 Days", "Day Temp": "> 60°C", "Night Temp": "< -50°C"},
        },
        {
            "stage": "05 // SCIENTIFIC REALITY",
            "clock": "VERIFIED SCIENCE",
            "mode": "timeline_science",
            "search": "Moon and Earth orbit",
            "mood": "calm",
            "headline": "Asli Sach: Prithvi Kyun Nahi Ruk Sakti?",
            "hinglish": "Chandrama ke gravity friction se Prithvi har sadi mein sirf 1.8 millisecond dheemi hoti hai.",
            "hindi_dub": "लेकिन घबराने की कोई बात नहीं है। विज्ञान के अनुसार कोई भी ताकत पृथ्वी को अचानक नहीं रोक सकती। चंद्रमा के गुरुत्वाकर्षण के कारण हमारी पृथ्वी हर सौ साल में केवल एक दशमलव आठ मिलीसेकंड ही धीमी होती है।",
            "telemetry": {"Real Slowdown": "1.8 ms/Century", "Status": "100% Safe", "Physics": "Verified"},
        },
    ]
}


def parse_or_generate_hindi_scenes(topic: str, custom_script: str, target_minutes: float) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Supports two modes:
    1) Custom pasted pipe-formatted script (from Gemini/ChatGPT) for 10-15 min long videos.
    2) Built-in automatic generator that scales to the requested duration.
    Format per line:
    Stage | Clock | Mode(earth_rotation/dual_reality/consequence_map/asteroid_impact/solar_system/photo) | Image Search Keyword | Mood(intense/dramatic/calm) | Hinglish Headline | Hinglish On-Screen Caption | Hindi Dubbing Text
    """
    clean_topic = (topic or "").strip() or "Agar Prithvi Achanak Ghumna Band Kar De?"
    parsed: List[Dict[str, Any]] = []

    for line in str(custom_script or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 8:
            parsed.append({
                "stage": parts[0],
                "clock": parts[1],
                "mode": parts[2].lower() if parts[2].lower() in ("earth_rotation", "dual_reality", "consequence_map", "asteroid_impact", "solar_system", "photo") else "photo",
                "search": parts[3],
                "mood": parts[4].lower() if parts[4].lower() in ("intense", "dramatic", "calm") else "dramatic",
                "headline": parts[5],
                "hinglish": parts[6],
                "hindi_dub": parts[7],
                "telemetry": {"Mode": "Custom Script", "Language": "Hindi + Hinglish", "Quality": "Pro Dub"},
            })

    if not parsed:
        base_list = BUILTIN_HINDI_SCENARIOS["earth_stop"]
        # If user asks for a longer duration without custom script, repeat/expand with custom topic framing
        if "stop" not in clean_topic.lower() and "prithvi" not in clean_topic.lower():
            base_list = [
                {
                    "stage": "01 // MYSTERY HOOK",
                    "clock": "T + 00:00:01",
                    "mode": "photo",
                    "search": clean_topic,
                    "mood": "intense",
                    "headline": f"Kya Hoga Agar: {clean_topic[:36]}?",
                    "hinglish": f"Aaj hum {clean_topic} ke peeche ka asli vigyan aur iske khatarnak parinam janenge.",
                    "hindi_dub": f"दोस्तों, क्या आपने कभी सोचा है कि {clean_topic} की असली सच्चाई क्या है? आज के इस वीडियो में हम विज्ञान और तथ्यों के जरिए इसके हर पहलू को गहराई से समझेंगे।",
                    "telemetry": {"Topic": "Deep Dive", "Status": "Analyzing", "Audio": "Pro Hindi"},
                },
                {
                    "stage": "02 // IMMEDIATE IMPACT",
                    "clock": "T + 00:05:00",
                    "mode": "dual_reality",
                    "search": f"{clean_topic} science",
                    "mood": "dramatic",
                    "headline": "Pehle 5 Minute Mein Kya Badlega?",
                    "hinglish": "Shuruati kuch hi palon mein iska seedha asar hamare aas-paas ki duniya par dikhne lagega.",
                    "hindi_dub": "घटना के शुरुआती कुछ ही मिनटों के भीतर इसके चौंकाने वाले परिणाम सामने आने लगेंगे। भौतिकी के नियमों के अनुसार ऊर्जा और संतुलन में अचानक बड़ा बदलाव देखने को मिलेगा।",
                    "telemetry": {"Phase": "Immediate", "Comparison": "Dual View", "Impact": "High"},
                },
                {
                    "stage": "03 // GLOBAL CASCADE",
                    "clock": "T + 24 HOURS",
                    "mode": "consequence_map",
                    "search": f"{clean_topic} world",
                    "mood": "intense",
                    "headline": "24 Ghante Baad Poori Duniya Par Asar",
                    "hinglish": "Agle 24 ghanton mein yeh badlav ek chain reaction ki tarah poori duniya mein fail jayega.",
                    "hindi_dub": "अगले चौबीस घंटों के भीतर यह असर किसी एक जगह तक सीमित नहीं रहेगा, बल्कि एक चेन रिएक्शन की तरह पूरी दुनिया के वातावरण और जीवन को प्रभावित करने लगेगा।",
                    "telemetry": {"Timescale": "24 Hours", "Propagation": "Global", "Alert": "Critical"},
                },
                {
                    "stage": "04 // SCIENTIFIC CONCLUSION",
                    "clock": "VERIFIED TRUTH",
                    "mode": "timeline_science",
                    "search": "Science laboratory research",
                    "mood": "calm",
                    "headline": "Asli Vigyan Aur Final Nishkarsh",
                    "hinglish": "Asli science humein batati hai ki prakriti ke niyam kitne santulit aur adbhut hain.",
                    "hindi_dub": "अंत में विज्ञान हमें यही सिखाता है कि प्रकृति के हर नियम के पीछे एक गहरा संतुलन छिपा है। उम्मीद है कि यह जानकारी आपको जरूर पसंद आई होगी।",
                    "telemetry": {"Verification": "Scientific", "Result": "Complete", "Channel": "NEXUS Hindi"},
                },
            ]
        parsed = [dict(x) for x in base_list]

    target_sec = max(25.0, min(900.0, float(target_minutes) * 60.0))
    base_dur = round(target_sec / float(len(parsed)), 2)
    scenes: List[Dict[str, Any]] = []
    cur = 0.0
    for i, sc in enumerate(parsed):
        d = round(target_sec - cur, 2) if i == len(parsed) - 1 else base_dur
        scenes.append({
            **sc,
            "index": i + 1,
            "start_sec": round(cur, 2),
            "duration_sec": max(5.0, d),
        })
        cur += d

    return clean_topic, scenes


# ==============================================================================
# 5. PRO EMOTIONAL HINDI DUBBING ENGINE (STREAMING PER-SCENE)
# ==============================================================================
class EmotionalHindiDubber:
    """Synthesizes expressive Hindi male documentary voiceover scene-by-scene."""

    VOICE = "hi-IN-MadhurNeural"

    MOOD_PROFILES = {
        "intense": {"rate": "+6%", "pitch": "-3Hz"},
        "dramatic": {"rate": "+0%", "pitch": "-5Hz"},
        "calm": {"rate": "-5%", "pitch": "-2Hz"},
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
    def dub_scene(cls, sc: Dict[str, Any], idx: int, work_dir: str) -> Tuple[str, float]:
        """Generates emotional Hindi dubbing for a single scene and returns (wav_path, actual_duration)."""
        if not HAS_EDGE_TTS:
            raise RuntimeError("edge-tts is not installed on the server.")

        mood = cls.MOOD_PROFILES.get(sc.get("mood", "dramatic"), cls.MOOD_PROFILES["dramatic"])
        text = str(sc.get("hindi_dub") or sc.get("hinglish") or "").strip()
        mp3_p = os.path.join(work_dir, f"dub_{idx:03d}.mp3")
        wav_p = os.path.join(work_dir, f"dub_{idx:03d}.wav")

        async def _run_tts():
            comm = edge_tts.Communicate(text, voice=cls.VOICE, rate=mood["rate"], pitch=mood["pitch"])
            await comm.save(mp3_p)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run_tts())
        finally:
            loop.close()

        speech_dur = cls.probe_dur(mp3_p)
        if speech_dur <= 0.3:
            raise RuntimeError(f"Hindi dubbing failed on scene {idx + 1}. Please retry.")

        # Ensure scene lasts at least as long as the spoken Hindi sentence + 0.7s natural pause
        final_dur = round(max(float(sc["duration_sec"]), speech_dur + 0.70), 2)

        subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_p,
                "-af", f"acompressor=threshold=-18dB:ratio=3,apad=whole_dur={final_dur:.3f}",
                "-t", f"{final_dur:.3f}",
                "-ar", "22050", "-ac", "1", wav_p,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        if os.path.exists(mp3_p):
            os.remove(mp3_p)
        return wav_p, final_dur

    @staticmethod
    def build_ambient_bed(total_dur: float, out_wav: str):
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
                drone = 0.48 * np.sin(2.0 * np.pi * 55.0 * t) + 0.30 * np.sin(2.0 * np.pi * 82.41 * t)
                pulse = np.exp(-((t % 2.0) * 6.0)) * np.sin(2.0 * np.pi * 440.0 * t) * 0.10
                sig = (drone + pulse) * 0.07
                wf.writeframes((np.clip(sig, -0.95, 0.95) * 32767.0).astype(np.int16).tobytes())
                cur += dur
                gc.collect()


# ==============================================================================
# 6. HYBRID REALITY RENDERER (3D SHADED + KEN BURNS PHOTO MOTION + HINGLISH HUD)
# ==============================================================================
class HybridHindiRenderer:
    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.is_vertical = height > width
        self.sc = min(width, height) / 540.0
        self.pal = {
            "bg_top": (11, 21, 48),
            "bg_bot": (3, 5, 13),
            "cyan": (34, 211, 238),
            "violet": (168, 85, 247),
            "amber": (251, 191, 36),
            "crimson": (244, 63, 94),
            "emerald": (16, 185, 129),
            "panel": (9, 15, 30),
            "text": (248, 250, 252),
            "muted": (148, 163, 184),
        }
        rng = np.random.default_rng(2027)
        self.stars = np.column_stack([
            rng.uniform(0, width, 95),
            rng.uniform(0, height, 95),
            rng.uniform(0.7, 2.2, 95),
            rng.uniform(0.3, 1.0, 95),
        ])
        self._bg_cache = self._build_backdrop()
        self._sphere_cache: Dict[Tuple[int, bool], Image.Image] = {}

    def _build_backdrop(self) -> Image.Image:
        yy, xx = np.mgrid[0 : self.h, 0 : self.w]
        nx = (xx - self.w * 0.5) / (self.w * 0.68)
        ny = (yy - self.h * 0.40) / (self.h * 0.68)
        radial = np.clip(1.0 - 0.78 * (nx * nx + ny * ny), 0.0, 1.0).astype(np.float32)

        arr = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        for c in range(3):
            vals = self.pal["bg_bot"][c] + (self.pal["bg_top"][c] - self.pal["bg_bot"][c]) * radial
            arr[:, :, c] = np.clip(vals, 0, 255).astype(np.uint8)

        img = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(img)
        step = max(28, int(48 * self.sc))
        for gx in range(0, self.w, step):
            draw.line([(gx, 0), (gx, self.h)], fill=(17, 27, 50), width=1)
        for gy in range(0, self.h, step):
            draw.line([(0, gy), (self.w, gy)], fill=(17, 27, 50), width=1)
        return img

    def _get_3d_sphere(self, radius: int, perturbed: bool) -> Image.Image:
        key = (radius, perturbed)
        if key in self._sphere_cache:
            return self._sphere_cache[key]

        pad = int(radius * 0.26)
        size = (radius + pad) * 2
        y, x = np.mgrid[-radius - pad : radius + pad, -radius - pad : radius + pad]
        dist = np.sqrt((x * x + y * y).astype(np.float32)) / float(radius)

        rgba = np.zeros((size, size, 4), dtype=np.uint8)
        halo_mask = (dist >= 0.96) & (dist <= 1.24)
        halo_alpha = np.clip(1.0 - (dist[halo_mask] - 0.96) / 0.28, 0.0, 1.0) ** 2.2
        halo_col = (244, 63, 94) if perturbed else (34, 211, 238)
        for c in range(3):
            rgba[halo_mask, c] = halo_col[c]
        rgba[halo_mask, 3] = (halo_alpha * 165).astype(np.uint8)

        in_mask = dist <= 1.0
        nx = x[in_mask].astype(np.float32) / float(radius)
        ny = y[in_mask].astype(np.float32) / float(radius)
        nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))

        ndotl = np.clip(nx * -0.55 + ny * -0.45 + nz * 0.70, 0.05, 1.0)
        fresnel = (1.0 - nz) ** 2.5

        base_col = np.array([150, 38, 62] if perturbed else [16, 102, 185], dtype=np.float32)
        rim_col = np.array([251, 146, 60] if perturbed else [56, 225, 255], dtype=np.float32)

        for c in range(3):
            lit = base_col[c] * (ndotl ** 0.85) + rim_col[c] * fresnel * 0.55
            rgba[in_mask, c] = np.clip(lit, 0, 255).astype(np.uint8)
        rgba[in_mask, 3] = 255

        img = Image.fromarray(rgba, mode="RGBA")
        self._sphere_cache[key] = img
        return img

    def _draw_panel(self, draw: ImageDraw.Draw, x0: int, y0: int, x1: int, y1: int, border: Tuple[int, int, int], glow: Tuple[int, int, int]):
        draw.rounded_rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], radius=14, outline=glow, width=1)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=12, fill=self.pal["panel"], outline=border, width=2)
        draw.rounded_rectangle([x0 + 10, y0 + 3, x1 - 10, y0 + 5], radius=2, fill=glow)

    def _draw_planet_3d(self, frame: Image.Image, draw: ImageDraw.Draw, cx: float, cy: float, radius: float, phase: float, perturbed: bool, label: str):
        sphere = self._get_3d_sphere(max(24, int(radius)), perturbed)
        sw, sh = sphere.size
        frame.paste(sphere, (int(cx - sw // 2), int(cy - sh // 2)), sphere)

        band_col = (234, 88, 12) if perturbed else (34, 197, 148)
        for i in range(4):
            ang = phase * (0.25 if perturbed else 1.7) + i * 1.4
            lx = cx - radius * 0.12 + math.sin(ang) * radius * 0.36
            ly = cy - radius * 0.08 + math.cos(i * 1.2) * radius * 0.36
            rw = radius * 0.26 * abs(math.cos(ang * 0.65))
            rh = radius * 0.14
            if rw > 2:
                draw.ellipse([lx - rw, ly - rh, lx + rw, ly + rh], fill=band_col)

        if label:
            f_lbl = get_font(max(11, int(12 * self.sc)), bold=True)
            bb = draw.textbbox((0, 0), label, font=f_lbl)
            col = self.pal["crimson"] if perturbed else self.pal["cyan"]
            draw.text((cx - (bb[2] - bb[0]) / 2, cy + radius + 10 * self.sc), label, font=f_lbl, fill=col)

    def prepare_scene_photo(self, img_path: Optional[str], vp_w: int, vp_h: int) -> Optional[Image.Image]:
        if not img_path or not os.path.exists(img_path):
            return None
        try:
            im = Image.open(img_path).convert("RGB")
            # Resize slightly larger than viewport to allow smooth Ken Burns zoom/pan
            target_w = int(vp_w * 1.16)
            target_h = int(vp_h * 1.16)
            im = im.resize((target_w, target_h), Image.Resampling.BILINEAR)
            im = ImageEnhance.Contrast(im).enhance(1.12)
            im = ImageEnhance.Color(im).enhance(1.15)
            return im
        except Exception:
            return None

    def render_frame(
        self,
        scene: Dict[str, Any],
        idx: int,
        total: int,
        sc_prog: float,
        global_t: float,
        ov_prog: float,
        photo_img: Optional[Image.Image] = None,
    ) -> bytes:
        frame = self._bg_cache.copy()
        draw = ImageDraw.Draw(frame)
        pad = int(18 * self.sc)

        # 1. Top HUD Header
        top_h = int(40 * self.sc)
        self._draw_panel(draw, pad, pad, self.w - pad, pad + top_h, self.pal["cyan"], (14, 116, 144))
        f_brand = get_font(max(11, int(13 * self.sc)), bold=True)
        draw.text((pad + 12, pad + int(12 * self.sc)), f"NEXUS HINDI // {scene['stage']}", font=f_brand, fill=self.pal["amber"])

        clk = f"TIME: {scene['clock']}"
        cb = draw.textbbox((0, 0), clk, font=f_brand)
        draw.text((self.w - pad - (cb[2] - cb[0]) - 12, pad + int(12 * self.sc)), clk, font=f_brand, fill=self.pal["cyan"])

        # 2. Main Visual Viewport
        vp_top = pad + top_h + int(10 * self.sc)
        bot_h = int(124 * self.sc) if not self.is_vertical else int(180 * self.sc)
        vp_bot = self.h - pad - bot_h - int(10 * self.sc)

        if self.is_vertical:
            vp_box = (pad, vp_top, self.w - pad, vp_bot)
        else:
            side_w = int(self.w * 0.26)
            vp_box = (pad, vp_top, self.w - pad - side_w - int(10 * self.sc), vp_bot)
            side_box = (self.w - pad - side_w, vp_top, self.w - pad, vp_bot)
            self._draw_panel(draw, *side_box, self.pal["cyan"], (14, 116, 144))

            f_sh = get_font(max(10, int(12 * self.sc)), bold=True)
            f_sv = get_font(max(10, int(11 * self.sc)), bold=False)
            sy = side_box[1] + int(12 * self.sc)
            draw.text((side_box[0] + 12, sy), f"SCENE {idx + 1}/{total} DATA", font=f_sh, fill=self.pal["cyan"])
            sy += int(24 * self.sc)
            for k_lbl, v_lbl in scene.get("telemetry", {}).items():
                draw.text((side_box[0] + 12, sy), f"{k_lbl}:", font=f_sv, fill=self.pal["muted"])
                sy += int(15 * self.sc)
                draw.text((side_box[0] + 12, sy), str(v_lbl), font=f_sh, fill=self.pal["text"])
                sy += int(23 * self.sc)

        x0, y0, x1, y1 = vp_box
        vw, vh = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        zoom = 1.0 + 0.08 * ease_in_out(sc_prog)

        # Render Photo with Smooth Ken Burns Pan & Zoom OR 3D Science Simulation
        if photo_img is not None:
            pw, ph = photo_img.size
            max_dx = max(1, pw - vw)
            max_dy = max(1, ph - vh)
            ox = int(max_dx * 0.5 * ease_in_out(sc_prog))
            oy = int(max_dy * 0.35 * ease_in_out(sc_prog))
            crop = photo_img.crop((ox, oy, ox + vw, oy + vh))
            frame.paste(crop, (x0, y0))
        else:
            for sx, sy, sr, spd in self.stars:
                px = x0 + ((sx + global_t * 14.0 * spd) % vw)
                py = y0 + ((sy + global_t * 4.0 * spd) % vh)
                r_px = max(1, int(sr * self.sc))
                draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=(160, 195, 245))

            mode = scene.get("mode", "earth_rotation")
            if mode == "dual_reality":
                mid_x = int(cx)
                draw.line([(mid_x, y0 + 10), (mid_x, y1 - 10)], fill=self.pal["violet"], width=2)
                rp = min(vw * 0.16, vh * 0.27) * zoom
                self._draw_planet_3d(frame, draw, (x0 + mid_x) * 0.5, cy, rp, global_t * 1.4, False, "NORMAL")
                self._draw_planet_3d(frame, draw, (mid_x + x1) * 0.5, cy, rp, global_t * 0.3, True, "CHANGED")
            elif mode == "consequence_map":
                rg = min(vw, vh) * 0.28 * zoom
                self._draw_planet_3d(frame, draw, cx, cy, rg, global_t * 0.8, True, "GLOBAL IMPACT")
                pr = rg * (0.35 + (sc_prog * 1.1) % 1.0)
                draw.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], outline=self.pal["amber"], width=2)
            elif mode == "asteroid_impact":
                re_r = min(vw, vh) * 0.23 * zoom
                ex, ey = x1 - vw * 0.28, cy + vh * 0.05
                self._draw_planet_3d(frame, draw, ex, ey, re_r, global_t, True, "EARTH")
                ax = x0 + vw * (0.14 + 0.55 * ease_in_out(sc_prog))
                ay = y0 + vh * (0.20 + 0.33 * ease_in_out(sc_prog))
                ar = max(7, int(13 * self.sc))
                draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=self.pal["amber"], outline=(255, 255, 255), width=2)
            elif mode == "solar_system":
                sun_r = min(vw, vh) * 0.10 * zoom
                draw.ellipse([cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r], fill=self.pal["amber"])
                for orb_i, mult in enumerate([0.23, 0.35, 0.47], start=1):
                    rx, ry = vw * mult, vh * mult * 0.60
                    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(48, 70, 112), width=1)
                    ang = global_t * (1.8 / orb_i) + orb_i
                    px, py = cx + math.cos(ang) * rx, cy + math.sin(ang) * ry
                    pr = max(4, int((8 - orb_i) * self.sc))
                    draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=self.pal["cyan"] if orb_i == 2 else self.pal["violet"])
            else:
                rp = min(vw, vh) * 0.29 * zoom
                self._draw_planet_3d(frame, draw, cx, cy, rp, global_t * 1.2, mode != "timeline_science", "SIMULATION")

        draw.rounded_rectangle(vp_box, radius=12, outline=self.pal["violet"], width=2)

        # 3. Bottom Hinglish Headline & Crisp Subtitle Deck
        deck_top = vp_bot + int(10 * self.sc)
        deck_bot = self.h - pad
        self._draw_panel(draw, pad, deck_top, self.w - pad, deck_bot, self.pal["cyan"], (14, 116, 144))

        f_head = get_font(max(13, int(17 * self.sc)), bold=True)
        f_sub = get_font(max(11, int(14 * self.sc)), bold=False)

        hy = deck_top + int(11 * self.sc)
        draw.text((pad + 14, hy), scene["headline"], font=f_head, fill=self.pal["amber"])

        ny = hy + int(25 * self.sc)
        for line in wrap_text(draw, scene["hinglish"], f_sub, (self.w - 2 * pad) - 28)[:3]:
            draw.text((pad + 14, ny), line, font=f_sub, fill=self.pal["text"])
            ny += int(19 * self.sc)

        bar_y = deck_bot - int(11 * self.sc)
        bar_w = (self.w - 2 * pad) - 28
        draw.rounded_rectangle([pad + 14, bar_y, pad + 14 + bar_w, bar_y + int(5 * self.sc)], radius=3, fill=(25, 36, 64))
        fill_w = max(6, int(bar_w * ov_prog))
        draw.rounded_rectangle([pad + 14, bar_y, pad + 14 + fill_w, bar_y + int(5 * self.sc)], radius=3, fill=self.pal["violet"])

        return frame.tobytes()


# ==============================================================================
# 7. STREAMING LONG-FORM RENDER PIPELINE (10-15 MIN CAPABLE, LOW-RAM)
# ==============================================================================
def update_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def run_long_hindi_job(job_id: str, payload: Dict[str, Any]):
    work_dir = tempfile.mkdtemp(prefix=f"nxh_{job_id}_", dir=TMP_ROOT)
    try:
        purge_old_files(600)
        if not HAS_FFMPEG:
            raise RuntimeError("FFmpeg not installed on server.")

        topic, scenes = parse_or_generate_hindi_scenes(
            payload.get("topic", ""),
            payload.get("custom_script", ""),
            float(payload.get("duration_min", 1.0)),
        )
        aspect = payload.get("aspect_ratio", "16:9")
        out_w, out_h = (540, 960) if aspect == "9:16" else (960, 540)
        fps = 12  # Optimized 12 FPS allows smooth 10-15 minute rendering without server timeouts

        renderer = HybridHindiRenderer(out_w, out_h)
        vp_w = out_w - int(36 * renderer.sc) if aspect == "9:16" else int(out_w * 0.74) - int(46 * renderer.sc)
        vp_h = out_h - int(260 * renderer.sc) if aspect == "9:16" else out_h - int(206 * renderer.sc)

        total_sc = len(scenes)
        est_total_dur = sum(float(s["duration_sec"]) for s in scenes)
        global_t = 0.0
        dt = 1.0 / float(fps)

        av_chunks: List[str] = []
        cinema_vf = "eq=contrast=1.06:brightness=0.01:saturation=1.16,unsharp=3:3:0.45:3:3:0.0,vignette=PI/6"

        # Stream each scene: Dub Hindi Voice -> Fetch Photo (if needed) -> Render & Mux AV Chunk -> Free RAM
        for idx, sc in enumerate(scenes):
            update_job(
                job_id,
                status="running",
                progress=5 + int((idx / float(total_sc)) * 85),
                current_scene=f"Dubbing & Rendering Scene {idx + 1}/{total_sc}: {sc['headline'][:32]}...",
            )

            # 1. Expressive Hindi Dubbing for this scene
            dub_wav, actual_dur = EmotionalHindiDubber.dub_scene(sc, idx, work_dir)
            sc["duration_sec"] = actual_dur

            # 2. Fetch Wikimedia HD photo if mode == 'photo'
            photo_img = None
            if sc.get("mode") == "photo" and sc.get("search"):
                img_file = os.path.join(work_dir, f"img_{idx:03d}.jpg")
                if fetch_wikimedia_image(sc["search"], img_file):
                    photo_img = renderer.prepare_scene_photo(img_file, vp_w, vp_h)
                if os.path.exists(img_file):
                    os.remove(img_file)

            # 3. Render & mux this scene directly into an AV chunk so memory stays near zero
            n_frames = max(1, int(round(actual_dur * fps)))
            av_chunk = os.path.join(work_dir, f"av_{idx:03d}.mp4")

            proc = subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
                    "-i", dub_wav,
                    "-vf", cinema_vf,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest", "-pix_fmt", "yuv420p", av_chunk,
                ],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )

            for f_i in range(n_frames):
                raw = renderer.render_frame(
                    sc,
                    idx,
                    total_sc,
                    f_i / float(max(1, n_frames - 1)),
                    global_t,
                    min(1.0, global_t / max(1.0, est_total_dur)),
                    photo_img=photo_img,
                )
                proc.stdin.write(raw)
                global_t += dt

            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f"FFmpeg encoding failed on Scene {idx + 1}.")

            av_chunks.append(av_chunk)
            if os.path.exists(dub_wav):
                os.remove(dub_wav)
            photo_img = None
            gc.collect()

        # 4. Concatenate all dubbed AV chunks & mix soft ambient background score
        update_job(job_id, progress=92, current_scene="Mixing Background Music & Finalizing Master MP4...")
        concat_txt = os.path.join(work_dir, "av_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as cf:
            for c in av_chunks:
                cf.write(f"file '{c}'\n")

        joined_av = os.path.join(work_dir, "joined_av.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", joined_av],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        actual_total_dur = EmotionalHindiDubber.probe_dur(joined_av) or global_t
        ambient_wav = os.path.join(work_dir, "ambient.wav")
        EmotionalHindiDubber.build_ambient_bed(actual_total_dur, ambient_wav)

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", topic)[:30].strip("_") or "NEXUS_HINDI"
        out_name = f"NEXUS_HINDI_{slug}_{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", joined_av,
                "-i", ambient_wav,
                "-filter_complex", "[0:a]volume=1.15[v];[1:a]volume=0.40[m];[v][m]amix=inputs=2:duration=first[aout]",
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
            current_scene=f"Completed ({dur_min} min • {size_mb} MB) & Auto-Saving!",
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
# 8. REST API ENDPOINTS
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
        return jsonify({"error": "Enter a topic or paste a custom Hindi/Hinglish script."}), 400

    with JOBS_LOCK:
        if len(ACTIVE_JOB_IDS) >= 1:
            return jsonify({"error": "Server is currently rendering a video. Please wait until it completes."}), 429
        job_id = f"nh_{uuid.uuid4().hex[:10]}"
        ACTIVE_JOB_IDS.add(job_id)
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 2,
            "current_scene": "Starting Pro Hindi Dubbing & Hybrid Visual Engine...",
            "error": None,
        }

    threading.Thread(target=run_long_hindi_job, args=(job_id, data), daemon=True).start()
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
