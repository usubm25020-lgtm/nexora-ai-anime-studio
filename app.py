import os
import re
import time
import math
import uuid
import wave
import random
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
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
JOBS = {}

def cleanup_old_files(max_age_sec: int = 1800):
    """Automatically deletes temporary WAV and old MP4 files older than 30 mins to keep Render storage empty."""
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
# 1. ULTRA-GAMING VISUAL THEMES & AUTO-DIRECTOR ANALYZER
# ===========================================================================
GAMING_THEMES = {
    "cyber": {
        "name": "CYBER ARENA",
        "bg_top": (10, 18, 44),
        "bg_bot": (4, 7, 18),
        "grid": (22, 44, 86),
        "primary": (34, 211, 238),    # Neon Cyan
        "secondary": (168, 85, 247),  # Electric Purple
        "accent": (250, 204, 21)      # Cyber Gold
    },
    "royal": {
        "name": "ROYAL GOLD",
        "bg_top": (30, 22, 10),
        "bg_bot": (6, 5, 4),
        "grid": (62, 44, 18),
        "primary": (250, 204, 21),    # Pure Gold
        "secondary": (249, 115, 22),  # Amber Blaze
        "accent": (56, 189, 248)      # Sky Highlight
    },
    "crimson": {
        "name": "BOSS RUSH",
        "bg_top": (36, 10, 24),
        "bg_bot": (6, 3, 10),
        "grid": (68, 18, 44),
        "primary": (244, 63, 94),     # Neon Crimson
        "secondary": (232, 121, 249), # Plasma Magenta
        "accent": (250, 204, 21)      # Gold
    },
    "emerald": {
        "name": "MATRIX PRO",
        "bg_top": (8, 30, 24),
        "bg_bot": (3, 9, 8),
        "grid": (16, 60, 48),
        "primary": (16, 185, 129),    # Neon Emerald
        "secondary": (34, 211, 238),  # Aqua
        "accent": (250, 204, 21)      # Gold
    }
}

DEFAULT_SAMPLE_QUESTIONS = [
    {"q": "Which planet rotates clockwise so the Sun rises in the west?", "opts": ["Mercury", "Venus", "Mars", "Uranus"], "ans": 1, "fact": "Venus spins backward compared to almost every other planet!"},
    {"q": "What is the SI unit of electrical resistance?", "opts": ["Ampere", "Volt", "Ohm", "Watt"], "ans": 2, "fact": "Named after German physicist Georg Simon Ohm."},
    {"q": "I speak without a mouth and hear without ears. What am I?", "opts": ["A Shadow", "An Echo", "The Wind", "A Dream"], "ans": 1, "fact": "Sound waves bouncing off hard surfaces create an echo!"},
    {"q": "If a train runs at 90 km/h, how far does it go in 10 seconds?", "opts": ["150 m", "200 m", "250 m", "300 m"], "ans": 2, "fact": "90 km/h = 25 m/s, so 25 x 10 = 250 meters."},
    {"q": "What has 13 hearts, but no lungs or stomach?", "opts": ["An Octopus", "A Deck of Cards", "A Starfish", "A Jellyfish"], "ans": 1, "fact": "Every standard deck has 13 cards in the Hearts suit!"}
]

def smart_parse_and_direct(raw_text: str, is_vertical: bool):
    items = []
    for ln in str(raw_text or "").splitlines():
        ln = ln.strip()
        if not ln or "|" not in ln:
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) >= 6:
            q_txt = re.sub(r"^Q\d*\s*[:.\-]\s*", "", parts[0], flags=re.I).strip()
            opts = parts[1:5]
            try:
                ans_idx = max(0, min(3, int(re.sub(r"[^0-9]", "", parts[5])) - 1))
            except Exception:
                ans_idx = 0
            fact_txt = parts[6].strip() if len(parts) >= 7 else ""
            items.append({"q": q_txt, "opts": opts, "ans": ans_idx, "fact": fact_txt})

    if not items:
        items = [dict(x) for x in DEFAULT_SAMPLE_QUESTIONS]

    if is_vertical and len(items) > 4:
        items = items[:4]
    elif len(items) < 2:
        items.append(dict(DEFAULT_SAMPLE_QUESTIONS[-1]))

    directed = []
    theme_keys = list(GAMING_THEMES.keys())
    total_n = len(items)

    for idx, item in enumerate(items):
        q_low = item["q"].lower()
        word_cnt = len(item["q"].split())
        is_secret_trap = (idx == total_n - 1)
        is_boss = ((idx + 1) % 5 == 0) and not is_secret_trap

        has_math = bool(re.search(r"[0-9%+\-*/=]|calculate|solve|how many|equation|ratio", q_low))
        if is_vertical:
            think_sec = 4.2 if has_math else 3.4
            read_sec = 1.3
            reveal_sec = 2.6 if is_secret_trap else 2.1
        else:
            if has_math or word_cnt >= 15:
                think_sec = 7.0
            elif word_cnt >= 10:
                think_sec = 5.2
            else:
                think_sec = 4.2
            read_sec = max(1.5, min(2.8, word_cnt * 0.14))
            reveal_sec = 3.2 if is_secret_trap else (2.9 if item["fact"] else 2.3)

        if is_secret_trap:
            t_key, holo_type = "crimson", "cube"
        elif is_boss:
            t_key, holo_type = "crimson", "atom"
        elif any(k in q_low for k in ["space", "planet", "atom", "physics", "science", "chem", "bio", "energy", "light", "si unit", "gas", "vitamin"]):
            t_key, holo_type = "cyber", "atom"
        elif any(k in q_low for k in ["history", "king", "india", "world", "capital", "country", "war", "earth", "coffee"]):
            t_key, holo_type = "royal", "globe"
        elif any(k in q_low for k in ["riddle", "brain", "logic", "number", "math", "train", "father", "hearts"]):
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
# 2. HIGH-ENERGY GAMING SYNTHWAVE + HOOK IMPACT, TICKS & MYSTERY LOCK AUDIO
# ===========================================================================
def build_gaming_sfx_track(intro_dur: float, timeline: list, outro_dur: float, total_dur: float, out_wav: str):
    rate = 22050
    n = int(total_dur * rate)
    t = np.linspace(0, total_dur, n, endpoint=False, dtype=np.float32)

    bpm = 128.0
    beat_dur = 60.0 / bpm
    beat_pos = (t % beat_dur) / beat_dur

    kick = np.sin(2.0 * np.pi * (54.0 + 60.0 * np.exp(-beat_pos * 18.0)) * (t % beat_dur)) * np.exp(-beat_pos * 8.5) * 0.25
    bass_notes = [110.0, 110.0, 130.81, 146.83, 98.0, 98.0, 130.81, 164.81]
    b_idx = (t / (beat_dur * 2.0)).astype(np.int32)
    b_freq = np.array([bass_notes[i % len(bass_notes)] for i in b_idx], dtype=np.float32)
    bass = np.sin(2.0 * np.pi * b_freq * t) * 0.11

    audio = kick + bass
    rng = np.random.default_rng(2026)

    imp_len = min(n, int(0.65 * rate))
    if imp_len > 0:
        ti = np.linspace(0, imp_len / float(rate), imp_len, endpoint=False, dtype=np.float32)
        boom = np.sin(2.0 * np.pi * (130.0 * np.exp(-ti * 8.0) + 40.0) * ti) * np.exp(-ti * 5.0) * 0.55
        audio[:imp_len] += boom

    for q_obj, t_start, t_think_start, t_reveal_start, t_end in timeline:
        s0 = int(t_start * rate)
        w_len = int(0.26 * rate)
        if s0 + w_len < n:
            tw = np.linspace(0, 0.26, w_len, endpoint=False, dtype=np.float32)
            noise = rng.uniform(-1.0, 1.0, w_len).astype(np.float32)
            env = np.sin(np.pi * (tw / 0.26)) ** 1.5
            audio[s0:s0 + w_len] += noise * env * 0.18

        cur_t = t_think_start + 0.15
        tick_i = 0
        while cur_t < t_reveal_start - 0.10:
            rem = t_reveal_start - cur_t
            step = 0.22 if rem <= 1.8 else 0.45
            st = int(cur_t * rate)
            t_len = int(0.05 * rate)
            if st + t_len < n:
                tt = np.linspace(0, 0.05, t_len, endpoint=False, dtype=np.float32)
                freq = 1560.0 if rem <= 1.8 else (1140.0 if tick_i % 2 == 0 else 940.0)
                tk = np.sin(2.0 * np.pi * freq * tt) * np.exp(-tt * 78.0) * 0.36
                audio[st:st + t_len] += tk
            cur_t += step
            tick_i += 1

        sr = int(t_reveal_start * rate)
        r_len = int(0.75 * rate)
        if sr + r_len < n:
            tr = np.linspace(0, 0.75, r_len, endpoint=False, dtype=np.float32)
            if q_obj["is_secret_trap"]:
                p1 = np.sin(2.0 * np.pi * 622.25 * tr) * np.exp(-tr * 8.0)
                p2 = np.sin(2.0 * np.pi * 587.33 * np.maximum(0, tr - 0.16)) * np.exp(-np.maximum(0, tr - 0.16) * 7.0)
                audio[sr:sr + r_len] += (p1 + p2) * 0.34
            else:
                c1 = np.sin(2.0 * np.pi * 1046.50 * tr) * np.exp(-tr * 6.0)
                c2 = np.sin(2.0 * np.pi * 1318.51 * np.maximum(0, tr - 0.06)) * np.exp(-np.maximum(0, tr - 0.06) * 5.5)
                c3 = np.sin(2.0 * np.pi * 1567.98 * np.maximum(0, tr - 0.12)) * np.exp(-np.maximum(0, tr - 0.12) * 5.0)
                c4 = np.sin(2.0 * np.pi * 2093.00 * np.maximum(0, tr - 0.18)) * np.exp(-np.maximum(0, tr - 0.18) * 4.2)
                audio[sr:sr + r_len] += (c1 + c2 + c3 + c4) * 0.24

    so = int((total_dur - outro_dur) * rate)
    o_len = min(n - so, int(0.9 * rate))
    if o_len > 0:
        to = np.linspace(0, o_len / float(rate), o_len, endpoint=False, dtype=np.float32)
        fanfare = (
            np.sin(2.0 * np.pi * 523.25 * to) +
            np.sin(2.0 * np.pi * 783.99 * to) +
            np.sin(2.0 * np.pi * 1046.50 * to)
        ) * np.exp(-to * 3.5) * 0.22
        audio[so:so + o_len] += fanfare

    fade_s = min(int(0.4 * rate), n // 6)
    if fade_s > 0:
        audio[-fade_s:] *= np.linspace(1, 0, fade_s)

    pcm = (np.clip(audio, -0.95, 0.95) * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


# ===========================================================================
# 3. ULTRA-GAMING RENDERER (HOOK INTRO + 3D HOLOGRAM WIDGET + COMMENT TRAP)
# ===========================================================================
_FONT_CACHE = {}
def get_font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    ]:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[size] = f
                return f
            except Exception:
                pass
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

class UltraGamingQuizV2Renderer:
    def __init__(self, w: int, h: int, total_q: int):
        self.w = w
        self.h = h
        self.total_q = total_q
        self.is_vertical = (h > w)
        self.rng = random.Random(999)

        self.ambient_orbs = [
            [self.rng.uniform(0, w), self.rng.uniform(0, h), self.rng.uniform(22, 55), self.rng.uniform(2, 4.5)]
            for _ in range(30)
        ]
        self.confetti = []
        self.last_confetti_q = -1

    def _draw_cyber_bg(self, theme: dict, global_t: float, sc: float) -> tuple:
        grad = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        for ch in range(3):
            col_line = np.linspace(theme["bg_top"][ch], theme["bg_bot"][ch], self.h, dtype=np.uint8)[:, None]
            grad[:, :, ch] = np.repeat(col_line, self.w, axis=1)
        frame = Image.fromarray(grad, mode="RGB")
        draw = ImageDraw.Draw(frame)

        grid_step = int(48 * sc)
        gx_off = int((global_t * 40) % grid_step)
        gy_off = int((global_t * 28) % grid_step)
        for gx in range(-grid_step, self.w + grid_step, grid_step):
            draw.line([(gx + gx_off, 0), (gx + gx_off, self.h)], fill=theme["grid"], width=1)
        for gy in range(-grid_step, self.h + grid_step, grid_step):
            draw.line([(0, gy + gy_off), (self.w, gy + gy_off)], fill=theme["grid"], width=1)

        for orb in self.ambient_orbs:
            ox = (orb[0] + math.sin(global_t * 0.9 + orb[1]) * 18) % self.w
            oy = (orb[1] - global_t * orb[2]) % self.h
            r_o = orb[3] * sc
            draw.ellipse([ox - r_o, oy - r_o, ox + r_o, oy + r_o], fill=theme["primary"])

        return frame, draw

    def _draw_3d_holo_badge(self, draw: ImageDraw.Draw, cx: float, cy: float, radius: float, holo_type: str, global_t: float, col: tuple):
        ang = global_t * 2.2
        if holo_type == "atom":
            draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(255, 255, 255))
            for tilt in [0.0, math.pi / 3.0, -math.pi / 3.0]:
                pts = []
                for s in range(16):
                    a = (2.0 * math.pi * s / 16.0) + ang
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
        for _ in range(70):
            ang = self.rng.uniform(0, 2.0 * math.pi)
            spd = self.rng.uniform(160.0, 560.0)
            self.confetti.append([cx, cy, math.cos(ang) * spd, math.sin(ang) * spd - 180.0, 1.0, self.rng.choice(cols)])

    def render_intro_hook(self, intro_prog: float, global_t: float) -> bytes:
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0
        theme = GAMING_THEMES["cyber"]
        frame, draw = self._draw_cyber_bg(theme, global_t, sc)

        cx, cy = self.w * 0.5, self.h * 0.48
        for r_i in range(3):
            ring_r = ((intro_prog * 1.4 + r_i * 0.33) % 1.0) * max(self.w, self.h) * 0.65
            draw.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], outline=theme["primary"], width=3)

        pop = ease_out_back(min(1.0, intro_prog * 2.4))
        font_big = get_font(max(26, int(38 * sc * pop)))
        font_sub = get_font(max(16, int(21 * sc)))

        hook_l1 = "ONLY 1% GENIUS" if self.is_vertical else "ONLY 1% GENIUS CAN SCORE 100%"
        hook_l2 = "CAN PASS THIS!" if self.is_vertical else "BEAT THE LASER TIMER • CLAIM YOUR IQ RANK"

        b1 = draw.textbbox((0, 0), hook_l1, font=font_big)
        draw.text(((self.w - (b1[2] - b1[0])) // 2, int(cy - 65 * sc)), hook_l1, font=font_big, fill=(250, 204, 21), stroke_width=3, stroke_fill=(0, 0, 0))

        b2 = draw.textbbox((0, 0), hook_l2, font=font_sub)
        draw.text(((self.w - (b2[2] - b2[0])) // 2, int(cy + 10 * sc)), hook_l2, font=font_sub, fill=(34, 211, 238))

        cd_num = max(1, 3 - int(intro_prog * 3))
        pill_txt = f"STARTING IN {cd_num}..."
        bp = draw.textbbox((0, 0), pill_txt, font=font_sub)
        pw = bp[2] - bp[0] + 36
        px0 = (self.w - pw) // 2
        py0 = int(cy + 65 * sc)
        draw.rounded_rectangle([px0, py0, px0 + pw, py0 + int(42 * sc)], radius=12, fill=(244, 63, 94))
        draw.text((px0 + 18, py0 + int(9 * sc)), pill_txt, font=font_sub, fill=(255, 255, 255))

        return frame.tobytes()

    def render_question_frame(self, q_idx: int, q_obj: dict, phase: str, phase_prog: float,
                              phase_elapsed: float, global_t: float, dt: float) -> bytes:
        theme = GAMING_THEMES[q_obj["theme_key"]]
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0
        frame, draw = self._draw_cyber_bg(theme, global_t, sc)

        rem_sec = q_obj["think_sec"] * (1.0 - phase_prog) if phase == "think" else 99.0
        shake_x = int(math.sin(global_t * 45.0) * 3.0 * sc) if (phase == "think" and rem_sec <= 1.8) else 0

        font_hud = get_font(max(13, int(16 * sc)))
        margin = int(self.w * 0.055)
        hud_y = int(self.h * 0.032)

        if q_obj["is_secret_trap"]:
            lvl_txt = "🔒 FINAL SECRET CHALLENGE (NO ANSWER!)"
        elif q_obj["is_boss"]:
            lvl_txt = "👑 BOSS ROUND • 2X XP"
        else:
            lvl_txt = f"LVL {q_obj['level']} • STREAK {q_idx + 1}x 🔥"

        xp_val = min(q_idx, self.total_q - 1) * 100 + (100 if (phase == "reveal" and not q_obj["is_secret_trap"]) else 0)
        xp_txt = f"Q {q_idx + 1}/{self.total_q} • {xp_val} XP"

        draw.rounded_rectangle([margin, hud_y, margin + int(275 * sc), hud_y + int(32 * sc)], radius=8, fill=(15, 23, 42), outline=theme["primary"], width=2)
        draw.text((margin + 10, hud_y + int(6 * sc)), lvl_txt, font=font_hud, fill=theme["primary"])

        xb = draw.textbbox((0, 0), xp_txt, font=font_hud)
        xw = xb[2] - xb[0] + 22
        draw.rounded_rectangle([self.w - margin - xw, hud_y, self.w - margin, hud_y + int(32 * sc)], radius=8, fill=(15, 23, 42), outline=theme["accent"], width=2)
        draw.text((self.w - margin - xw + 11, hud_y + int(6 * sc)), xp_txt, font=font_hud, fill=theme["accent"])

        slide_t = ease_out_back(min(1.0, phase_elapsed / 0.38)) if phase == "read" else 1.0
        y_shift = int((1.0 - slide_t) * -130 * sc)

        q_top = int(self.h * 0.11) + y_shift
        q_h = int(self.h * (0.23 if self.is_vertical else 0.25))
        card_w = self.w - margin * 2

        if phase == "think" and rem_sec <= 1.8 and int(global_t * 10) % 2 == 0:
            q_border = (244, 63, 94)
        elif q_obj["is_secret_trap"] or q_obj["is_boss"]:
            q_border = (250, 204, 21)
        else:
            q_border = theme["primary"]

        draw.rounded_rectangle([margin + shake_x, q_top, margin + card_w + shake_x, q_top + q_h], radius=16, fill=(11, 17, 32), outline=q_border, width=3)

        holo_r = 16 * sc
        self._draw_3d_holo_badge(draw, margin + card_w - holo_r - 14 * sc + shake_x, q_top + holo_r + 12 * sc, holo_r, q_obj["holo_type"], global_t, theme["primary"])

        font_q = get_font(max(19, int(25 * sc)))
        q_lines = wrap_lines(draw, q_obj["q"], font_q, card_w - int(68 * sc))
        lh = int(font_q.size * 1.32)
        ty_start = q_top + (q_h - len(q_lines) * lh) // 2
        for li, ln in enumerate(q_lines):
            lb = draw.textbbox((0, 0), ln, font=font_q)
            draw.text(((self.w - (lb[2] - lb[0])) // 2 + shake_x, ty_start + li * lh), ln, font=font_q, fill=(248, 250, 252))

        timer_y = q_top + q_h + int(self.h * 0.024)
        timer_h = max(14, int(18 * sc))
        draw.rounded_rectangle([margin, timer_y, margin + card_w, timer_y + timer_h], radius=timer_h // 2, fill=(22, 30, 52))

        if phase == "read":
            frac, t_col, sec_lbl = 1.0, theme["primary"], f"{q_obj['think_sec']:.1f}s"
        elif phase == "think":
            frac = max(0.0, 1.0 - phase_prog)
            t_col = (244, 63, 94) if rem_sec <= 1.8 else theme["accent"]
            sec_lbl = f"{max(0.0, rem_sec):.1f}s"
        else:
            frac = 1.0
            t_col = (244, 63, 94) if q_obj["is_secret_trap"] else (16, 185, 129)
            sec_lbl = "LOCKED!" if q_obj["is_secret_trap"] else "SOLVED!"

        fill_w = int(card_w * frac)
        if fill_w > 10:
            draw.rounded_rectangle([margin, timer_y, margin + fill_w, timer_y + timer_h], radius=timer_h // 2, fill=t_col)
            draw.ellipse([margin + fill_w - 8, timer_y - 2, margin + fill_w + 4, timer_y + timer_h + 2], fill=(255, 255, 255))

        font_tm = get_font(max(12, int(14 * sc)))
        sb = draw.textbbox((0, 0), sec_lbl, font=font_tm)
        draw.text(((self.w - (sb[2] - sb[0])) // 2, timer_y - 1), sec_lbl, font=font_tm, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))

        opts = q_obj["opts"]
        ans_idx = q_obj["ans"]
        labels = ["A", "B", "C", "D"]
        font_opt = get_font(max(17, int(21 * sc)))
        font_lbl = get_font(max(16, int(19 * sc)))

        opts_top = timer_y + timer_h + int(self.h * 0.03)
        opts_area_h = int(self.h * (0.44 if self.is_vertical else 0.42))

        if self.is_vertical:
            gap = int(12 * sc)
            box_h = (opts_area_h - gap * 3) // 4
            for i in range(4):
                stagger = ease_out_back(min(1.0, max(0.0, (phase_elapsed - i * 0.07) / 0.32))) if phase == "read" else 1.0
                x_off = int((1.0 - stagger) * (170 if i % 2 == 0 else -170) * sc)
                oy0 = opts_top + i * (box_h + gap)
                self._draw_option(draw, margin + x_off, oy0, margin + card_w + x_off, oy0 + box_h,
                                  labels[i], opts[i], i == ans_idx, q_obj["is_secret_trap"], phase, theme, font_lbl, font_opt, sc)
        else:
            gap_x = int(18 * sc)
            gap_y = int(16 * sc)
            box_w = (card_w - gap_x) // 2
            box_h = (opts_area_h - gap_y) // 2
            for i in range(4):
                stagger = ease_out_back(min(1.0, max(0.0, (phase_elapsed - i * 0.07) / 0.32))) if phase == "read" else 1.0
                y_off = int((1.0 - stagger) * 85 * sc)
                col_i, row_i = i % 2, i // 2
                ox0 = margin + col_i * (box_w + gap_x)
                oy0 = opts_top + row_i * (box_h + gap_y) + y_off
                self._draw_option(draw, ox0, oy0, ox0 + box_w, oy0 + box_h,
                                  labels[i], opts[i], i == ans_idx, q_obj["is_secret_trap"], phase, theme, font_lbl, font_opt, sc)

        if phase == "reveal":
            if q_obj["is_secret_trap"]:
                trap_h = int(86 * sc)
                trap_y = opts_top + (opts_area_h - trap_h) // 2
                draw.rounded_rectangle([margin + 12, trap_y, margin + card_w - 12, trap_y + trap_h], radius=16, fill=(15, 23, 42), outline=(250, 204, 21), width=4)
                font_tr1 = get_font(max(17, int(22 * sc)))
                font_tr2 = get_font(max(13, int(16 * sc)))
                t1 = "🔒 ANSWER HIDDEN!"
                t2 = "COMMENT A, B, C OR D TO PROVE YOUR IQ! 👇"
                b_t1 = draw.textbbox((0, 0), t1, font=font_tr1)
                b_t2 = draw.textbbox((0, 0), t2, font=font_tr2)
                draw.text(((self.w - (b_t1[2] - b_t1[0])) // 2, trap_y + int(12 * sc)), t1, font=font_tr1, fill=(250, 204, 21))
                draw.text(((self.w - (b_t2[2] - b_t2[0])) // 2, trap_y + int(48 * sc)), t2, font=font_tr2, fill=(255, 255, 255))
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

                xp_pop = "+200 XP BOSS CLEAR!" if q_obj["is_boss"] else "+100 XP CORRECT!"
                font_xp = get_font(max(16, int(20 * sc)))
                xb2 = draw.textbbox((0, 0), xp_pop, font=font_xp)
                float_y = int(opts_top - 12 * sc - min(22 * sc, phase_elapsed * 30 * sc))
                draw.text(((self.w - (xb2[2] - xb2[0])) // 2, float_y), xp_pop, font=font_xp, fill=(52, 211, 153), stroke_width=3, stroke_fill=(0, 0, 0))

                if q_obj["fact"]:
                    fact_y = opts_top + opts_area_h + int(self.h * 0.024)
                    font_fc = get_font(max(13, int(16 * sc)))
                    fc_txt = f"💡 {q_obj['fact']}"
                    fb = draw.textbbox((0, 0), fc_txt, font=font_fc)
                    fw = min(card_w, fb[2] - fb[0] + 28)
                    fx0 = (self.w - fw) // 2
                    draw.rounded_rectangle([fx0, fact_y, fx0 + fw, fact_y + int(34 * sc)], radius=10, fill=(15, 23, 42), outline=(52, 211, 153), width=2)
                    draw.text((fx0 + 14, fact_y + int(7 * sc)), fc_txt[:68], font=font_fc, fill=(248, 250, 252))

        return frame.tobytes()

    def _draw_option(self, draw, x0, y0, x1, y1, letter, text, is_correct, is_secret_trap, phase, theme, font_lbl, font_opt, sc):
        if phase == "reveal" and not is_secret_trap:
            if is_correct:
                bg_col, border_col, txt_col, pill_col = (6, 78, 59), (52, 211, 153), (255, 255, 255), (16, 185, 129)
            else:
                bg_col, border_col, txt_col, pill_col = (12, 16, 28), (30, 41, 59), (71, 85, 105), (30, 41, 59)
        else:
            bg_col, border_col, txt_col, pill_col = (15, 23, 42), theme["secondary"], (241, 245, 249), theme["secondary"]

        draw.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=bg_col, outline=border_col, width=3)

        bh = y1 - y0
        cr = int(min(bh * 0.34, 21 * sc))
        cx = x0 + cr + int(15 * sc)
        cy = y0 + bh // 2
        draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=pill_col)

        lb = draw.textbbox((0, 0), letter, font=font_lbl)
        draw.text((cx - (lb[2] - lb[0]) // 2, cy - (lb[3] - lb[1]) // 2 - 2), letter, font=font_lbl, fill=(255, 255, 255))

        tb = draw.textbbox((0, 0), text, font=font_opt)
        draw.text((cx + cr + int(14 * sc), cy - (tb[3] - tb[1]) // 2 - 2), text, font=font_opt, fill=txt_col)

    def render_outro_comment_trap(self, outro_prog: float, global_t: float) -> bytes:
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0
        theme = GAMING_THEMES["royal"]
        frame, draw = self._draw_cyber_bg(theme, global_t, sc)

        margin = int(self.w * 0.07)
        box_w = self.w - margin * 2
        box_y = int(self.h * 0.16)
        box_h = int(self.h * 0.66)

        draw.rounded_rectangle([margin, box_y, margin + box_w, box_y + box_h], radius=20, fill=(11, 17, 32), outline=(250, 204, 21), width=4)

        font_hdr = get_font(max(20, int(28 * sc)))
        font_row = get_font(max(15, int(20 * sc)))
        font_cta = get_font(max(16, int(21 * sc)))

        hdr = "CLAIM YOUR IQ RANK BELOW! 👇"
        bh = draw.textbbox((0, 0), hdr, font=font_hdr)
        draw.text(((self.w - (bh[2] - bh[0])) // 2, box_y + int(28 * sc)), hdr, font=font_hdr, fill=(250, 204, 21))

        max_xp = (self.total_q - 1) * 100
        ranks = [
            (f"0 - {int(max_xp * 0.4)} XP  →  🌱 ROOKIE BRAIN", (148, 163, 184)),
            (f"{int(max_xp * 0.5)} - {int(max_xp * 0.8)} XP  →  ⚡ PRO CHALLENGER", (34, 211, 238)),
            (f"{max_xp}+ XP + BONUS  →  👑 TOP 1% GENIUS", (52, 211, 153))
        ]
        for idx, (r_txt, r_col) in enumerate(ranks):
            ry = box_y + int((105 + idx * 62) * sc)
            br = draw.textbbox((0, 0), r_txt, font=font_row)
            draw.text(((self.w - (br[2] - br[0])) // 2, ry), r_txt, font=font_row, fill=r_col)

        pulse = 1.0 + 0.04 * math.sin(global_t * 10.0)
        cta_txt = "COMMENT YOUR XP + BONUS ANSWER!"
        bc = draw.textbbox((0, 0), cta_txt, font=font_cta)
        cw = int((bc[2] - bc[0] + 36) * pulse)
        cx0 = (self.w - cw) // 2
        cy0 = box_y + box_h - int(85 * sc)
        draw.rounded_rectangle([cx0, cy0, cx0 + cw, cy0 + int(48 * sc)], radius=14, fill=(244, 63, 94))
        draw.text(((self.w - (bc[2] - bc[0])) // 2, cy0 + int(12 * sc)), cta_txt, font=font_cta, fill=(255, 255, 255))

        return frame.tobytes()


# ===========================================================================
# 4. RENDER PIPELINE & API ROUTES (WITH SMART COMPRESSION & AUTO-CLEANUP)
# ===========================================================================
def run_ultra_quiz_v2_job(job_id: str, ratio: str, custom_raw: str):
    job = JOBS[job_id]
    wav_path = None
    try:
        cleanup_old_files(1800)
        is_vertical = (ratio == "9:16")
        questions = smart_parse_and_direct(custom_raw, is_vertical)
        out_w, out_h = (540, 960) if is_vertical else (960, 540)
        fps = 24
        dt = 1.0 / float(fps)
        total_q = len(questions)

        intro_dur = 1.5
        outro_dur = 2.8

        timeline = []
        chapters = ["00:00 Shock-Wave IQ Challenge Intro"]
        t_cursor = intro_dur

        for idx, q_obj in enumerate(questions):
            if idx % 5 == 0 and not q_obj["is_secret_trap"]:
                mins = int(t_cursor // 60)
                secs = int(t_cursor % 60)
                chapters.append(f"{mins:02d}:{secs:02d} Level {q_obj['level']} Arena")
            elif q_obj["is_secret_trap"]:
                mins = int(t_cursor // 60)
                secs = int(t_cursor % 60)
                chapters.append(f"{mins:02d}:{secs:02d} Final Secret Bonus Question")

            t_start = t_cursor
            t_think = t_start + q_obj["read_sec"]
            t_reveal = t_think + q_obj["think_sec"]
            t_end = t_reveal + q_obj["reveal_sec"]
            timeline.append((q_obj, t_start, t_think, t_reveal, t_end))
            t_cursor = t_end

        total_dur = t_cursor + outro_dur
        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_gaming_v2_sfx.wav")
        job["progress"] = 10
        job["message"] = "Synthesizing Hook Impact, Countdown Ticks & Mystery Lock Audio..."
        build_gaming_sfx_track(intro_dur, timeline, outro_dur, total_dur, wav_path)

        out_name = f"NEXORA_VIRAL_QUIZ_{total_q}Q_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        # Smart Compression (-crf 26 -preset fast -b:a 96k) cuts file size by ~75% with crisp UI sharpness!
        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", wav_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        renderer = UltraGamingQuizV2Renderer(out_w, out_h, total_q)
        global_t = 0.0

        intro_frames = int(intro_dur * fps)
        for f in range(intro_frames):
            raw_bytes = renderer.render_intro_hook(f / max(1, intro_frames - 1), global_t)
            proc.stdin.write(raw_bytes)
            global_t += dt

        for idx, (q_obj, t_start, t_think, t_reveal, t_end) in enumerate(timeline):
            for p_name, p_dur in [("read", q_obj["read_sec"]), ("think", q_obj["think_sec"]), ("reveal", q_obj["reveal_sec"])]:
                n_frames = max(1, int(p_dur * fps))
                for f in range(n_frames):
                    raw_bytes = renderer.render_question_frame(idx, q_obj, p_name, f / max(1, n_frames - 1), f * dt, global_t, dt)
                    proc.stdin.write(raw_bytes)
                    global_t += dt

            job["progress"] = 15 + int(((idx + 1) / total_q) * 78)
            job["message"] = f"Rendering Compressed Ultra-Gaming Scene ({idx + 1}/{total_q})..."

        outro_frames = int(outro_dur * fps)
        for f in range(outro_frames):
            raw_bytes = renderer.render_outro_comment_trap(f / max(1, outro_frames - 1), global_t)
            proc.stdin.write(raw_bytes)
            global_t += dt

        proc.stdin.close()
        proc.wait()

        # Delete temporary WAV file immediately after MP4 muxing
        if wav_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except Exception:
                pass

        size_mb = round(os.path.getsize(out_path) / (1024.0 * 1024.0), 1)
        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["size_mb"] = size_mb
        job["chapters"] = "\n".join(chapters)
        job["message"] = f"Completed! Ultra-Light MP4 Ready ({size_mb} MB — Saves 75% Bandwidth!)"

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

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Building Scroll-Stop Hook & 3D Hologram Quiz..."}
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
