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
from io import BytesIO

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
# 1. VOICE ENGINE (AUTO HINDI/HINGLISH TTS + MANUAL USER AUDIO ENVELOPE)
# ===========================================================================
VOICE_MAP = {
    "MALE_HINDI": "hi-IN-MadhurNeural",
    "FEMALE_HINDI": "hi-IN-SwaraNeural",
    "MALE_ENGLISH": "en-IN-PrabhatNeural",
    "FEMALE_ENGLISH": "en-IN-NeerjaNeural"
}

async def _tts_save(text: str, voice_id: str, mp3_path: str):
    comm = edge_tts.Communicate(text, voice_id, rate="+6%")
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
        # Fallback silent audio if TTS network hiccups
        dur = max(2.2, len(clean_txt.split()) * 0.42)
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "lavfi", "-i", f"anullsrc=r=22050:cl=mono", "-t", str(dur), out_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    with wave.open(out_wav, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return max(1.8, frames / float(rate))

def convert_custom_audio_to_wav(src_path: str, out_wav: str) -> float:
    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", src_path, "-ar", "22050", "-ac", "1", out_wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )
    with wave.open(out_wav, "rb") as wf:
        return max(1.8, wf.getnframes() / float(wf.getframerate()))

def extract_lip_envelope(wav_path: str, fps: int) -> list:
    """Reads actual voice loudness per video frame so lips open/close strictly with the voice!"""
    try:
        with wave.open(wav_path, "rb") as wf:
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return [0.0]

    step = max(1, int(rate / fps))
    env = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        mouth_open = min(1.0, max(0.0, (rms - 0.015) * 9.5))
        env.append(mouth_open)
    return env if env else [0.0]


# ===========================================================================
# 2. MULTI-LAYER 2D VECTOR ENVIRONMENT & MOVING PROPS (CARS, BIKES, TREES, WINDOW)
# ===========================================================================
def draw_swaying_tree(draw: ImageDraw.Draw, x: int, ground_y: int, scale: float, sway: float):
    trunk_w = int(18 * scale)
    trunk_h = int(115 * scale)
    top_x = x + int(sway * 10 * scale)
    draw.polygon([
        (x - trunk_w // 2, ground_y),
        (x + trunk_w // 2, ground_y),
        (top_x + trunk_w // 3, ground_y - trunk_h),
        (top_x - trunk_w // 3, ground_y - trunk_h)
    ], fill=(82, 52, 34), outline=(30, 18, 12))

    r1 = int(52 * scale)
    r2 = int(40 * scale)
    cy = ground_y - trunk_h
    draw.ellipse([top_x - r1, cy - r1, top_x + r1, cy + int(r1 * 0.6)], fill=(22, 163, 74), outline=(15, 23, 42), width=3)
    draw.ellipse([top_x - r1 - int(18 * scale), cy - r2 + 8, top_x - int(6 * scale), cy + r2], fill=(34, 197, 94), outline=(15, 23, 42), width=3)
    draw.ellipse([top_x + int(6 * scale), cy - r2 + 8, top_x + r1 + int(18 * scale), cy + r2], fill=(21, 128, 61), outline=(15, 23, 42), width=3)

def draw_moving_car(draw: ImageDraw.Draw, cx: int, road_y: int, scale: float, color=(239, 68, 68)):
    w = int(150 * scale)
    h = int(42 * scale)
    y = road_y - int(16 * scale)
    # Body
    draw.rounded_rectangle([cx - w // 2, y - h, cx + w // 2, y], radius=int(12 * scale), fill=color, outline=(15, 23, 42), width=3)
    # Cabin
    draw.polygon([
        (cx - int(w * 0.30), y - h),
        (cx - int(w * 0.16), y - h - int(28 * scale)),
        (cx + int(w * 0.22), y - h - int(28 * scale)),
        (cx + int(w * 0.36), y - h)
    ], fill=(56, 189, 248), outline=(15, 23, 42))
    # Headlight beam
    draw.polygon([
        (cx + w // 2, y - int(18 * scale)),
        (cx + w // 2 + int(85 * scale), y - int(32 * scale)),
        (cx + w // 2 + int(85 * scale), y + int(4 * scale))
    ], fill=(254, 240, 138, 95))
    # Wheels
    wr = int(17 * scale)
    for wx in (cx - int(w * 0.30), cx + int(w * 0.30)):
        draw.ellipse([wx - wr, y - wr, wx + wr, y + wr], fill=(15, 23, 42), outline=(226, 232, 240), width=3)

def draw_moving_bike(draw: ImageDraw.Draw, bx: int, road_y: int, scale: float):
    wr = int(18 * scale)
    y = road_y - wr
    for wx in (bx - int(36 * scale), bx + int(36 * scale)):
        draw.ellipse([wx - wr, y - wr, wx + wr, y + wr], fill=(15, 23, 42), outline=(56, 189, 248), width=3)
    # Frame
    draw.line([(bx - int(36 * scale), y), (bx, y - int(26 * scale)), (bx + int(36 * scale), y)], fill=(249, 115, 22), width=max(4, int(6 * scale)))
    draw.polygon([
        (bx - int(18 * scale), y - int(26 * scale)),
        (bx + int(16 * scale), y - int(34 * scale)),
        (bx + int(24 * scale), y - int(18 * scale))
    ], fill=(234, 88, 12), outline=(15, 23, 42))

def draw_home_exterior(draw: ImageDraw.Draw, hx: int, ground_y: int, scale: float, window_open_prog: float):
    hw = int(230 * scale)
    hh = int(250 * scale)
    # Main wall
    draw.rectangle([hx, ground_y - hh, hx + hw, ground_y], fill=(253, 230, 138), outline=(15, 23, 42), width=4)
    # Sloped Roof
    draw.polygon([
        (hx - int(22 * scale), ground_y - hh),
        (hx + hw // 2, ground_y - hh - int(85 * scale)),
        (hx + hw + int(22 * scale), ground_y - hh)
    ], fill=(185, 28, 28), outline=(15, 23, 42))
    # Door
    dw = int(54 * scale)
    dh = int(105 * scale)
    draw.rectangle([hx + int(28 * scale), ground_y - dh, hx + int(28 * scale) + dw, ground_y], fill=(120, 53, 15), outline=(15, 23, 42), width=3)
    # Large Window (where character exits from!)
    wx = hx + int(112 * scale)
    wy = ground_y - int(165 * scale)
    ww = int(88 * scale)
    wh = int(84 * scale)
    draw.rectangle([wx, wy, wx + ww, wy + wh], fill=(30, 41, 59), outline=(15, 23, 42), width=4)
    # Window shutters opening outward
    shutter_w = int((ww // 2) * max(0.15, 1.0 - window_open_prog * 0.85))
    draw.rectangle([wx - shutter_w, wy, wx, wy + wh], fill=(146, 64, 14), outline=(15, 23, 42), width=3)
    draw.rectangle([wx + ww, wy, wx + ww + shutter_w, wy + wh], fill=(146, 64, 14), outline=(15, 23, 42), width=3)


# ===========================================================================
# 3. ARTICULATED 2D PUPPET & STICKMAN RIG (HEAD, EYES, LIP-SYNC, ARMS, LEGS)
# ===========================================================================
def draw_character_rig(draw: ImageDraw.Draw, cx: int, foot_y: int, scale: float,
                       style_mode: str, is_female: bool, action: str, emotion: str,
                       mouth_open: float, t: float, shirt_col=(37, 99, 235)):
    # Walk / Jump / Idle body bob
    is_walking = action in {"walk", "run", "window_exit"}
    speed = 11.0 if action == "run" else 7.0
    stride = math.sin(t * speed) if is_walking else 0.0
    jump_offset = abs(math.sin(t * 6.0)) * 28 * scale if action == "jump" else 0.0
    bob = abs(stride) * 6 * scale + jump_offset

    hip_y = foot_y - int(78 * scale) - int(bob)
    shoulder_y = hip_y - int(76 * scale)
    head_cy = shoulder_y - int(44 * scale)
    head_r = int(34 * scale)

    # 1. LEGS (Articulated Knee & Foot Swing)
    leg_len = int(76 * scale)
    l_foot_x = cx - int(12 * scale) + int(stride * 28 * scale)
    r_foot_x = cx + int(12 * scale) - int(stride * 28 * scale)
    l_foot_y = foot_y - int(max(0, -stride) * 12 * scale) - int(jump_offset)
    r_foot_y = foot_y - int(max(0, stride) * 12 * scale) - int(jump_offset)

    lw_leg = max(6, int(11 * scale)) if style_mode == "cartoon" else max(5, int(7 * scale))
    leg_col = (30, 41, 59) if style_mode == "cartoon" else (15, 23, 42)
    draw.line([(cx - int(10 * scale), hip_y), (l_foot_x, l_foot_y)], fill=leg_col, width=lw_leg)
    draw.line([(cx + int(10 * scale), hip_y), (r_foot_x, r_foot_y)], fill=leg_col, width=lw_leg)
    # Shoes
    draw.rounded_rectangle([l_foot_x - int(12 * scale), l_foot_y - int(8 * scale), l_foot_x + int(16 * scale), l_foot_y + int(4 * scale)], radius=6, fill=(15, 23, 42))
    draw.rounded_rectangle([r_foot_x - int(12 * scale), r_foot_y - int(8 * scale), r_foot_x + int(16 * scale), r_foot_y + int(4 * scale)], radius=6, fill=(15, 23, 42))

    # 2. TORSO (Cartoon Outfit vs Stickman Line)
    if style_mode == "cartoon":
        draw.rounded_rectangle(
            [cx - int(24 * scale), shoulder_y, cx + int(24 * scale), hip_y + int(6 * scale)],
            radius=int(12 * scale), fill=shirt_col, outline=(15, 23, 42), width=3
        )
    else:
        draw.line([(cx, shoulder_y), (cx, hip_y)], fill=(15, 23, 42), width=max(6, int(8 * scale)))

    # 3. ARMS & HANDS (Pointing, Waving, Holding Paper, Fighting, Walking Swing)
    arm_w = max(5, int(9 * scale)) if style_mode == "cartoon" else max(4, int(6 * scale))
    skin_col = (255, 219, 188) if style_mode == "cartoon" else (15, 23, 42)

    # Left arm
    if action in {"point", "wave", "heroic"}:
        l_elbow = (cx - int(34 * scale), shoulder_y - int(12 * scale))
        l_hand = (cx - int(54 * scale), shoulder_y - int(42 * scale) + int(math.sin(t * 10) * 8 * scale))
    elif action == "paper":
        l_elbow = (cx + int(12 * scale), shoulder_y + int(26 * scale))
        l_hand = (cx + int(36 * scale), shoulder_y + int(18 * scale))
    else:
        l_elbow = (cx - int(24 * scale) - int(stride * 12 * scale), shoulder_y + int(32 * scale))
        l_hand = (cx - int(28 * scale) - int(stride * 22 * scale), shoulder_y + int(62 * scale))

    draw.line([(cx - int(18 * scale), shoulder_y + int(6 * scale)), l_elbow, l_hand], fill=skin_col, width=arm_w)
    draw.ellipse([l_hand[0] - int(7 * scale), l_hand[1] - int(7 * scale), l_hand[0] + int(7 * scale), l_hand[1] + int(7 * scale)], fill=(255, 219, 188), outline=(15, 23, 42), width=2)

    # Right arm
    if action in {"point", "fight"}:
        r_elbow = (cx + int(36 * scale), shoulder_y + int(4 * scale))
        r_hand = (cx + int(68 * scale), shoulder_y - int(10 * scale) + int(math.sin(t * 8) * 5 * scale))
    elif action == "paper":
        r_elbow = (cx + int(24 * scale), shoulder_y + int(26 * scale))
        r_hand = (cx + int(48 * scale), shoulder_y + int(16 * scale))
        # White script sheet in hand (Not Your Type style!)
        draw.polygon([
            (r_hand[0] - int(8 * scale), r_hand[1] - int(24 * scale)),
            (r_hand[0] + int(22 * scale), r_hand[1] - int(28 * scale)),
            (r_hand[0] + int(26 * scale), r_hand[1] + int(12 * scale)),
            (r_hand[0] - int(4 * scale), r_hand[1] + int(16 * scale))
        ], fill=(255, 255, 255), outline=(15, 23, 42))
    else:
        r_elbow = (cx + int(24 * scale) + int(stride * 12 * scale), shoulder_y + int(32 * scale))
        r_hand = (cx + int(28 * scale) + int(stride * 22 * scale), shoulder_y + int(62 * scale))

    draw.line([(cx + int(18 * scale), shoulder_y + int(6 * scale)), r_elbow, r_hand], fill=skin_col, width=arm_w)
    draw.ellipse([r_hand[0] - int(7 * scale), r_hand[1] - int(7 * scale), r_hand[0] + int(7 * scale), r_hand[1] + int(7 * scale)], fill=(255, 219, 188), outline=(15, 23, 42), width=2)

    # 4. HEAD, HAIR, BLINKING EYES & AUDIO LIP-SYNC MOUTH
    nod_y = int(math.sin(t * 9.0) * 3 * scale * mouth_open)
    hy = head_cy + nod_y
    face_fill = (255, 224, 196) if style_mode == "cartoon" else (255, 255, 255)
    draw.ellipse([cx - head_r, hy - head_r, cx + head_r, hy + head_r], fill=face_fill, outline=(15, 23, 42), width=3)

    # Hair (Cartoon mode)
    if style_mode == "cartoon":
        if is_female:
            draw.pieslice([cx - head_r - 4, hy - head_r - 6, cx + head_r + 4, hy + head_r + 18], start=170, end=10, fill=(30, 27, 75), outline=(15, 23, 42), width=2)
        else:
            draw.pieslice([cx - head_r - 2, hy - head_r - 8, cx + head_r + 2, hy + 4], start=180, end=360, fill=(15, 23, 42))

    # Eyes + Auto Blink every 3.2 seconds
    blink = (int(t * 10) % 32 == 0)
    eye_y = hy - int(4 * scale)
    for ex in (cx - int(11 * scale), cx + int(11 * scale)):
        if blink:
            draw.line([(ex - int(6 * scale), eye_y), (ex + int(6 * scale), eye_y)], fill=(15, 23, 42), width=3)
        else:
            er = int(6 * scale)
            draw.ellipse([ex - er, eye_y - er, ex + er, eye_y + er], fill=(255, 255, 255), outline=(15, 23, 42), width=2)
            pr = int(3 * scale)
            draw.ellipse([ex - pr + int(2 * scale), eye_y - pr, ex + pr + int(2 * scale), eye_y + pr], fill=(15, 23, 42))

    # Eyebrows (Emotion-driven)
    brow_tilt = 5 if emotion in {"ANGRY", "HEROIC"} else (-4 if emotion in {"SAD", "FEAR"} else 0)
    draw.line([(cx - int(18 * scale), eye_y - int(11 * scale) - brow_tilt), (cx - int(5 * scale), eye_y - int(11 * scale) + brow_tilt)], fill=(15, 23, 42), width=3)
    draw.line([(cx + int(5 * scale), eye_y - int(11 * scale) + brow_tilt), (cx + int(18 * scale), eye_y - int(11 * scale) - brow_tilt)], fill=(15, 23, 42), width=3)

    # Lip-Sync Mouth driven directly by voice audio loudness!
    my = hy + int(14 * scale)
    if mouth_open > 0.12:
        mh = int((4 + mouth_open * 11) * scale)
        mw = int((9 + mouth_open * 4) * scale)
        draw.ellipse([cx - mw, my - mh // 2, cx + mw, my + mh // 2], fill=(185, 28, 28), outline=(15, 23, 42), width=2)
        # Tongue highlight
        draw.ellipse([cx - mw // 2, my, cx + mw // 2, my + mh // 2], fill=(244, 63, 94))
    else:
        if emotion in {"HAPPY", "ROMANTIC"}:
            draw.arc([cx - int(10 * scale), my - int(6 * scale), cx + int(10 * scale), my + int(6 * scale)], start=10, end=170, fill=(15, 23, 42), width=3)
        else:
            draw.line([(cx - int(9 * scale), my), (cx + int(9 * scale), my)], fill=(15, 23, 42), width=3)


# ===========================================================================
# 4. SCRIPT PARSER & FRAME COMPOSITOR
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
    current_scene = "SUNNY CITY ROAD WITH HOME AND TREES"

    for ln in lines:
        if re.match(r"^SCENE\s*\d*", ln, re.I):
            loc = re.sub(r"^SCENE\s*\d*\s*[:\-]?\s*", "", ln, flags=re.I).strip()
            if loc:
                current_scene = loc.upper()
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
            elif u in {"WALK", "RUN", "JUMP", "WAVE", "POINT", "PAPER", "FIGHT", "WINDOW_EXIT"}:
                action = u.lower()
            elif u not in {"FULL", "HALF", "WIDE", "MEDIUM", "CLOSEUP", "OTS", "ORBIT"}:
                speaker = tg

        low = (current_scene + " " + caption).lower()
        if action == "auto":
            if any(k in low for k in ["window", "janala", "bahar", "exit", "nikal"]):
                action = "window_exit"
            elif any(k in low for k in ["walk", "road", "rasta", "chal"]):
                action = "walk"
            elif any(k in low for k in ["run", "bhag", "daud"]):
                action = "run"
            elif any(k in low for k in ["jump", "kood"]):
                action = "jump"
            elif any(k in low for k in ["paper", "script", "letter", "khat"]):
                action = "paper"
            elif any(k in low for k in ["point", "dekho", "bata", "samjha"]):
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

def render_2d_animated_frame(out_w: int, out_h: int, beat: dict, prog: float, global_t: float,
                             mouth_open: float, style_mode: str) -> bytes:
    scene_low = (beat["scene"] + " " + beat["caption"]).lower()
    is_night = any(k in scene_low for k in ["night", "raat", "midnight", "horror", "bhoot", "dark"])
    is_rain = any(k in scene_low for k in ["rain", "baarish", "storm"])

    # 1. SKY & ATMOSPHERE
    sky_col = (15, 23, 42) if is_night else ((100, 116, 139) if is_rain else (186, 230, 253))
    img = Image.new("RGBA", (out_w, out_h), sky_col)
    draw = ImageDraw.Draw(img)

    # Sun or Moon
    cel_x = int(out_w * 0.80)
    cel_y = int(out_h * 0.14)
    cel_col = (248, 250, 252) if is_night else (253, 224, 71)
    draw.ellipse([cel_x - 36, cel_y - 36, cel_x + 36, cel_y + 36], fill=cel_col)

    # Drifting Clouds
    for c_i in range(3):
        cx = int(((c_i * 290 + global_t * 22) % (out_w + 180)) - 90)
        cy = int(out_h * (0.12 + c_i * 0.05))
        c_fill = (51, 65, 85, 190) if (is_night or is_rain) else (255, 255, 255, 220)
        draw.ellipse([cx, cy, cx + 110, cy + 38], fill=c_fill)
        draw.ellipse([cx + 25, cy - 14, cx + 85, cy + 28], fill=c_fill)

    # 2. GROUND, GRASS & ASPHALT ROAD
    ground_y = int(out_h * 0.72)
    road_top = int(out_h * 0.74)
    road_bot = int(out_h * 0.90)
    grass_col = (20, 83, 45) if is_night else (74, 222, 128)
    draw.rectangle([0, ground_y - 25, out_w, out_h], fill=grass_col)
    draw.rectangle([0, road_top, out_w, road_bot], fill=(51, 65, 85))
    # Moving Road Lane Markings
    lane_y = (road_top + road_bot) // 2
    scroll = int((global_t * 140) % 90) if beat["action"] in {"walk", "run"} else 0
    for lx in range(-90, out_w + 90, 90):
        draw.rectangle([lx - scroll, lane_y - 4, lx + 44 - scroll, lane_y + 4], fill=(248, 250, 252))

    # 3. SWAYING TREES (Background & Midground)
    sway = math.sin(global_t * 2.4)
    scale_base = out_w / 620.0
    draw_swaying_tree(draw, int(out_w * 0.14), ground_y, scale_base * 0.95, sway)
    draw_swaying_tree(draw, int(out_w * 0.88), ground_y, scale_base * 1.10, -sway)

    # 4. HOME & WINDOW-TO-ROAD EXIT CHOREOGRAPHY
    has_home = any(k in scene_low for k in ["home", "ghar", "house", "window", "janala", "haveli"])
    char_foot_y = road_top + int(36 * scale_base)

    if beat["action"] == "window_exit" or has_home:
        home_x = int(out_w * 0.08)
        win_prog = min(1.0, prog * 1.6) if beat["action"] == "window_exit" else 0.4
        draw_home_exterior(draw, home_x, ground_y + 10, scale_base * 1.05, win_prog)

    if beat["action"] == "window_exit":
        # Starts inside window -> hops out -> walks onto center of the road!
        start_x = int(out_w * 0.28)
        end_x = int(out_w * 0.56)
        start_y = ground_y - int(45 * scale_base)
        end_y = road_top + int(42 * scale_base)
        char_x = int(start_x + (end_x - start_x) * prog)
        arc_jump = math.sin(prog * math.pi) * 45 * scale_base
        char_foot_y = int(start_y + (end_y - start_y) * prog - arc_jump)
    elif beat["action"] in {"walk", "run"}:
        char_x = int(out_w * (0.25 + prog * 0.48))
    else:
        char_x = int(out_w * 0.50)

    # 5. MOVING CARS & BIKES ON THE ROAD
    if any(k in scene_low for k in ["car", "gaadi", "road", "city", "street", "traffic"]):
        car_x = int(((prog * 1.45) - 0.2) * out_w)
        draw_moving_car(draw, car_x, road_top + int(18 * scale_base), scale_base * 0.95)

    if any(k in scene_low for k in ["bike", "motorcycle", "road", "street", "ride"]):
        bike_x = int((1.15 - prog * 1.35) * out_w)
        draw_moving_bike(draw, bike_x, road_bot - int(8 * scale_base), scale_base * 1.05)

    # 6. FULL-BODY ARTICULATED CHARACTER RIG
    shirt_col = (225, 29, 72) if beat["is_female"] else (37, 99, 235)
    draw_character_rig(
        draw=draw,
        cx=char_x,
        foot_y=char_foot_y,
        scale=scale_base * 1.18,
        style_mode=style_mode,
        is_female=beat["is_female"],
        action=beat["action"],
        emotion=beat["emotion"],
        mouth_open=mouth_open,
        t=global_t,
        shirt_col=shirt_col
    )

    # Rain VFX
    if is_rain:
        for i in range(45):
            rx = int((i * 73 + global_t * 160) % (out_w + 60)) - 30
            ry = int((i * 97 + global_t * 760) % (out_h + 40)) - 20
            draw.line([(rx, ry), (rx - 8, ry + 22)], fill=(186, 230, 253, 175), width=2)

    # 7. CLEAN DYNAMIC CAPTIONS
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
        top_y = int(out_h * 0.91) - (len(lines) * line_h) // 2

        for l_idx, tokens in enumerate(lines):
            row_w = sum(tk[2] for tk in tokens) + space_w * (len(tokens) - 1)
            cx = (out_w - row_w) // 2
            cy = top_y + l_idx * line_h
            for wd, idx, ww in tokens:
                col = (250, 204, 21) if (idx == active_idx) else (255, 255, 255)
                draw.text((cx, cy), wd, font=font, fill=(*col, 255), stroke_width=3, stroke_fill=(15, 23, 42, 255))
                cx += ww + space_w

    return img.convert("RGB").tobytes()


# ===========================================================================
# 5. FULL RENDER PIPELINE (COMBINES VIDEO + AUTO OR MANUAL VOICE AUDIO)
# ===========================================================================
def run_animation_job(job_id: str, script_text: str, ratio: str, style_mode: str,
                      voice_mode: str, lang_mode: str, custom_wav_path: str | None):
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

        # Step 1: Prepare Audio Track (Auto TTS per line OR User's Custom Recorded/Uploaded Voice)
        job["message"] = "Preparing Voiceover & Lip-Sync Audio Track..."
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

            # Concatenate all TTS segments into master WAV
            concat_txt = os.path.join(AUDIO_DIR, f"{job_id}_list.txt")
            with open(concat_txt, "w") as cf:
                for sw in seg_wavs:
                    cf.write(f"file '{sw}'\n")
            subprocess.run(
                [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", master_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )

        lip_envelope = extract_lip_envelope(master_wav, fps)

        # Step 2: Render 24 FPS Articulated 2D Animation + Mux Audio
        out_name = f"NEXORA_2D_STUDIO_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
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
                raw_bytes = render_2d_animated_frame(out_w, out_h, b, prog, global_t, mouth_val, style_mode)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps
                frame_cursor += 1

            job["progress"] = 30 + int(((i + 1) / total) * 68)
            job["message"] = f"Animating Scene {i + 1}/{total} (24 FPS Puppet & Lip-Sync)..."

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


# ===========================================================================
# 6. API ROUTES (AUTO TTS + CUSTOM AUDIO UPLOAD / RECORDING)
# ===========================================================================
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

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing 2D Puppet Rig & Lip-Sync Engine..."}
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
