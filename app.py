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
from PIL import Image, ImageDraw, ImageOps
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
# 1. PURE COMBAT SFX AUDIO ENGINE (NO VOICEOVER — ONLY SWORD CLASH & WHOOSH!)
# ===========================================================================
def generate_combat_sfx_wav(total_dur: float, impact_times: list, out_wav: str):
    rate = 22050
    n_samples = int(total_dur * rate)
    sfx_track = np.zeros(n_samples, dtype=np.float32)

    for t_hit, kind in impact_times:
        start_idx = int(t_hit * rate)
        if kind == "clash":
            dur_s = int(0.28 * rate)
            if start_idx + dur_s < n_samples:
                t_arr = np.linspace(0, 0.28, dur_s, endpoint=False)
                env = np.exp(-t_arr * 15.0)
                ring = (
                    0.50 * np.sin(2 * np.pi * 1620 * t_arr) +
                    0.40 * np.sin(2 * np.pi * 2640 * t_arr) +
                    0.75 * np.sin(2 * np.pi * 88 * np.exp(-t_arr * 11) * t_arr)
                ) * env * 0.65
                sfx_track[start_idx:start_idx + dur_s] += ring
        else:
            dur_s = int(0.20 * rate)
            if start_idx + dur_s < n_samples:
                t_arr = np.linspace(0, 0.20, dur_s, endpoint=False)
                env = np.sin(np.pi * (t_arr / 0.20)) ** 1.5
                whoosh = np.random.uniform(-1, 1, dur_s) * np.sin(2 * np.pi * 350 * t_arr) * env * 0.35
                sfx_track[start_idx:start_idx + dur_s] += whoosh

    mixed = np.clip(sfx_track, -0.96, 0.96)
    pcm = (mixed * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf_out:
        wf_out.setnchannels(1)
        wf_out.setsampwidth(2)
        wf_out.setframerate(rate)
        wf_out.writeframes(pcm.tobytes())


# ===========================================================================
# 2. LOCKED FORWARD KINEMATICS POSE LIBRARY + PERFECT HORIZONTAL MIRRORING
# ===========================================================================
POSE_LIBRARY = {
    "stance": {
        "hip_dy": 0.0, "spine": 12.0,
        "l_thigh": -22.0, "l_knee": 28.0,
        "r_thigh": 26.0,  "r_knee": 24.0,
        "l_sh": -25.0,    "l_elb": -45.0,
        "r_sh": 38.0,     "r_elb": -55.0,
        "sword_ang": -125.0
    },
    "dash_forward": {
        "hip_dy": 8.0, "spine": 38.0,
        "l_thigh": -50.0, "l_knee": 60.0,
        "r_thigh": 54.0,  "r_knee": 38.0,
        "l_sh": -60.0,    "l_elb": -20.0,
        "r_sh": -45.0,    "r_elb": -30.0,
        "sword_ang": -155.0
    },
    "windup": {
        "hip_dy": 9.0, "spine": -16.0,
        "l_thigh": -25.0, "l_knee": 34.0,
        "r_thigh": 32.0,  "r_knee": 30.0,
        "l_sh": 40.0,     "l_elb": -40.0,
        "r_sh": -145.0,   "r_elb": -40.0,
        "sword_ang": -175.0
    },
    "slash_follow": {
        "hip_dy": 12.0, "spine": 40.0,
        "l_thigh": -40.0, "l_knee": 22.0,
        "r_thigh": 50.0,  "r_knee": 40.0,
        "l_sh": -55.0,    "l_elb": -22.0,
        "r_sh": 90.0,     "r_elb": -10.0,
        "sword_ang": 85.0
    },
    "uppercut_air": {
        "hip_dy": -80.0, "spine": 24.0,
        "l_thigh": -34.0, "l_knee": 74.0,
        "r_thigh": 44.0,  "r_knee": 58.0,
        "l_sh": -44.0,    "l_elb": -28.0,
        "r_sh": 135.0,    "r_elb": -12.0,
        "sword_ang": 142.0
    },
    "block_guard": {
        "hip_dy": 8.0, "spine": -10.0,
        "l_thigh": -26.0, "l_knee": 34.0,
        "r_thigh": 26.0,  "r_knee": 30.0,
        "l_sh": 52.0,     "l_elb": -68.0,
        "r_sh": 66.0,     "r_elb": -80.0,
        "sword_ang": -20.0
    },
    "knockback_hit": {
        "hip_dy": 15.0, "spine": -44.0,
        "l_thigh": 22.0,  "l_knee": 46.0,
        "r_thigh": 40.0,  "r_knee": 60.0,
        "l_sh": 70.0,     "l_elb": -18.0,
        "r_sh": 80.0,     "r_elb": -14.0,
        "sword_ang": -58.0
    }
}

def lerp_pose(p_a: dict, p_b: dict, t: float) -> dict:
    u = max(0.0, min(1.0, t))
    e = u * u * (3.0 - 2.0 * u)
    return {k: p_a[k] + (p_b[k] - p_a[k]) * e for k in p_a}

def fk_limb_points(root: tuple, upper_len: float, lower_len: float,
                   ang1_deg: float, ang2_rel_deg: float) -> tuple:
    a1 = math.radians(ang1_deg)
    a2 = math.radians(ang1_deg + ang2_rel_deg)
    mid = (root[0] + math.sin(a1) * upper_len, root[1] + math.cos(a1) * upper_len)
    end = (mid[0] + math.sin(a2) * lower_len, mid[1] + math.cos(a2) * lower_len)
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

def draw_fighter_sprite(scale: float, pose: dict, facing: int, global_t: float,
                        accent_col: tuple, slash_arc_alpha: float) -> tuple:
    """
    Renders a single fighter facing RIGHT (+1) onto a local transparent canvas,
    and if facing == -1, cleanly mirrors the entire sprite horizontally!
    Guarantees 100% anatomically accurate face-to-face poses with zero backwards joints.
    """
    box_w = int(340 * scale)
    box_h = int(340 * scale)
    sprite = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sprite)

    cx = box_w * 0.42
    ground_y = box_h * 0.88
    ink = (10, 12, 16, 255)

    thigh_l = 44.0 * scale
    shin_l = 44.0 * scale
    spine_l = 60.0 * scale
    u_arm = 33.0 * scale
    f_arm = 31.0 * scale

    hip_x = cx
    hip_y = ground_y - (thigh_l + shin_l - 8.0 * scale) + pose["hip_dy"] * scale

    sp_rad = math.radians(pose["spine"])
    sh_x = hip_x + math.sin(sp_rad) * spine_l
    sh_y = hip_y - math.cos(sp_rad) * spine_l

    # 1. Back Leg
    l_knee, l_foot = fk_limb_points((hip_x, hip_y), thigh_l, shin_l, pose["l_thigh"], pose["l_knee"])
    draw_tapered_limb(draw, (hip_x, hip_y), l_knee, l_foot, 6.2 * scale, 4.8 * scale, 3.4 * scale, ink)

    # 2. Tapered Torso
    px, py = math.cos(sp_rad), math.sin(sp_rad)
    tw_top, tw_bot = 9.5 * scale, 6.8 * scale
    draw.polygon([
        (sh_x - px * tw_top, sh_y - py * tw_top),
        (sh_x + px * tw_top, sh_y + py * tw_top),
        (hip_x + px * tw_bot, hip_y + py * tw_bot),
        (hip_x - px * tw_bot, hip_y - py * tw_bot)
    ], fill=ink)

    # 3. Front Leg
    r_knee, r_foot = fk_limb_points((hip_x, hip_y), thigh_l, shin_l, pose["r_thigh"], pose["r_knee"])
    draw_tapered_limb(draw, (hip_x, hip_y), r_knee, r_foot, 6.4 * scale, 5.0 * scale, 3.5 * scale, ink)

    # 4. Back Arm
    l_elb, l_hnd = fk_limb_points((sh_x, sh_y + 4 * scale), u_arm, f_arm, pose["l_sh"], pose["l_elb"])
    draw_tapered_limb(draw, (sh_x, sh_y + 4 * scale), l_elb, l_hnd, 4.8 * scale, 3.8 * scale, 2.8 * scale, ink)

    # 5. Head & Flowing Headband Ribbon trailing behind (-X)
    head_x = sh_x + math.sin(sp_rad) * 24.0 * scale
    head_y = sh_y - math.cos(sp_rad) * 24.0 * scale
    hr = 17.5 * scale
    draw.ellipse([head_x - hr, head_y - hr, head_x + hr, head_y + hr], fill=ink)

    band_y = head_y - hr * 0.35
    draw.line([(head_x - hr * 0.92, band_y), (head_x + hr * 0.92, band_y)], fill=(*accent_col, 255), width=max(3, int(4.5 * scale)))

    knot_x = head_x - hr * 0.92
    w1 = math.sin(global_t * 16.0) * 6.0 * scale
    w2 = math.cos(global_t * 13.0) * 8.0 * scale
    draw.line([
        (knot_x, band_y),
        (knot_x - 22.0 * scale, band_y - 3.0 * scale + w1),
        (knot_x - 42.0 * scale, band_y + 6.0 * scale + w2)
    ], fill=(*accent_col, 255), width=max(2, int(3.5 * scale)))

    # 6. Front Sword Arm & Katana Blade + Slash Crescent
    r_elb, r_hnd = fk_limb_points((sh_x, sh_y + 4 * scale), u_arm, f_arm, pose["r_sh"], pose["r_elb"])
    draw_tapered_limb(draw, (sh_x, sh_y + 4 * scale), r_elb, r_hnd, 5.0 * scale, 4.0 * scale, 3.0 * scale, ink)

    sw_rad = math.radians(pose["sword_ang"])
    blade_len = 92.0 * scale
    tip_x = r_hnd[0] + math.sin(sw_rad) * blade_len
    tip_y = r_hnd[1] + math.cos(sw_rad) * blade_len

    hilt_x = r_hnd[0] - math.sin(sw_rad) * 14.0 * scale
    hilt_y = r_hnd[1] - math.cos(sw_rad) * 14.0 * scale
    draw.line([(hilt_x, hilt_y), r_hnd], fill=ink, width=max(3, int(4.5 * scale)))

    draw.line([r_hnd, (tip_x, tip_y)], fill=(255, 255, 255, 255), width=max(3, int(4.0 * scale)))
    draw.line([r_hnd, (tip_x, tip_y)], fill=(*accent_col, 255), width=max(1, int(1.8 * scale)))

    if slash_arc_alpha > 0.05:
        arc_r = int(104 * scale)
        bbox = [r_hnd[0] - arc_r, r_hnd[1] - arc_r, r_hnd[0] + arc_r, r_hnd[1] + arc_r]
        draw.arc(bbox, start=-78, end=58, fill=(255, 255, 255, 255), width=max(7, int(18 * scale * slash_arc_alpha)))
        draw.arc(bbox, start=-68, end=48, fill=(*accent_col, 255), width=max(3, int(8 * scale * slash_arc_alpha)))

    if facing == -1:
        sprite = ImageOps.mirror(sprite)
        anchor_x = box_w - cx
    else:
        anchor_x = cx

    return sprite, anchor_x, ground_y


# ===========================================================================
# 3. CINEMATIC ARENAS & PURE ACTION FRAME COMPOSITOR (NO CAPTIONS!)
# ===========================================================================
def build_arena_bg(w: int, h: int, theme: str, custom_bg_path=None) -> Image.Image:
    ground_y = int(h * 0.80)

    if custom_bg_path and os.path.exists(custom_bg_path):
        bg = Image.open(custom_bg_path).convert("RGB").resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")
        d = ImageDraw.Draw(bg)
        d.rectangle([0, ground_y, w, h], fill=(10, 12, 16, 235))
        return bg

    if theme == "blood_moon":
        top_c, bot_c = (48, 12, 24), (16, 6, 12)
        moon_c = (244, 63, 94, 180)
    elif theme == "rain_rooftop":
        top_c, bot_c = (36, 50, 68), (16, 24, 34)
        moon_c = (186, 230, 253, 135)
    else:
        top_c, bot_c = (192, 202, 212), (96, 106, 120)
        moon_c = (255, 255, 255, 175)

    grad = np.zeros((h, w, 3), dtype=np.uint8)
    for ch in range(3):
        col_line = np.linspace(top_c[ch], bot_c[ch], h, dtype=np.uint8)[:, None]
        grad[:, :, ch] = np.repeat(col_line, w, axis=1)
    img = Image.fromarray(grad, mode="RGB").convert("RGBA")
    d = ImageDraw.Draw(img)

    # Giant Atmospheric Moon
    mx, my = int(w * 0.50), int(h * 0.35)
    mr = int(min(w, h) * 0.24)
    d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=moon_c)

    # Japanese Torii Gate Silhouette
    gx, gw = int(w * 0.50), int(w * 0.60)
    gh = int(h * 0.36)
    gy_top = ground_y - gh
    pillar_col = (24, 28, 36, 140)
    d.rectangle([gx - gw // 3, gy_top, gx - gw // 3 + 16, ground_y], fill=pillar_col)
    d.rectangle([gx + gw // 3 - 16, gy_top, gx + gw // 3, ground_y], fill=pillar_col)
    d.rectangle([gx - gw // 2, gy_top, gx + gw // 2, gy_top + 16], fill=pillar_col)
    d.rectangle([gx - int(gw * 0.42), gy_top + 34, gx + int(gw * 0.42), gy_top + 46], fill=pillar_col)

    # Clean Solid Combat Ground
    d.rectangle([0, ground_y, w, h], fill=(10, 12, 16, 255))
    d.line([(0, ground_y), (w, ground_y)], fill=(255, 255, 255, 130), width=2)

    vig = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vig)
    for b in range(28):
        al = int((28 - b) * 3.2)
        vd.rectangle([b * 3, b * 3, w - b * 3, h - b * 3], outline=(6, 8, 12, al), width=3)
    return Image.alpha_composite(img, vig)

def spawn_sparks(count: int = 30) -> list:
    rng = random.Random(42)
    return [(math.cos(rng.uniform(0, 2 * math.pi)) * rng.uniform(160.0, 520.0),
             math.sin(rng.uniform(0, 2 * math.pi)) * rng.uniform(160.0, 520.0) - 110.0,
             rng.uniform(2.5, 6.0)) for _ in range(count)]

def render_pure_combat_frame(base_bg: Image.Image, out_w: int, out_h: int, action: str,
                             global_t: float, sparks: list, theme: str) -> bytes:
    scale = out_w / 520.0
    ground_y = out_h * 0.80

    # Fast 1.25s high-energy anime combat cycle!
    cycle_len = 1.25
    local_t = (global_t % cycle_len) / cycle_len

    hit_flash = 0.0
    shake_x, shake_y = 0, 0
    hero_slash = 0.0
    enemy_slash = 0.0

    if action == "combo_clash":
        if local_t < 0.26:
            sub = local_t / 0.26
            hero_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["windup"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["block_guard"], sub)
            hero_x = out_w * (0.35 + sub * 0.08)
            enemy_x = out_w * (0.65 - sub * 0.06)
        elif local_t < 0.50:
            sub = (local_t - 0.26) / 0.24
            hero_pose = lerp_pose(POSE_LIBRARY["windup"], POSE_LIBRARY["slash_follow"], sub)
            enemy_pose = POSE_LIBRARY["block_guard"]
            hero_x = out_w * 0.43
            enemy_x = out_w * 0.59
            hero_slash = math.sin(sub * math.pi)
            if sub > 0.60:
                hit_flash = 0.75
                shake_x = int(math.sin(global_t * 80.0) * 14 * scale)
        elif local_t < 0.76:
            sub = (local_t - 0.50) / 0.26
            hero_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["block_guard"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["windup"], POSE_LIBRARY["slash_follow"], sub)
            hero_x = out_w * 0.41
            enemy_x = out_w * 0.57
            enemy_slash = math.sin(sub * math.pi)
            if sub > 0.60:
                hit_flash = 0.70
                shake_x = int(math.cos(global_t * 80.0) * 14 * scale)
        else:
            sub = (local_t - 0.76) / 0.24
            hero_pose = lerp_pose(POSE_LIBRARY["block_guard"], POSE_LIBRARY["stance"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["stance"], sub)
            hero_x = out_w * (0.41 - sub * 0.06)
            enemy_x = out_w * (0.57 + sub * 0.08)

    elif action in {"jump_attack", "finisher"}:
        if local_t < 0.40:
            sub = local_t / 0.40
            hero_pose = lerp_pose(POSE_LIBRARY["dash_forward"], POSE_LIBRARY["uppercut_air"], sub)
            enemy_pose = POSE_LIBRARY["stance"]
            hero_x = out_w * (0.28 + sub * 0.20)
            enemy_x = out_w * 0.64
            hero_slash = sub * 0.85
        elif local_t < 0.68:
            sub = (local_t - 0.40) / 0.28
            hero_pose = lerp_pose(POSE_LIBRARY["uppercut_air"], POSE_LIBRARY["slash_follow"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["knockback_hit"], sub)
            hero_x = out_w * 0.48
            enemy_x = out_w * (0.64 + sub * 0.14)
            hero_slash = 1.0 - sub * 0.5
            hit_flash = (1.0 - sub) * 0.90
            shake_x = int(math.sin(global_t * 85.0) * 16 * scale)
            shake_y = int(math.cos(global_t * 75.0) * 12 * scale)
        else:
            sub = (local_t - 0.68) / 0.32
            hero_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["stance"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["knockback_hit"], POSE_LIBRARY["stance"], sub)
            hero_x = out_w * (0.48 - sub * 0.13)
            enemy_x = out_w * (0.78 - sub * 0.13)

    elif action == "shadow_dash":
        cyc = 0.5 + 0.5 * math.sin(global_t * 18.0)
        hero_pose = lerp_pose(POSE_LIBRARY["dash_forward"], POSE_LIBRARY["windup"], cyc)
        enemy_pose = POSE_LIBRARY["stance"]
        hero_x = out_w * (0.24 + (local_t * 0.26))
        enemy_x = out_w * 0.68

    else:
        if local_t < 0.34:
            sub = local_t / 0.34
            hero_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["windup"], sub)
            enemy_pose = POSE_LIBRARY["stance"]
            hero_x = out_w * (0.31 + sub * 0.14)
            enemy_x = out_w * 0.63
        elif local_t < 0.62:
            sub = (local_t - 0.34) / 0.28
            hero_pose = lerp_pose(POSE_LIBRARY["windup"], POSE_LIBRARY["slash_follow"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["stance"], POSE_LIBRARY["knockback_hit"], sub)
            hero_x = out_w * 0.45
            enemy_x = out_w * (0.63 + sub * 0.12)
            hero_slash = math.sin(sub * math.pi)
            if sub > 0.5:
                hit_flash = 0.85
                shake_x = int(math.sin(global_t * 80.0) * 15 * scale)
        else:
            sub = (local_t - 0.62) / 0.38
            hero_pose = lerp_pose(POSE_LIBRARY["slash_follow"], POSE_LIBRARY["stance"], sub)
            enemy_pose = lerp_pose(POSE_LIBRARY["knockback_hit"], POSE_LIBRARY["stance"], sub)
            hero_x = out_w * (0.45 - sub * 0.14)
            enemy_x = out_w * (0.75 - sub * 0.12)

    # Dynamic Camera Zoom + Screen Shake
    zoom = 1.04 + math.sin(local_t * math.pi) * 0.06
    cw, ch = int(out_w / zoom), int(out_h / zoom)
    cx0 = max(0, min(out_w - cw, (out_w - cw) // 2 + shake_x))
    cy0 = max(0, min(out_h - ch, int((out_h - ch) * 0.55) + shake_y))

    frame = base_bg.crop((cx0, cy0, cx0 + cw, cy0 + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR).convert("RGBA")
    draw = ImageDraw.Draw(frame)

    if theme == "rain_rooftop":
        for i in range(42):
            rx = int((i * 73 + global_t * 180) % (out_w + 60)) - 30
            ry = int((i * 97 + global_t * 820) % (out_h + 40)) - 20
            draw.line([(rx, ry), (rx - 8, ry + 22)], fill=(186, 230, 253, 145), width=2)

    for fx in (hero_x, enemy_x):
        draw.ellipse([fx - 30 * scale, ground_y - 6 * scale, fx + 30 * scale, ground_y + 6 * scale], fill=(0, 0, 0, 150))

    # Composite Mirrored Enemy (facing LEFT = -1) & Hero (facing RIGHT = +1)
    enemy_spr, e_ax, e_ay = draw_fighter_sprite(scale * 1.28, enemy_pose, facing=-1, global_t=global_t, accent_col=(244, 63, 94), slash_arc_alpha=enemy_slash)
    frame.alpha_composite(enemy_spr, (int(enemy_x - e_ax), int(ground_y - e_ay)))

    hero_spr, h_ax, h_ay = draw_fighter_sprite(scale * 1.30, hero_pose, facing=1, global_t=global_t, accent_col=(56, 189, 248), slash_arc_alpha=hero_slash)
    frame.alpha_composite(hero_spr, (int(hero_x - h_ax), int(ground_y - h_ay)))

    # Sparks burst on impact
    if local_t > 0.44 and action != "shadow_dash":
        dt = (local_t - 0.44) * 1.8
        ix, iy = (hero_x + enemy_x) * 0.5, ground_y - 90 * scale
        for vx, vy, sz in sparks:
            px = ix + vx * dt * scale * 0.42
            py = iy + (vy * dt + 260.0 * (dt ** 2)) * scale * 0.42
            if py < ground_y:
                r = max(1.5, sz * scale * (1.0 - dt * 0.65))
                draw.ellipse([px - r, py - r, px + r, py + r], fill=(255, 255, 255, 225))

    if hit_flash > 0.05:
        flash_layer = Image.new("RGBA", (out_w, out_h), (255, 255, 255, int(175 * hit_flash)))
        frame = Image.alpha_composite(frame, flash_layer)

    return frame.convert("RGB").tobytes()


# ===========================================================================
# 4. FAST PURE-ACTION RENDER PIPELINE
# ===========================================================================
def parse_action_sequence(raw_script: str, total_dur: float):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip() and not re.match(r"^SCENE", ln, re.I)]
    actions = []
    for ln in lines:
        u = ln.upper()
        if "JUMP" in u or "FINISHER" in u or "AIR" in u:
            actions.append("jump_attack")
        elif "CLASH" in u or "COMBO" in u or "BLOCK" in u:
            actions.append("combo_clash")
        elif "DASH" in u or "RUN" in u:
            actions.append("shadow_dash")
        else:
            actions.append("slash")

    if not actions:
        actions = ["shadow_dash", "combo_clash", "jump_attack"]

    seg_dur = total_dur / float(len(actions))
    return [(act, seg_dur) for act in actions]

def run_pure_combat_job(job_id: str, script_text: str, ratio: str, arena_theme: str, duration_sec: float, custom_bg_path):
    job = JOBS[job_id]
    try:
        seq = parse_action_sequence(script_text, duration_sec)
        out_w, out_h = (576, 1024) if ratio == "9:16" else (1024, 576)
        fps = 24

        job["message"] = "Synthesizing Sword Clash & Whoosh SFX..."
        job["progress"] = 12

        sfx_wav = os.path.join(AUDIO_DIR, f"{job_id}_sfx.wav")
        impact_times = []
        t_mark = 0.42
        while t_mark < duration_sec - 0.3:
            impact_times.append((t_mark, "whoosh"))
            impact_times.append((t_mark + 0.18, "clash"))
            t_mark += 1.25
        generate_combat_sfx_wav(duration_sec, impact_times, sfx_wav)

        base_bg = build_arena_bg(out_w, out_h, arena_theme, custom_bg_path)
        sparks = spawn_sparks(30)

        out_name = f"NEXORA_PURE_COMBAT_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", sfx_wav,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        total_frames = int(duration_sec * fps)
        frame_count = 0

        for act, seg_dur in seq:
            n_frames = max(1, int(seg_dur * fps))
            for _ in range(n_frames):
                raw_bytes = render_pure_combat_frame(base_bg, out_w, out_h, act, global_t, sparks, arena_theme)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps
                frame_count += 1
                if frame_count % 24 == 0:
                    job["progress"] = 15 + int((frame_count / max(1, total_frames)) * 82)
                    job["message"] = f"Rendering Pure Action Frames ({frame_count}/{total_frames})..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Clean Action Video Ready ({out_name})"

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
    custom_bg_path = None

    if request.content_type and "multipart/form-data" in request.content_type:
        script_text = request.form.get("script", "")
        ratio = request.form.get("ratio", "9:16")
        arena_theme = request.form.get("arena", "dojo_moon")
        duration_sec = float(request.form.get("duration", "9"))
        if "bg_file" in request.files and request.files["bg_file"].filename:
            bf = request.files["bg_file"]
            custom_bg_path = os.path.join(AUDIO_DIR, f"{job_id}_bg_{bf.filename}")
            bf.save(custom_bg_path)
    else:
        data = request.get_json(force=True)
        script_text = data.get("script", "")
        ratio = data.get("ratio", "9:16")
        arena_theme = data.get("arena", "dojo_moon")
        duration_sec = float(data.get("duration", "9"))

    duration_sec = max(4.0, min(30.0, duration_sec))
    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing Pure Action Combat Studio..."}
    threading.Thread(
        target=run_pure_combat_job,
        args=(job_id, script_text, ratio, arena_theme, duration_sec, custom_bg_path),
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
