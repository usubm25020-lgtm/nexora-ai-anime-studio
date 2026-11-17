import os
import re
import time
import math
import uuid
import wave
import random
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
# 1. VOICE & METALLIC SWORD CLASH SFX MIXER
# ===========================================================================
VOICE_MAP = {
    "MALE_HINDI": "hi-IN-MadhurNeural",
    "FEMALE_HINDI": "hi-IN-SwaraNeural",
    "MALE_ENGLISH": "en-IN-PrabhatNeural",
    "FEMALE_ENGLISH": "en-IN-NeerjaNeural"
}

async def _tts_save(text: str, voice_id: str, mp3_path: str):
    comm = edge_tts.Communicate(text, voice_id, rate="+10%")
    await comm.save(mp3_path)

def generate_tts_wav(text: str, is_female: bool, lang_mode: str, out_wav: str) -> float:
    clean_txt = re.sub(r"[^\w\s,?!.'\-]", "", text).strip() or "Attack"
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
        dur = max(2.0, len(clean_txt.split()) * 0.36)
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", str(dur), out_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    with wave.open(out_wav, "rb") as wf:
        return max(1.6, wf.getnframes() / float(wf.getframerate()))

def convert_custom_audio_to_wav(src_path: str, out_wav: str) -> float:
    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", src_path, "-ar", "22050", "-ac", "1", out_wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )
    with wave.open(out_wav, "rb") as wf:
        return max(1.6, wf.getnframes() / float(wf.getframerate()))

def inject_combat_sfx(master_wav: str, impact_times: list, out_mixed_wav: str):
    try:
        with wave.open(master_wav, "rb") as wf:
            rate = wf.getframerate()
            samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

        sfx_track = np.zeros_like(samples)
        for t_hit, kind in impact_times:
            start_idx = int(t_hit * rate)
            if kind == "clash":
                dur_s = int(0.24 * rate)
                if start_idx + dur_s < len(sfx_track):
                    t_arr = np.linspace(0, 0.24, dur_s, endpoint=False)
                    env = np.exp(-t_arr * 18.0)
                    ring = (
                        0.45 * np.sin(2 * np.pi * 1580 * t_arr) +
                        0.35 * np.sin(2 * np.pi * 2520 * t_arr) +
                        0.60 * np.sin(2 * np.pi * 92 * np.exp(-t_arr * 12) * t_arr)
                    ) * env * 0.50
                    sfx_track[start_idx:start_idx + dur_s] += ring
            else:
                dur_s = int(0.18 * rate)
                if start_idx + dur_s < len(sfx_track):
                    t_arr = np.linspace(0, 0.18, dur_s, endpoint=False)
                    env = np.sin(np.pi * (t_arr / 0.18)) ** 1.5
                    whoosh = np.random.uniform(-1, 1, dur_s) * np.sin(2 * np.pi * 340 * t_arr) * env * 0.30
                    sfx_track[start_idx:start_idx + dur_s] += whoosh

        mixed = np.clip(samples + sfx_track, -0.96, 0.96)
        pcm = (mixed * 32767.0).astype(np.int16)
        with wave.open(out_mixed_wav, "wb") as wf_out:
            wf_out.setnchannels(1)
            wf_out.setsampwidth(2)
            wf_out.setframerate(rate)
            wf_out.writeframes(pcm.tobytes())
    except Exception:
        import shutil
        shutil.copy(master_wav, out_mixed_wav)


# ===========================================================================
# 2. LOCKED FORWARD KINEMATICS POSE LIBRARY (FACE-TO-FACE & PROPER HEADBAND!)
# ===========================================================================
POSE_LIBRARY = {
    "stance": {
        "hip_dy": 0.0, "spine": 10.0,
        "l_thigh": -20.0, "l_knee": 26.0,
        "r_thigh": 24.0,  "r_knee": 22.0,
        "l_sh": -25.0,    "l_elb": -45.0,
        "r_sh": 35.0,     "r_elb": -55.0,
        "sword_ang": -120.0
    },
    "dash_forward": {
        "hip_dy": 7.0, "spine": 36.0,
        "l_thigh": -48.0, "l_knee": 58.0,
        "r_thigh": 52.0,  "r_knee": 36.0,
        "l_sh": -58.0,    "l_elb": -20.0,
        "r_sh": -45.0,    "r_elb": -30.0,
        "sword_ang": -150.0
    },
    "windup": {
        "hip_dy": 8.0, "spine": -18.0,
        "l_thigh": -24.0, "l_knee": 32.0,
        "r_thigh": 30.0,  "r_knee": 28.0,
        "l_sh": 38.0,     "l_elb": -40.0,
        "r_sh": -142.0,   "r_elb": -42.0,
        "sword_ang": -172.0
    },
    "slash_follow": {
        "hip_dy": 11.0, "spine": 38.0,
        "l_thigh": -38.0, "l_knee": 20.0,
        "r_thigh": 48.0,  "r_knee": 38.0,
        "l_sh": -52.0,    "l_elb": -22.0,
        "r_sh": 88.0,     "r_elb": -10.0,
        "sword_ang": 82.0
    },
    "uppercut_air": {
        "hip_dy": -75.0, "spine": 22.0,
        "l_thigh": -32.0, "l_knee": 72.0,
        "r_thigh": 42.0,  "r_knee": 56.0,
        "l_sh": -42.0,    "l_elb": -28.0,
        "r_sh": 132.0,    "r_elb": -12.0,
        "sword_ang": 140.0
    },
    "block_guard": {
        "hip_dy": 8.0, "spine": -12.0,
        "l_thigh": -26.0, "l_knee": 34.0,
        "r_thigh": 26.0,  "r_knee": 30.0,
        "l_sh": 50.0,     "l_elb": -68.0,
        "r_sh": 64.0,     "r_elb": -80.0,
        "sword_ang": -22.0
    },
    "knockback_hit": {
        "hip_dy": 14.0, "spine": -42.0,
        "l_thigh": 20.0,  "l_knee": 45.0,
        "r_thigh": 38.0,  "r_knee": 58.0,
        "l_sh": 68.0,     "l_elb": -18.0,
        "r_sh": 78.0,     "r_elb": -14.0,
        "sword_ang": -60.0
    }
}

def lerp_pose(p_a: dict, p_b: dict, t: float) -> dict:
    u = max(0.0, min(1.0, t))
    e = u * u * (3.0 - 2.0 * u)
    return {k: p_a[k] + (p_b[k] - p_a[k]) * e for k in p_a}

def fk_limb_points(root: tuple, upper_len: float, lower_len: float,
                   ang1_deg: float, ang2_rel_deg: float, facing: int) -> tuple:
    a1 = math.radians(ang1_deg)
    a2 = math.radians(ang1_deg + ang2_rel_deg)
    mid = (
        root[0] + math.sin(a1) * upper_len * facing,
        root[1] + math.cos(a1) * upper_len
    )
    end = (
        mid[0] + math.sin(a2) * lower_len * facing,
        mid[1] + math.cos(a2) * lower_len
    )
    return mid, end

def draw_tapered_limb(draw: ImageDraw.Draw, p0: tuple, p1: tuple, p2: tuple,
                      w0: float, w1: float, w2: float, color: tuple):
    for (ax, ay, wa), (bx, by, wb) in [((p0[0], p0[1], w0), (p1[0], p1[1], w1)), ((p1[0], p1[1], w1), (p2[0], p2[1], w2))]:
        dx, dy = bx - ax, by - ay
        ln = max(1e-5, math.hypot(dx, dy))
        nx, ny = -dy / ln, dx / ln
        draw.polygon([
            (ax + nx * wa, ay + ny * wa),
            (bx + nx * wb, by + ny * wb),
            (bx - nx * wb, by - ny * wb),
            (ax - nx * wa, ay - ny * wa)
        ], fill=color)
    rm = int(w1)
    draw.ellipse([p1[0] - rm, p1[1] - rm, p1[0] + rm, p1[1] + rm], fill=color)

def draw_silhouette_fighter(draw: ImageDraw.Draw, cx: float, ground_y: float, scale: float,
                            pose: dict, facing: int, global_t: float,
                            accent_col: tuple = (56, 189, 248), slash_arc_alpha: float = 0.0):
    ink = (10, 12, 16)
    thigh_l = 44.0 * scale
    shin_l = 44.0 * scale
    spine_l = 60.0 * scale
    u_arm = 33.0 * scale
    f_arm = 31.0 * scale

    hip_x = cx
    hip_y = ground_y - (thigh_l + shin_l - 8.0 * scale) + pose["hip_dy"] * scale

    sp_rad = math.radians(pose["spine"])
    sh_x = hip_x + math.sin(sp_rad) * spine_l * facing
    sh_y = hip_y - math.cos(sp_rad) * spine_l

    # 1. Back Leg
    l_knee, l_foot = fk_limb_points((hip_x, hip_y), thigh_l, shin_l, pose["l_thigh"], pose["l_knee"], facing)
    draw_tapered_limb(draw, (hip_x, hip_y), l_knee, l_foot, 6.2 * scale, 4.8 * scale, 3.4 * scale, ink)

    # 2. Tapered Torso
    px, py = math.cos(sp_rad), math.sin(sp_rad) * facing
    tw_top, tw_bot = 9.5 * scale, 6.8 * scale
    draw.polygon([
        (sh_x - px * tw_top, sh_y - py * tw_top),
        (sh_x + px * tw_top, sh_y + py * tw_top),
        (hip_x + px * tw_bot, hip_y + py * tw_bot),
        (hip_x - px * tw_bot, hip_y - py * tw_bot)
    ], fill=ink)

    # 3. Front Leg
    r_knee, r_foot = fk_limb_points((hip_x, hip_y), thigh_l, shin_l, pose["r_thigh"], pose["r_knee"], facing)
    draw_tapered_limb(draw, (hip_x, hip_y), r_knee, r_foot, 6.4 * scale, 5.0 * scale, 3.5 * scale, ink)

    # 4. Back Arm
    l_elb, l_hnd = fk_limb_points((sh_x, sh_y + 4 * scale), u_arm, f_arm, pose["l_sh"], pose["l_elb"], facing)
    draw_tapered_limb(draw, (sh_x, sh_y + 4 * scale), l_elb, l_hnd, 4.8 * scale, 3.8 * scale, 2.8 * scale, ink)

    # 5. Head + Forehead Headband + Back-Trailing Ribbon (FIXED: never sticks out like a nose!)
    head_x = sh_x + math.sin(sp_rad) * 24.0 * scale * facing
    head_y = sh_y - math.cos(sp_rad) * 24.0 * scale
    hr = 17.5 * scale
    draw.ellipse([head_x - hr, head_y - hr, head_x + hr, head_y + hr], fill=ink)

    # Forehead band wrapped across upper head
    band_y = head_y - hr * 0.35
    draw.line([(head_x - hr * 0.92, band_y), (head_x + hr * 0.92, band_y)], fill=accent_col, width=max(3, int(4.5 * scale)))

    # Ribbon tails streaming BEHIND the head (-facing direction!)
    knot_x = head_x - hr * 0.92 * facing
    knot_y = band_y
    w1 = math.sin(global_t * 16.0) * 6.0 * scale
    w2 = math.cos(global_t * 13.0) * 8.0 * scale
    draw.line([
        (knot_x, knot_y),
        (knot_x - 22.0 * scale * facing, knot_y - 3.0 * scale + w1),
        (knot_x - 42.0 * scale * facing, knot_y + 6.0 * scale + w2)
    ], fill=accent_col, width=max(2, int(3.5 * scale)))

    # 6. Front Sword Arm & Katana Blade + Glowing Slash Crescent
    r_elb, r_hnd = fk_limb_points((sh_x, sh_y + 4 * scale), u_arm, f_arm, pose["r_sh"], pose["r_elb"], facing)
    draw_tapered_limb(draw, (sh_x, sh_y + 4 * scale), r_elb, r_hnd, 5.0 * scale, 4.0 * scale, 3.0 * scale, ink)

    sw_rad = math.radians(pose["sword_ang"])
    blade_len = 90.0 * scale
    tip_x = r_hnd[0] + math.sin(sw_rad) * blade_len * facing
    tip_y = r_hnd[1] + math.cos(sw_rad) * blade_len

    hilt_x = r_hnd[0] - math.sin(sw_rad) * 14.0 * scale * facing
    hilt_y = r_hnd[1] - math.cos(sw_rad) * 14.0 * scale
    draw.line([(hilt_x, hilt_y), r_hnd], fill=ink, width=max(3, int(4.5 * scale)))

    # Glowing Katana Edge
    draw.line([r_hnd, (tip_x, tip_y)], fill=(255, 255, 255), width=max(3, int(4.0 * scale)))
    draw.line([r_hnd, (tip_x, tip_y)], fill=accent_col, width=max(1, int(1.8 * scale)))

    if slash_arc_alpha > 0.05:
        arc_r = int(102 * scale)
        bbox = [r_hnd[0] - arc_r, r_hnd[1] - arc_r, r_hnd[0] + arc_r, r_hnd[1] + arc_r]
        if facing == 1:
            draw.arc(bbox, start=-78, end=58, fill=(255, 255, 255), width=max(7, int(18 * scale * slash_arc_alpha)))
            draw.arc(bbox, start=-68, end=48, fill=accent_col, width=max(3, int(8 * scale * slash_arc_alpha)))
        else:
            draw.arc(bbox, start=122, end=258, fill=(255, 255, 255), width=max(7, int(18 * scale * slash_arc_alpha)))
            draw.arc(bbox, start=132, end=248, fill=accent_col, width=max(3, int(8 * scale * slash_arc_alpha)))


# ===========================================================================
# 3. CINEMATIC ARENAS (DOJO / RAINY ROOFTOP / BLOOD MOON / CUSTOM USER BG)
# ===========================================================================
def build_arena_bg(w: int, h: int, theme: str, custom_bg_path=None) -> Image.Image:
    ground_y = int(h * 0.77)

    if custom_bg_path and os.path.exists(custom_bg_path):
        bg = Image.open(custom_bg_path).convert("RGB").resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")
        d = ImageDraw.Draw(bg)
        d.rectangle([0, ground_y, w, h], fill=(10, 12, 16, 235))
        return bg

    if theme == "blood_moon":
        top_c, bot_c = (45, 12, 22), (18, 8, 14)
        moon_c = (244, 63, 94, 175)
    elif theme == "rain_rooftop":
        top_c, bot_c = (38, 52, 68), (18, 26, 36)
        moon_c = (186, 230, 253, 130)
    else:
        top_c, bot_c = (185, 195, 206), (95, 105, 118)
        moon_c = (248, 250, 252, 165)

    # Smooth Vertical Gradient
    grad = np.zeros((h, w, 3), dtype=np.uint8)
    for ch in range(3):
        col_line = np.linspace(top_c[ch], bot_c[ch], h, dtype=np.uint8)[:, None]
        grad[:, :, ch] = np.repeat(col_line, w, axis=1)
    img = Image.fromarray(grad, mode="RGB").convert("RGBA")
    d = ImageDraw.Draw(img)

    # Giant Atmospheric Moon
    mx, my = int(w * 0.50), int(h * 0.34)
    mr = int(min(w, h) * 0.23)
    d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=moon_c)

    # Japanese Torii Gate / Dojo Silhouette in Background
    gx, gw = int(w * 0.50), int(w * 0.58)
    gh = int(h * 0.34)
    gy_top = ground_y - gh
    pillar_col = (24, 28, 36, 135)
    d.rectangle([gx - gw // 3, gy_top, gx - gw // 3 + 16, ground_y], fill=pillar_col)
    d.rectangle([gx + gw // 3 - 16, gy_top, gx + gw // 3, ground_y], fill=pillar_col)
    d.rectangle([gx - gw // 2, gy_top, gx + gw // 2, gy_top + 16], fill=pillar_col)
    d.rectangle([gx - int(gw * 0.42), gy_top + 34, gx + int(gw * 0.42), gy_top + 46], fill=pillar_col)

    # Solid Black Combat Floor
    d.rectangle([0, ground_y, w, h], fill=(10, 12, 16, 255))
    d.line([(0, ground_y), (w, ground_y)], fill=(255, 255, 255, 120), width=2)

    # Vignette
    vig = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vig)
    for b in range(28):
        al = int((28 - b) * 3.2)
        vd.rectangle([b * 3, b * 3, w - b * 3, h - b * 3], outline=(6, 8, 12, al), width=3)
    return Image.alpha_composite(img, vig)

def spawn_sparks(count: int = 28) -> list:
    rng = random.Random(42)
    sparks = []
    for _ in range(count):
        ang = rng.uniform(0, 2 * math.pi)
        spd = rng.uniform(160.0, 520.0)
        sz = rng.uniform(2.5, 6.0)
        sparks.append((math.cos(ang) * spd, math.sin(ang) * spd - 110.0, sz))
    return sparks

def render_combat_frame(base_bg: Image.Image, out_w: int, out_h: int, beat: dict,
                        prog: float, global_t: float, sparks: list, theme: str) -> bytes:
    scale = out_w / 540.0
    ground_y = out_h * 0.77
    action = beat["action"]

    # FAST 1.35-second combat cycle so fighters clash repeatedly instead of 7-second slow-mo!
    cycle_len = 1.35
    local_t = (global_t % cycle_len) / cycle_len

    hit_flash = 0.0
    shake_x, shake_y = 0, 0
    hero_slash = 0.0
    enemy_slash = 0.0

    if action == "combo_clash":
        # Fast back-and-forth sword exchange!
        if local_t < 0.28:
            sub = local_t / 0.28
            hero_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["windup"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["block_guard"], sub)
            hero_x = out_w * (0.34 + sub * 0.08)
            enemy_x = out_w * (0.66 - sub * 0.06)
        elif local_t < 0.52:
            sub = (local_t - 0.28) / 0.24
            hero_pose = lerp_pose(POSE_LIBRARY["windup"], POSE_LIBRARY["slash_follow"], sub)
            enemy_pose = POSE_LIBRARY["block_guard"]
            hero_x = out_w * 0.42
            enemy_x = out_w * 0.60
            hero_slash = math.sin(sub * math.pi)
            if sub > 0.65:
                hit_flash = 0.75
                shake_x = int(math.sin(global_t * 75.0) * 14 * scale)
        elif local_t < 0.78:
            # Enemy counter-attacks and Hero blocks!
            sub = (local_t - 0.52) / 0.26
            hero_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["block_guard"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["windup"], POSE_LIBRARY["slash_follow"], sub)
            hero_x = out_w * 0.40
            enemy_x = out_w * 0.58
            enemy_slash = math.sin(sub * math.pi)
            if sub > 0.65:
                hit_flash = 0.70
                shake_x = int(math.cos(global_t * 75.0) * 14 * scale)
        else:
            sub = (local_t - 0.78) / 0.22
            hero_pose = lerp_pose(POSE_LIBRARY["block_guard"], POSE_LIBRARY["stance"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["stance"], sub)
            hero_x = out_w * (0.40 - sub * 0.06)
            enemy_x = out_w * (0.58 + sub * 0.08)

    elif action in {"jump_attack", "finisher"}:
        if local_t < 0.42:
            sub = local_t / 0.42
            hero_pose = lerp_pose(POSE_LIBRARY["dash_forward"], POSE_LIBRARY["uppercut_air"], sub)
            enemy_pose = POSE_LIBRARY["stance"]
            hero_x = out_w * (0.28 + sub * 0.20)
            enemy_x = out_w * 0.64
            hero_slash = sub * 0.85
        elif local_t < 0.68:
            sub = (local_t - 0.42) / 0.26
            hero_pose = lerp_pose(POSE_LIBRARY["uppercut_air"], POSE_LIBRARY["slash_follow"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["knockback_hit"], sub)
            hero_x = out_w * 0.48
            enemy_x = out_w * (0.64 + sub * 0.14)
            hero_slash = 1.0 - sub * 0.5
            hit_flash = (1.0 - sub) * 0.90
            shake_x = int(math.sin(global_t * 80.0) * 16 * scale)
            shake_y = int(math.cos(global_t * 70.0) * 12 * scale)
        else:
            sub = (local_t - 0.68) / 0.32
            hero_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["stance"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["knockback_hit"], POSE_LIBRARY["stance"], sub)
            hero_x = out_w * (0.48 - sub * 0.14)
            enemy_x = out_w * (0.78 - sub * 0.12)

    elif action == "shadow_dash":
        cyc = 0.5 + 0.5 * math.sin(global_t * 16.0)
        hero_pose = lerp_pose(POSE_LIBRARY["dash_forward"], POSE_LIBRARY["windup"], cyc)
        enemy_pose = POSE_LIBRARY["stance"]
        hero_x = out_w * (0.24 + (local_t * 0.26))
        enemy_x = out_w * 0.68

    else:
        # Rapid Slash Assault
        if local_t < 0.35:
            sub = local_t / 0.35
            hero_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["windup"], sub)
            enemy_pose = POSE_LIBRARY["stance"]
            hero_x = out_w * (0.30 + sub * 0.14)
            enemy_x = out_w * 0.64
        elif local_t < 0.62:
            sub = (local_t - 0.35) / 0.27
            hero_pose = lerp_pose(POSE_LIBRARY["windup"], POSE_LIBRARY["slash_follow"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["knockback_hit"], sub)
            hero_x = out_w * 0.44
            enemy_x = out_w * (0.64 + sub * 0.12)
            hero_slash = math.sin(sub * math.pi)
            if sub > 0.5:
                hit_flash = 0.85
                shake_x = int(math.sin(global_t * 75.0) * 15 * scale)
        else:
            sub = (local_t - 0.62) / 0.38
            hero_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["stance"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["knockback_hit"], POSE_LIBRARY["stance"], sub)
            hero_x = out_w * (0.44 - sub * 0.14)
            enemy_x = out_w * (0.76 - sub * 0.12)

    # Dynamic Camera Zoom + Shake
    zoom = 1.04 + math.sin(local_t * math.pi) * 0.06
    cw, ch = int(out_w / zoom), int(out_h / zoom)
    cx0 = max(0, min(out_w - cw, (out_w - cw) // 2 + shake_x))
    cy0 = max(0, min(out_h - ch, int((out_h - ch) * 0.55) + shake_y))

    frame = base_bg.crop((cx0, cy0, cx0 + cw, cy0 + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR).convert("RGBA")
    draw = ImageDraw.Draw(frame)

    # Rain particles if Rainy Rooftop arena selected
    if theme == "rain_rooftop":
        for i in range(40):
            rx = int((i * 73 + global_t * 180) % (out_w + 60)) - 30
            ry = int((i * 97 + global_t * 820) % (out_h + 40)) - 20
            draw.line([(rx, ry), (rx - 8, ry + 22)], fill=(186, 230, 253, 140), width=2)

    # Ground Shadows
    for fx in (hero_x, enemy_x):
        draw.ellipse([fx - 30 * scale, ground_y - 6 * scale, fx + 30 * scale, ground_y + 6 * scale], fill=(0, 0, 0, 150))

    # Draw Enemy Fighter (facing LEFT = -1 toward Hero!) & Hero Fighter (facing RIGHT = +1 toward Enemy!)
    draw_silhouette_fighter(draw, enemy_x, ground_y, scale * 1.24, enemy_pose, facing=-1, global_t=global_t, accent_col=(244, 63, 94), slash_arc_alpha=enemy_slash)
    draw_silhouette_fighter(draw, hero_x, ground_y, scale * 1.26, hero_pose, facing=1, global_t=global_t, accent_col=(56, 189, 248), slash_arc_alpha=hero_slash)

    # Sparks burst on impact
    if local_t > 0.45 and action != "shadow_dash":
        dt = (local_t - 0.45) * 1.8
        ix, iy = (hero_x + enemy_x) * 0.5, ground_y - 88 * scale
        for vx, vy, sz in sparks:
            px = ix + vx * dt * scale * 0.42
            py = iy + (vy * dt + 260.0 * (dt ** 2)) * scale * 0.42
            if py < ground_y:
                r = max(1.5, sz * scale * (1.0 - dt * 0.65))
                draw.ellipse([px - r, py - r, px + r, py + r], fill=(255, 255, 255, 220))

    if hit_flash > 0.05:
        flash_layer = Image.new("RGBA", (out_w, out_h), (255, 255, 255, int(175 * hit_flash)))
        frame = Image.alpha_composite(frame, flash_layer)

    # Clean Studio Captions
    draw_f = ImageDraw.Draw(frame)
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
        top_y = int(out_h * 0.89) - (len(lines) * line_h) // 2

        for l_idx, tokens in enumerate(lines):
            row_w = sum(tk[2] for tk in tokens) + space_w * (len(tokens) - 1)
            cx = (out_w - row_w) // 2
            cy = top_y + l_idx * line_h
            for wd, idx, ww in tokens:
                col = (56, 189, 248) if (idx == active_idx) else (255, 255, 255)
                draw_f.text((cx, cy), wd, font=font, fill=(*col, 255), stroke_width=3, stroke_fill=(8, 10, 14, 255))
                cx += ww + space_w

    return frame.convert("RGB").tobytes()


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

def parse_combat_beats(raw_script: str):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip()]
    beats = []

    for ln in lines:
        if re.match(r"^SCENE\s*\d*", ln, re.I):
            continue

        tags = []
        caption = re.sub(r"\[([^\]]+)\]", lambda m: (tags.append(m.group(1).strip()), "")[1], ln).strip()
        if not caption:
            continue

        speaker = "Hero"
        action = "auto"

        for tg in tags:
            u = tg.upper()
            if u in {"COMBO_CLASH", "SHADOW_DASH", "SLASH", "JUMP_ATTACK", "FINISHER"}:
                action = u.lower()
            elif u not in {"HAPPY", "SAD", "SHOCK", "FEAR", "HEROIC", "FULL", "CLOSEUP"}:
                speaker = tg

        low = caption.lower()
        if action == "auto":
            if any(k in low for k in ["jump", "chhalang", "air", "upar", "finisher", "aakhiri"]):
                action = "jump_attack"
            elif any(k in low for k in ["clash", "takkar", "block", "ladai", "fight"]):
                action = "combo_clash"
            elif any(k in low for k in ["dash", "run", "raftar", "bhag", "aage"]):
                action = "shadow_dash"
            else:
                action = "slash"

        is_female = bool(re.search(r"(girl|meera|ananya|priya|aditi|neha|sara|riya|sakchunni)", speaker.lower()))

        beats.append({
            "index": len(beats) + 1,
            "speaker": speaker,
            "is_female": is_female,
            "action": action,
            "caption": caption,
            "words": caption.split()
        })
    return beats

def run_combat_job(job_id: str, script_text: str, ratio: str, arena_theme: str,
                   voice_mode: str, lang_mode: str, custom_wav_path, custom_bg_path):
    job = JOBS[job_id]
    try:
        beats = parse_combat_beats(script_text)
        if not beats:
            job["status"] = "error"
            job["message"] = "Script is empty!"
            return

        out_w, out_h = (576, 1024) if ratio == "9:16" else (1024, 576)
        fps = 24
        total = len(beats)

        job["message"] = "Generating Voiceover & Sword Clash SFX..."
        job["progress"] = 10

        raw_wav = os.path.join(AUDIO_DIR, f"{job_id}_raw.wav")
        final_wav = os.path.join(AUDIO_DIR, f"{job_id}_final.wav")
        beat_durations = []

        if voice_mode == "manual" and custom_wav_path and os.path.exists(custom_wav_path):
            total_audio_dur = convert_custom_audio_to_wav(custom_wav_path, raw_wav)
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
                job["progress"] = 10 + int(((idx + 1) / total) * 18)

            concat_txt = os.path.join(AUDIO_DIR, f"{job_id}_list.txt")
            with open(concat_txt, "w") as cf:
                for sw in seg_wavs:
                    cf.write(f"file '{sw}'\n")
            subprocess.run(
                [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", raw_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )

        # Inject fast sword clash & whoosh SFX on every 1.35s combo cycle!
        total_vid_dur = sum(beat_durations)
        impact_times = []
        t_mark = 0.45
        while t_mark < total_vid_dur - 0.3:
            impact_times.append((t_mark, "whoosh"))
            impact_times.append((t_mark + 0.18, "clash"))
            t_mark += 1.35

        inject_combat_sfx(raw_wav, impact_times, final_wav)

        base_bg = build_arena_bg(out_w, out_h, arena_theme, custom_bg_path)
        sparks = spawn_sparks(28)

        out_name = f"NEXORA_PRO_COMBAT_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", final_wav,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        for i, b in enumerate(beats):
            dur = beat_durations[i]
            n_frames = max(1, int(dur * fps))
            for f in range(n_frames):
                prog = f / max(1, n_frames - 1)
                raw_bytes = render_combat_frame(base_bg, out_w, out_h, b, prog, global_t, sparks, arena_theme)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps

            job["progress"] = 30 + int(((i + 1) / total) * 68)
            job["message"] = f"Rendering Fast Combat Combo {i + 1}/{total}..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Ready to Watch & Download ({out_name})"

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
    custom_wav_path = None
    custom_bg_path = None

    if request.content_type and "multipart/form-data" in request.content_type:
        script_text = request.form.get("script", "")
        ratio = request.form.get("ratio", "9:16")
        arena_theme = request.form.get("arena", "dojo_moon")
        voice_mode = request.form.get("voice_mode", "auto")
        lang_mode = request.form.get("lang_mode", "HINDI")
        if "voice_file" in request.files and request.files["voice_file"].filename:
            vf = request.files["voice_file"]
            custom_wav_path = os.path.join(AUDIO_DIR, f"{job_id}_voice_{vf.filename}")
            vf.save(custom_wav_path)
        if "bg_file" in request.files and request.files["bg_file"].filename:
            bf = request.files["bg_file"]
            custom_bg_path = os.path.join(AUDIO_DIR, f"{job_id}_bg_{bf.filename}")
            bf.save(custom_bg_path)
    else:
        data = request.get_json(force=True)
        script_text = data.get("script", "")
        ratio = data.get("ratio", "9:16")
        arena_theme = data.get("arena", "dojo_moon")
        voice_mode = data.get("voice_mode", "auto")
        lang_mode = data.get("lang_mode", "HINDI")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing Face-to-Face Combat Studio..."}
    threading.Thread(
        target=run_combat_job,
        args=(job_id, script_text, ratio, arena_theme, voice_mode, lang_mode, custom_wav_path, custom_bg_path),
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
