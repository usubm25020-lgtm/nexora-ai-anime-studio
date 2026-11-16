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
    comm = edge_tts.Communicate(text, voice_id, rate="+8%")
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
    env = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        env.append(min(1.0, max(0.0, (rms - 0.015) * 9.5)))
    return env if env else [0.0]


# ===========================================================================
# 2. CURVED BEZIER LIMBS & VIRAL SKETCH-CARTOON CHARACTER RIG
# ===========================================================================
def draw_curved_limb(draw: ImageDraw.Draw, p0: tuple, p1: tuple, p2: tuple, color: tuple, width: int, outline_col=None):
    pts = []
    steps = 12
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        pts.append((int(x), int(y)))
    if outline_col:
        draw.line(pts, fill=outline_col, width=width + 4, joint="curve")
    draw.line(pts, fill=color, width=width, joint="curve")

def draw_viral_character(draw: ImageDraw.Draw, cx: int, foot_y: int, scale: float,
                         style_mode: str, is_female: bool, action: str, emotion: str,
                         mouth_open: float, t: float, is_dark_scene: bool = False):
    is_moving = action in {"walk", "run", "window_exit", "stairs_up", "door_slam"}
    spd = 13.0 if action in {"run", "stairs_up"} else 7.5
    stride = math.sin(t * spd) if is_moving else 0.0
    bob = abs(stride) * 7 * scale

    hip_y = foot_y - int(74 * scale) - int(bob)
    shoulder_y = hip_y - int(68 * scale)
    head_cy = shoulder_y - int(48 * scale)
    head_rx = int(38 * scale)
    head_ry = int(42 * scale)

    # High-contrast sketch ink + rim outline so character NEVER disappears in dark scenes!
    ink = (235, 240, 245) if is_dark_scene else (24, 28, 34)
    rim = (15, 23, 42) if is_dark_scene else None
    skin = (210, 214, 218) if style_mode == "stickman" else (255, 224, 198)
    torso_fill = (205, 210, 215) if style_mode == "stickman" else ((225, 29, 72) if is_female else (59, 130, 246))

    # Motion smear lines when running/jumping (exact viral cartoon effect!)
    if action in {"run", "stairs_up", "window_exit"}:
        for m_i in range(4):
            my = shoulder_y + m_i * int(22 * scale)
            mx = cx - int((45 + m_i * 12) * scale)
            draw.line([(mx - int(35 * scale), my), (mx, my)], fill=(*ink, 140), width=max(2, int(3 * scale)))

    # 1. CURVED RUBBER-HOSE LEGS & BIG CARTOON SHOES
    l_knee = (cx - int(18 * scale) + int(stride * 18 * scale), foot_y - int(38 * scale))
    r_knee = (cx + int(18 * scale) - int(stride * 18 * scale), foot_y - int(38 * scale))
    l_foot = (cx - int(16 * scale) + int(stride * 34 * scale), foot_y - int(max(0, -stride) * 14 * scale))
    r_foot = (cx + int(16 * scale) - int(stride * 34 * scale), foot_y - int(max(0, stride) * 14 * scale))

    leg_w = max(5, int(8 * scale))
    draw_curved_limb(draw, (cx - int(10 * scale), hip_y), l_knee, l_foot, ink, leg_w, rim)
    draw_curved_limb(draw, (cx + int(10 * scale), hip_y), r_knee, r_foot, ink, leg_w, rim)

    # Big expressive cartoon shoes
    for fx, fy in (l_foot, r_foot):
        draw.pieslice(
            [fx - int(14 * scale), fy - int(12 * scale), fx + int(22 * scale), fy + int(8 * scale)],
            start=180, end=360, fill=(148, 163, 184) if is_dark_scene else (71, 85, 105), outline=ink, width=3
        )

    # 2. EXPRESSIVE PEAR/BEAN TORSO
    draw.rounded_rectangle(
        [cx - int(22 * scale), shoulder_y, cx + int(22 * scale), hip_y + int(6 * scale)],
        radius=int(18 * scale), fill=torso_fill, outline=ink, width=4
    )

    # 3. CURVED RUBBER-HOSE ARMS & 4-FINGER CARTOON HANDS
    arm_w = max(5, int(7 * scale))
    if action in {"run", "stairs_up", "fear"}:
        # Panic raised arms with motion arc!
        l_elb = (cx - int(42 * scale), shoulder_y - int(18 * scale))
        l_hnd = (cx - int(62 * scale), shoulder_y - int(48 * scale) + int(math.sin(t * 16) * 8 * scale))
        r_elb = (cx + int(42 * scale), shoulder_y - int(12 * scale))
        r_hnd = (cx + int(66 * scale), shoulder_y - int(36 * scale) - int(math.sin(t * 16) * 8 * scale))
    elif action == "door_slam":
        # Both hands pushing door closed!
        l_elb = (cx + int(26 * scale), shoulder_y + int(10 * scale))
        l_hnd = (cx + int(56 * scale), shoulder_y + int(4 * scale))
        r_elb = (cx + int(28 * scale), shoulder_y + int(24 * scale))
        r_hnd = (cx + int(58 * scale), shoulder_y + int(18 * scale))
    elif action == "paper":
        l_elb = (cx + int(16 * scale), shoulder_y + int(26 * scale))
        l_hnd = (cx + int(44 * scale), shoulder_y + int(16 * scale))
        r_elb = (cx + int(24 * scale), shoulder_y + int(28 * scale))
        r_hnd = (cx + int(52 * scale), shoulder_y + int(18 * scale))
    elif action in {"point", "wave", "heroic"}:
        l_elb = (cx - int(32 * scale), shoulder_y + int(24 * scale))
        l_hnd = (cx - int(28 * scale), shoulder_y + int(52 * scale))
        r_elb = (cx + int(38 * scale), shoulder_y - int(6 * scale))
        r_hnd = (cx + int(72 * scale), shoulder_y - int(24 * scale) + int(math.sin(t * 12) * 8 * scale))
    else:
        l_elb = (cx - int(32 * scale), shoulder_y + int(26 * scale))
        l_hnd = (cx - int(26 * scale) - int(stride * 16 * scale), shoulder_y + int(56 * scale))
        r_elb = (cx + int(32 * scale), shoulder_y + int(26 * scale))
        r_hnd = (cx + int(26 * scale) + int(stride * 16 * scale), shoulder_y + int(56 * scale))

    draw_curved_limb(draw, (cx - int(18 * scale), shoulder_y + int(8 * scale)), l_elb, l_hnd, ink, arm_w, rim)
    draw_curved_limb(draw, (cx + int(18 * scale), shoulder_y + int(8 * scale)), r_elb, r_hnd, ink, arm_w, rim)

    # Cartoon hands
    hr = int(9 * scale)
    draw.ellipse([l_hnd[0] - hr, l_hnd[1] - hr, l_hnd[0] + hr, l_hnd[1] + hr], fill=skin, outline=ink, width=3)
    draw.ellipse([r_hnd[0] - hr, r_hnd[1] - hr, r_hnd[0] + hr, r_hnd[1] + hr], fill=skin, outline=ink, width=3)

    if action == "paper":
        draw.polygon([
            (r_hnd[0] - int(6 * scale), r_hnd[1] - int(28 * scale)),
            (r_hnd[0] + int(26 * scale), r_hnd[1] - int(32 * scale)),
            (r_hnd[0] + int(30 * scale), r_hnd[1] + int(12 * scale)),
            (r_hnd[0] - int(2 * scale), r_hnd[1] + int(16 * scale))
        ], fill=(255, 255, 255), outline=ink, width=3)

    # 4. VIRAL CARTOON HEAD, BIG OVERLAPPING EYES, NOSE & LIP-SYNC MOUTH
    nod_y = int(math.sin(t * 10.0) * 3 * scale * mouth_open)
    hy = head_cy + nod_y
    draw.ellipse([cx - head_rx, hy - head_ry, cx + head_rx, hy + head_ry], fill=skin, outline=ink, width=4)

    if style_mode == "cartoon":
        if is_female:
            draw.pieslice([cx - head_rx - 4, hy - head_ry - 6, cx + head_rx + 4, hy + head_ry + 16], start=170, end=10, fill=(30, 27, 75), outline=ink, width=3)
        else:
            draw.pieslice([cx - head_rx - 2, hy - head_ry - 8, cx + head_rx + 2, hy + 2], start=180, end=360, fill=(30, 41, 59))

    # Big Overlapping Cartoon Eyes (exact Not Your Type / video_2 style!)
    blink = (int(t * 10) % 34 == 0)
    eye_y = hy - int(8 * scale)
    eye_rx = int(13 * scale)
    eye_ry = int(16 * scale)
    for idx_e, ex in enumerate((cx - int(8 * scale), cx + int(12 * scale))):
        if blink:
            draw.line([(ex - eye_rx, eye_y), (ex + eye_rx, eye_y)], fill=ink, width=4)
        else:
            draw.ellipse([ex - eye_rx, eye_y - eye_ry, ex + eye_rx, eye_y + eye_ry], fill=(255, 255, 255), outline=(24, 28, 34), width=3)
            pr = int(4 * scale)
            px = ex + int(4 * scale)
            draw.ellipse([px - pr, eye_y - pr, px + pr, eye_y + pr], fill=(15, 23, 42))

    # Cartoon Nose
    nose_x = cx + int(14 * scale)
    nose_y = hy + int(4 * scale)
    draw.arc([nose_x - int(6 * scale), nose_y - int(6 * scale), nose_x + int(10 * scale), nose_y + int(8 * scale)], start=250, end=110, fill=(24, 28, 34), width=3)

    # Expressive Eyebrows
    brow_y = eye_y - eye_ry - int(5 * scale)
    if emotion in {"FEAR", "SHOCK", "SAD"}:
        draw.line([(cx - int(22 * scale), brow_y + 4), (cx - int(4 * scale), brow_y - 5)], fill=(24, 28, 34), width=4)
        draw.line([(cx + int(4 * scale), brow_y - 5), (cx + int(22 * scale), brow_y + 4)], fill=(24, 28, 34), width=4)
    elif emotion in {"ANGRY", "HEROIC"}:
        draw.line([(cx - int(22 * scale), brow_y - 5), (cx - int(4 * scale), brow_y + 5)], fill=(24, 28, 34), width=4)
        draw.line([(cx + int(4 * scale), brow_y + 5), (cx + int(22 * scale), brow_y - 5)], fill=(24, 28, 34), width=4)

    # Audio-Driven Lip-Sync Mouth
    my = hy + int(20 * scale)
    if mouth_open > 0.12 or emotion in {"FEAR", "SHOCK"}:
        open_amt = max(mouth_open, 0.55 if emotion in {"FEAR", "SHOCK"} else 0.0)
        mh = int((6 + open_amt * 14) * scale)
        mw = int((11 + open_amt * 5) * scale)
        draw.ellipse([cx - mw, my - mh // 2, cx + mw, my + mh // 2], fill=(153, 27, 27), outline=(24, 28, 34), width=3)
        draw.ellipse([cx - mw // 2, my, cx + mw // 2, my + mh // 2], fill=(244, 63, 94))
    else:
        draw.arc([cx - int(12 * scale), my - int(6 * scale), cx + int(12 * scale), my + int(8 * scale)], start=10, end=170, fill=(24, 28, 34), width=3)


# ===========================================================================
# 3. CINEMATIC ENVIRONMENTS (INDOOR ROOM + LAMP GLOW, DARK EYES, OUTDOOR ROAD)
# ===========================================================================
def apply_vignette_and_glow(img: Image.Image, glow_pts: list) -> Image.Image:
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    # Warm Lamp Glows
    for gx, gy, gr, gcol in glow_pts:
        od.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=gcol)

    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=32))
    out = Image.alpha_composite(img.convert("RGBA"), overlay)

    # Subtle Cinema Vignette Border
    vig = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vig)
    for b in range(28):
        alpha = int((28 - b) * 3.2)
        vd.rectangle([b * 3, b * 3, w - b * 3, h - b * 3], outline=(10, 14, 20, alpha), width=3)
    return Image.alpha_composite(out, vig)

def render_scene_frame(out_w: int, out_h: int, beat: dict, prog: float, global_t: float,
                       mouth_open: float, style_mode: str) -> bytes:
    scene_low = (beat["scene"] + " " + beat["caption"]).lower()
    action = beat["action"]
    scale_base = out_w / 580.0

    # --- SCENE TYPE A: PITCH-DARK EYES & MONSTER SHADOW (exact video_2 climax!) ---
    if any(k in scene_low for k in ["darkness", "andhera", "ghost eyes", "monster", "bhoot", "light off", "bijli"]):
        img = Image.new("RGBA", (out_w, out_h), (24, 26, 28, 255))
        draw = ImageDraw.Draw(img)

        # Protagonist wide terrified eyes blinking in the dark
        ex_base = int(out_w * 0.66)
        ey_base = int(out_h * 0.56)
        blink = (int(global_t * 10) % 28 == 0)
        for dx_e in (-24, 24):
            ex = ex_base + int(dx_e * scale_base)
            if not blink:
                draw.ellipse([ex - 16, ey_base - 18, ex + 16, ey_base + 18], fill=(226, 232, 240))
                look_x = -6 if prog > 0.4 else 0
                draw.ellipse([ex - 5 + look_x, ey_base - 5, ex + 5 + look_x, ey_base + 5], fill=(15, 23, 42))

        # Creepy glowing monster eyes & sharp grin appearing behind at prog > 0.35!
        if prog > 0.30:
            fade = min(1.0, (prog - 0.30) * 2.5)
            alpha = int(235 * fade)
            mx = int(out_w * 0.44)
            my = int(out_h * 0.56)
            # Narrow angry monster eyes
            draw.pieslice([mx - 42, my - 18, mx - 4, my + 14], start=10, end=195, fill=(226, 232, 240, alpha))
            draw.pieslice([mx + 4, my - 18, mx + 42, my + 14], start=345, end=170, fill=(226, 232, 240, alpha))
            # Sharp creepy teeth grin
            draw.arc([mx - 36, my + 16, mx + 36, my + 48], start=10, end=170, fill=(226, 232, 240, alpha), width=6)

        final_img = apply_vignette_and_glow(img, [])

    # --- SCENE TYPE B: INDOOR ROOM / STAIRS / BEDROOM WITH WARM LAMP (exact video_2!) ---
    elif any(k in scene_low for k in ["room", "kamra", "home alone", "stairs", "seedhi", "bedroom", "door", "darwaza", "lamp"]):
        # Atmospheric warm grey-sage hand-drawn sketch room (like video_2)
        img = Image.new("RGBA", (out_w, out_h), (142, 152, 144, 255))
        draw = ImageDraw.Draw(img)
        ink = (32, 36, 38)

        # Room perspective walls & floor
        floor_y = int(out_h * 0.72)
        draw.line([(0, floor_y), (int(out_w * 0.22), int(out_h * 0.62)), (int(out_w * 0.82), int(out_h * 0.62)), (out_w, floor_y)], fill=ink, width=3)
        draw.line([(int(out_w * 0.22), 0), (int(out_w * 0.22), int(out_h * 0.62))], fill=ink, width=3)
        draw.line([(int(out_w * 0.82), 0), (int(out_w * 0.82), int(out_h * 0.62))], fill=ink, width=3)

        # Doorway on right
        dx0, dy0 = int(out_w * 0.72), int(out_h * 0.34)
        dx1, dy1 = int(out_w * 0.86), int(out_h * 0.66)
        draw.rectangle([dx0, dy0, dx1, dy1], fill=(118, 126, 120), outline=ink, width=3)

        # Nightstand & Glowing Table Lamp
        nx, ny = int(out_w * 0.28), int(out_h * 0.50)
        draw.rectangle([nx, ny, nx + int(110 * scale_base), int(out_h * 0.64)], fill=(156, 164, 158), outline=ink, width=3)
        lx, ly = nx + int(45 * scale_base), ny - int(55 * scale_base)
        draw.polygon([(lx - 22, ly + 32), (lx - 12, ly), (lx + 12, ly), (lx + 22, ly + 32)], fill=(254, 249, 195), outline=ink, width=3)
        draw.line([(lx, ly + 32), (lx, ny)], fill=ink, width=4)

        # Framed Portrait on Wall
        fx, fy = int(out_w * 0.46), int(out_h * 0.30)
        draw.rectangle([fx, fy, fx + int(75 * scale_base), fy + int(95 * scale_base)], fill=(168, 176, 170), outline=ink, width=3)

        char_x = int(out_w * (0.35 + prog * 0.32)) if action in {"walk", "run", "door_slam"} else int(out_w * 0.50)
        draw_viral_character(draw, char_x, int(out_h * 0.74), scale_base * 1.15, style_mode, beat["is_female"], action, beat["emotion"], mouth_open, global_t, False)

        final_img = apply_vignette_and_glow(img, [(lx, ly + 15, int(95 * scale_base), (253, 224, 71, 145))])

    # --- SCENE TYPE C: OUTDOOR CITY ROAD WITH MOVING CARS, BIKES & SWAYING TREES ---
    else:
        is_night = any(k in scene_low for k in ["night", "raat", "midnight", "evening", "shaam"])
        is_rain = any(k in scene_low for k in ["rain", "baarish", "storm"])
        sky = (24, 32, 48) if is_night else ((115, 130, 145) if is_rain else (186, 224, 245))
        img = Image.new("RGBA", (out_w, out_h), sky)
        draw = ImageDraw.Draw(img)

        # Sun / Moon
        cx_s, cy_s = int(out_w * 0.80), int(out_h * 0.14)
        draw.ellipse([cx_s - 34, cy_s - 34, cx_s + 34, cy_s + 34], fill=(248, 250, 252) if is_night else (253, 224, 71))

        # Ground & Asphalt Road
        ground_y = int(out_h * 0.70)
        road_top = int(out_h * 0.72)
        road_bot = int(out_h * 0.88)
        draw.rectangle([0, ground_y - 20, out_w, out_h], fill=(34, 68, 52) if is_night else (95, 195, 130))
        draw.rectangle([0, road_top, out_w, road_bot], fill=(45, 55, 68))

        scroll = int((global_t * 140) % 90) if action in {"walk", "run"} else 0
        for lx in range(-90, out_w + 90, 90):
            draw.rectangle([lx - scroll, (road_top + road_bot) // 2 - 3, lx + 42 - scroll, (road_top + road_bot) // 2 + 3], fill=(241, 245, 249))

        # Swaying Trees
        sway = math.sin(global_t * 2.5)
        for tx, sc in ((int(out_w * 0.14), 0.95), (int(out_w * 0.88), 1.1)):
            top_x = tx + int(sway * 10 * sc)
            draw.rectangle([tx - 8, ground_y - int(110 * sc), tx + 8, ground_y], fill=(92, 64, 51), outline=(24, 28, 34), width=3)
            draw.ellipse([top_x - int(48 * sc), ground_y - int(155 * sc), top_x + int(48 * sc), ground_y - int(75 * sc)], fill=(34, 150, 82) if is_night else (52, 200, 105), outline=(24, 28, 34), width=3)

        # Home & Window-to-Road Exit
        if action == "window_exit" or any(k in scene_low for k in ["home", "ghar", "window", "khidki"]):
            hx = int(out_w * 0.08)
            hw, hh = int(210 * scale_base), int(230 * scale_base)
            draw.rectangle([hx, ground_y - hh, hx + hw, ground_y], fill=(245, 222, 145), outline=(24, 28, 34), width=3)
            draw.polygon([(hx - 18, ground_y - hh), (hx + hw // 2, ground_y - hh - int(75 * scale_base)), (hx + hw + 18, ground_y - hh)], fill=(190, 50, 50), outline=(24, 28, 34))
            wx, wy, ww, wh = hx + int(105 * scale_base), ground_y - int(155 * scale_base), int(80 * scale_base), int(76 * scale_base)
            draw.rectangle([wx, wy, wx + ww, wy + wh], fill=(30, 41, 59), outline=(24, 28, 34), width=3)

        # Moving Car & Bike on Road
        car_x = int(((prog * 1.45) - 0.2) * out_w)
        cy_car = road_top + int(16 * scale_base)
        draw.rounded_rectangle([car_x - 65, cy_car - 36, car_x + 65, cy_car], radius=10, fill=(225, 55, 55), outline=(24, 28, 34), width=3)
        draw.ellipse([car_x - 45, cy_car - 14, car_x - 17, cy_car + 14], fill=(24, 28, 34), outline=(241, 245, 249), width=3)
        draw.ellipse([car_x + 17, cy_car - 14, car_x + 45, cy_car + 14], fill=(24, 28, 34), outline=(241, 245, 249), width=3)

        # Character Position
        if action == "window_exit":
            char_x = int(out_w * (0.28 + prog * 0.28))
            char_y = int((ground_y - 35) + ((road_top + 38) - (ground_y - 35)) * prog - math.sin(prog * math.pi) * 45)
        elif action in {"walk", "run"}:
            char_x = int(out_w * (0.25 + prog * 0.48))
            char_y = road_top + int(38 * scale_base)
        else:
            char_x = int(out_w * 0.50)
            char_y = road_top + int(38 * scale_base)

        draw_viral_character(draw, char_x, char_y, scale_base * 1.15, style_mode, beat["is_female"], action, beat["emotion"], mouth_open, global_t, is_night)

        if is_rain:
            for i in range(42):
                rx = int((i * 73 + global_t * 160) % (out_w + 60)) - 30
                ry = int((i * 97 + global_t * 760) % (out_h + 40)) - 20
                draw.line([(rx, ry), (rx - 8, ry + 22)], fill=(186, 230, 253, 175), width=2)

        glows = [(cx_s, cy_s, 75, (253, 224, 71, 90))] if not is_night else []
        final_img = apply_vignette_and_glow(img, glows)

    # Dynamic Clean Captions
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
        top_y = int(out_h * 0.91) - (len(lines) * line_h) // 2

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
# 4. SCRIPT PARSER & PIPELINE
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
            elif u in {"WALK", "RUN", "JUMP", "WAVE", "POINT", "PAPER", "FIGHT", "WINDOW_EXIT", "DOOR_SLAM", "STAIRS_UP"}:
                action = u.lower()
            elif u not in {"FULL", "HALF", "WIDE", "MEDIUM", "CLOSEUP", "OTS", "ORBIT"}:
                speaker = tg

        low = (current_scene + " " + caption).lower()
        if action == "auto":
            if any(k in low for k in ["window", "khidki", "janala", "kood"]):
                action = "window_exit"
            elif any(k in low for k in ["door", "darwaza", "band"]):
                action = "door_slam"
            elif any(k in low for k in ["stairs", "seedhi", "upar"]):
                action = "stairs_up"
            elif any(k in low for k in ["run", "bhag", "daud"]):
                action = "run"
            elif any(k in low for k in ["walk", "road", "sadak", "chal"]):
                action = "walk"
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

            concat_txt = os.path.join(AUDIO_DIR, f"{job_id}_list.txt")
            with open(concat_txt, "w") as cf:
                for sw in seg_wavs:
                    cf.write(f"file '{sw}'\n")
            subprocess.run(
                [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", master_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )

        lip_envelope = extract_lip_envelope(master_wav, fps)

        out_name = f"NEXORA_VIRAL_2D_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
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
                raw_bytes = render_scene_frame(out_w, out_h, b, prog, global_t, mouth_val, style_mode)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps
                frame_cursor += 1

            job["progress"] = 30 + int(((i + 1) / total) * 68)
            job["message"] = f"Animating Viral Scene {i + 1}/{total}..."

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

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Building Viral 2D Sketch & Cartoon Scene..."}
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
