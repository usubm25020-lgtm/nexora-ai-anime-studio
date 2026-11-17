import os
import time
import math
import uuid
import wave
import random
import subprocess
import threading
import numpy as np

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
# 1. MUSICAL COLLISION SOUND SYNTHESIZER (EVERY BOUNCE PLAYS A MELODY!)
# ===========================================================================
# Pentatonic & Epic Cinematic Scale Frequencies (Hz)
MELODY_NOTES = [
    261.63, 293.66, 329.63, 392.00, 440.00,
    523.25, 587.33, 659.25, 783.99, 880.00,
    1046.50, 1174.66, 1318.51, 1567.98
]

def build_collision_audio_wav(duration_sec: float, events: list, out_wav: str):
    """
    Synthesizes crystal-clear bell/piano notes on every ball wall-hit,
    plus shattering glass/bass boom when a ring breaks!
    """
    rate = 22050
    n_samples = int(duration_sec * rate)
    audio = np.zeros(n_samples, dtype=np.float32)

    for ev_time, ev_type, note_idx in events:
        s_idx = int(ev_time * rate)
        if s_idx >= n_samples:
            continue

        if ev_type == "shatter":
            dur = 0.45
            e_idx = min(n_samples, s_idx + int(dur * rate))
            seg_len = e_idx - s_idx
            t = np.linspace(0, seg_len / float(rate), seg_len, endpoint=False, dtype=np.float32)
            # Sub-bass drop + sparkling chime burst
            sub = np.sin(2.0 * np.pi * (110.0 * np.exp(-t * 7.0) + 42.0) * t) * np.exp(-t * 6.5) * 0.65
            sparkle = (
                np.sin(2.0 * np.pi * 1318.5 * t) +
                np.sin(2.0 * np.pi * 1760.0 * t)
            ) * np.exp(-t * 11.0) * 0.25
            audio[s_idx:e_idx] += (sub + sparkle)
        else:
            dur = 0.36
            e_idx = min(n_samples, s_idx + int(dur * rate))
            seg_len = e_idx - s_idx
            t = np.linspace(0, seg_len / float(rate), seg_len, endpoint=False, dtype=np.float32)
            freq = MELODY_NOTES[note_idx % len(MELODY_NOTES)]
            env = np.exp(-t * 9.5)
            tone = (
                0.65 * np.sin(2.0 * np.pi * freq * t) +
                0.25 * np.sin(2.0 * np.pi * freq * 2.0 * t) +
                0.10 * np.sin(2.0 * np.pi * freq * 3.0 * t)
            ) * env * 0.42
            audio[s_idx:e_idx] += tone

    # Soft ambient sub-drone so silence never feels empty
    t_full = np.linspace(0, duration_sec, n_samples, endpoint=False, dtype=np.float32)
    drone = 0.045 * np.sin(2.0 * np.pi * 65.41 * t_full)
    audio = np.clip(audio + drone, -0.95, 0.95)

    pcm = (audio * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


# ===========================================================================
# 2. RIGID-BODY PHYSICS SIMULATION ENGINE (3 VIRAL MODES)
# ===========================================================================
def normalize_angle(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi

def angle_in_gap(theta: float, gap_center: float, gap_half: float) -> bool:
    diff = abs(normalize_angle(theta - gap_center))
    return diff <= gap_half

class PhysicsArcadeSimulator:
    def __init__(self, w: int, h: int, mode: str, duration_sec: float, fps: int = 30):
        self.w = w
        self.h = h
        self.mode = mode
        self.duration = duration_sec
        self.fps = fps
        self.cx = w * 0.50
        self.cy = h * 0.50
        self.scale = min(w, h) / 540.0

        self.events = []      # (time_sec, "bounce"|"shatter", note_idx)
        self.particles = []   # [x, y, vx, vy, life, color]
        self.shake_timer = 0.0
        self.note_counter = 0

        # Background gradient
        grad = np.zeros((h, w, 3), dtype=np.uint8)
        top_c, bot_c = (10, 14, 26), (3, 5, 10)
        for ch in range(3):
            col_line = np.linspace(top_c[ch], bot_c[ch], h, dtype=np.uint8)[:, None]
            grad[:, :, ch] = np.repeat(col_line, w, axis=1)
        self.base_bg = Image.fromarray(grad, mode="RGB").convert("RGBA")

        # Trail layer for silky neon motion ribbons
        self.trail_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))

        self.rng = random.Random(2026)
        self._init_entities()

    def _init_entities(self):
        sc = self.scale
        if self.mode == "ring_escape":
            # 5 Concentric Rotating Neon Rings with moving escape gaps!
            palette = [
                (34, 211, 238),   # Cyan
                (168, 85, 247),   # Violet
                (244, 63, 94),    # Rose
                (250, 204, 21),   # Gold
                (16, 185, 129)    # Emerald
            ]
            self.rings = []
            for i in range(5):
                r_rad = (85.0 + i * 36.0) * sc
                self.rings.append({
                    "r": r_rad,
                    "gap_angle": i * 1.1,
                    "gap_half": 0.44 - i * 0.02,
                    "omega": (1.65 - i * 0.16) * (1 if i % 2 == 0 else -1),
                    "color": palette[i % len(palette)],
                    "alive": True
                })
            self.balls = [{
                "x": self.cx + 12.0 * sc,
                "y": self.cy - 18.0 * sc,
                "vx": 210.0 * sc,
                "vy": -160.0 * sc,
                "r": 12.0 * sc,
                "color": (255, 255, 255),
                "trail_col": (56, 189, 248),
                "gravity": 480.0 * sc,
                "restitution": 1.015
            }]

        elif self.mode == "growing_chaos":
            # Single giant arena where ball grows larger & faster on every single bounce!
            self.arena_r = 225.0 * sc
            self.balls = [{
                "x": self.cx - 35.0 * sc,
                "y": self.cy - 40.0 * sc,
                "vx": 240.0 * sc,
                "vy": 120.0 * sc,
                "r": 14.0 * sc,
                "color": (250, 204, 21),
                "trail_col": (244, 63, 94),
                "gravity": 520.0 * sc,
                "restitution": 1.012
            }]

        else:
            # marble_duel: Cyan vs Crimson high-speed battle inside rotating arena!
            self.arena_r = 225.0 * sc
            self.balls = [
                {
                    "x": self.cx - 65.0 * sc,
                    "y": self.cy - 30.0 * sc,
                    "vx": 275.0 * sc,
                    "vy": -190.0 * sc,
                    "r": 18.0 * sc,
                    "color": (34, 211, 238),
                    "trail_col": (34, 211, 238),
                    "gravity": 420.0 * sc,
                    "restitution": 1.005,
                    "score": 0
                },
                {
                    "x": self.cx + 65.0 * sc,
                    "y": self.cy - 20.0 * sc,
                    "vx": -265.0 * sc,
                    "vy": -210.0 * sc,
                    "r": 18.0 * sc,
                    "color": (244, 63, 94),
                    "trail_col": (244, 63, 94),
                    "gravity": 420.0 * sc,
                    "restitution": 1.005,
                    "score": 0
                }
            ]

    def _spawn_sparks(self, x: float, y: float, color: tuple, count: int = 16):
        for _ in range(count):
            ang = self.rng.uniform(0, 2.0 * math.pi)
            spd = self.rng.uniform(90.0, 360.0) * self.scale
            self.particles.append([x, y, math.cos(ang) * spd, math.sin(ang) * spd, 1.0, color])

    def step_physics(self, t: float, dt: float):
        sub_steps = 6
        sdt = dt / float(sub_steps)
        sc = self.scale

        if self.shake_timer > 0:
            self.shake_timer = max(0.0, self.shake_timer - dt)

        # Update particles
        alive_p = []
        for p in self.particles:
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[3] += 320.0 * sc * dt
            p[4] -= dt * 2.4
            if p[4] > 0:
                alive_p.append(p)
        self.particles = alive_p

        if self.mode == "ring_escape":
            for ring in self.rings:
                if ring["alive"]:
                    ring["gap_angle"] = normalize_angle(ring["gap_angle"] + ring["omega"] * dt)

            b = self.balls[0]
            for _ in range(sub_steps):
                b["vy"] += b["gravity"] * sdt
                b["x"] += b["vx"] * sdt
                b["y"] += b["vy"] * sdt

                # Cap max speed so physics stays crisp
                spd = math.hypot(b["vx"], b["vy"])
                max_spd = 680.0 * sc
                if spd > max_spd:
                    b["vx"] = (b["vx"] / spd) * max_spd
                    b["vy"] = (b["vy"] / spd) * max_spd

                dx = b["x"] - self.cx
                dy = b["y"] - self.cy
                dist = math.hypot(dx, dy)
                theta = math.atan2(dy, dx)

                # Check innermost alive ring
                active_ring = next((r for r in self.rings if r["alive"]), None)
                if active_ring:
                    r_lim = active_ring["r"] - b["r"]
                    if dist >= r_lim and dist > 1e-4:
                        if angle_in_gap(theta, active_ring["gap_angle"], active_ring["gap_half"]):
                            # Escaped through the rotating gap! Shatter ring!
                            active_ring["alive"] = False
                            self.shake_timer = 0.22
                            self.events.append((t, "shatter", self.note_counter))
                            self.note_counter += 1
                            for a_sp in np.linspace(0, 2 * math.pi, 32, endpoint=False):
                                sx = self.cx + math.cos(a_sp) * active_ring["r"]
                                sy = self.cy + math.sin(a_sp) * active_ring["r"]
                                self._spawn_sparks(sx, sy, active_ring["color"], count=2)
                        else:
                            # Elastic wall bounce with slight energy boost
                            nx, ny = dx / dist, dy / dist
                            b["x"] = self.cx + nx * (r_lim - 0.5)
                            b["y"] = self.cy + ny * (r_lim - 0.5)
                            dot = b["vx"] * nx + b["vy"] * ny
                            if dot > 0:
                                b["vx"] = (b["vx"] - 2.0 * dot * nx) * b["restitution"]
                                b["vy"] = (b["vy"] - 2.0 * dot * ny) * b["restitution"]
                                self._spawn_sparks(self.cx + nx * active_ring["r"], self.cy + ny * active_ring["r"], active_ring["color"], count=10)
                                self.events.append((t, "bounce", self.note_counter))
                                self.note_counter += 1
                else:
                    # All 5 rings shattered! Bounce inside outer screen boundary with celebration!
                    outer_lim = 235.0 * sc - b["r"]
                    if dist >= outer_lim and dist > 1e-4:
                        nx, ny = dx / dist, dy / dist
                        b["x"] = self.cx + nx * (outer_lim - 0.5)
                        b["y"] = self.cy + ny * (outer_lim - 0.5)
                        dot = b["vx"] * nx + b["vy"] * ny
                        if dot > 0:
                            b["vx"] -= 2.0 * dot * nx
                            b["vy"] -= 2.0 * dot * ny
                            self._spawn_sparks(b["x"], b["y"], (250, 204, 21), count=14)
                            self.events.append((t, "bounce", self.note_counter))
                            self.note_counter += 1

        elif self.mode == "growing_chaos":
            b = self.balls[0]
            max_r = self.arena_r * 0.86
            for _ in range(sub_steps):
                b["vy"] += b["gravity"] * sdt
                b["x"] += b["vx"] * sdt
                b["y"] += b["vy"] * sdt

                spd = math.hypot(b["vx"], b["vy"])
                max_spd = 740.0 * sc
                if spd > max_spd:
                    b["vx"] = (b["vx"] / spd) * max_spd
                    b["vy"] = (b["vy"] / spd) * max_spd

                dx = b["x"] - self.cx
                dy = b["y"] - self.cy
                dist = math.hypot(dx, dy)
                r_lim = self.arena_r - b["r"]

                if dist >= r_lim and dist > 1e-4:
                    nx, ny = dx / dist, dy / dist
                    b["x"] = self.cx + nx * (r_lim - 0.5)
                    b["y"] = self.cy + ny * (r_lim - 0.5)
                    dot = b["vx"] * nx + b["vy"] * ny
                    if dot > 0:
                        b["vx"] = (b["vx"] - 2.0 * dot * nx) * b["restitution"]
                        b["vy"] = (b["vy"] - 2.0 * dot * ny) * b["restitution"]
                        # Grow ball size on every bounce!
                        b["r"] = min(max_r, b["r"] + 2.3 * sc)
                        # Shift color dynamically as tension builds
                        hue_idx = self.note_counter % 4
                        cols = [(250, 204, 21), (249, 115, 22), (244, 63, 94), (168, 85, 247)]
                        b["color"] = cols[hue_idx]
                        b["trail_col"] = cols[(hue_idx + 1) % 4]
                        self._spawn_sparks(self.cx + nx * self.arena_r, self.cy + ny * self.arena_r, b["color"], count=12)
                        self.events.append((t, "bounce", self.note_counter))
                        self.note_counter += 1
                        if b["r"] >= max_r * 0.96:
                            # Reset with massive explosion shockwave!
                            b["r"] = 16.0 * sc
                            self.shake_timer = 0.28
                            self.events.append((t, "shatter", self.note_counter))
                            self._spawn_sparks(self.cx, self.cy, (255, 255, 255), count=45)

        else:
            # marble_duel: Two rival balls colliding with each other & arena wall!
            for _ in range(sub_steps):
                for b in self.balls:
                    b["vy"] += b["gravity"] * sdt
                    b["x"] += b["vx"] * sdt
                    b["y"] += b["vy"] * sdt

                    spd = math.hypot(b["vx"], b["vy"])
                    max_spd = 720.0 * sc
                    if spd > max_spd:
                        b["vx"] = (b["vx"] / spd) * max_spd
                        b["vy"] = (b["vy"] / spd) * max_spd

                    dx = b["x"] - self.cx
                    dy = b["y"] - self.cy
                    dist = math.hypot(dx, dy)
                    r_lim = self.arena_r - b["r"]
                    if dist >= r_lim and dist > 1e-4:
                        nx, ny = dx / dist, dy / dist
                        b["x"] = self.cx + nx * (r_lim - 0.5)
                        b["y"] = self.cy + ny * (r_lim - 0.5)
                        dot = b["vx"] * nx + b["vy"] * ny
                        if dot > 0:
                            b["vx"] = (b["vx"] - 2.0 * dot * nx) * b["restitution"]
                            b["vy"] = (b["vy"] - 2.0 * dot * ny) * b["restitution"]
                            b["score"] += 1
                            self._spawn_sparks(self.cx + nx * self.arena_r, self.cy + ny * self.arena_r, b["color"], count=10)
                            self.events.append((t, "bounce", self.note_counter))
                            self.note_counter += 1

                # Ball-to-Ball Elastic Collision!
                b1, b2 = self.balls[0], self.balls[1]
                dx = b2["x"] - b1["x"]
                dy = b2["y"] - b1["y"]
                dist = math.hypot(dx, dy)
                min_d = b1["r"] + b2["r"]
                if dist < min_d and dist > 1e-4:
                    nx, ny = dx / dist, dy / dist
                    overlap = (min_d - dist) * 0.52
                    b1["x"] -= nx * overlap
                    b1["y"] -= ny * overlap
                    b2["x"] += nx * overlap
                    b2["y"] += ny * overlap

                    rvx = b2["vx"] - b1["vx"]
                    rvy = b2["vy"] - b1["vy"]
                    vel_along_normal = rvx * nx + rvy * ny
                    if vel_along_normal < 0:
                        imp = -vel_along_normal * 1.04
                        b1["vx"] -= imp * nx
                        b1["vy"] -= imp * ny
                        b2["vx"] += imp * nx
                        b2["vy"] += imp * ny
                        self.shake_timer = 0.14
                        mx, my = (b1["x"] + b2["x"]) * 0.5, (b1["y"] + b2["y"]) * 0.5
                        self._spawn_sparks(mx, my, (255, 255, 255), count=22)
                        self.events.append((t, "shatter", self.note_counter))
                        self.note_counter += 1

    def render_frame(self, t: float) -> bytes:
        sc = self.scale
        # Fade previous motion trail smoothly
        t_arr = np.array(self.trail_img, dtype=np.float32)
        t_arr[:, :, 3] *= 0.84
        self.trail_img = Image.fromarray(t_arr.astype(np.uint8), mode="RGBA")
        tdraw = ImageDraw.Draw(self.trail_img)

        for b in self.balls:
            r_tr = b["r"] * 0.82
            tdraw.ellipse(
                [b["x"] - r_tr, b["y"] - r_tr, b["x"] + r_tr, b["y"] + r_tr],
                fill=(*b["trail_col"], 185)
            )

        frame = Image.alpha_composite(self.base_bg, self.trail_img)
        draw = ImageDraw.Draw(frame)

        # Draw Arena / Rotating Rings
        if self.mode == "ring_escape":
            for ring in self.rings:
                if not ring["alive"]:
                    continue
                rr = ring["r"]
                bbox = [self.cx - rr, self.cy - rr, self.cx + rr, self.cy + rr]
                g_deg = math.degrees(ring["gap_angle"])
                h_deg = math.degrees(ring["gap_half"])
                start_deg = g_deg + h_deg
                end_deg = g_deg + 360.0 - h_deg
                # Outer soft glow + crisp neon arc
                draw.arc(bbox, start=start_deg, end=end_deg, fill=(*ring["color"], 90), width=max(8, int(13 * sc)))
                draw.arc(bbox, start=start_deg, end=end_deg, fill=(*ring["color"], 255), width=max(4, int(6 * sc)))
        else:
            rr = self.arena_r
            bbox = [self.cx - rr, self.cy - rr, self.cx + rr, self.cy + rr]
            ring_col = (250, 204, 21) if self.mode == "growing_chaos" else (148, 163, 184)
            draw.ellipse(bbox, outline=(*ring_col, 70), width=max(8, int(14 * sc)))
            draw.ellipse(bbox, outline=(*ring_col, 240), width=max(3, int(5 * sc)))

        # Draw Collision Sparks
        for px, py, _, _, life, pcol in self.particles:
            pr = max(1.5, 4.2 * sc * life)
            al = int(min(255, max(0, life * 255)))
            draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=(*pcol, al))

        # Draw Glowing Balls
        for b in self.balls:
            br = b["r"]
            gr = br * 1.45
            draw.ellipse([b["x"] - gr, b["y"] - gr, b["x"] + gr, b["y"] + gr], fill=(*b["trail_col"], 95))
            draw.ellipse([b["x"] - br, b["y"] - br, b["x"] + br, b["y"] + br], fill=(*b["color"], 255), outline=(255, 255, 255, 255), width=2)

        # Minimalist HUD Counter (Hooks viewer curiosity without bulky captions!)
        font_hud = get_font(max(22, int(26 * sc)))
        if self.mode == "ring_escape":
            rem = sum(1 for r in self.rings if r["alive"])
            hud_txt = f"RINGS LEFT: {rem} / 5" if rem > 0 else "ESCAPED!"
            hud_col = (56, 189, 248) if rem > 0 else (52, 211, 153)
        elif self.mode == "growing_chaos":
            pct = min(100, int((self.balls[0]["r"] / (self.arena_r * 0.86)) * 100))
            hud_txt = f"SIZE: {pct}%"
            hud_col = (250, 204, 21)
        else:
            hud_txt = f"CYAN {self.balls[0]['score']}  •  RED {self.balls[1]['score']}"
            hud_col = (248, 250, 252)

        bbox_t = draw.textbbox((0, 0), hud_txt, font=font_hud)
        tw = bbox_t[2] - bbox_t[0]
        draw.text(((self.w - tw) // 2, int(self.h * 0.14)), hud_txt, font=font_hud, fill=(*hud_col, 235))

        # Apply Impact Screen Shake on ring shatter / heavy collision
        if self.shake_timer > 0:
            sx = int(math.sin(t * 95.0) * 10 * sc)
            sy = int(math.cos(t * 85.0) * 10 * sc)
            frame = frame.transform(frame.size, Image.Transform.AFFINE, (1, 0, sx, 0, 1, sy))

        return frame.convert("RGB").tobytes()


# ===========================================================================
# 3. RENDER PIPELINE & API ROUTES
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

def run_physics_job(job_id: str, ratio: str, mode: str, duration_sec: float):
    job = JOBS[job_id]
    try:
        out_w, out_h = (540, 960) if ratio == "9:16" else (960, 540)
        fps = 30
        total_frames = int(duration_sec * fps)
        dt = 1.0 / float(fps)

        sim = PhysicsArcadeSimulator(out_w, out_h, mode, duration_sec, fps=fps)

        out_name = f"NEXORA_PHYSICS_{mode.upper()}_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        raw_vid_path = os.path.join(OUTPUT_DIR, f"{job_id}_silent.mp4")
        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_melody.wav")

        # 1. Render 30 FPS Physics Frames directly into FFmpeg pipe
        ffmpeg_vid_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "19",
            "-pix_fmt", "yuv420p", raw_vid_path
        ]
        proc = subprocess.Popen(ffmpeg_vid_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        for f in range(total_frames):
            sim.step_physics(global_t, dt)
            raw_bytes = sim.render_frame(global_t)
            proc.stdin.write(raw_bytes)
            global_t += dt

            if f % 30 == 0:
                job["progress"] = 10 + int((f / max(1, total_frames)) * 75)
                job["message"] = f"Simulating 30 FPS Rigid-Body Physics ({f}/{total_frames})..."

        proc.stdin.close()
        proc.wait()

        # 2. Synthesize exact Musical Notes for every recorded collision timestamp!
        job["progress"] = 88
        job["message"] = "Synthesizing Musical Collision Notes & Sub-Bass..."
        build_collision_audio_wav(duration_sec, sim.events, wav_path)

        # 3. Mux Video + Synchronized Musical Audio
        job["progress"] = 94
        job["message"] = "Mastering Final MP4..."
        subprocess.run([
            FFMPEG_BIN, "-y",
            "-i", raw_vid_path,
            "-i", wav_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-shortest", out_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(raw_vid_path):
            os.remove(raw_vid_path)

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Ready! Viral Physics Video Created ({out_name})"

    except Exception as exc:
        job["status"] = "error"
        job["message"] = f"Simulation Error: {exc}"

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
    mode = data.get("mode", "ring_escape")
    duration_sec = max(8.0, min(60.0, float(data.get("duration", "15"))))

    JOBS[job_id] = {"status": "running", "progress": 4, "message": "Initializing 30 FPS Physics Engine..."}
    threading.Thread(
        target=run_physics_job,
        args=(job_id, ratio, mode, duration_sec),
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
