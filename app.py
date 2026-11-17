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
# 1. VOICE & PROCEDURAL SWORD SFX AUDIO MIXER
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
        dur = max(2.2, len(clean_txt.split()) * 0.38)
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

def inject_combat_sfx(master_wav: str, impact_times: list, out_mixed_wav: str):
    """Synthesizes crisp sword whoosh & metallic clash SFX at exact impact timestamps!"""
    try:
        with wave.open(master_wav, "rb") as wf:
            rate = wf.getframerate()
            samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

        sfx_track = np.zeros_like(samples)
        for t_hit, kind in impact_times:
            start_idx = int(t_hit * rate)
            if kind == "clash":
                dur_s = int(0.28 * rate)
                if start_idx + dur_s < len(sfx_track):
                    t_arr = np.linspace(0, 0.28, dur_s, endpoint=False)
                    env = np.exp(-t_arr * 16.0)
                    # Metallic clang + low sub punch
                    ring = (
                        0.45 * np.sin(2 * np.pi * 1450 * t_arr) +
                        0.35 * np.sin(2 * np.pi * 2380 * t_arr) +
                        0.65 * np.sin(2 * np.pi * 95 * np.exp(-t_arr * 12) * t_arr) +
                        0.25 * np.random.uniform(-1, 1, dur_s)
                    ) * env * 0.55
                    sfx_track[start_idx:start_idx + dur_s] += ring
            else:
                dur_s = int(0.22 * rate)
                if start_idx + dur_s < len(sfx_track):
                    t_arr = np.linspace(0, 0.22, dur_s, endpoint=False)
                    env = np.sin(np.pi * (t_arr / 0.22)) ** 1.5
                    whoosh = np.random.uniform(-1, 1, dur_s) * np.sin(2 * np.pi * 320 * t_arr) * env * 0.35
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
# 2. PRE-CALIBRATED FORWARD KINEMATICS KEYFRAME POSE LIBRARY (ZERO GLITCHES!)
# ===========================================================================
# Angles in degrees relative to vertical downward (0 = straight down, + = forward in facing dir, - = backward)
POSE_LIBRARY = {
    "stance": {
        "hip_dy": 0.0, "spine": 8.0,
        "l_thigh": -18.0, "l_knee": 24.0,
        "r_thigh": 22.0,  "r_knee": 20.0,
        "l_sh": -25.0,    "l_elb": -45.0,
        "r_sh": 32.0,     "r_elb": -55.0,
        "sword_ang": -115.0
    },
    "dash_forward": {
        "hip_dy": 6.0, "spine": 32.0,
        "l_thigh": -45.0, "l_knee": 55.0,
        "r_thigh": 48.0,  "r_knee": 35.0,
        "l_sh": -55.0,    "l_elb": -20.0,
        "r_sh": -40.0,    "r_elb": -30.0,
        "sword_ang": -145.0
    },
    "windup": {
        "hip_dy": 8.0, "spine": -16.0,
        "l_thigh": -22.0, "l_knee": 30.0,
        "r_thigh": 28.0,  "r_knee": 26.0,
        "l_sh": 35.0,     "l_elb": -40.0,
        "r_sh": -135.0,   "r_elb": -45.0,
        "sword_ang": -165.0
    },
    "slash_follow": {
        "hip_dy": 10.0, "spine": 34.0,
        "l_thigh": -34.0, "l_knee": 22.0,
        "r_thigh": 44.0,  "r_knee": 36.0,
        "l_sh": -48.0,    "l_elb": -25.0,
        "r_sh": 82.0,     "r_elb": -12.0,
        "sword_ang": 78.0
    },
    "uppercut_air": {
        "hip_dy": -65.0, "spine": 18.0,
        "l_thigh": -28.0, "l_knee": 68.0,
        "r_thigh": 38.0,  "r_knee": 52.0,
        "l_sh": -40.0,    "l_elb": -30.0,
        "r_sh": 125.0,    "r_elb": -15.0,
        "sword_ang": 135.0
    },
    "block_guard": {
        "hip_dy": 7.0, "spine": -10.0,
        "l_thigh": -24.0, "l_knee": 32.0,
        "r_thigh": 24.0,  "r_knee": 28.0,
        "l_sh": 45.0,     "l_elb": -70.0,
        "r_sh": 58.0,     "r_elb": -82.0,
        "sword_ang": -25.0
    },
    "knockback_hit": {
        "hip_dy": 14.0, "spine": -38.0,
        "l_thigh": 18.0,  "l_knee": 42.0,
        "r_thigh": 35.0,  "r_knee": 55.0,
        "l_sh": 65.0,     "l_elb": -20.0,
        "r_sh": 75.0,     "r_elb": -15.0,
        "sword_ang": -65.0
    }
}

def lerp_pose(p_a: dict, p_b: dict, t: float) -> dict:
    # Smooth cubic ease-in-out
    u = max(0.0, min(1.0, t))
    e = u * u * (3.0 - 2.0 * u)
    return {k: p_a[k] + (p_b[k] - p_a[k]) * e for k in p_a}

def fk_limb_points(root: tuple, upper_len: float, lower_len: float,
                   ang1_deg: float, ang2_rel_deg: float, facing: int) -> tuple:
    """Computes anatomically locked joint positions using Forward Kinematics (never bends backwards!)."""
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

def draw_tapered_silhouette_limb(draw: ImageDraw.Draw, p0: tuple, p1: tuple, p2: tuple,
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
                            has_sword: bool = True, accent_col: tuple = (255, 255, 255),
                            slash_arc_alpha: float = 0.0):
    """
    Renders a sleek, solid-black silhouette stickman fighter (44835.mp4 style)
    with flowing headband/scarf, anatomical limb tapering, katana sword & slash smear!
    """
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
    draw_tapered_silhouette_limb(draw, (hip_x, hip_y), l_knee, l_foot, 6.2 * scale, 4.8 * scale, 3.4 * scale, ink)

    # 2. Sleek Tapered Torso
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
    draw_tapered_silhouette_limb(draw, (hip_x, hip_y), r_knee, r_foot, 6.4 * scale, 5.0 * scale, 3.5 * scale, ink)

    # 4. Back Arm
    l_elb, l_hnd = fk_limb_points((sh_x, sh_y + 4 * scale), u_arm, f_arm, pose["l_sh"], pose["l_elb"], facing)
    draw_tapered_silhouette_limb(draw, (sh_x, sh_y + 4 * scale), l_elb, l_hnd, 4.8 * scale, 3.8 * scale, 2.8 * scale, ink)

    # 5. Head & Flowing Anime Combat Headband
    head_x = sh_x + math.sin(sp_rad) * 24.0 * scale * facing
    head_y = sh_y - math.cos(sp_rad) * 24.0 * scale
    hr = 17.5 * scale
    draw.ellipse([head_x - hr, head_y - hr, head_x + hr, head_y + hr], fill=ink)

    # Flowing headband tails trailing behind fighter
    tail_x1 = head_x - hr * 0.8 * facing
    tail_y1 = head_y - 3 * scale
    wave1 = math.sin(global_t * 14.0) * 7.0 * scale
    wave2 = math.cos(global_t * 12.0) * 9.0 * scale
    draw.line([
        (tail_x1, tail_y1),
        (tail_x1 - 24 * scale * facing, tail_y1 - 4 * scale + wave1),
        (tail_x1 - 48 * scale * facing, tail_y1 + 5 * scale + wave2)
    ], fill=accent_col, width=max(2, int(3.5 * scale)))

    # 6. Front Sword Arm & Katana Blade + Glowing White Slash Smear Arc!
    r_elb, r_hnd = fk_limb_points((sh_x, sh_y + 4 * scale), u_arm, f_arm, pose["r_sh"], pose["r_elb"], facing)
    draw_tapered_silhouette_limb(draw, (sh_x, sh_y + 4 * scale), r_elb, r_hnd, 5.0 * scale, 4.0 * scale, 3.0 * scale, ink)

    if has_sword:
        sw_rad = math.radians(pose["sword_ang"])
        blade_len = 86.0 * scale
        tip_x = r_hnd[0] + math.sin(sw_rad) * blade_len * facing
        tip_y = r_hnd[1] + math.cos(sw_rad) * blade_len

        # Sword Hilt & Crossguard
        hilt_x = r_hnd[0] - math.sin(sw_rad) * 14.0 * scale * facing
        hilt_y = r_hnd[1] - math.cos(sw_rad) * 14.0 * scale
        draw.line([(hilt_x, hilt_y), r_hnd], fill=ink, width=max(3, int(4.5 * scale)))

        # Crisp Katana Blade
        draw.line([r_hnd, (tip_x, tip_y)], fill=(248, 250, 252), width=max(2, int(3.5 * scale)))
        draw.line([r_hnd, (tip_x, tip_y)], fill=ink, width=max(1, int(1.5 * scale)))

        # Dramatic White/Cyan Slash Smear Crescent (exact 44835.mp4 impact look!)
        if slash_arc_alpha > 0.05:
            arc_r = int(96 * scale)
            bbox = [r_hnd[0] - arc_r, r_hnd[1] - arc_r, r_hnd[0] + arc_r, r_hnd[1] + arc_r]
            if facing == 1:
                draw.arc(bbox, start=-75, end=55, fill=(255, 255, 255), width=max(6, int(16 * scale * slash_arc_alpha)))
                draw.arc(bbox, start=-65, end=45, fill=accent_col, width=max(3, int(7 * scale * slash_arc_alpha)))
            else:
                draw.arc(bbox, start=125, end=255, fill=(255, 255, 255), width=max(6, int(16 * scale * slash_arc_alpha)))
                draw.arc(bbox, start=135, end=245, fill=accent_col, width=max(3, int(7 * scale * slash_arc_alpha)))


# ===========================================================================
# 3. CINEMATIC COMBAT STAGE, PARTICLES, HIT-FLASH & SCREEN SHAKE
# ===========================================================================
def build_monochrome_dojo_bg(w: int, h: int, scene_low: str) -> Image.Image:
    """Creates the atmospheric grey-gradient + subtle dojo/moonlight combat stage (44835.mp4 vibe)."""
    y_grad = np.linspace(195, 118, h, dtype=np.uint8)[:, None]
    arr = np.repeat(y_grad, w, axis=1)
    rgb = np.stack([arr, arr + 2, arr + 5], axis=2)
    img = Image.fromarray(rgb, mode="RGB").convert("RGBA")
    d = ImageDraw.Draw(img)

    # Subtle Giant Moon / Sun Disc in background
    mx, my = int(w * 0.50), int(h * 0.36)
    mr = int(min(w, h) * 0.24)
    d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(226, 232, 240, 125))

    # Distant Silhouette Mountains / Dojo Pillars
    ground_y = int(h * 0.77)
    d.polygon([(0, ground_y), (int(w * 0.25), int(h * 0.52)), (int(w * 0.55), ground_y)], fill=(100, 108, 118, 95))
    d.polygon([(int(w * 0.42), ground_y), (int(w * 0.78), int(h * 0.48)), (w, ground_y)], fill=(90, 98, 108, 105))

    # Solid Black Combat Ground Plane
    d.rectangle([0, ground_y, w, h], fill=(12, 14, 18, 255))
    d.line([(0, ground_y), (w, ground_y)], fill=(241, 245, 249, 140), width=2)

    # Cinema Vignette
    vig = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vig)
    for b in range(28):
        al = int((28 - b) * 3.2)
        vd.rectangle([b * 3, b * 3, w - b * 3, h - b * 3], outline=(8, 10, 14, al), width=3)
    return Image.alpha_composite(img, vig)

def spawn_sparks(cx: float, cy: float, count: int = 24) -> list:
    rng = random.Random(int(cx * 100 + cy))
    sparks = []
    for _ in range(count):
        ang = rng.uniform(0, 2 * math.pi)
        spd = rng.uniform(140.0, 480.0)
        sz = rng.uniform(2.5, 6.5)
        sparks.append((math.cos(ang) * spd, math.sin(ang) * spd - 90.0, sz))
    return sparks

def render_combat_frame(base_bg: Image.Image, out_w: int, out_h: int, beat: dict,
                        prog: float, global_t: float, sparks: list) -> bytes:
    scale = out_w / 540.0
    ground_y = out_h * 0.77
    action = beat["action"]

    # Determine Hero & Enemy Keyframe Interpolation + Impact Timing
    hit_flash = 0.0
    shake_x, shake_y = 0, 0
    slash_alpha = 0.0
    impact_t = 0.52

    if action in {"slash", "heavy_slash", "fight"}:
        if prog < 0.35:
            hero_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["windup"], prog / 0.35)
            enemy_pose = POSE_LIBRARY["stance"]
            hero_x = out_w * (0.28 + (prog / 0.35) * 0.14)
            enemy_x = out_w * 0.68
        elif prog < 0.58:
            sub = (prog - 0.35) / 0.23
            hero_pose = lerp_pose(POSE_LIBRARY["windup"], POSE_LIBRARY["slash_follow"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["block_guard"], sub)
            hero_x = out_w * (0.42 + sub * 0.12)
            enemy_x = out_w * 0.68
            slash_alpha = math.sin(sub * math.pi)
        else:
            sub = (prog - 0.58) / 0.42
            hero_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["stance"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["knockback_hit"], POSE_LIBRARY["stance"], sub)
            hero_x = out_w * 0.54
            enemy_x = out_w * (0.68 + math.sin(sub * math.pi) * 0.14)
            if sub < 0.25:
                hit_flash = (1.0 - sub / 0.25) * 0.85
                shake_x = int(math.sin(global_t * 65.0) * 14 * scale * (1.0 - sub / 0.25))
                shake_y = int(math.cos(global_t * 55.0) * 10 * scale * (1.0 - sub / 0.25))

    elif action in {"jump_attack", "jump"}:
        if prog < 0.45:
            sub = prog / 0.45
            hero_pose = lerp_pose(POSE_LIBRARY["dash_forward"], POSE_LIBRARY["uppercut_air"], sub)
            enemy_pose = POSE_LIBRARY["stance"]
            hero_x = out_w * (0.26 + sub * 0.26)
            enemy_x = out_w * 0.68
            slash_alpha = sub * 0.8
        else:
            sub = (prog - 0.45) / 0.55
            hero_pose = lerp_pose(POSE_LIBRARY["uppercut_air"], POSE_LIBRARY["slash_follow"], min(1.0, sub * 1.6))
            enemy_pose = lerp_pose(POSE_LIBRARY["knockback_hit"], POSE_LIBRARY["stance"], sub)
            hero_x = out_w * 0.52
            enemy_x = out_w * (0.68 + math.sin(sub * math.pi) * 0.16)
            if sub < 0.22:
                hit_flash = (1.0 - sub / 0.22) * 0.90
                shake_x = int(math.sin(global_t * 70.0) * 16 * scale)

    elif action in {"run", "walk"}:
        # Smooth cyclic dash/walk using keyframe blend
        cyc = 0.5 + 0.5 * math.sin(global_t * (12.0 if action == "run" else 7.0))
        hero_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["dash_forward"], cyc)
        enemy_pose = POSE_LIBRARY["stance"]
        hero_x = out_w * (0.24 + prog * 0.36)
        enemy_x = out_w * 0.74

    else:
        breathe = 0.5 + 0.5 * math.sin(global_t * 3.5)
        hero_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["windup"], breathe * 0.22)
        enemy_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["block_guard"], breathe * 0.20)
        hero_x = out_w * 0.34
        enemy_x = out_w * 0.68

    # Dynamic Camera Zoom on Combat Climax
    zoom = 1.03 + math.sin(prog * math.pi) * 0.08
    cw, ch = int(out_w / zoom), int(out_h / zoom)
    cx0 = max(0, min(out_w - cw, (out_w - cw) // 2 + shake_x))
    cy0 = max(0, min(out_h - ch, int((out_h - ch) * 0.55) + shake_y))

    frame = base_bg.crop((cx0, cy0, cx0 + cw, cy0 + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR).convert("RGBA")
    draw = ImageDraw.Draw(frame)

    # Ground Contact Shadows
    for fx in (hero_x, enemy_x):
        draw.ellipse([fx - 30 * scale, ground_y - 6 * scale, fx + 30 * scale, ground_y + 6 * scale], fill=(0, 0, 0, 140))

    # Draw Enemy Fighter (facing left = -1) & Hero Fighter (facing right = +1)
    draw_silhouette_fighter(draw, enemy_x, ground_y, scale * 1.22, enemy_pose, facing=-1, global_t=global_t, has_sword=True, accent_col=(244, 63, 94), slash_arc_alpha=0.0)
    draw_silhouette_fighter(draw, hero_x, ground_y, scale * 1.25, hero_pose, facing=1, global_t=global_t, has_sword=True, accent_col=(56, 189, 248), slash_arc_alpha=slash_alpha)

    # Impact Sparks & Ink Splatter after hit moment!
    if prog > impact_t and action in {"slash", "heavy_slash", "fight", "jump_attack", "jump"}:
        dt = (prog - impact_t) * 1.6
        ix, iy = (hero_x + enemy_x) * 0.5, ground_y - 85 * scale
        for vx, vy, sz in sparks:
            px = ix + vx * dt * scale * 0.45
            py = iy + (vy * dt + 280.0 * (dt ** 2)) * scale * 0.45
            if py < ground_y:
                r = max(1.5, sz * scale * (1.0 - dt * 0.6))
                draw.ellipse([px - r, py - r, px + r, py + r], fill=(12, 14, 18, 230))

    # Dramatic White Impact Flash Overlay (44835.mp4 signature frame!)
    if hit_flash > 0.05:
        flash_layer = Image.new("RGBA", (out_w, out_h), (255, 255, 255, int(190 * hit_flash)))
        frame = Image.alpha_composite(frame, flash_layer)

    # Clean Cinematic Action Captions
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
    current_scene = "MOONLIT DOJO ARENA"

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

        speaker = "Hero"
        action = "auto"

        for tg in tags:
            u = tg.upper()
            if u in {"SLASH", "HEAVY_SLASH", "JUMP_ATTACK", "FIGHT", "RUN", "WALK", "JUMP", "BLOCK", "STANCE"}:
                action = u.lower()
            elif u not in {"HAPPY", "SAD", "SHOCK", "ROMANTIC", "CONFUSED", "FEAR", "CHILL", "ANGRY", "HEROIC", "FULL", "HALF", "WIDE", "CLOSEUP"}:
                speaker = tg

        low = (current_scene + " " + caption).lower()
        if action == "auto":
            if any(k in low for k in ["jump", "uchhal", "air", "upar", "kood"]):
                action = "jump_attack"
            elif any(k in low for k in ["slash", "sword", "talwar", "attack", "vaar", "fight", "ladai"]):
                action = "slash"
            elif any(k in low for k in ["run", "dash", "bhag", "daud", "tez"]):
                action = "run"
            else:
                action = "slash" if (len(beats) % 2 == 1) else "run"

        is_female = bool(re.search(r"(girl|meera|ananya|priya|aditi|neha|sara|riya|sakchunni)", speaker.lower()))

        beats.append({
            "index": len(beats) + 1,
            "scene": current_scene,
            "speaker": speaker,
            "is_female": is_female,
            "action": action,
            "caption": caption,
            "words": caption.split()
        })
    return beats

def run_combat_job(job_id: str, script_text: str, ratio: str, voice_mode: str, lang_mode: str, custom_wav_path):
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

        job["message"] = "Preparing Voiceover & Sword Clash Sound FX..."
        job["progress"] = 10

        raw_wav = os.path.join(AUDIO_DIR, f"{job_id}_raw.wav")
        final_wav = os.path.join(AUDIO_DIR, f"{job_id}_final.wav")
        beat_durations = []

        if voice_mode == "manual" and custom_wav_path and os.path.exists(custom_wav_path):
            total_audio_dur = convert_custom_audio_to_wav(custom_wav_path, raw_wav)
            total_words = sum(max(1, len(b["words"])) for b in beats)
            for b in beats:
                share = max(1, len(b["words"])) / float(total_words)
                beat_durations.append(max(1.6, total_audio_dur * share))
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

        # Calculate exact sword clash & whoosh timestamps for SFX injection
        impact_times = []
        t_acc = 0.0
        for i, b in enumerate(beats):
            dur = beat_durations[i]
            if b["action"] in {"slash", "heavy_slash", "fight", "jump_attack", "jump"}:
                impact_times.append((t_acc + dur * 0.36, "whoosh"))
                impact_times.append((t_acc + dur * 0.54, "clash"))
            t_acc += dur

        inject_combat_sfx(raw_wav, impact_times, final_wav)

        base_bg = build_monochrome_dojo_bg(out_w, out_h, beats[0]["scene"])
        sparks = spawn_sparks(out_w * 0.56, out_h * 0.62, count=28)

        out_name = f"NEXORA_COMBAT_STICKMAN_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
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
                raw_bytes = render_combat_frame(base_bg, out_w, out_h, b, prog, global_t, sparks)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps

            job["progress"] = 30 + int(((i + 1) / total) * 68)
            job["message"] = f"Animating Combat Scene {i + 1}/{total} (24 FPS Keyframe Rig)..."

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

    if request.content_type and "multipart/form-data" in request.content_type:
        script_text = request.form.get("script", "")
        ratio = request.form.get("ratio", "9:16")
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
        voice_mode = data.get("voice_mode", "auto")
        lang_mode = data.get("lang_mode", "HINDI")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing Silhouette Stickman Combat Engine..."}
    threading.Thread(
        target=run_combat_job,
        args=(job_id, script_text, ratio, voice_mode, lang_mode, custom_wav_path),
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
