import os
import gc
import re
import time
import math
import uuid
import wave
import random
import urllib.request
import subprocess
import threading
import numpy as np

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "rendered_videos")
AUDIO_DIR = os.path.join(BASE_DIR, "audio_assets")
FONT_DIR = os.path.join(BASE_DIR, "fonts")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(FONT_DIR, exist_ok=True)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
JOBS = {}

def ensure_bengali_font() -> str:
    """Downloads Noto Sans Bengali Bold once so all Bengali conjuncts (যুক্তাক্ষর) render crisply."""
    local_ttf = os.path.join(FONT_DIR, "NotoSansBengali-Bold.ttf")
    if os.path.exists(local_ttf) and os.path.getsize(local_ttf) > 30000:
        return local_ttf

    for sys_p in [
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf"
    ]:
        if os.path.exists(sys_p):
            return sys_p

    urls = [
        "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansBengali/NotoSansBengali-Bold.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansbengali/NotoSansBengali%5Bwdth%2Cwght%5D.ttf"
    ]
    for u in urls:
        try:
            urllib.request.urlretrieve(u, local_ttf)
            if os.path.exists(local_ttf) and os.path.getsize(local_ttf) > 30000:
                return local_ttf
        except Exception:
            pass

    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

BENGALI_FONT_PATH = ensure_bengali_font()

def cleanup_old_files(max_age_sec: int = 900):
    now = time.time()
    for folder in (OUTPUT_DIR, AUDIO_DIR):
        try:
            for fname in os.listdir(folder):
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_sec:
                    os.remove(fpath)
        except Exception:
            pass

# ===========================================================================
# 1. NEXT-GEN ESPORTS & GLASSMORPHISM PALETTES
# ===========================================================================
GAMING_THEMES = {
    "cyber": {
        "name": "NEON CYBER",
        "bg_top": (13, 21, 52),
        "bg_bot": (4, 6, 16),
        "grid": (26, 48, 96),
        "card_bg": (12, 19, 38),
        "opt_bg": (17, 26, 50),
        "primary": (34, 211, 238),    # Electric Cyan
        "secondary": (168, 85, 247),  # Neon Violet
        "accent": (250, 204, 21)      # Cyber Gold
    },
    "royal": {
        "name": "SOLAR GOLD",
        "bg_top": (34, 22, 12),
        "bg_bot": (6, 4, 4),
        "grid": (64, 42, 20),
        "card_bg": (24, 17, 12),
        "opt_bg": (32, 22, 16),
        "primary": (250, 204, 21),    # Pure Gold
        "secondary": (249, 115, 22),  # Blaze Orange
        "accent": (56, 189, 248)      # Ice Blue
    },
    "crimson": {
        "name": "VALKYRIE RED",
        "bg_top": (42, 10, 26),
        "bg_bot": (6, 3, 10),
        "grid": (74, 18, 46),
        "card_bg": (26, 10, 20),
        "opt_bg": (36, 14, 28),
        "primary": (244, 63, 94),     # Esports Crimson
        "secondary": (232, 121, 249), # Plasma Pink
        "accent": (250, 204, 21)      # Gold
    },
    "emerald": {
        "name": "HYPER MATRIX",
        "bg_top": (8, 34, 26),
        "bg_bot": (3, 8, 7),
        "grid": (16, 66, 52),
        "card_bg": (10, 24, 20),
        "opt_bg": (14, 34, 28),
        "primary": (16, 185, 129),    # Emerald Neon
        "secondary": (34, 211, 238),  # Aqua
        "accent": (250, 204, 21)      # Gold
    }
}

DEFAULT_BENGALI_QUESTIONS = [
    {"q": "বায়ুমণ্ডলের কোন স্তরটি সূর্যের ক্ষতিকারক অতিবেগুনি রশ্মি শোষণ করে?", "opts": ["ট্রপোস্ফিয়ার", "স্ট্র্যাটোস্ফিয়ার", "মেসোস্ফিয়ার", "থার্মোস্ফিয়ার"], "ans": 1, "fact": "স্ট্র্যাটোস্ফিয়ারে থাকা ওজোন স্তর পৃথিবীর রক্ষাকবচ হিসেবে কাজ করে!"},
    {"q": "রোধের এসআই (SI) একক কী?", "opts": ["অ্যাম্পিয়ার", "ভোল্ট", "ওহম", "কুলম্ব"], "ans": 2, "fact": "বিজ্ঞানী জর্জ সাইমন ওহমের নামানুসারে এই একক রাখা হয়েছে।"},
    {"q": "১৮২৮ সালে কলকাতায় 'ব্রাহ্মসমাজ' কে প্রতিষ্ঠা করেন?", "opts": ["স্বামী বিবেকানন্দ", "ঈশ্বরচন্দ্র বিদ্যাসাগর", "রাজা রামমোহন রায়", "কেশবচন্দ্র সেন"], "ans": 2, "fact": "রামমোহন রায় ১৮২৯ সালে সতীদাহ প্রথা রদে প্রধান ভূমিকা নেন!"},
    {"q": "পশ্চিমবঙ্গের কোন নদীকে 'বাংলার দুঃখ' বলা হয়?", "opts": ["তিস্তা", "দামোদর", "সুবর্ণরেখা", "মহানন্দা"], "ans": 1, "fact": "অতীতে ভয়াবহ বন্যার কারণে দামোদর নদকে বাংলার দুঃখ বলা হতো।"}
]

def smart_parse_and_direct(raw_text: str, is_vertical: bool):
    items = []
    for ln in str(raw_text or "").splitlines():
        ln = ln.strip()
        if not ln or "|" not in ln:
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) >= 6:
            q_txt = re.sub(r"^(Q?\d+|প্রশ্ন\s*\d*)\s*[:.\-]\s*", "", parts[0], flags=re.I).strip()
            opts = parts[1:5]
            try:
                ans_idx = max(0, min(3, int(re.sub(r"[^0-9]", "", parts[5])) - 1))
            except Exception:
                ans_idx = 0
            fact_txt = parts[6].strip() if len(parts) >= 7 else ""
            items.append({"q": q_txt, "opts": opts, "ans": ans_idx, "fact": fact_txt})

    if not items:
        items = [dict(x) for x in DEFAULT_BENGALI_QUESTIONS]

    if is_vertical and len(items) > 4:
        items = items[:4]
    elif len(items) < 2:
        items.append(dict(DEFAULT_BENGALI_QUESTIONS[-1]))

    directed = []
    theme_keys = list(GAMING_THEMES.keys())
    total_n = len(items)

    for idx, item in enumerate(items):
        q_txt = item["q"]
        word_cnt = len(q_txt.split())
        is_secret_trap = (idx == total_n - 1)
        is_boss = ((idx + 1) % 5 == 0) and not is_secret_trap

        has_math = bool(re.search(r"[0-9০-৯%+\-*/=]|হিসাব|মান কত|সমীকরণ|অনুপাত|সুদ", q_txt))
        if is_vertical:
            think_sec = 4.4 if has_math else 3.5
            read_sec = 1.1
            reveal_sec = 2.6 if is_secret_trap else 2.0
        else:
            if has_math or word_cnt >= 14:
                think_sec = 6.5
            elif word_cnt >= 9:
                think_sec = 5.0
            else:
                think_sec = 4.0
            read_sec = max(1.5, min(2.6, word_cnt * 0.15))
            reveal_sec = 3.2 if is_secret_trap else (2.8 if item["fact"] else 2.2)

        if is_secret_trap:
            t_key, holo_type = "crimson", "cube"
        elif is_boss:
            t_key, holo_type = "crimson", "atom"
        elif any(k in q_txt for k in ["বিজ্ঞান", "ভৌত", "রসায়ন", "গ্যাস", "আলো", "লেন্স", "দর্পণ", "তড়িৎ", "রোধ", "একক", "হরমোন", "কোষ", "ক্রোমোজোম", "ওজোন", "অ্যাসিড", "ধাতু"]):
            t_key, holo_type = "cyber", "atom"
        elif any(k in q_txt for k in ["ইতিহাস", "ভূগোল", "বিদ্রোহ", "আন্দোলন", "সাল", "নদী", "পর্বত", "মৃত্তিকা", "ভারত", "বাংলা", "গান্ধী", "সুভাষ", "বঙ্কিম", "রামমোহন", "প্রতিষ্ঠা"]):
            t_key, holo_type = "royal", "globe"
        elif any(k in q_txt for k in ["গণিত", "সমীকরণ", "বৃত্ত", "গোলক", "গড়", "মধ্যমা", "মান", "সুদ", "ত্রিকোণমিতি"]) or has_math:
            t_key, holo_type = "emerald", "cube"
        else:
            t_key = theme_keys[(idx // 3) % len(theme_keys)]
            holo_type = ["atom", "globe", "cube"][idx % 3]

        directed.append({
            **item,
            "is_boss": is_boss,
            "is_secret_trap": is_secret_trap,
            "theme_key": t_key,
            "holo_type": holo_type,
            "read_sec": read_sec,
            "think_sec": think_sec,
            "reveal_sec": reveal_sec,
            "level": (idx // 5) + 1
        })

    return directed


# ===========================================================================
# 2. HIGH-IMPACT ESPORTS AUDIO ENGINE (NO VOICEOVER)
# ===========================================================================
def build_gaming_sfx_track(intro_dur: float, timeline: list, outro_dur: float, total_dur: float, out_wav: str):
    rate = 16000
    bpm = 132.0
    beat_dur = 60.0 / bpm
    bass_notes = [110.0, 110.0, 130.81, 146.83, 98.0, 98.0, 130.81, 164.81]
    rng = np.random.default_rng(2027)

    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)

        chunk_sec = 30.0
        cur_start = 0.0
        while cur_start < total_dur:
            c_dur = min(chunk_sec, total_dur - cur_start)
            n = int(round(c_dur * rate))
            if n <= 0:
                break
            t = np.linspace(cur_start, cur_start + c_dur, n, endpoint=False, dtype=np.float32)

            beat_pos = (t % beat_dur) / beat_dur
            kick = np.sin(2.0 * np.pi * (56.0 + 65.0 * np.exp(-beat_pos * 18.0)) * (t % beat_dur)) * np.exp(-beat_pos * 8.5) * 0.26
            b_idx = (t / (beat_dur * 2.0)).astype(np.int32) % len(bass_notes)
            b_freq = np.take(np.array(bass_notes, dtype=np.float32), b_idx)
            audio = kick + np.sin(2.0 * np.pi * b_freq * t) * 0.12

            if cur_start < 0.65:
                imp_end = min(n, int((0.65 - cur_start) * rate))
                ti = t[:imp_end]
                audio[:imp_end] += np.sin(2.0 * np.pi * (140.0 * np.exp(-ti * 8.0) + 42.0) * ti) * np.exp(-ti * 5.0) * 0.58

            for q_obj, t_start, t_think_start, t_reveal_start, t_end in timeline:
                if t_end < cur_start or t_start > cur_start + c_dur:
                    continue

                if cur_start <= t_start < cur_start + c_dur:
                    s0 = int((t_start - cur_start) * rate)
                    w_len = min(n - s0, int(0.24 * rate))
                    if w_len > 0:
                        tw = np.linspace(0, w_len / float(rate), w_len, endpoint=False, dtype=np.float32)
                        noise = rng.uniform(-1.0, 1.0, w_len).astype(np.float32)
                        audio[s0:s0 + w_len] += noise * (np.sin(np.pi * (tw / 0.24)) ** 1.5) * 0.19

                cur_t = t_think_start + 0.15
                tick_i = 0
                while cur_t < t_reveal_start - 0.10:
                    rem = t_reveal_start - cur_t
                    step = 0.22 if rem <= 1.8 else 0.45
                    if cur_start <= cur_t < cur_start + c_dur:
                        st = int((cur_t - cur_start) * rate)
                        t_len = min(n - st, int(0.045 * rate))
                        if t_len > 0:
                            tt = np.linspace(0, t_len / float(rate), t_len, endpoint=False, dtype=np.float32)
                            freq = 1620.0 if rem <= 1.8 else (1180.0 if tick_i % 2 == 0 else 960.0)
                            audio[st:st + t_len] += np.sin(2.0 * np.pi * freq * tt) * np.exp(-tt * 78.0) * 0.36
                    cur_t += step
                    tick_i += 1

                if cur_start <= t_reveal_start < cur_start + c_dur:
                    sr = int((t_reveal_start - cur_start) * rate)
                    r_len = min(n - sr, int(0.72 * rate))
                    if r_len > 0:
                        tr = np.linspace(0, r_len / float(rate), r_len, endpoint=False, dtype=np.float32)
                        if q_obj["is_secret_trap"]:
                            p1 = np.sin(2.0 * np.pi * 622.25 * tr) * np.exp(-tr * 8.0)
                            p2 = np.sin(2.0 * np.pi * 587.33 * np.maximum(0, tr - 0.16)) * np.exp(-np.maximum(0, tr - 0.16) * 7.0)
                            audio[sr:sr + r_len] += (p1 + p2) * 0.36
                        else:
                            c1 = np.sin(2.0 * np.pi * 1046.50 * tr) * np.exp(-tr * 6.0)
                            c2 = np.sin(2.0 * np.pi * 1318.51 * np.maximum(0, tr - 0.06)) * np.exp(-np.maximum(0, tr - 0.06) * 5.5)
                            c3 = np.sin(2.0 * np.pi * 1567.98 * np.maximum(0, tr - 0.12)) * np.exp(-np.maximum(0, tr - 0.12) * 5.0)
                            audio[sr:sr + r_len] += (c1 + c2 + c3) * 0.26

            pcm = (np.clip(audio, -0.95, 0.95) * 32767.0).astype(np.int16)
            wf.writeframes(pcm.tobytes())
            cur_start += c_dur
            gc.collect()


# ===========================================================================
# 3. NEXT-GEN MODERN ESPORTS GLASSMORPHISM RENDERER (100% BENGALI)
# ===========================================================================
_FONT_CACHE = {}
def get_font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    try:
        f = ImageFont.truetype(BENGALI_FONT_PATH, size, layout_engine=ImageFont.Layout.RAQM)
        _FONT_CACHE[size] = f
        return f
    except Exception:
        try:
            f = ImageFont.truetype(BENGALI_FONT_PATH, size)
            _FONT_CACHE[size] = f
            return f
        except Exception:
            return ImageFont.load_default()

def ease_out_back(x: float) -> float:
    x = max(0.0, min(1.0, x))
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * ((x - 1.0) ** 3) + c1 * ((x - 1.0) ** 2)

def wrap_lines(draw: ImageDraw.Draw, text: str, font, max_w: int) -> list:
    words = text.split()
    lines, cur = [], ""
    for wd in words:
        test = (cur + " " + wd).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines

def draw_modern_esports_panel(draw: ImageDraw.Draw, x0: int, y0: int, x1: int, y1: int,
                              fill_col: tuple, border_col: tuple, glow_col: tuple, sc: float):
    """Draws a multi-layered modern esports panel with outer aura, bevel highlight & corner tech-brackets."""
    # Outer neon aura
    draw.rounded_rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], radius=18, outline=glow_col, width=2)
    # Main glass body
    draw.rounded_rectangle([x0, y0, x1, y1], radius=16, fill=fill_col, outline=border_col, width=3)
    # Top glossy glass reflection bar
    gloss_h = max(4, int(6 * sc))
    draw.rounded_rectangle([x0 + 12, y0 + 4, x1 - 12, y0 + 4 + gloss_h], radius=3, fill=glow_col)

    # Sharp esports corner L-brackets
    bl = int(16 * sc)
    off = 5
    for (cx, cy, dx, dy) in [
        (x0 - off, y0 - off, 1, 1),
        (x1 + off, y0 - off, -1, 1),
        (x0 - off, y1 + off, 1, -1),
        (x1 + off, y1 + off, -1, -1)
    ]:
        draw.line([(cx, cy), (cx + dx * bl, cy)], fill=(255, 255, 255), width=2)
        draw.line([(cx, cy), (cx, cy + dy * bl)], fill=(255, 255, 255), width=2)

class ModernEsportsBengaliRenderer:
    def __init__(self, w: int, h: int, total_q: int):
        self.w = w
        self.h = h
        self.total_q = total_q
        self.is_vertical = (h > w)
        self.rng = random.Random(2027)
        self.sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0

        # Pre-render modern radial spotlight + perspective floor grid per theme
        self.bg_cache = {}
        for t_key, theme in GAMING_THEMES.items():
            yy, xx = np.ogrid[0:self.h, 0:self.w]
            nx = (xx - self.w * 0.5) / (self.w * 0.65)
            ny = (yy - self.h * 0.35) / (self.h * 0.65)
            radial = np.clip(1.0 - 0.72 * (nx * nx + ny * ny), 0.0, 1.0).astype(np.float32)

            arr = np.zeros((self.h, self.w, 3), dtype=np.uint8)
            for ch in range(3):
                c_val = theme["bg_bot"][ch] + (theme["bg_top"][ch] - theme["bg_bot"][ch]) * radial
                arr[:, :, ch] = np.clip(c_val, 0, 255).astype(np.uint8)

            bg_img = Image.fromarray(arr, mode="RGB")
            d = ImageDraw.Draw(bg_img)

            # Perspective synth-arena floor lines
            hor_y = int(self.h * 0.72)
            cx = self.w * 0.5
            for i in range(-10, 11):
                xt = cx + i * int(36 * self.sc)
                xb = cx + i * int(145 * self.sc)
                d.line([(xt, hor_y), (xb, self.h)], fill=theme["grid"], width=1)
            for r_i in range(1, 7):
                ry = hor_y + int((self.h - hor_y) * ((r_i / 6.0) ** 1.5))
                d.line([(0, ry), (self.w, ry)], fill=theme["grid"], width=1)

            # Side esports accent rails
            d.line([(8, int(self.h * 0.12)), (8, int(self.h * 0.88))], fill=theme["primary"], width=2)
            d.line([(self.w - 8, int(self.h * 0.12)), (self.w - 8, int(self.h * 0.88))], fill=theme["secondary"], width=2)

            self.bg_cache[t_key] = bg_img

        self.ambient_orbs = [
            [self.rng.uniform(0, w), self.rng.uniform(0, h), self.rng.uniform(25, 60), self.rng.uniform(2, 4.2)]
            for _ in range(20)
        ]
        self.confetti = []
        self.last_confetti_q = -1

    def _get_bg_frame(self, t_key: str, theme: dict, global_t: float) -> tuple:
        frame = self.bg_cache[t_key].copy()
        draw = ImageDraw.Draw(frame)
        for orb in self.ambient_orbs:
            ox = (orb[0] + math.sin(global_t * 1.1 + orb[1]) * 18) % self.w
            oy = (orb[1] - global_t * orb[2]) % self.h
            r_o = orb[3] * self.sc
            draw.ellipse([ox - r_o, oy - r_o, ox + r_o, oy + r_o], fill=theme["primary"])
        return frame, draw

    def _draw_3d_holo_badge(self, draw: ImageDraw.Draw, cx: float, cy: float, radius: float, holo_type: str, global_t: float, col: tuple):
        ang = global_t * 2.4
        # Outer radar ring
        draw.ellipse([cx - radius * 1.25, cy - radius * 1.25, cx + radius * 1.25, cy + radius * 1.25], outline=(51, 65, 85), width=1)
        if holo_type == "atom":
            draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(255, 255, 255))
            for tilt in [0.0, math.pi / 3.0, -math.pi / 3.0]:
                pts = []
                for s in range(12):
                    a = (2.0 * math.pi * s / 12.0) + ang
                    lx = radius * math.cos(a)
                    ly = radius * 0.36 * math.sin(a)
                    rx = lx * math.cos(tilt) - ly * math.sin(tilt)
                    ry = lx * math.sin(tilt) + ly * math.cos(tilt)
                    pts.append((cx + rx, cy + ry))
                draw.polygon(pts, outline=col)
        elif holo_type == "globe":
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=col, width=2)
            mer_w = abs(math.cos(ang)) * radius
            draw.ellipse([cx - mer_w, cy - radius, cx + mer_w, cy + radius], outline=col, width=1)
            draw.line([(cx - radius, cy), (cx + radius, cy)], fill=col, width=1)
        else:
            r = radius * 0.65
            verts = [(-r, -r, -r), (r, -r, -r), (r, r, -r), (-r, r, -r),
                     (-r, -r, r), (r, -r, r), (r, r, r), (-r, r, r)]
            ca, sa = math.cos(ang), math.sin(ang)
            proj = []
            for vx, vy, vz in verts:
                rx = vx * ca + vz * sa
                rz = -vx * sa + vz * ca
                ry = vy * 0.85 - rz * 0.35
                proj.append((cx + rx, cy + ry))
            for a, b in [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]:
                draw.line([proj[a], proj[b]], fill=col, width=2)

    def _spawn_confetti(self, cx: float, cy: float):
        cols = [(34, 211, 238), (250, 204, 21), (52, 211, 153), (244, 63, 94), (168, 85, 247), (255, 255, 255)]
        self.confetti = []
        for _ in range(42):
            ang = self.rng.uniform(0, 2.0 * math.pi)
            spd = self.rng.uniform(160.0, 520.0)
            self.confetti.append([cx, cy, math.cos(ang) * spd, math.sin(ang) * spd - 170.0, 1.0, self.rng.choice(cols)])

    def render_long_intro_hook(self, intro_prog: float, global_t: float) -> bytes:
        sc = self.sc
        theme = GAMING_THEMES["cyber"]
        frame, draw = self._get_bg_frame("cyber", theme, global_t)

        cx, cy = self.w * 0.5, self.h * 0.48
        for r_i in range(3):
            ring_r = ((intro_prog * 1.4 + r_i * 0.33) % 1.0) * max(self.w, self.h) * 0.65
            draw.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], outline=theme["primary"], width=3)

        pop = ease_out_back(min(1.0, intro_prog * 2.4))
        font_big = get_font(max(26, int(36 * sc * pop)))
        font_sub = get_font(max(16, int(21 * sc)))

        hook_l1 = "মাধ্যমিক ২০২৭: ১০০% কমন সাজেশন টেস্ট!"
        hook_l2 = "দেখা যাক তুমি কত স্কোর করতে পারো!"

        b1 = draw.textbbox((0, 0), hook_l1, font=font_big)
        draw.text(((self.w - (b1[2] - b1[0])) // 2, int(cy - 65 * sc)), hook_l1, font=font_big, fill=(250, 204, 21), stroke_width=3, stroke_fill=(0, 0, 0))

        b2 = draw.textbbox((0, 0), hook_l2, font=font_sub)
        draw.text(((self.w - (b2[2] - b2[0])) // 2, int(cy + 10 * sc)), hook_l2, font=font_sub, fill=(34, 211, 238))

        cd_num = max(1, 3 - int(intro_prog * 3))
        pill_txt = f"শুরু হচ্ছে {cd_num} সেকেন্ডে..."
        bp = draw.textbbox((0, 0), pill_txt, font=font_sub)
        pw = bp[2] - bp[0] + 36
        px0 = (self.w - pw) // 2
        py0 = int(cy + 65 * sc)
        draw.rounded_rectangle([px0, py0, px0 + pw, py0 + int(44 * sc)], radius=12, fill=(244, 63, 94))
        draw.text((px0 + 18, py0 + int(9 * sc)), pill_txt, font=font_sub, fill=(255, 255, 255))

        return frame.tobytes()

    def render_question_frame(self, q_idx: int, q_obj: dict, phase: str, phase_prog: float,
                              phase_elapsed: float, global_t: float, dt: float) -> bytes:
        sc = self.sc
        t_key = q_obj["theme_key"]
        theme = GAMING_THEMES[t_key]
        frame, draw = self._get_bg_frame(t_key, theme, global_t)

        rem_sec = q_obj["think_sec"] * (1.0 - phase_prog) if phase == "think" else 99.0
        shake_x = int(math.sin(global_t * 45.0) * 3.0 * sc) if (phase == "think" and rem_sec <= 1.8) else 0

        # 1. Top Modern Esports Telemetry Strip
        font_hud = get_font(max(13, int(16 * sc)))
        margin = int(self.w * 0.055)
        hud_y = int(self.h * 0.030)
        hud_h = int(36 * sc)

        if q_obj["is_secret_trap"]:
            lvl_txt = "বোনাস চ্যালেঞ্জ (উত্তর লুকানো!)"
            badge_col = (244, 63, 94)
        elif q_obj["is_boss"]:
            lvl_txt = "বস রাউন্ড • ডাবল XP"
            badge_col = (250, 204, 21)
        elif self.is_vertical and q_idx == 0:
            lvl_txt = "৯৯% ছাত্রছাত্রী ভুল করে!"
            badge_col = (244, 63, 94)
        else:
            lvl_txt = f"মাধ্যমিক ২০২৭ • লেভেল {q_obj['level']}"
            badge_col = theme["primary"]

        xp_val = min(q_idx, self.total_q - 1) * 100 + (100 if (phase == "reveal" and not q_obj["is_secret_trap"]) else 0)
        xp_txt = f"প্রশ্ন {q_idx + 1}/{self.total_q} • {xp_val} XP"

        lb_hud = draw.textbbox((0, 0), lvl_txt, font=font_hud)
        lw = lb_hud[2] - lb_hud[0] + 24
        draw.rounded_rectangle([margin, hud_y, margin + lw, hud_y + hud_h], radius=10, fill=theme["card_bg"], outline=badge_col, width=2)
        draw.text((margin + 12, hud_y + int(7 * sc)), lvl_txt, font=font_hud, fill=badge_col)

        xb = draw.textbbox((0, 0), xp_txt, font=font_hud)
        xw = xb[2] - xb[0] + 24
        draw.rounded_rectangle([self.w - margin - xw, hud_y, self.w - margin, hud_y + hud_h], radius=10, fill=theme["card_bg"], outline=theme["accent"], width=2)
        draw.text((self.w - margin - xw + 12, hud_y + int(7 * sc)), xp_txt, font=font_hud, fill=theme["accent"])

        # 2. Modern Chamfered Glass Question Arena Panel
        if self.is_vertical and q_idx == 0:
            y_shift = 0
        else:
            slide_t = ease_out_back(min(1.0, phase_elapsed / 0.36)) if phase == "read" else 1.0
            y_shift = int((1.0 - slide_t) * -130 * sc)

        q_top = int(self.h * 0.105) + y_shift
        q_h = int(self.h * (0.24 if self.is_vertical else 0.26))
        card_w = self.w - margin * 2

        if phase == "think" and rem_sec <= 1.8:
            q_border = (244, 63, 94)
            q_glow = (159, 18, 57)
        elif q_obj["is_secret_trap"] or q_obj["is_boss"]:
            q_border = (250, 204, 21)
            q_glow = (161, 98, 7)
        else:
            q_border = theme["primary"]
            q_glow = theme["secondary"]

        draw_modern_esports_panel(draw, margin + shake_x, q_top, margin + card_w + shake_x, q_top + q_h,
                                  theme["card_bg"], q_border, q_glow, sc)

        holo_r = 15 * sc
        self._draw_3d_holo_badge(draw, margin + card_w - holo_r - 16 * sc + shake_x, q_top + holo_r + 16 * sc, holo_r, q_obj["holo_type"], global_t, theme["primary"])

        font_q = get_font(max(20, int(25 * sc)))
        q_lines = wrap_lines(draw, q_obj["q"], font_q, card_w - int(72 * sc))
        lh = int(font_q.size * 1.40)
        ty_start = q_top + int(10 * sc) + (q_h - len(q_lines) * lh) // 2
        for li, ln in enumerate(q_lines):
            lb = draw.textbbox((0, 0), ln, font=font_q)
            draw.text(((self.w - (lb[2] - lb[0])) // 2 + shake_x, ty_start + li * lh), ln, font=font_q, fill=(255, 255, 255))

        # 3. Chromatic Energy Laser Timer Bar
        timer_y = q_top + q_h + int(self.h * 0.024)
        timer_h = max(16, int(20 * sc))
        draw.rounded_rectangle([margin, timer_y, margin + card_w, timer_y + timer_h], radius=timer_h // 2, fill=(15, 23, 42), outline=(51, 65, 85), width=2)

        if phase == "read":
            frac, t_col, sec_lbl = 1.0, theme["primary"], f"{q_obj['think_sec']:.1f}s"
        elif phase == "think":
            frac = max(0.0, 1.0 - phase_prog)
            if rem_sec <= 1.8:
                t_col = (244, 63, 94)
            elif frac <= 0.55:
                t_col = (250, 204, 21)
            else:
                t_col = theme["primary"]
            sec_lbl = f"{max(0.0, rem_sec):.1f}s"
        else:
            frac = 1.0
            t_col = (244, 63, 94) if q_obj["is_secret_trap"] else (16, 185, 129)
            sec_lbl = "লক করা হলো!" if q_obj["is_secret_trap"] else "সঠিক উত্তর!"

        fill_w = int(card_w * frac)
        if fill_w > 12:
            draw.rounded_rectangle([margin + 2, timer_y + 2, margin + fill_w - 2, timer_y + timer_h - 2], radius=timer_h // 2, fill=t_col)
            draw.ellipse([margin + fill_w - 10, timer_y - 3, margin + fill_w + 6, timer_y + timer_h + 3], fill=(255, 255, 255))

        font_tm = get_font(max(12, int(14 * sc)))
        sb = draw.textbbox((0, 0), sec_lbl, font=font_tm)
        draw.text(((self.w - (sb[2] - sb[0])) // 2, timer_y), sec_lbl, font=font_tm, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))

        # 4. Modern Esports Option Plates (A, B, C, D)
        opts = q_obj["opts"]
        ans_idx = q_obj["ans"]
        labels = ["A", "B", "C", "D"]
        font_opt = get_font(max(18, int(22 * sc)))
        font_lbl = get_font(max(16, int(19 * sc)))

        opts_top = timer_y + timer_h + int(self.h * 0.028)
        opts_area_h = int(self.h * (0.44 if self.is_vertical else 0.42))

        if self.is_vertical:
            gap = int(12 * sc)
            box_h = (opts_area_h - gap * 3) // 4
            for i in range(4):
                if q_idx == 0:
                    x_off = 0
                else:
                    stagger = ease_out_back(min(1.0, max(0.0, (phase_elapsed - i * 0.07) / 0.30))) if phase == "read" else 1.0
                    x_off = int((1.0 - stagger) * (170 if i % 2 == 0 else -170) * sc)
                oy0 = opts_top + i * (box_h + gap)
                self._draw_modern_option(draw, margin + x_off, oy0, margin + card_w + x_off, oy0 + box_h,
                                         labels[i], opts[i], i == ans_idx, q_obj["is_secret_trap"], phase, theme, font_lbl, font_opt, sc)
        else:
            gap_x = int(18 * sc)
            gap_y = int(16 * sc)
            box_w = (card_w - gap_x) // 2
            box_h = (opts_area_h - gap_y) // 2
            for i in range(4):
                stagger = ease_out_back(min(1.0, max(0.0, (phase_elapsed - i * 0.07) / 0.30))) if phase == "read" else 1.0
                y_off = int((1.0 - stagger) * 85 * sc)
                col_i, row_i = i % 2, i // 2
                ox0 = margin + col_i * (box_w + gap_x)
                oy0 = opts_top + row_i * (box_h + gap_y) + y_off
                self._draw_modern_option(draw, ox0, oy0, ox0 + box_w, oy0 + box_h,
                                         labels[i], opts[i], i == ans_idx, q_obj["is_secret_trap"], phase, theme, font_lbl, font_opt, sc)

        # 5. Reveal Phase: Psychological Comment Trap OR Confetti + XP + Fact
        if phase == "reveal":
            if q_obj["is_secret_trap"]:
                trap_h = int(96 * sc)
                trap_y = opts_top + (opts_area_h - trap_h) // 2
                draw_modern_esports_panel(draw, margin + 10, trap_y, margin + card_w - 10, trap_y + trap_h,
                                          (15, 23, 42), (250, 204, 21), (244, 63, 94), sc)
                font_tr1 = get_font(max(18, int(23 * sc)))
                font_tr2 = get_font(max(14, int(17 * sc)))
                t1 = "উত্তর লুকানো আছে!"
                t2 = "সঠিক উত্তর (A, B, C বা D) কমেন্টে লেখো!"
                b_t1 = draw.textbbox((0, 0), t1, font=font_tr1)
                b_t2 = draw.textbbox((0, 0), t2, font=font_tr2)
                draw.text(((self.w - (b_t1[2] - b_t1[0])) // 2, trap_y + int(16 * sc)), t1, font=font_tr1, fill=(250, 204, 21))
                draw.text(((self.w - (b_t2[2] - b_t2[0])) // 2, trap_y + int(54 * sc)), t2, font=font_tr2, fill=(255, 255, 255))
            else:
                if self.last_confetti_q != q_idx:
                    self.last_confetti_q = q_idx
                    self._spawn_confetti(self.w * 0.5, opts_top + opts_area_h * 0.5)

                for p in self.confetti:
                    p[0] += p[2] * dt
                    p[1] += p[3] * dt
                    p[3] += 500.0 * dt
                    p[4] -= dt * 0.85
                    if p[4] > 0:
                        pr = max(2.0, 5.0 * sc * p[4])
                        draw.rectangle([p[0] - pr, p[1] - pr, p[0] + pr, p[1] + pr], fill=p[5])

                xp_pop = "+২০০ XP বস ক্লিয়ার!" if q_obj["is_boss"] else "+১০০ XP সঠিক উত্তর!"
                font_xp = get_font(max(16, int(20 * sc)))
                xb2 = draw.textbbox((0, 0), xp_pop, font=font_xp)
                float_y = int(opts_top - 12 * sc - min(22 * sc, phase_elapsed * 30 * sc))
                draw.text(((self.w - (xb2[2] - xb2[0])) // 2, float_y), xp_pop, font=font_xp, fill=(52, 211, 153), stroke_width=3, stroke_fill=(0, 0, 0))

                if q_obj["fact"]:
                    fact_y = opts_top + opts_area_h + int(self.h * 0.022)
                    font_fc = get_font(max(13, int(16 * sc)))
                    fc_txt = f"তথ্য: {q_obj['fact']}"
                    fb = draw.textbbox((0, 0), fc_txt, font=font_fc)
                    fw = min(card_w, fb[2] - fb[0] + 28)
                    fx0 = (self.w - fw) // 2
                    draw.rounded_rectangle([fx0, fact_y, fx0 + fw, fact_y + int(36 * sc)], radius=10, fill=theme["card_bg"], outline=(52, 211, 153), width=2)
                    draw.text((fx0 + 14, fact_y + int(7 * sc)), fc_txt[:65], font=font_fc, fill=(248, 250, 252))

        return frame.tobytes()

    def _draw_modern_option(self, draw, x0, y0, x1, y1, letter, text, is_correct, is_secret_trap, phase, theme, font_lbl, font_opt, sc):
        if phase == "reveal" and not is_secret_trap:
            if is_correct:
                bg_col, border_col, txt_col, pill_col = (6, 78, 59), (52, 211, 153), (255, 255, 255), (16, 185, 129)
            else:
                bg_col, border_col, txt_col, pill_col = (10, 14, 24), (30, 41, 59), (71, 85, 105), (30, 41, 59)
        else:
            bg_col, border_col, txt_col, pill_col = theme["opt_bg"], theme["secondary"], (248, 250, 252), theme["secondary"]

        draw.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=bg_col, outline=border_col, width=3)
        # Left neon energy accent stripe
        draw.rounded_rectangle([x0 + 4, y0 + 10, x0 + 8, y1 - 10], radius=2, fill=border_col)

        bh = y1 - y0
        cr = int(min(bh * 0.34, 21 * sc))
        cx = x0 + cr + int(18 * sc)
        cy = y0 + bh // 2
        # Hex/diamond-style modern badge circle with white ring
        draw.ellipse([cx - cr - 2, cy - cr - 2, cx + cr + 2, cy + cr + 2], outline=(255, 255, 255), width=1)
        draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=pill_col)

        lb = draw.textbbox((0, 0), letter, font=font_lbl)
        draw.text((cx - (lb[2] - lb[0]) // 2, cy - (lb[3] - lb[1]) // 2 - 2), letter, font=font_lbl, fill=(255, 255, 255))

        tb = draw.textbbox((0, 0), text, font=font_opt)
        draw.text((cx + cr + int(14 * sc), cy - (tb[3] - tb[1]) // 2 - 2), text, font=font_opt, fill=txt_col)

    def render_outro_comment_trap(self, outro_prog: float, global_t: float) -> bytes:
        sc = self.sc
        theme = GAMING_THEMES["royal"]
        frame, draw = self._get_bg_frame("royal", theme, global_t)

        margin = int(self.w * 0.07)
        box_w = self.w - margin * 2
        box_y = int(self.h * 0.16)
        box_h = int(self.h * 0.66)

        draw_modern_esports_panel(draw, margin, box_y, margin + box_w, box_y + box_h,
                                  theme["card_bg"], (250, 204, 21), (249, 115, 22), sc)

        font_hdr = get_font(max(20, int(27 * sc)))
        font_row = get_font(max(15, int(20 * sc)))
        font_cta = get_font(max(15, int(20 * sc)))

        hdr = "কমেন্টে তোমার র‍্যাঙ্ক জানাও!"
        bh = draw.textbbox((0, 0), hdr, font=font_hdr)
        draw.text(((self.w - (bh[2] - bh[0])) // 2, box_y + int(30 * sc)), hdr, font=font_hdr, fill=(250, 204, 21))

        max_xp = (self.total_q - 1) * 100
        ranks = [
            (f"0 - {int(max_xp * 0.4)} XP  ->  আরও রিভিশন দরকার", (148, 163, 184)),
            (f"{int(max_xp * 0.5)} - {int(max_xp * 0.8)} XP  ->  বোর্ড পরীক্ষার জন্য প্রস্তুত", (34, 211, 238)),
            (f"{max_xp}+ XP + বোনাস  ->  মাধ্যমিক ২০২৭ টপার!", (52, 211, 153))
        ]
        for idx, (r_txt, r_col) in enumerate(ranks):
            ry = box_y + int((108 + idx * 62) * sc)
            br = draw.textbbox((0, 0), r_txt, font=font_row)
            draw.text(((self.w - (br[2] - br[0])) // 2, ry), r_txt, font=font_row, fill=r_col)

        pulse = 1.0 + 0.04 * math.sin(global_t * 10.0)
        cta_txt = "তোমার XP ও বোনাস উত্তর কমেন্ট করো!"
        bc = draw.textbbox((0, 0), cta_txt, font=font_cta)
        cw = int((bc[2] - bc[0] + 36) * pulse)
        cx0 = (self.w - cw) // 2
        cy0 = box_y + box_h - int(85 * sc)
        draw.rounded_rectangle([cx0, cy0, cx0 + cw, cy0 + int(48 * sc)], radius=14, fill=(244, 63, 94))
        draw.text(((self.w - (bc[2] - bc[0])) // 2, cy0 + int(12 * sc)), cta_txt, font=font_cta, fill=(255, 255, 255))

        return frame.tobytes()


# ===========================================================================
# 4. LOW-RAM LONG-VIDEO RENDER PIPELINE
# ===========================================================================
def run_ultra_quiz_v2_job(job_id: str, ratio: str, custom_raw: str):
    job = JOBS[job_id]
    wav_path = None
    try:
        cleanup_old_files(900)
        is_vertical = (ratio == "9:16")
        questions = smart_parse_and_direct(custom_raw, is_vertical)
        out_w, out_h = (540, 960) if is_vertical else (960, 540)
        fps = 14
        dt = 1.0 / float(fps)
        total_q = len(questions)

        intro_dur = 0.0 if is_vertical else 1.5
        outro_dur = 2.6

        timeline = []
        chapters = ["00:00 মাধ্যমিক ২০২৭ সাজেশন কুইজ শুরু"]
        t_cursor = intro_dur

        for idx, q_obj in enumerate(questions):
            if idx % 5 == 0 and not q_obj["is_secret_trap"]:
                mins = int(t_cursor // 60)
                secs = int(t_cursor % 60)
                chapters.append(f"{mins:02d}:{secs:02d} লেভেল {q_obj['level']} প্রশ্নাবলি")
            elif q_obj["is_secret_trap"]:
                mins = int(t_cursor // 60)
                secs = int(t_cursor % 60)
                chapters.append(f"{mins:02d}:{secs:02d} বোনাস চ্যালেঞ্জ প্রশ্ন")

            t_start = t_cursor
            t_think = t_start + q_obj["read_sec"]
            t_reveal = t_think + q_obj["think_sec"]
            t_end = t_reveal + q_obj["reveal_sec"]
            timeline.append((q_obj, t_start, t_think, t_reveal, t_end))
            t_cursor = t_end

        total_dur = t_cursor + outro_dur
        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_bengali_sfx.wav")
        job["progress"] = 8
        job["message"] = f"Generating Esports Sound Effects ({total_q} Bengali Questions)..."
        build_gaming_sfx_track(intro_dur, timeline, outro_dur, total_dur, wav_path)
        gc.collect()

        out_name = f"MADHYAMIK_2027_ESPORTS_{total_q}Q_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", wav_path,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        renderer = ModernEsportsBengaliRenderer(out_w, out_h, total_q)
        global_t = 0.0

        if intro_dur > 0:
            intro_frames = int(intro_dur * fps)
            for f in range(intro_frames):
                raw_bytes = renderer.render_long_intro_hook(f / max(1, intro_frames - 1), global_t)
                proc.stdin.write(raw_bytes)
                global_t += dt

        for idx, (q_obj, t_start, t_think, t_reveal, t_end) in enumerate(timeline):
            for p_name, p_dur in [("read", q_obj["read_sec"]), ("think", q_obj["think_sec"]), ("reveal", q_obj["reveal_sec"])]:
                n_frames = max(1, int(p_dur * fps))
                for f in range(n_frames):
                    raw_bytes = renderer.render_question_frame(idx, q_obj, p_name, f / max(1, n_frames - 1), f * dt, global_t, dt)
                    proc.stdin.write(raw_bytes)
                    global_t += dt

            job["progress"] = 12 + int(((idx + 1) / total_q) * 82)
            job["message"] = f"Rendering Modern Bengali Arena ({idx + 1} / {total_q})..."
            if (idx + 1) % 10 == 0:
                gc.collect()

        outro_frames = int(outro_dur * fps)
        for f in range(outro_frames):
            raw_bytes = renderer.render_outro_comment_trap(f / max(1, outro_frames - 1), global_t)
            proc.stdin.write(raw_bytes)
            global_t += dt

        proc.stdin.close()
        proc.wait()

        if wav_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except Exception:
                pass
        gc.collect()

        size_mb = round(os.path.getsize(out_path) / (1024.0 * 1024.0), 1)
        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["size_mb"] = size_mb
        job["chapters"] = "\n".join(chapters)
        job["message"] = f"Completed & Auto-Downloading ({size_mb} MB)!"

    except Exception as exc:
        job["status"] = "error"
        job["message"] = f"Render Error: {exc}"

@app.route("/")
def serve_home():
    return send_file(os.path.join(BASE_DIR, "index.html"))

@app.route("/api/ping")
def api_ping():
    return jsonify({"ok": True})

@app.route("/api/start", methods=["POST", "OPTIONS"])
def api_start():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    job_id = uuid.uuid4().hex[:10]
    data = request.get_json(force=True) if request.is_json else request.form
    ratio = data.get("ratio", "9:16")
    custom_raw = data.get("custom_raw", "")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Starting Modern Esports Bengali Studio..."}
    threading.Thread(
        target=run_ultra_quiz_v2_job,
        args=(job_id, ratio, custom_raw),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    return jsonify(JOBS.get(job_id, {"status": "error", "message": "Server restarted — click Generate again!"}))

@app.route("/api/video/<job_id>")
def api_video(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("file_path") or not os.path.exists(job["file_path"]):
        return "Not ready", 404
    return send_file(job["file_path"], as_attachment=(request.args.get("dl") == "1"), mimetype="video/mp4")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
