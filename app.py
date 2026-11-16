import os
import re
import time
import math
import uuid
import wave
import asyncio
import subprocess
import urllib.parse
import urllib.request
import threading
import numpy as np
from io import BytesIO

import edge_tts
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
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
BG_CACHE_DIR = os.path.join(BASE_DIR, "bg_cache")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(BG_CACHE_DIR, exist_ok=True)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
JOBS = {}

# ===========================================================================
# 1. VOICE & WAVEFORM LIP-SYNC ENGINE (AUTO TTS + MANUAL VOICE)
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
        raw_env.append(min(1.0, max(0.0, (rms - 0.015) * 10.0)))

    # Temporal smoothing for natural jaw inertia
    smooth_env = []
    prev = 0.0
    for v in raw_env:
        cur = prev * 0.35 + v * 0.65
        smooth_env.append(cur)
        prev = cur
    return smooth_env if smooth_env else [0.0]


# ===========================================================================
# 2. HAND-DRAWN 2D SKETCH BACKGROUND GENERATOR (NO TOY BOXES!)
# ===========================================================================
def build_procedural_sketch_bg(w: int, h: int, scene_text: str) -> Image.Image:
    """High-craft architectural perspective sketch fallback if network is busy."""
    low = scene_text.lower()
    is_dark = any(k in low for k in ["night", "raat", "dark", "horror", "bhoot", "andhera"])
    bg_tone = (32, 36, 44) if is_dark else (228, 232, 236)
    ink = (210, 218, 226) if is_dark else (48, 54, 62)

    img = Image.new("RGB", (w, h), bg_tone)
    d = ImageDraw.Draw(img)

    # Perspective vanishing point lines for depth
    vp_x, vp_y = int(w * 0.5), int(h * 0.48)
    horizon_y = int(h * 0.68)
    d.line([(0, horizon_y), (w, horizon_y)], fill=ink, width=3)

    # Architectural perspective buildings / room walls
    for x_off in [0.12, 0.28, 0.72, 0.88]:
        bx = int(w * x_off)
        d.line([(bx, int(h * 0.15)), (bx, horizon_y)], fill=ink, width=2)
        d.line([(bx, int(h * 0.15)), (vp_x, vp_y)], fill=(*ink[:3],), width=1)

    # Ground perspective grid lines
    for gx in range(-w, w * 2, int(w * 0.18)):
        d.line([(vp_x, horizon_y), (gx, h)], fill=ink, width=2)

    # Subtle paper grain & vignette
    vig = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vig)
    for b in range(30):
        alpha = int((30 - b) * 2.8)
        vd.rectangle([b * 3, b * 3, w - b * 3, h - b * 3], outline=(12, 15, 22, alpha), width=3)
    return Image.alpha_composite(img.convert("RGBA"), vig).convert("RGB")

def get_scene_background(scene_desc: str, out_w: int, out_h: int, seed: int) -> Image.Image:
    """Fetches a clean hand-drawn 2D cartoon/sketch background with NO characters."""
    gen_w = int(out_w * 1.15)
    gen_h = int(out_h * 1.15)
    req_w = 512 if out_h > out_w else 896
    req_h = 896 if out_h > out_w else 512

    clean_sc = re.sub(r"[^a-zA-Z0-9\s,.-]", "", scene_desc).strip()[:70]
    prompt = (
        f"hand-drawn 2D cartoon animation background scenery, Not Your Type YouTube animation art style, "
        f"{clean_sc}, clean architectural perspective, soft shading, empty background with no people, no characters, no text"
    )
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width={req_w}&height={req_h}&seed={seed}&model=flux&nologo=true"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.0.1 Safari/537.36"}

    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=18) as resp:
                raw = resp.read()
                if len(raw) > 6000:
                    bg = Image.open(BytesIO(raw)).convert("RGB")
                    bg = bg.resize((gen_w, gen_h), Image.Resampling.LANCZOS)
                    # Slight soften so foreground IK character pops cleanly like a real studio!
                    bg = bg.filter(ImageFilter.GaussianBlur(radius=1.2))
                    return bg
        except Exception:
            time.sleep(0.8)

    return build_procedural_sketch_bg(gen_w, gen_h, scene_desc)


# ===========================================================================
# 3. 2-BONE INVERSE KINEMATICS (IK) BIOMECHANICAL RIG & NATURAL MOTION
# ===========================================================================
def solve_2bone_ik(root: tuple, target: tuple, len1: float, len2: float, bend_dir: int = 1) -> tuple:
    """
    Exact 2-Bone Inverse Kinematics solver!
    Given hip/shoulder (root) and foot/hand (target), computes natural anatomical knee/elbow joint.
    """
    rx, ry = root
    tx, ty = target
    dx = tx - rx
    dy = ty - ry
    dist = math.hypot(dx, dy)

    # Clamp reach so limbs never hyper-extend or break
    max_reach = (len1 + len2) * 0.985
    min_reach = abs(len1 - len2) + 2.0
    dist = max(min_reach, min(max_reach, dist))

    # Law of cosines for shoulder/hip angle
    cos_a = (len1 ** 2 + dist ** 2 - len2 ** 2) / (2.0 * len1 * dist)
    cos_a = max(-1.0, min(1.0, cos_a))
    angle_a = math.acos(cos_a)

    base_angle = math.atan2(dy, dx)
    joint_angle = base_angle + bend_dir * angle_a

    jx = rx + len1 * math.cos(joint_angle)
    jy = ry + len1 * math.sin(joint_angle)
    return (jx, jy)

def draw_tapered_limb(draw: ImageDraw.Draw, p0: tuple, p1: tuple, p2: tuple,
                      w0: float, w1: float, w2: float, fill_col: tuple, outline_col: tuple):
    """Draws organic tapered anatomical limb segments with smooth circular joints."""
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
        draw.polygon(poly, fill=fill_col, outline=outline_col)
    # Smooth elbow/knee cap
    r_mid = int(w1)
    draw.ellipse([p1[0] - r_mid, p1[1] - r_mid, p1[0] + r_mid, p1[1] + r_mid], fill=fill_col, outline=outline_col, width=2)

def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)

def draw_human_ik_character(draw: ImageDraw.Draw, base_x: float, ground_y: float, scale: float,
                            style_mode: str, is_female: bool, action: str, emotion: str,
                            mouth_open: float, t: float, prog: float, facing: int = 1):
    """
    Human-like biomechanical character animation using 2-Bone IK, spine lean,
    weight-shift pelvis bounce, overlapping arm swing, and squash/stretch.
    """
    is_walk = action in {"walk", "window_exit"}
    is_run = action in {"run", "stairs_up"}
    is_jump = action in {"jump"}

    # 1. BIOMECHANICAL GAIT CYCLE (Pelvis Bounce + Spine Lean)
    if is_run:
        cycle = t * 11.5
        stride_len = 52.0 * scale
        lift_h = 26.0 * scale
        pelvis_drop = abs(math.cos(cycle)) * 10.0 * scale
        spine_lean = 0.24 * facing  # Lean forward dynamically when running!
    elif is_walk:
        cycle = t * 6.8
        stride_len = 34.0 * scale
        lift_h = 14.0 * scale
        pelvis_drop = abs(math.cos(cycle)) * 5.5 * scale
        spine_lean = 0.08 * facing
    elif is_jump:
        jump_wave = abs(math.sin(t * 4.5))
        cycle = 0.0
        stride_len = 12.0 * scale
        lift_h = 0.0
        pelvis_drop = -jump_wave * 48.0 * scale + (1.0 - jump_wave) * 10.0 * scale
        spine_lean = 0.12 * facing
    else:
        # Natural living idle breathing & subtle weight shift
        cycle = 0.0
        stride_len = 0.0
        lift_h = 0.0
        pelvis_drop = math.sin(t * 2.6) * 2.5 * scale
        spine_lean = math.sin(t * 1.4) * 0.03 + (0.06 * facing if action in {"point", "paper"} else 0.0)

    # Squash & Stretch factor for expressive cartoon life
    squash = 1.0 + (pelvis_drop / (120.0 * scale)) * 0.14
    stretch = 1.0 / math.sqrt(max(0.75, squash))

    thigh_len = 44.0 * scale
    shin_len = 44.0 * scale
    leg_total = thigh_len + shin_len

    hip_x = base_x + math.sin(t * 2.0) * 2.0 * scale
    hip_y = ground_y - (leg_total - 6.0 * scale) + pelvis_drop

    # Spine vector (from Hip to Neck/Shoulders)
    spine_len = 66.0 * scale * stretch
    shoulder_x = hip_x + math.sin(spine_lean) * spine_len
    shoulder_y = hip_y - math.cos(spine_lean) * spine_len

    # Palette
    ink = (20, 24, 32)
    skin = (232, 236, 242) if style_mode == "stickman" else (255, 224, 198)
    cloth_top = (225, 230, 236) if style_mode == "stickman" else ((225, 29, 72) if is_female else (37, 99, 235))
    cloth_bot = (30, 41, 59) if style_mode == "cartoon" else (20, 24, 32)

    # 2. COMPUTE FOOT TARGETS & SOLVE 2-BONE KNEE IK
    if is_walk or is_run:
        # Phase-shifted foot trajectories (one planted on ground, one lifting forward in arc)
        ph_l = cycle
        ph_r = cycle + math.pi

        lx = base_x + math.sin(ph_l) * stride_len * facing
        ly = ground_y - max(0.0, math.cos(ph_l)) * lift_h

        rx = base_x + math.sin(ph_r) * stride_len * facing
        ry = ground_y - max(0.0, math.cos(ph_r)) * lift_h
    elif is_jump:
        air = max(0.0, -pelvis_drop)
        lx = base_x - 14.0 * scale
        ly = ground_y - air * 0.85
        rx = base_x + 14.0 * scale
        ry = ground_y - air * 0.70
    else:
        # Natural contrapposto stance (feet planted firmly on ground!)
        lx = base_x - 15.0 * scale
        ly = ground_y
        rx = base_x + 15.0 * scale
        ry = ground_y

    l_hip = (hip_x - 8.0 * scale, hip_y)
    r_hip = (hip_x + 8.0 * scale, hip_y)

    # Knees bend forward in direction character is facing!
    l_knee = solve_2bone_ik(l_hip, (lx, ly), thigh_len, shin_len, bend_dir=facing)
    r_knee = solve_2bone_ik(r_hip, (rx, ry), thigh_len, shin_len, bend_dir=facing)

    # Back Leg first, then Torso, then Front Leg (proper 3D depth layering!)
    w_thigh = 8.5 * scale if style_mode == "cartoon" else 5.0 * scale
    w_knee = 6.5 * scale if style_mode == "cartoon" else 4.2 * scale
    w_ankle = 5.0 * scale if style_mode == "cartoon" else 3.5 * scale

    draw_tapered_limb(draw, l_hip, l_knee, (lx, ly), w_thigh, w_knee, w_ankle, cloth_bot, ink)
    # Foot / Shoe
    draw.pieslice(
        [lx - 10 * scale, ly - 10 * scale, lx + 20 * scale * facing if facing == 1 else lx + 10 * scale, ly + 4 * scale],
        start=180, end=360, fill=(51, 65, 85), outline=ink, width=2
    )

    # 3. ORGANIC TORSO (Tapered Spine Shape)
    torso_w_top = (21.0 * scale * squash) if style_mode == "cartoon" else (6.0 * scale)
    torso_w_bot = (16.0 * scale * squash) if style_mode == "cartoon" else (5.0 * scale)
    perp_x = math.cos(spine_lean)
    perp_y = math.sin(spine_lean)

    torso_poly = [
        (shoulder_x - perp_x * torso_w_top, shoulder_y - perp_y * torso_w_top),
        (shoulder_x + perp_x * torso_w_top, shoulder_y + perp_y * torso_w_top),
        (hip_x + perp_x * torso_w_bot, hip_y + perp_y * torso_w_bot),
        (hip_x - perp_x * torso_w_bot, hip_y - perp_y * torso_w_bot)
    ]
    draw.polygon(torso_poly, fill=cloth_top, outline=ink)
    draw.line(torso_poly + [torso_poly[0]], fill=ink, width=3)

    # Front Leg
    draw_tapered_limb(draw, r_hip, r_knee, (rx, ry), w_thigh, w_knee, w_ankle, cloth_bot, ink)
    draw.pieslice(
        [rx - 10 * scale, ry - 10 * scale, rx + 20 * scale, ry + 4 * scale],
        start=180, end=360, fill=(71, 85, 105), outline=ink, width=2
    )

    # 4. COMPUTE HAND TARGETS & SOLVE 2-BONE ELBOW IK (WITH OVERLAPPING ACTION)
    upper_arm = 34.0 * scale
    forearm = 32.0 * scale
    l_sh = (shoulder_x - 10.0 * scale, shoulder_y + 4.0 * scale)
    r_sh = (shoulder_x + 10.0 * scale, shoulder_y + 4.0 * scale)

    # Delayed secondary phase for natural overlapping arm swing
    arm_phase = cycle - 0.35

    if is_walk or is_run:
        swing = 36.0 * scale if is_run else 24.0 * scale
        lh_target = (l_sh[0] - math.sin(arm_phase) * swing * facing, l_sh[1] + 50.0 * scale - abs(math.sin(arm_phase)) * 12.0 * scale)
        rh_target = (r_sh[0] + math.sin(arm_phase) * swing * facing, r_sh[1] + 50.0 * scale - abs(math.sin(arm_phase)) * 12.0 * scale)
    elif action in {"point", "heroic"}:
        # Expressive pointing gesture with subtle breathing drift
        lh_target = (l_sh[0] - 12.0 * scale, hip_y + 4.0 * scale)
        rh_target = (r_sh[0] + 56.0 * scale * facing, shoulder_y - 12.0 * scale + math.sin(t * 5.0) * 4.0 * scale)
    elif action == "wave":
        lh_target = (l_sh[0] - 12.0 * scale, hip_y + 4.0 * scale)
        rh_target = (r_sh[0] + (36.0 + math.sin(t * 11.0) * 16.0) * scale * facing, shoulder_y - 42.0 * scale)
    elif action == "paper":
        # Both hands holding script paper naturally in front of chest
        lh_target = (r_sh[0] + 24.0 * scale * facing, shoulder_y + 18.0 * scale)
        rh_target = (r_sh[0] + 38.0 * scale * facing, shoulder_y + 14.0 * scale)
    elif action in {"fear", "shock"}:
        # Hands raised near face in shock!
        lh_target = (shoulder_x - 24.0 * scale, shoulder_y - 26.0 * scale + math.sin(t * 18.0) * 3.0 * scale)
        rh_target = (shoulder_x + 28.0 * scale, shoulder_y - 24.0 * scale - math.sin(t * 18.0) * 3.0 * scale)
    else:
        # Conversational hand gestures synced with speaking!
        talk_gest = mouth_open * 16.0 * scale
        lh_target = (l_sh[0] - 14.0 * scale, hip_y + 2.0 * scale - talk_gest * 0.4)
        rh_target = (r_sh[0] + (22.0 + talk_gest) * scale * facing, shoulder_y + 32.0 * scale - talk_gest)

    # Elbows bend naturally backward (-facing)
    l_elbow = solve_2bone_ik(l_sh, lh_target, upper_arm, forearm, bend_dir=-facing)
    r_elbow = solve_2bone_ik(r_sh, rh_target, upper_arm, forearm, bend_dir=-facing)

    w_arm0 = 6.5 * scale if style_mode == "cartoon" else 4.2 * scale
    w_arm1 = 5.2 * scale if style_mode == "cartoon" else 3.6 * scale
    w_arm2 = 4.2 * scale if style_mode == "cartoon" else 3.0 * scale

    draw_tapered_limb(draw, l_sh, l_elbow, lh_target, w_arm0, w_arm1, w_arm2, skin, ink)
    draw_tapered_limb(draw, r_sh, r_elbow, rh_target, w_arm0, w_arm1, w_arm2, skin, ink)

    # Hands
    hr = 6.5 * scale
    for hx, hy in (lh_target, rh_target):
        draw.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=skin, outline=ink, width=2)

    if action == "paper":
        px, py = rh_target
        draw.polygon([
            (px - 6 * scale, py - 24 * scale),
            (px + 24 * scale, py - 28 * scale),
            (px + 28 * scale, py + 12 * scale),
            (px - 2 * scale, py + 16 * scale)
        ], fill=(255, 255, 255), outline=ink, width=2)

    # 5. EXPRESSIVE HEAD (SQUASH/STRETCH + HEAD TILT + BLINKING + AUDIO LIP-SYNC)
    head_nod = math.sin(t * 8.5) * 3.5 * scale * mouth_open
    head_x = shoulder_x + math.sin(spine_lean) * 14.0 * scale
    head_y = shoulder_y - 42.0 * scale + head_nod

    hr_x = 34.0 * scale * squash
    hr_y = 38.0 * scale * stretch

    draw.ellipse([head_x - hr_x, head_y - hr_y, head_x + hr_x, head_y + hr_y], fill=skin, outline=ink, width=3)

    if style_mode == "cartoon":
        if is_female:
            draw.pieslice([head_x - hr_x - 3, head_y - hr_y - 5, head_x + hr_x + 3, head_y + hr_y + 14], start=170, end=10, fill=(30, 27, 75), outline=ink, width=2)
        else:
            draw.pieslice([head_x - hr_x - 2, head_y - hr_y - 7, head_x + hr_x + 2, head_y], start=180, end=360, fill=(30, 41, 59))

    # Big Overlapping Cartoon Eyes (Not Your Type signature look)
    blink = (int(t * 10) % 34 == 0)
    eye_y = head_y - 6.0 * scale
    eye_rx = 11.5 * scale
    eye_ry = 14.0 * scale
    look_shift = 4.0 * scale * facing

    for ex in (head_x - 6.0 * scale + look_shift, head_x + 11.0 * scale + look_shift):
        if blink:
            draw.line([(ex - eye_rx, eye_y), (ex + eye_rx, eye_y)], fill=ink, width=3)
        else:
            draw.ellipse([ex - eye_rx, eye_y - eye_ry, ex + eye_rx, eye_y + eye_ry], fill=(255, 255, 255), outline=ink, width=2)
            pr = 3.8 * scale
            draw.ellipse([ex - pr + 2 * facing, eye_y - pr, ex + pr + 2 * facing, eye_y + pr], fill=ink)

    # Expressive Eyebrows
    brow_y = eye_y - eye_ry - 4.0 * scale
    tilt = 4.0 * scale if emotion in {"ANGRY", "HEROIC"} else (-4.0 * scale if emotion in {"SAD", "FEAR", "SHOCK"} else 0.0)
    draw.line([(head_x - 16 * scale + look_shift, brow_y - tilt), (head_x - 2 * scale + look_shift, brow_y + tilt)], fill=ink, width=3)
    draw.line([(head_x + 4 * scale + look_shift, brow_y + tilt), (head_x + 18 * scale + look_shift, brow_y - tilt)], fill=ink, width=3)

    # Audio-Synced Lip-Sync Mouth
    my = head_y + 17.0 * scale
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
# 4. FRAME COMPOSITOR & SCRIPT PARSER
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

def parse_script_beats(raw_script: str):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip()]
    beats = []
    current_scene = "modern city street with trees and houses"

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
            elif any(k in low for k in ["paper", "script", "plan"]):
                action = "paper"
            elif any(k in low for k in ["point", "dekho", "bata"]):
                action = "point"
            else:
                action = "wave"

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

def render_ik_scene_frame(bg_img: Image.Image, out_w: int, out_h: int, beat: dict,
                          prog: float, global_t: float, mouth_open: float, style_mode: str) -> bytes:
    # Smooth Ease-In/Ease-Out Camera Pan & Zoom on Hand-Drawn Sketch Background
    eased = ease_in_out(prog)
    zoom = 1.03 + eased * 0.08
    sw, sh = bg_img.size
    cw, ch = int(out_w / zoom), int(out_h / zoom)
    dx, dy = max(0, sw - cw), max(0, sh - ch)

    pan_dir = eased if (beat["index"] % 2 == 1) else (1.0 - eased)
    x0 = max(0, min(dx, int(dx * pan_dir)))
    y0 = max(0, min(dy, int(dy * 0.5)))

    frame = bg_img.crop((x0, y0, x0 + cw, y0 + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR).convert("RGBA")
    draw = ImageDraw.Draw(frame)

    scale_base = out_w / 540.0
    ground_y = out_h * 0.78
    action = beat["action"]

    # Smooth Ease-In/Out Character Trajectory across the scene
    if action in {"walk", "run", "window_exit"}:
        char_x = out_w * (0.24 + eased * 0.50)
    else:
        char_x = out_w * 0.50

    # Ground Contact Shadow (anchors character realistically onto the floor/road!)
    sh_rx = int(38 * scale_base)
    sh_ry = int(9 * scale_base)
    draw.ellipse([char_x - sh_rx, ground_y - sh_ry, char_x + sh_rx, ground_y + sh_ry], fill=(15, 23, 42, 110))

    draw_human_ik_character(
        draw=draw,
        base_x=char_x,
        ground_y=ground_y,
        scale=scale_base * 1.25,
        style_mode=style_mode,
        is_female=beat["is_female"],
        action=action,
        emotion=beat["emotion"],
        mouth_open=mouth_open,
        t=global_t,
        prog=prog,
        facing=1
    )

    # Clean Studio Captions
    words = beat.get("words", [])
    if words:
        active_idx = min(len(words) - 1, int(prog * len(words)))
        font_size = max(21, int(out_w * 0.044)) if out_h > out_w else max(23, int(out_w * 0.026))
        font = get_font(font_size)
        space_w = int(font_size * 0.35)
        max_line_w = int(out_w * 0.86)

        lines, curr_line, curr_w = [], [], 0
        for idx, wd in enumerate(words):
            bbox = draw.textbbox((0, 0), wd, font=font)
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
                draw.text((cx, cy), wd, font=font, fill=(*col, 255), stroke_width=3, stroke_fill=(15, 23, 42, 255))
                cx += ww + space_w

    return frame.convert("RGB").tobytes()


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

        # 1. Audio & Lip-Sync Envelope
        job["message"] = "Generating Voiceover & Audio Lip-Sync Track..."
        job["progress"] = 8

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
                job["progress"] = 8 + int(((idx + 1) / total) * 18)

            concat_txt = os.path.join(AUDIO_DIR, f"{job_id}_list.txt")
            with open(concat_txt, "w") as cf:
                for sw in seg_wavs:
                    cf.write(f"file '{sw}'\n")
            subprocess.run(
                [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", master_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )

        lip_envelope = extract_lip_envelope(master_wav, fps)

        # 2. Fetch Hand-Drawn Sketch Backgrounds per unique scene
        scene_bgs = {}
        for idx, b in enumerate(beats):
            sc = b["scene"]
            if sc not in scene_bgs:
                job["message"] = f"Drawing 2D Studio Background ({sc[:28]})..."
                scene_bgs[sc] = get_scene_background(sc, out_w, out_h, seed=4000 + idx * 53)
            job["progress"] = 26 + int(((idx + 1) / total) * 18)

        # 3. Render 24 FPS IK Biomechanical Animation
        out_name = f"NEXORA_IK_STUDIO_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
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
            bg_img = scene_bgs[b["scene"]]

            for f in range(n_frames):
                prog = f / max(1, n_frames - 1)
                mouth_val = lip_envelope[frame_cursor] if frame_cursor < len(lip_envelope) else 0.0
                raw_bytes = render_ik_scene_frame(bg_img, out_w, out_h, b, prog, global_t, mouth_val, style_mode)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps
                frame_cursor += 1

            job["progress"] = 45 + int(((i + 1) / total) * 53)
            job["message"] = f"Animating IK Biomechanics Scene {i + 1}/{total}..."

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

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing 2-Bone IK Biomechanical Rig..."}
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
