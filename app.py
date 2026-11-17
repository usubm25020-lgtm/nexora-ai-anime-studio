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

# ===========================================================================
# 1. ULTRA-GAMING VISUAL THEMES & SMART AUTO-DIRECTOR ANALYZER
# ===========================================================================
GAMING_THEMES = {
    "cyber": {
        "name": "CYBER ARENA",
        "bg_top": (10, 18, 42),
        "bg_bot": (4, 7, 18),
        "grid": (22, 42, 82),
        "primary": (34, 211, 238),    # Neon Cyan
        "secondary": (168, 85, 247),  # Electric Purple
        "accent": (250, 204, 21)      # Cyber Gold
    },
    "royal": {
        "name": "ROYAL GOLD",
        "bg_top": (28, 20, 10),
        "bg_bot": (6, 5, 4),
        "grid": (58, 42, 18),
        "primary": (250, 204, 21),    # Pure Gold
        "secondary": (249, 115, 22),  # Amber Blaze
        "accent": (56, 189, 248)      # Sky Highlight
    },
    "crimson": {
        "name": "BOSS RUSH",
        "bg_top": (32, 10, 24),
        "bg_bot": (6, 3, 10),
        "grid": (64, 18, 44),
        "primary": (244, 63, 94),     # Neon Crimson
        "secondary": (232, 121, 249), # Plasma Magenta
        "accent": (250, 204, 21)      # Gold
    },
    "emerald": {
        "name": "MATRIX PRO",
        "bg_top": (8, 28, 24),
        "bg_bot": (3, 9, 8),
        "grid": (16, 56, 48),
        "primary": (16, 185, 129),    # Neon Emerald
        "secondary": (34, 211, 238),  # Aqua
        "accent": (250, 204, 21)      # Gold
    }
}

DEFAULT_SAMPLE_QUESTIONS = [
    {"q": "Which planet has the shortest day in our Solar System?", "opts": ["Mercury", "Jupiter", "Mars", "Venus"], "ans": 1, "fact": "Jupiter spins once every 9 hours and 55 minutes!"},
    {"q": "If you freeze seawater, what happens to the salt?", "opts": ["Turns into crystals", "Gets pushed out", "Makes ice hotter", "Turns blue"], "ans": 1, "fact": "Pure water freezes first, expelling salt into surrounding brine."},
    {"q": "What has 13 hearts, but no other organs?", "opts": ["An Octopus", "A Deck of Cards", "A Starfish", "A Jellyfish"], "ans": 1, "fact": "All 13 cards in the Hearts suit!"},
    {"q": "Which metal can melt right in the palm of your hand?", "opts": ["Gallium", "Titanium", "Zinc", "Nickel"], "ans": 0, "fact": "Gallium has a melting point of just 29.76 degrees Celsius."},
    {"q": "How many triangles can you form using 3 straight lines?", "opts": ["Zero", "One", "Two", "Three"], "ans": 1, "fact": "Three non-parallel lines intersect to enclose exactly 1 triangle."}
]

def smart_parse_and_direct(raw_text: str):
    """
    Parses pasted ChatGPT/Gemini questions and automatically assigns:
    - Optimal reading + thinking timer based on question length & math/logic complexity
    - Dynamic gaming theme based on topic keywords & level progression
    - Boss Question status every 5th question
    """
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

    directed_questions = []
    theme_keys = list(GAMING_THEMES.keys())

    for idx, item in enumerate(items):
        q_low = item["q"].lower()
        word_cnt = len(item["q"].split())
        is_boss = ((idx + 1) % 5 == 0)

        # 1. Auto-decide think timer duration (4s fast, 6s medium, 8s complex/math)
        has_math = bool(re.search(r"[0-9%+\-*/=]|calculate|solve|how many|equation|ratio", q_low))
        if has_math or word_cnt >= 15:
            think_sec = 7.5
        elif word_cnt >= 10:
            think_sec = 5.5
        else:
            think_sec = 4.5

        read_sec = max(1.6, min(3.2, word_cnt * 0.16))
        reveal_sec = 3.2 if item["fact"] else 2.5

        # 2. Auto-select Gaming Theme based on subject matter + level shifts
        if is_boss:
            t_key = "crimson"
        elif any(k in q_low for k in ["space", "planet", "atom", "physics", "science", "chem", "bio", "energy", "light"]):
            t_key = "cyber"
        elif any(k in q_low for k in ["history", "king", "india", "world", "capital", "country", "war", "constitution"]):
            t_key = "royal"
        elif any(k in q_low for k in ["riddle", "brain", "logic", "number", "math", "code", "computer"]):
            t_key = "emerald"
        else:
            t_key = theme_keys[(idx // 3) % len(theme_keys)]

        directed_questions.append({
            **item,
            "is_boss": is_boss,
            "theme_key": t_key,
            "read_sec": read_sec,
            "think_sec": think_sec,
            "reveal_sec": reveal_sec,
            "level": (idx // 5) + 1
        })

    return directed_questions


# ===========================================================================
# 2. ULTRA-GAMING SYNTHWAVE BEAT + WHOOSH, TICK-TOCK & XP CHIME AUDIO
# ===========================================================================
def build_gaming_sfx_track(timeline: list, total_dur: float, out_wav: str):
    """
    Zero robotic voiceover! Generates energetic gaming synthwave beat,
    card slide whooshes, crisp digital countdown ticks, and victory XP fanfare.
    """
    rate = 22050
    n = int(total_dur * rate)
    t = np.linspace(0, total_dur, n, endpoint=False, dtype=np.float32)

    bpm = 124.0
    beat_dur = 60.0 / bpm
    beat_pos = (t % beat_dur) / beat_dur

    # Punchy lo-fi gaming kick + warm bassline
    kick = np.sin(2.0 * np.pi * (52.0 + 55.0 * np.exp(-beat_pos * 18.0)) * (t % beat_dur)) * np.exp(-beat_pos * 8.5) * 0.24
    bass_notes = [110.0, 110.0, 130.81, 146.83, 98.0, 98.0, 130.81, 123.47]
    b_idx = (t / (beat_dur * 2.0)).astype(np.int32)
    b_freq = np.array([bass_notes[i % len(bass_notes)] for i in b_idx], dtype=np.float32)
    bass = np.sin(2.0 * np.pi * b_freq * t) * 0.11

    audio = kick + bass
    rng = np.random.default_rng(2026)

    for q_obj, t_start, t_think_start, t_reveal_start, t_end in timeline:
        # 1. Card Slide-In Whoosh at t_start
        s0 = int(t_start * rate)
        w_len = int(0.28 * rate)
        if s0 + w_len < n:
            tw = np.linspace(0, 0.28, w_len, endpoint=False, dtype=np.float32)
            noise = rng.uniform(-1.0, 1.0, w_len).astype(np.float32)
            env = np.sin(np.pi * (tw / 0.28)) ** 1.5
            audio[s0:s0 + w_len] += noise * env * 0.18

        # 2. Digital Countdown Ticks during think phase (accelerates in final 2 seconds!)
        cur_t = t_think_start + 0.2
        tick_i = 0
        while cur_t < t_reveal_start - 0.12:
            rem = t_reveal_start - cur_t
            step = 0.25 if rem <= 2.0 else 0.50
            st = int(cur_t * rate)
            t_len = int(0.055 * rate)
            if st + t_len < n:
                tt = np.linspace(0, 0.055, t_len, endpoint=False, dtype=np.float32)
                freq = 1480.0 if rem <= 2.0 else (1120.0 if tick_i % 2 == 0 else 920.0)
                tk = np.sin(2.0 * np.pi * freq * tt) * np.exp(-tt * 75.0) * 0.36
                audio[st:st + t_len] += tk
            cur_t += step
            tick_i += 1

        # 3. Gaming Victory Chord / XP Level-Up Chime at t_reveal_start
        sr = int(t_reveal_start * rate)
        r_len = int(0.75 * rate)
        if sr + r_len < n:
            tr = np.linspace(0, 0.75, r_len, endpoint=False, dtype=np.float32)
            # Arpeggiated bright major triad (C6 - E6 - G6 - C7)
            c1 = np.sin(2.0 * np.pi * 1046.50 * tr) * np.exp(-tr * 6.0)
            c2 = np.sin(2.0 * np.pi * 1318.51 * np.maximum(0, tr - 0.06)) * np.exp(-np.maximum(0, tr - 0.06) * 5.5)
            c3 = np.sin(2.0 * np.pi * 1567.98 * np.maximum(0, tr - 0.12)) * np.exp(-np.maximum(0, tr - 0.12) * 5.0)
            c4 = np.sin(2.0 * np.pi * 2093.00 * np.maximum(0, tr - 0.18)) * np.exp(-np.maximum(0, tr - 0.18) * 4.2)
            audio[sr:sr + r_len] += (c1 + c2 + c3 + c4) * 0.24

    fade_s = min(int(0.8 * rate), n // 4)
    if fade_s > 0:
        audio[:fade_s] *= np.linspace(0, 1, fade_s)
        audio[-fade_s:] *= np.linspace(1, 0, fade_s)

    pcm = (np.clip(audio, -0.95, 0.95) * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


# ===========================================================================
# 3. HIGH-MOTION ULTRA-GAMING QUIZ GRAPHICS & CONFETTI ENGINE
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

class UltraGamingQuizRenderer:
    def __init__(self, w: int, h: int, total_q: int):
        self.w = w
        self.h = h
        self.total_q = total_q
        self.is_vertical = (h > w)
        self.rng = random.Random(777)

        # Pre-spawn floating ambient cyber particles
        self.ambient_orbs = [
            [self.rng.uniform(0, w), self.rng.uniform(0, h), self.rng.uniform(18, 45), self.rng.uniform(2, 5)]
            for _ in range(28)
        ]
        # Confetti particles burst on answer reveal
        self.confetti = []
        self.last_confetti_q = -1

    def _spawn_confetti(self, cx: float, cy: float):
        cols = [(34, 211, 238), (250, 204, 21), (52, 211, 153), (244, 63, 94), (168, 85, 247), (255, 255, 255)]
        self.confetti = []
        for _ in range(65):
            ang = self.rng.uniform(0, 2.0 * math.pi)
            spd = self.rng.uniform(140.0, 520.0)
            self.confetti.append([cx, cy, math.cos(ang) * spd, math.sin(ang) * spd - 160.0, 1.0, self.rng.choice(cols)])

    def render_frame(self, q_idx: int, q_obj: dict, phase: str, phase_prog: float,
                     phase_elapsed: float, global_t: float, dt: float) -> bytes:
        theme = GAMING_THEMES[q_obj["theme_key"]]
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0

        # 1. Dynamic Gradient + Moving Perspective Cyber-Grid Background
        grad = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        for ch in range(3):
            col_line = np.linspace(theme["bg_top"][ch], theme["bg_bot"][ch], self.h, dtype=np.uint8)[:, None]
            grad[:, :, ch] = np.repeat(col_line, self.w, axis=1)
        frame = Image.fromarray(grad, mode="RGB")
        draw = ImageDraw.Draw(frame)

        grid_step = int(52 * sc)
        gx_off = int((global_t * 36) % grid_step)
        gy_off = int((global_t * 24) % grid_step)
        for gx in range(-grid_step, self.w + grid_step, grid_step):
            draw.line([(gx + gx_off, 0), (gx + gx_off, self.h)], fill=theme["grid"], width=1)
        for gy in range(-grid_step, self.h + grid_step, grid_step):
            draw.line([(0, gy + gy_off), (self.w, gy + gy_off)], fill=theme["grid"], width=1)

        # Floating ambient glowing orbs
        for orb in self.ambient_orbs:
            ox = (orb[0] + math.sin(global_t * 0.8 + orb[1]) * 18) % self.w
            oy = (orb[1] - global_t * orb[2]) % self.h
            r_o = orb[3] * sc
            draw.ellipse([ox - r_o, oy - r_o, ox + r_o, oy + r_o], fill=theme["primary"])

        # 2. Top Gaming HUD Bar (Level, Question Counter, XP Score)
        font_hud = get_font(max(13, int(16 * sc)))
        margin = int(self.w * 0.055)
        hud_y = int(self.h * 0.035)

        lvl_txt = f"LVL {q_obj['level']} • {'BOSS 2X XP' if q_obj['is_boss'] else theme['name']}"
        xp_val = (q_idx + (1 if phase == "reveal" else 0)) * 100
        xp_txt = f"Q {q_idx + 1}/{self.total_q}  •  {xp_val} XP"

        draw.rounded_rectangle([margin, hud_y, margin + int(230 * sc), hud_y + int(32 * sc)], radius=8, fill=(15, 23, 42), outline=theme["primary"], width=2)
        draw.text((margin + 12, hud_y + int(6 * sc)), lvl_txt, font=font_hud, fill=theme["primary"])

        xb = draw.textbbox((0, 0), xp_txt, font=font_hud)
        xw = xb[2] - xb[0] + 24
        draw.rounded_rectangle([self.w - margin - xw, hud_y, self.w - margin, hud_y + int(32 * sc)], radius=8, fill=(15, 23, 42), outline=theme["accent"], width=2)
        draw.text((self.w - margin - xw + 12, hud_y + int(6 * sc)), xp_txt, font=font_hud, fill=theme["accent"])

        # 3. Elastic Slide-In Question Card
        slide_t = ease_out_back(min(1.0, phase_elapsed / 0.42)) if phase == "read" else 1.0
        y_shift = int((1.0 - slide_t) * -140 * sc)

        q_top = int(self.h * 0.115) + y_shift
        q_h = int(self.h * (0.23 if self.is_vertical else 0.25))
        card_w = self.w - margin * 2

        # Pulse border red in final 2 seconds of countdown!
        rem_sec = q_obj["think_sec"] * (1.0 - phase_prog) if phase == "think" else 99.0
        if phase == "think" and rem_sec <= 2.0 and int(global_t * 8) % 2 == 0:
            q_border = (244, 63, 94)
        elif q_obj["is_boss"]:
            q_border = (250, 204, 21)
        else:
            q_border = theme["primary"]

        draw.rounded_rectangle([margin, q_top, margin + card_w, q_top + q_h], radius=16, fill=(11, 17, 32), outline=q_border, width=3)

        font_q = get_font(max(19, int(25 * sc)))
        q_lines = wrap_lines(draw, q_obj["q"], font_q, card_w - int(44 * sc))
        lh = int(font_q.size * 1.32)
        ty_start = q_top + (q_h - len(q_lines) * lh) // 2
        for li, ln in enumerate(q_lines):
            lb = draw.textbbox((0, 0), ln, font=font_q)
            draw.text(((self.w - (lb[2] - lb[0])) // 2, ty_start + li * lh), ln, font=font_q, fill=(248, 250, 252))

        # 4. Neon Laser Countdown Bar + Numeric Seconds Badge
        timer_y = q_top + q_h + int(self.h * 0.025)
        timer_h = max(14, int(18 * sc))
        draw.rounded_rectangle([margin, timer_y, margin + card_w, timer_y + timer_h], radius=timer_h // 2, fill=(22, 30, 52))

        if phase == "read":
            frac = 1.0
            t_col = theme["primary"]
            sec_lbl = f"{q_obj['think_sec']:.0f}s"
        elif phase == "think":
            frac = max(0.0, 1.0 - phase_prog)
            t_col = (244, 63, 94) if rem_sec <= 2.0 else theme["accent"]
            sec_lbl = f"{max(0.0, rem_sec):.1f}s"
        else:
            frac = 1.0
            t_col = (16, 185, 129)
            sec_lbl = "DONE!"

        fill_w = int(card_w * frac)
        if fill_w > 10:
            draw.rounded_rectangle([margin, timer_y, margin + fill_w, timer_y + timer_h], radius=timer_h // 2, fill=t_col)
            # Bright laser tip spark
            draw.ellipse([margin + fill_w - 8, timer_y - 2, margin + fill_w + 4, timer_y + timer_h + 2], fill=(255, 255, 255))

        font_tm = get_font(max(12, int(14 * sc)))
        sb = draw.textbbox((0, 0), sec_lbl, font=font_tm)
        draw.text(((self.w - (sb[2] - sb[0])) // 2, timer_y - 1), sec_lbl, font=font_tm, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))

        # 5. Staggered Pop-In Option Cards (A, B, C, D)
        opts = q_obj["opts"]
        ans_idx = q_obj["ans"]
        labels = ["A", "B", "C", "D"]
        font_opt = get_font(max(17, int(21 * sc)))
        font_lbl = get_font(max(16, int(19 * sc)))

        opts_top = timer_y + timer_h + int(self.h * 0.032)
        opts_area_h = int(self.h * (0.44 if self.is_vertical else 0.42))

        if self.is_vertical:
            gap = int(12 * sc)
            box_h = (opts_area_h - gap * 3) // 4
            for i in range(4):
                stagger = ease_out_back(min(1.0, max(0.0, (phase_elapsed - i * 0.08) / 0.35))) if phase == "read" else 1.0
                x_off = int((1.0 - stagger) * (180 if i % 2 == 0 else -180) * sc)
                oy0 = opts_top + i * (box_h + gap)
                self._draw_gaming_option(draw, margin + x_off, oy0, margin + card_w + x_off, oy0 + box_h,
                                         labels[i], opts[i], i == ans_idx, phase, theme, font_lbl, font_opt, sc)
        else:
            gap_x = int(18 * sc)
            gap_y = int(16 * sc)
            box_w = (card_w - gap_x) // 2
            box_h = (opts_area_h - gap_y) // 2
            for i in range(4):
                stagger = ease_out_back(min(1.0, max(0.0, (phase_elapsed - i * 0.08) / 0.35))) if phase == "read" else 1.0
                y_off = int((1.0 - stagger) * 90 * sc)
                col_i, row_i = i % 2, i // 2
                ox0 = margin + col_i * (box_w + gap_x)
                oy0 = opts_top + row_i * (box_h + gap_y) + y_off
                self._draw_gaming_option(draw, ox0, oy0, ox0 + box_w, oy0 + box_h,
                                         labels[i], opts[i], i == ans_idx, phase, theme, font_lbl, font_opt, sc)

        # 6. Confetti Burst + Quick Fact Banner on Reveal Phase
        if phase == "reveal":
            if self.last_confetti_q != q_idx:
                self.last_confetti_q = q_idx
                self._spawn_confetti(self.w * 0.5, opts_top + opts_area_h * 0.5)

            for p in self.confetti:
                p[0] += p[2] * dt
                p[1] += p[3] * dt
                p[3] += 480.0 * dt
                p[4] -= dt * 0.85
                if p[4] > 0:
                    pr = max(2.0, 5.0 * sc * p[4])
                    draw.rectangle([p[0] - pr, p[1] - pr, p[0] + pr, p[1] + pr], fill=p[5])

            # Floating "+100 XP!" Tag
            xp_pop = "+200 XP BOSS CLEAR!" if q_obj["is_boss"] else "+100 XP CORRECT!"
            font_xp = get_font(max(16, int(20 * sc)))
            xb2 = draw.textbbox((0, 0), xp_pop, font=font_xp)
            float_y = int(opts_top - 12 * sc - min(22 * sc, phase_elapsed * 30 * sc))
            draw.text(((self.w - (xb2[2] - xb2[0])) // 2, float_y), xp_pop, font=font_xp, fill=(52, 211, 153), stroke_width=3, stroke_fill=(0, 0, 0))

            # Quick Fact Footer Box (if present)
            if q_obj["fact"]:
                fact_y = opts_top + opts_area_h + int(self.h * 0.025)
                font_fc = get_font(max(13, int(16 * sc)))
                fc_txt = f"💡 {q_obj['fact']}"
                fb = draw.textbbox((0, 0), fc_txt, font=font_fc)
                fw = min(card_w, fb[2] - fb[0] + 28)
                fx0 = (self.w - fw) // 2
                draw.rounded_rectangle([fx0, fact_y, fx0 + fw, fact_y + int(34 * sc)], radius=10, fill=(15, 23, 42), outline=(52, 211, 153), width=2)
                draw.text((fx0 + 14, fact_y + int(7 * sc)), fc_txt[:68], font=font_fc, fill=(248, 250, 252))

        return frame.tobytes()

    def _draw_gaming_option(self, draw, x0, y0, x1, y1, letter, text, is_correct, phase, theme, font_lbl, font_opt, sc):
        if phase == "reveal":
            if is_correct:
                bg_col = (6, 78, 59)
                border_col = (52, 211, 153)
                txt_col = (255, 255, 255)
                pill_col = (16, 185, 129)
            else:
                bg_col = (12, 16, 28)
                border_col = (30, 41, 59)
                txt_col = (71, 85, 105)
                pill_col = (30, 41, 59)
        else:
            bg_col = (15, 23, 42)
            border_col = theme["secondary"]
            txt_col = (241, 245, 249)
            pill_col = theme["secondary"]

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


# ===========================================================================
# 4. RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_ultra_quiz_job(job_id: str, ratio: str, custom_raw: str):
    job = JOBS[job_id]
    try:
        questions = smart_parse_and_direct(custom_raw)
        out_w, out_h = (960, 540) if ratio == "16:9" else (540, 960)
        fps = 24
        dt = 1.0 / float(fps)
        total_q = len(questions)

        # Build exact timeline & YouTube chapter timestamps automatically
        timeline = []
        chapters = []
        t_cursor = 0.0
        for idx, q_obj in enumerate(questions):
            if idx % 5 == 0:
                mins = int(t_cursor // 60)
                secs = int(t_cursor % 60)
                chapters.append(f"{mins:02d}:{secs:02d} Level {q_obj['level']} Challenge")

            t_start = t_cursor
            t_think = t_start + q_obj["read_sec"]
            t_reveal = t_think + q_obj["think_sec"]
            t_end = t_reveal + q_obj["reveal_sec"]
            timeline.append((q_obj, t_start, t_think, t_reveal, t_end))
            t_cursor = t_end

        total_dur = max(4.0, t_cursor)
        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_gaming_sfx.wav")
        job["progress"] = 10
        job["message"] = "Synthesizing Synthwave Beat, Countdown Ticks & XP Chimes..."
        build_gaming_sfx_track(timeline, total_dur, wav_path)

        out_name = f"NEXORA_GAMING_QUIZ_{total_q}Q_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", wav_path,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "19",
            "-c:a", "aac", "-b:a", "160k",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        renderer = UltraGamingQuizRenderer(out_w, out_h, total_q)
        global_t = 0.0

        for idx, (q_obj, t_start, t_think, t_reveal, t_end) in enumerate(timeline):
            phases = [
                ("read", q_obj["read_sec"]),
                ("think", q_obj["think_sec"]),
                ("reveal", q_obj["reveal_sec"])
            ]
            for p_name, p_dur in phases:
                n_frames = max(1, int(p_dur * fps))
                for f in range(n_frames):
                    prog = f / max(1, n_frames - 1)
                    elapsed = f * dt
                    raw_bytes = renderer.render_frame(idx, q_obj, p_name, prog, elapsed, global_t, dt)
                    proc.stdin.write(raw_bytes)
                    global_t += dt

            job["progress"] = 15 + int(((idx + 1) / total_q) * 82)
            job["message"] = f"Rendering Ultra-Gaming Animations ({idx + 1}/{total_q})..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["chapters"] = "\n".join(chapters)
        job["message"] = f"Completed! {total_q}-Question Ultra-Gaming Video Ready ({out_name})"

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
    ratio = data.get("ratio", "16:9")
    custom_raw = data.get("custom_raw", "")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Auto-Director Analyzing Questions & Setting Timers..."}
    threading.Thread(
        target=run_ultra_quiz_job,
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
