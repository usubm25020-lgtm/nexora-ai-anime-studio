import os
import re
import time
import math
import uuid
import wave
import asyncio
import subprocess
import threading
import numpy as np

import edge_tts
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageFilter
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
# 1. VOICE & WAVEFORM LIP-SYNC ENGINE
# ===========================================================================
VOICE_MAP = {
    "MALE_HINDI": "hi-IN-MadhurNeural",
    "FEMALE_HINDI": "hi-IN-SwaraNeural",
    "MALE_ENGLISH": "en-IN-PrabhatNeural",
    "FEMALE_ENGLISH": "en-IN-NeerjaNeural"
}

async def _tts_save(text: str, voice_id: str, mp3_path: str):
    comm = edge_tts.Communicate(text, voice_id, rate="+7%")
    await comm.save(mp3_path)

def generate_tts_wav(text: str, is_female: bool, lang_mode: str, out_wav: str) -> float:
    clean_txt = re.sub(r"[^\w\s,?!.'\-]", "", text).strip() or "Hello"
    if lang_mode == "ENGLISH":
        vid = VOICE_MAP["FEMALE_ENGLISH"] if is_female else VOICE_MAP["MALE_ENGLISH"]
    else:
        vid = VOICE_MAP["FEMALE_HINDI"] if is_female else VOICE_MAP["MALE_HINDI"]

    tmp_mp3 = out_wav + ".mp3"
    try:
        asyncio.run(_tts_save(clean_txt, vid, tmp_mp3))
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", tmp_mp3, "-ar", "22050", "-ac", "1", out_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
        if os.path.exists(tmp_mp3):
            os.remove(tmp_mp3)
    except Exception:
        dur = max(2.2, len(clean_txt.split()) * 0.40)
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", str(dur), out_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    with wave.open(out_wav, "rb") as wf:
        return max(1.8, wf.getnframes() / float(wf.getframerate()))

def convert_custom_audio_to_wav(src_path: str, out_wav: str) -> float:
    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", src_path, "-ar", "22050", "-ac", "1", out_wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )
    with wave.open(out_wav, "rb") as wf:
        return max(1.8, wf.getnframes() / float(wf.getframerate()))

def extract_lip_envelope(wav_path: str, fps: int) -> list:
    try:
        with wave.open(wav_path, "rb") as wf:
            rate = wf.getframerate()
            samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return [0.0]

    step = max(1, int(rate / fps))
    raw_env = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        raw_env.append(min(1.0, max(0.0, (rms - 0.015) * 10.5)))

    smooth_env, prev = [], 0.0
    for v in raw_env:
        cur = prev * 0.30 + v * 0.70
        smooth_env.append(cur)
        prev = cur
    return smooth_env if smooth_env else [0.0]


# ===========================================================================
# 2. RICH HAND-DRAWN SKETCH BACKGROUNDS (BEDROOM, CITY STREET, SCHOOL, DARK)
# ===========================================================================
def add_cinema_vignette_and_glow(img: Image.Image, glows: list) -> Image.Image:
    w, h = img.size
    if glows:
        gl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(gl)
        for gx, gy, gr, gcol in glows:
            gd.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=gcol)
        gl = gl.filter(ImageFilter.GaussianBlur(radius=34))
        img = Image.alpha_composite(img.convert("RGBA"), gl)

    vig = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vig)
    for b in range(28):
        alpha = int((28 - b) * 3.0)
        vd.rectangle([b * 3, b * 3, w - b * 3, h - b * 3], outline=(12, 16, 20, alpha), width=3)
    return Image.alpha_composite(img.convert("RGBA"), vig)

def draw_studio_environment(w: int, h: int, scene_text: str, global_t: float, prog: float) -> tuple:
    """
    Draws rich hand-drawn 2D studio environments with warm lighting, depth props,
    and returns (canvas_image, ground_y, glow_list).
    """
    low = scene_text.lower()
    sc = w / 580.0

    # --- 1. PITCH-DARK HORROR / MONSTER EYES SCENE ---
    if any(k in low for k in ["darkness", "andhera", "monster", "bhoot", "ghost", "light off"]):
        img = Image.new("RGBA", (w, h), (22, 25, 28, 255))
        d = ImageDraw.Draw(img)
        ex_b, ey_b = int(w * 0.66), int(h * 0.54)
        blink = (int(global_t * 10) % 28 == 0)
        for dx_e in (-22, 22):
            ex = ex_b + int(dx_e * sc)
            if not blink:
                d.ellipse([ex - 15, ey_b - 17, ex + 15, ey_b + 17], fill=(230, 236, 240))
                lx = -5 if prog > 0.35 else 0
                d.ellipse([ex - 5 + lx, ey_b - 5, ex + 5 + lx, ey_b + 5], fill=(15, 23, 42))

        if prog > 0.28:
            fade = min(1.0, (prog - 0.28) * 2.5)
            al = int(240 * fade)
            mx, my = int(w * 0.42), int(h * 0.54)
            d.pieslice([mx - 40, my - 16, mx - 4, my + 14], start=10, end=195, fill=(230, 236, 240, al))
            d.pieslice([mx + 4, my - 16, mx + 40, my + 14], start=345, end=170, fill=(230, 236, 240, al))
            d.arc([mx - 34, my + 16, mx + 34, my + 46], start=10, end=170, fill=(230, 236, 240, al), width=5)

        return img, int(h * 0.78), [], True

    # --- 2. COZY BEDROOM / STUDY ROOM INTERIOR (WARM LAMP & DESK) ---
    if any(k in low for k in ["bedroom", "room", "kamra", "desk", "study", "home", "ghar", "exam"]):
        # Warm hand-drawn sage-cream studio room (matching reference aesthetic)
        img = Image.new("RGBA", (w, h), (196, 202, 192, 255))
        d = ImageDraw.Draw(img)
        ink = (38, 42, 46)

        floor_y = int(h * 0.72)
        back_y = int(h * 0.63)
        # Floor & Back Wall
        d.rectangle([0, back_y, w, h], fill=(164, 170, 160))
        d.line([(0, floor_y), (int(w * 0.16), back_y), (int(w * 0.86), back_y), (w, floor_y)], fill=ink, width=3)
        d.line([(int(w * 0.16), 0), (int(w * 0.16), back_y)], fill=ink, width=3)
        d.line([(int(w * 0.86), 0), (int(w * 0.86), back_y)], fill=ink, width=3)

        # Window on left with morning sky & curtains
        wx, wy, ww, wh = int(w * 0.21), int(h * 0.22), int(125 * sc), int(145 * sc)
        d.rectangle([wx, wy, wx + ww, wy + wh], fill=(186, 224, 245), outline=ink, width=3)
        d.line([(wx + ww // 2, wy), (wx + ww // 2, wy + wh)], fill=ink, width=3)
        d.line([(wx, wy + wh // 2), (wx + ww, wy + wh // 2)], fill=ink, width=3)

        # Wall Poster / Framed Sketch
        px, py = int(w * 0.52), int(h * 0.24)
        d.rectangle([px, py, px + int(85 * sc), py + int(105 * sc)], fill=(224, 228, 220), outline=ink, width=3)
        d.ellipse([px + int(22 * sc), py + int(25 * sc), px + int(62 * sc), py + int(65 * sc)], outline=ink, width=2)

        # Study Desk + Books + Glowing Study Lamp on Right
        dx, dy = int(w * 0.62), int(h * 0.52)
        dw, dh = int(175 * sc), int(115 * sc)
        d.rectangle([dx, dy, dx + dw, dy + dh], fill=(148, 124, 102), outline=ink, width=3)
        d.rectangle([dx - 8, dy - 10, dx + dw + 8, dy + 6], fill=(168, 142, 118), outline=ink, width=3)
        # Stack of books on desk
        d.rectangle([dx + int(18 * sc), dy - int(24 * sc), dx + int(78 * sc), dy - 10], fill=(225, 75, 75), outline=ink, width=2)
        d.rectangle([dx + int(22 * sc), dy - int(36 * sc), dx + int(74 * sc), dy - int(24 * sc)], fill=(59, 130, 246), outline=ink, width=2)
        # Glowing Lamp
        lx, ly = dx + int(122 * sc), dy - int(68 * sc)
        d.line([(lx, dy - 10), (lx, ly + 28)], fill=ink, width=4)
        d.polygon([(lx - 24, ly + 30), (lx - 13, ly), (lx + 13, ly), (lx + 24, ly + 30)], fill=(254, 249, 195), outline=ink, width=3)

        return img, int(h * 0.77), [(lx, ly + 15, int(110 * sc), (253, 224, 71, 140))], False

    # --- 3. SCHOOL GATE / CAMPUS AT SUNSET ---
    if any(k in low for k in ["school", "college", "gate", "class", "sunset", "shaam"]):
        img = Image.new("RGBA", (w, h), (253, 186, 116, 255))
        d = ImageDraw.Draw(img)
        ink = (38, 42, 46)

        # Warm Sunset Gradient Band
        d.rectangle([0, int(h * 0.35), w, int(h * 0.68)], fill=(254, 215, 170))
        d.ellipse([int(w * 0.72), int(h * 0.16), int(w * 0.72) + 68, int(h * 0.16) + 68], fill=(254, 240, 138))

        # School Building & Clock Tower
        bx, by = int(w * 0.16), int(h * 0.34)
        bw, bh = int(390 * sc), int(210 * sc)
        d.rectangle([bx, by, bx + bw, by + bh], fill=(241, 232, 218), outline=ink, width=3)
        d.polygon([(bx - 16, by), (bx + bw // 2, by - int(65 * sc)), (bx + bw + 16, by)], fill=(185, 65, 65), outline=ink)
        # School Clock
        cx_c, cy_c = bx + bw // 2, by - int(22 * sc)
        d.ellipse([cx_c - 22, cy_c - 22, cx_c + 22, cy_c + 22], fill=(255, 255, 255), outline=ink, width=3)
        d.line([(cx_c, cy_c), (cx_c, cy_c - 12)], fill=ink, width=2)
        d.line([(cx_c, cy_c), (cx_c + 10, cy_c)], fill=ink, width=2)

        # School Windows
        for wx_i in range(3):
            wx = bx + int((35 + wx_i * 120) * sc)
            wy = by + int(35 * sc)
            d.rectangle([wx, wy, wx + int(65 * sc), wy + int(75 * sc)], fill=(147, 197, 253), outline=ink, width=3)

        # Sidewalk & Road
        ground_y = int(h * 0.68)
        d.rectangle([0, ground_y, w, h], fill=(71, 85, 105))
        d.rectangle([0, ground_y, w, ground_y + int(28 * sc)], fill=(203, 213, 225), outline=ink, width=2)

        return img, int(h * 0.79), [(cx_c + 140, int(h * 0.20), 90, (254, 240, 138, 100))], False

    # --- 4. MODERN CITY STREET WITH HOUSES, TREES, CLOUDS & MOVING CAR ---
    img = Image.new("RGBA", (w, h), (186, 226, 248, 255))
    d = ImageDraw.Draw(img)
    ink = (38, 42, 46)

    # Drifting Clouds
    for ci in range(3):
        cx = int(((ci * 260 + global_t * 18) % (w + 160)) - 80)
        cy = int(h * (0.12 + ci * 0.05))
        d.ellipse([cx, cy, cx + 105, cy + 36], fill=(255, 255, 255, 220))
        d.ellipse([cx + 22, cy - 14, cx + 82, cy + 24], fill=(255, 255, 255, 220))

    # City Houses Silhouette & Midground
    ground_y = int(h * 0.68)
    for hx_r, hw_r, hh_r, hcol in [
        (0.06, 155, 185, (245, 226, 184)),
        (0.36, 140, 215, (218, 228, 238)),
        (0.68, 165, 195, (242, 216, 204))
    ]:
        hx = int(w * hx_r)
        hw, hh = int(hw_r * sc), int(hh_r * sc)
        d.rectangle([hx, ground_y - hh, hx + hw, ground_y], fill=hcol, outline=ink, width=3)
        d.polygon([(hx - 10, ground_y - hh), (hx + hw // 2, ground_y - hh - int(45 * sc)), (hx + hw + 10, ground_y - hh)], fill=(168, 72, 72), outline=ink)
        # Windows
        for wy_off in (0.28, 0.62):
            d.rectangle([hx + int(hw * 0.22), ground_y - int(hh * wy_off), hx + int(hw * 0.44), ground_y - int(hh * (wy_off - 0.18))], fill=(147, 197, 253), outline=ink, width=2)
            d.rectangle([hx + int(hw * 0.56), ground_y - int(hh * wy_off), hx + int(hw * 0.78), ground_y - int(hh * (wy_off - 0.18))], fill=(147, 197, 253), outline=ink, width=2)

    # Swaying Trees
    sway = math.sin(global_t * 2.4)
    for tx_r, tsc in ((0.12, 0.9), (0.89, 1.05)):
        tx = int(w * tx_r)
        top_x = tx + int(sway * 9 * tsc)
        d.rectangle([tx - 7, ground_y - int(95 * tsc), tx + 7, ground_y], fill=(102, 72, 54), outline=ink, width=2)
        d.ellipse([top_x - int(44 * tsc), ground_y - int(142 * tsc), top_x + int(44 * tsc), ground_y - int(68 * tsc)], fill=(74, 192, 118), outline=ink, width=3)

    # Sidewalk & Asphalt Road
    d.rectangle([0, ground_y, w, h], fill=(55, 65, 81))
    d.rectangle([0, ground_y, w, ground_y + int(24 * sc)], fill=(203, 213, 225), outline=ink, width=2)
    lane_y = int(h * 0.79)
    scroll = int((global_t * 120) % 85)
    for lx in range(-85, w + 85, 85):
        d.rectangle([lx - scroll, lane_y - 3, lx + 40 - scroll, lane_y + 3], fill=(241, 245, 249))

    return img, int(h * 0.79), [], False


# ===========================================================================
# 3. EXPRESSIVE RUBBER-HOSE & 2-BONE IK CHARACTER RIG (NATURAL HUMAN MOTION)
# ===========================================================================
def solve_2bone_ik(root: tuple, target: tuple, len1: float, len2: float, bend_dir: int = 1) -> tuple:
    rx, ry = root
    tx, ty = target
    dx, dy = tx - rx, ty - ry
    dist = math.hypot(dx, dy)
    max_reach = (len1 + len2) * 0.98
    min_reach = abs(len1 - len2) + 2.0
    dist = max(min_reach, min(max_reach, dist))

    cos_a = max(-1.0, min(1.0, (len1 ** 2 + dist ** 2 - len2 ** 2) / (2.0 * len1 * dist)))
    angle_a = math.acos(cos_a)
    joint_angle = math.atan2(dy, dx) + bend_dir * angle_a
    return (rx + len1 * math.cos(joint_angle), ry + len1 * math.sin(joint_angle))

def draw_smooth_limb(draw: ImageDraw.Draw, p0: tuple, p1: tuple, p2: tuple,
                     w0: float, w1: float, w2: float, fill_col: tuple, ink: tuple):
    for (ax, ay, wa), (bx, by, wb) in [((p0[0], p0[1], w0), (p1[0], p1[1], w1)), ((p1[0], p1[1], w1), (p2[0], p2[1], w2))]:
        dx, dy = bx - ax, by - ay
        ln = max(1e-5, math.hypot(dx, dy))
        nx, ny = -dy / ln, dx / ln
        poly = [
            (ax + nx * wa, ay + ny * wa),
            (bx + nx * wb, by + ny * wb),
            (bx - nx * wb, by - ny * wb),
            (ax - nx * wa, ay - ny * wa)
        ]
        draw.polygon(poly, fill=fill_col, outline=ink)
    rm = int(w1)
    draw.ellipse([p1[0] - rm, p1[1] - rm, p1[0] + rm, p1[1] + rm], fill=fill_col, outline=ink, width=2)

def draw_cartoon_hand(draw: ImageDraw.Draw, hx: float, hy: float, scale: float, skin: tuple, ink: tuple, pointing: bool = False):
    r = 6.5 * scale
    draw.ellipse([hx - r, hy - r, hx + r, hy + r], fill=skin, outline=ink, width=2)
    if pointing:
        draw.rounded_rectangle([hx + 2 * scale, hy - 3 * scale, hx + 16 * scale, hy + 2 * scale], radius=3, fill=skin, outline=ink, width=2)
    else:
        for f_off in (-3.5, 0.0, 3.5):
            draw.ellipse([hx + 3 * scale, hy + (f_off - 2) * scale, hx + 9 * scale, hy + (f_off + 2) * scale], fill=skin, outline=ink, width=1)

def draw_expressive_character(draw: ImageDraw.Draw, base_x: float, ground_y: float, scale: float,
                              style_mode: str, is_female: bool, action: str, emotion: str,
                              mouth_open: float, t: float, facing: int = 1):
    is_walk = action in {"walk", "window_exit"}
    is_run = action in {"run", "stairs_up"}
    is_jump = action == "jump"

    if is_run:
        cycle = t * 11.5
        stride_len = 48.0 * scale
        lift_h = 24.0 * scale
        pelvis_drop = abs(math.cos(cycle)) * 9.0 * scale
        spine_lean = 0.25 * facing
    elif is_walk:
        cycle = t * 6.5
        stride_len = 32.0 * scale
        lift_h = 13.0 * scale
        pelvis_drop = abs(math.cos(cycle)) * 5.0 * scale
        spine_lean = 0.07 * facing
    elif is_jump:
        jw = abs(math.sin(t * 4.5))
        cycle = 0.0
        stride_len = 0.0
        lift_h = 0.0
        pelvis_drop = -jw * 46.0 * scale + (1.0 - jw) * 8.0 * scale
        spine_lean = 0.10 * facing
    else:
        cycle = 0.0
        stride_len = 0.0
        lift_h = 0.0
        pelvis_drop = math.sin(t * 2.5) * 2.5 * scale
        spine_lean = math.sin(t * 1.4) * 0.03 + (0.05 * facing if action in {"point", "paper"} else 0.0)

    squash = 1.0 + (pelvis_drop / (125.0 * scale)) * 0.13
    stretch = 1.0 / math.sqrt(max(0.75, squash))

    thigh_len = 42.0 * scale
    shin_len = 42.0 * scale
    hip_x = base_x + math.sin(t * 2.0) * 2.0 * scale
    hip_y = ground_y - (thigh_len + shin_len - 6.0 * scale) + pelvis_drop

    spine_len = 64.0 * scale * stretch
    shoulder_x = hip_x + math.sin(spine_lean) * spine_len
    shoulder_y = hip_y - math.cos(spine_lean) * spine_len

    ink = (22, 26, 34)
    skin = (232, 236, 242) if style_mode == "stickman" else (255, 224, 198)
    cloth_top = (225, 230, 236) if style_mode == "stickman" else ((225, 29, 72) if is_female else (37, 99, 235))
    cloth_bot = (30, 41, 59) if style_mode == "cartoon" else (22, 26, 34)

    # Motion Smear Arcs when running/jumping
    if is_run or is_jump:
        for mi in range(3):
            my_arc = shoulder_y + mi * 22 * scale
            mx_arc = hip_x - (42 + mi * 12) * scale * facing
            draw.line([(mx_arc - 28 * scale * facing, my_arc), (mx_arc, my_arc)], fill=(255, 255, 255, 160), width=3)

    # 1. LEGS (2-Bone IK)
    if is_walk or is_run:
        lx = base_x + math.sin(cycle) * stride_len * facing
        ly = ground_y - max(0.0, math.cos(cycle)) * lift_h
        rx = base_x + math.sin(cycle + math.pi) * stride_len * facing
        ry = ground_y - max(0.0, math.cos(cycle + math.pi)) * lift_h
    elif is_jump:
        air = max(0.0, -pelvis_drop)
        lx, ly = base_x - 14.0 * scale, ground_y - air * 0.85
        rx, ry = base_x + 14.0 * scale, ground_y - air * 0.65
    else:
        lx, ly = base_x - 14.0 * scale, ground_y
        rx, ry = base_x + 14.0 * scale, ground_y

    l_hip = (hip_x - 7.0 * scale, hip_y)
    r_hip = (hip_x + 7.0 * scale, hip_y)
    l_knee = solve_2bone_ik(l_hip, (lx, ly), thigh_len, shin_len, bend_dir=facing)
    r_knee = solve_2bone_ik(r_hip, (rx, ry), thigh_len, shin_len, bend_dir=facing)

    wt = 8.0 * scale if style_mode == "cartoon" else 4.8 * scale
    wk = 6.2 * scale if style_mode == "cartoon" else 4.0 * scale
    wa = 4.8 * scale if style_mode == "cartoon" else 3.2 * scale

    draw_smooth_limb(draw, l_hip, l_knee, (lx, ly), wt, wk, wa, cloth_bot, ink)
    draw.pieslice([lx - 10 * scale, ly - 10 * scale, lx + 20 * scale, ly + 4 * scale], start=180, end=360, fill=(51, 65, 85), outline=ink, width=2)

    # 2. TORSO
    tw_top = (20.0 * scale * squash) if style_mode == "cartoon" else (6.0 * scale)
    tw_bot = (15.0 * scale * squash) if style_mode == "cartoon" else (5.0 * scale)
    px, py = math.cos(spine_lean), math.sin(spine_lean)
    torso_poly = [
        (shoulder_x - px * tw_top, shoulder_y - py * tw_top),
        (shoulder_x + px * tw_top, shoulder_y + py * tw_top),
        (hip_x + px * tw_bot, hip_y + py * tw_bot),
        (hip_x - px * tw_bot, hip_y - py * tw_bot)
    ]
    draw.polygon(torso_poly, fill=cloth_top, outline=ink)
    draw.line(torso_poly + [torso_poly[0]], fill=ink, width=3)

    draw_smooth_limb(draw, r_hip, r_knee, (rx, ry), wt, wk, wa, cloth_bot, ink)
    draw.pieslice([rx - 10 * scale, ry - 10 * scale, rx + 20 * scale, ry + 4 * scale], start=180, end=360, fill=(71, 85, 105), outline=ink, width=2)

    # 3. NATURAL CONVERSATIONAL ARMS (FORWARD BEND FOR GESTURES, BACKWARD FOR RUNNING!)
    upper_arm = 33.0 * scale
    forearm = 31.0 * scale
    l_sh = (shoulder_x - 10.0 * scale, shoulder_y + 4.0 * scale)
    r_sh = (shoulder_x + 10.0 * scale, shoulder_y + 4.0 * scale)
    arm_phase = cycle - 0.35

    if is_walk or is_run:
        swing = 34.0 * scale if is_run else 22.0 * scale
        lh_t = (l_sh[0] - math.sin(arm_phase) * swing * facing, l_sh[1] + 46.0 * scale)
        rh_t = (r_sh[0] + math.sin(arm_phase) * swing * facing, r_sh[1] + 46.0 * scale)
        elbow_dir = -facing
    elif action in {"point", "heroic"}:
        lh_t = (l_sh[0] - 8.0 * scale, hip_y + 2.0 * scale)
        rh_t = (r_sh[0] + 54.0 * scale * facing, shoulder_y - 10.0 * scale + math.sin(t * 6.0) * 5.0 * scale)
        elbow_dir = facing
    elif action == "wave":
        lh_t = (l_sh[0] - 8.0 * scale, hip_y + 2.0 * scale)
        rh_t = (r_sh[0] + (34.0 + math.sin(t * 11.0) * 16.0) * scale * facing, shoulder_y - 42.0 * scale)
        elbow_dir = facing
    elif action == "paper":
        lh_t = (r_sh[0] + 24.0 * scale * facing, shoulder_y + 18.0 * scale)
        rh_t = (r_sh[0] + 38.0 * scale * facing, shoulder_y + 14.0 * scale)
        elbow_dir = facing
    elif action in {"fear", "shock"}:
        lh_t = (shoulder_x - 22.0 * scale, shoulder_y - 24.0 * scale + math.sin(t * 18.0) * 3.0 * scale)
        rh_t = (shoulder_x + 28.0 * scale, shoulder_y - 22.0 * scale - math.sin(t * 18.0) * 3.0 * scale)
        elbow_dir = facing
    else:
        # Expressive human hand-talking rhythm synced to voice!
        gest = math.sin(t * 7.5) * 10.0 * scale * (0.3 + mouth_open)
        lh_t = (l_sh[0] + 12.0 * scale * facing, shoulder_y + 26.0 * scale - gest * 0.6)
        rh_t = (r_sh[0] + 34.0 * scale * facing, shoulder_y + 18.0 * scale + gest)
        elbow_dir = facing

    l_elb = solve_2bone_ik(l_sh, lh_t, upper_arm, forearm, bend_dir=elbow_dir)
    r_elb = solve_2bone_ik(r_sh, rh_t, upper_arm, forearm, bend_dir=elbow_dir)

    wa0 = 6.0 * scale if style_mode == "cartoon" else 4.0 * scale
    wa1 = 5.0 * scale if style_mode == "cartoon" else 3.4 * scale
    wa2 = 4.0 * scale if style_mode == "cartoon" else 2.8 * scale

    draw_smooth_limb(draw, l_sh, l_elb, lh_t, wa0, wa1, wa2, skin, ink)
    draw_smooth_limb(draw, r_sh, r_elb, rh_t, wa0, wa1, wa2, skin, ink)

    draw_cartoon_hand(draw, lh_t[0], lh_t[1], scale, skin, ink, pointing=False)
    draw_cartoon_hand(draw, rh_t[0], rh_t[1], scale, skin, ink, pointing=(action in {"point", "heroic"}))

    if action == "paper":
        px_p, py_p = rh_t
        draw.polygon([
            (px_p - 6 * scale, py_p - 26 * scale),
            (px_p + 26 * scale, py_p - 30 * scale),
            (px_p + 30 * scale, py_p + 12 * scale),
            (px_p - 2 * scale, py_p + 16 * scale)
        ], fill=(255, 255, 255), outline=ink, width=2)
        # Sketch text lines on paper
        for ln_i in range(3):
            yy = py_p - (16 - ln_i * 8) * scale
            draw.line([(px_p + 2 * scale, yy), (px_p + 20 * scale, yy)], fill=(100, 116, 139), width=2)

    # 4. EXPRESSIVE HEAD WITH EAR, NOSE, PROPER HAIR & AUDIO LIP-SYNC
    head_nod = math.sin(t * 8.5) * 3.5 * scale * mouth_open
    head_x = shoulder_x + math.sin(spine_lean) * 14.0 * scale
    head_y = shoulder_y - 42.0 * scale + head_nod

    hr_x = 34.0 * scale * squash
    hr_y = 38.0 * scale * stretch

    # Back hair ponytail for female character (clean, NEVER covers eyes like a mask!)
    if style_mode == "cartoon" and is_female:
        ponytail = [
            (head_x - hr_x * 0.7, head_y - hr_y * 0.3),
            (head_x - hr_x * 1.45, head_y + hr_y * 0.6),
            (head_x - hr_x * 0.85, head_y + hr_y * 0.9)
        ]
        draw.polygon(ponytail, fill=(30, 27, 75), outline=ink)

    # Head oval
    draw.ellipse([head_x - hr_x, head_y - hr_y, head_x + hr_x, head_y + hr_y], fill=skin, outline=ink, width=3)

    # Ear on back side of head
    ear_x = head_x - hr_x * 0.85 * facing
    draw.ellipse([ear_x - 6 * scale, head_y - 6 * scale, ear_x + 6 * scale, head_y + 8 * scale], fill=skin, outline=ink, width=2)

    # Clean Hair Top Cap (leaves forehead & eyes 100% visible!)
    if style_mode == "cartoon":
        hair_col = (88, 28, 135) if is_female else (24, 28, 36)
        draw.pieslice(
            [head_x - hr_x - 2, head_y - hr_y - 6, head_x + hr_x + 2, head_y - hr_y * 0.25],
            start=180, end=360, fill=hair_col, outline=ink, width=2
        )

    # Big Overlapping Cartoon Eyes
    blink = (int(t * 10) % 34 == 0)
    eye_y = head_y - 5.0 * scale
    eye_rx = 11.0 * scale
    eye_ry = 13.5 * scale
    look_shift = 5.0 * scale * facing

    for ex in (head_x - 5.0 * scale + look_shift, head_x + 11.0 * scale + look_shift):
        if blink:
            draw.line([(ex - eye_rx, eye_y), (ex + eye_rx, eye_y)], fill=ink, width=3)
        else:
            draw.ellipse([ex - eye_rx, eye_y - eye_ry, ex + eye_rx, eye_y + eye_ry], fill=(255, 255, 255), outline=ink, width=2)
            pr = 3.8 * scale
            draw.ellipse([ex - pr + 3 * facing, eye_y - pr, ex + pr + 3 * facing, eye_y + pr], fill=ink)

    # Expressive Cartoon Nose (matches reference video!)
    nx_pos = head_x + 18.0 * scale * facing
    ny_pos = head_y + 5.0 * scale
    draw.arc([nx_pos - 6 * scale, ny_pos - 6 * scale, nx_pos + 10 * scale, ny_pos + 7 * scale], start=250, end=110, fill=ink, width=3)

    # Eyebrows
    brow_y = eye_y - eye_ry - 4.0 * scale
    tilt = 4.0 * scale if emotion in {"ANGRY", "HEROIC"} else (-4.0 * scale if emotion in {"SAD", "FEAR", "SHOCK"} else 0.0)
    draw.line([(head_x - 15 * scale + look_shift, brow_y - tilt), (head_x - 1 * scale + look_shift, brow_y + tilt)], fill=ink, width=3)
    draw.line([(head_x + 4 * scale + look_shift, brow_y + tilt), (head_x + 18 * scale + look_shift, brow_y - tilt)], fill=ink, width=3)

    # Audio-Synced Lip-Sync Mouth
    my = head_y + 18.0 * scale
    mx = head_x + look_shift
    if mouth_open > 0.10 or emotion in {"FEAR", "SHOCK"}:
        open_v = max(mouth_open, 0.55 if emotion in {"FEAR", "SHOCK"} else 0.0)
        mw = (9.0 + open_v * 4.5) * scale
        mh = (4.5 + open_v * 13.0) * scale
        draw.ellipse([mx - mw, my - mh / 2, mx + mw, my + mh / 2], fill=(185, 28, 28), outline=ink, width=2)
        draw.ellipse([mx - mw * 0.55, my, mx + mw * 0.55, my + mh * 0.45], fill=(244, 63, 94))
    else:
        if emotion in {"HAPPY", "ROMANTIC"}:
            draw.arc([mx - 10 * scale, my - 5 * scale, mx + 10 * scale, my + 7 * scale], start=10, end=170, fill=ink, width=3)
        else:
            draw.line([(mx - 8 * scale, my), (mx + 8 * scale, my)], fill=ink, width=3)


# ===========================================================================
# 4. CINEMATIC CAMERA DIRECTOR & FRAME COMPOSITOR
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

def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)

def parse_script_beats(raw_script: str):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip()]
    beats = []
    current_scene = "modern city street with houses and trees"

    for ln in lines:
        if re.match(r"^SCENE\s*\d*", ln, re.I):
            loc = re.sub(r"^SCENE\s*\d*\s*[:\-]?\s*", "", ln, flags=re.I).strip()
            if loc:
                current_scene = loc
            continue

        tags = []
        caption = re.sub(r"\[([^\]]+)\]", lambda m: (tags.append(m.group(1).strip()), "")[1], ln).strip()
        if not caption:
            continue

        speaker = "Narrator"
        emotion = "HAPPY"
        action = "auto"

        for tg in tags:
            u = tg.upper()
            if u in {"HAPPY", "SAD", "SHOCK", "ROMANTIC", "CONFUSED", "FEAR", "CHILL", "ANGRY", "HEROIC"}:
                emotion = u
            elif u in {"WALK", "RUN", "JUMP", "WAVE", "POINT", "PAPER", "FIGHT", "WINDOW_EXIT", "STAIRS_UP"}:
                action = u.lower()
            elif u not in {"FULL", "HALF", "WIDE", "MEDIUM", "CLOSEUP", "OTS", "ORBIT"}:
                speaker = tg

        low = (current_scene + " " + caption).lower()
        if action == "auto":
            if any(k in low for k in ["window", "khidki", "janala", "kood"]):
                action = "window_exit"
            elif any(k in low for k in ["run", "bhag", "daud"]):
                action = "run"
            elif any(k in low for k in ["walk", "road", "sadak", "chal"]):
                action = "walk"
            elif any(k in low for k in ["jump", "uchhal"]):
                action = "jump"
            elif any(k in low for k in ["paper", "script", "plan", "book"]):
                action = "paper"
            elif any(k in low for k in ["point", "dekho", "bata"]):
                action = "point"
            elif any(k in low for k in ["wave", "haath hila"]):
                action = "wave"
            else:
                action = "talk"

        is_female = bool(re.search(r"(girl|meera|ananya|priya|aditi|neha|sara|riya|sakchunni|maa|mom)", speaker.lower()))

        beats.append({
            "index": len(beats) + 1,
            "scene": current_scene,
            "speaker": "" if speaker.upper() == "NARRATOR" else speaker,
            "is_female": is_female,
            "emotion": emotion,
            "action": action,
            "caption": caption,
            "words": caption.split()
        })
    return beats

def render_studio_frame(out_w: int, out_h: int, beat: dict, prog: float, global_t: float,
                        mouth_open: float, style_mode: str) -> bytes:
    eased = ease_in_out(prog)
    canvas, ground_y, glows, is_dark_only = draw_studio_environment(out_w, out_h, beat["scene"] + " " + beat["caption"], global_t, prog)
    draw = ImageDraw.Draw(canvas)

    scale_base = out_w / 540.0
    action = beat["action"]

    if not is_dark_only:
        if action in {"walk", "run", "window_exit"}:
            char_x = out_w * (0.24 + eased * 0.50)
        else:
            char_x = out_w * 0.48

        # Ground Contact Shadow
        sh_rx = int(38 * scale_base)
        sh_ry = int(9 * scale_base)
        draw.ellipse([char_x - sh_rx, ground_y - sh_ry, char_x + sh_rx, ground_y + sh_ry], fill=(15, 23, 42, 115))

        draw_expressive_character(
            draw=draw,
            base_x=char_x,
            ground_y=ground_y,
            scale=scale_base * 1.24,
            style_mode=style_mode,
            is_female=beat["is_female"],
            action=action,
            emotion=beat["emotion"],
            mouth_open=mouth_open,
            t=global_t,
            facing=1
        )

    final_img = add_cinema_vignette_and_glow(canvas, glows)

    # Dynamic Camera Zoom on Medium/Close-up Beats (like a real director!)
    if beat["index"] % 2 == 0 and not is_dark_only:
        zoom = 1.04 + eased * 0.09
        cw, ch = int(out_w / zoom), int(out_h / zoom)
        x0 = (out_w - cw) // 2
        y0 = int((out_h - ch) * 0.35)
        final_img = final_img.crop((x0, y0, x0 + cw, y0 + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR)

    # Clean Studio Captions
    draw_f = ImageDraw.Draw(final_img)
    words = beat.get("words", [])
    if words:
        active_idx = min(len(words) - 1, int(prog * len(words)))
        font_size = max(21, int(out_w * 0.044)) if out_h > out_w else max(23, int(out_w * 0.026))
        font = get_font(font_size)
        space_w = int(font_size * 0.35)
        max_line_w = int(out_w * 0.86)

        lines, curr_line, curr_w = [], [], 0
        for idx, wd in enumerate(words):
            bbox = draw_f.textbbox((0, 0), wd, font=font)
            ww = bbox[2] - bbox[0]
            if curr_line and curr_w + space_w + ww > max_line_w:
                lines.append(curr_line)
                curr_line = [(wd, idx, ww)]
                curr_w = ww
            else:
                curr_line.append((wd, idx, ww))
                curr_w += (space_w if len(curr_line) > 1 else 0) + ww
        if curr_line:
            lines.append(curr_line)

        line_h = int(font_size * 1.32)
        top_y = int(out_h * 0.90) - (len(lines) * line_h) // 2

        for l_idx, tokens in enumerate(lines):
            row_w = sum(tk[2] for tk in tokens) + space_w * (len(tokens) - 1)
            cx = (out_w - row_w) // 2
            cy = top_y + l_idx * line_h
            for wd, idx, ww in tokens:
                col = (250, 204, 21) if (idx == active_idx) else (255, 255, 255)
                draw_f.text((cx, cy), wd, font=font, fill=(*col, 255), stroke_width=3, stroke_fill=(15, 23, 42, 255))
                cx += ww + space_w

    return final_img.convert("RGB").tobytes()


# ===========================================================================
# 5. FULL RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_animation_job(job_id: str, script_text: str, ratio: str, style_mode: str,
                      voice_mode: str, lang_mode: str, custom_wav_path):
    job = JOBS[job_id]
    try:
        beats = parse_script_beats(script_text)
        if not beats:
            job["status"] = "error"
            job["message"] = "Script is empty!"
            return

        out_w, out_h = (576, 1024) if ratio == "9:16" else (1024, 576)
        fps = 24
        total = len(beats)

        job["message"] = "Generating Voiceover & Audio Lip-Sync Track..."
        job["progress"] = 10

        master_wav = os.path.join(AUDIO_DIR, f"{job_id}_master.wav")
        beat_durations = []

        if voice_mode == "manual" and custom_wav_path and os.path.exists(custom_wav_path):
            total_audio_dur = convert_custom_audio_to_wav(custom_wav_path, master_wav)
            total_words = sum(max(1, len(b["words"])) for b in beats)
            for b in beats:
                share = max(1, len(b["words"])) / float(total_words)
                beat_durations.append(max(1.5, total_audio_dur * share))
        else:
            seg_wavs = []
            for idx, b in enumerate(beats):
                seg_path = os.path.join(AUDIO_DIR, f"{job_id}_seg_{idx}.wav")
                dur = generate_tts_wav(b["caption"], b["is_female"], lang_mode, seg_path)
                beat_durations.append(dur)
                seg_wavs.append(seg_path)
                job["progress"] = 10 + int(((idx + 1) / total) * 20)

            concat_txt = os.path.join(AUDIO_DIR, f"{job_id}_list.txt")
            with open(concat_txt, "w") as cf:
                for sw in seg_wavs:
                    cf.write(f"file '{sw}'\n")
            subprocess.run(
                [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", master_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )

        lip_envelope = extract_lip_envelope(master_wav, fps)

        out_name = f"NEXORA_PRO_2D_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", master_wav,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "21",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        frame_cursor = 0
        for i, b in enumerate(beats):
            dur = beat_durations[i]
            n_frames = max(1, int(dur * fps))

            for f in range(n_frames):
                prog = f / max(1, n_frames - 1)
                mouth_val = lip_envelope[frame_cursor] if frame_cursor < len(lip_envelope) else 0.0
                raw_bytes = render_studio_frame(out_w, out_h, b, prog, global_t, mouth_val, style_mode)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps
                frame_cursor += 1

            job["progress"] = 30 + int(((i + 1) / total) * 68)
            job["message"] = f"Rendering Studio Scene {i + 1}/{total}..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Ready to Watch & Download ({out_name})"

    except Exception as exc:
        job["status"] = "error"
        job["message"] = f"Animation Error: {exc}"

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
    custom_wav_path = None

    if request.content_type and "multipart/form-data" in request.content_type:
        script_text = request.form.get("script", "")
        ratio = request.form.get("ratio", "9:16")
        style_mode = request.form.get("style", "cartoon")
        voice_mode = request.form.get("voice_mode", "auto")
        lang_mode = request.form.get("lang_mode", "HINDI")
        if "voice_file" in request.files and request.files["voice_file"].filename:
            vf = request.files["voice_file"]
            custom_wav_path = os.path.join(AUDIO_DIR, f"{job_id}_upload_{vf.filename}")
            vf.save(custom_wav_path)
    else:
        data = request.get_json(force=True)
        script_text = data.get("script", "")
        ratio = data.get("ratio", "9:16")
        style_mode = data.get("style", "cartoon")
        voice_mode = data.get("voice_mode", "auto")
        lang_mode = data.get("lang_mode", "HINDI")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Building Studio Environment & Character Rig..."}
    threading.Thread(
        target=run_animation_job,
        args=(job_id, script_text, ratio, style_mode, voice_mode, lang_mode, custom_wav_path),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    return jsonify(JOBS.get(job_id, {"status": "error", "message": "Job not found"}))

@app.route("/api/video/<job_id>")
def api_video(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("file_path") or not os.path.exists(job["file_path"]):
        return "Not ready", 404
    return send_file(job["file_path"], as_attachment=(request.args.get("dl") == "1"), mimetype="video/mp4")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
