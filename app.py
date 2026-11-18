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
# 1. PROCEDURAL 3D MESH LIBRARY (TRUE 3D VERTICES, EDGES & SHADED FACES)
# ===========================================================================
def build_3d_model(model_type: str):
    """Returns (name, vertices_Nx3, edges_list, faces_list_of_tuples, accent_color)"""
    verts = []
    edges = []
    faces = []

    if model_type == "jet":
        name = "STEALTH CYBER FIGHTER JET"
        accent = (34, 211, 238)
        # Sleek delta-wing fighter jet geometry
        verts = [
            (0.0, 0.0, 1.55),     # 0: Nose tip
            (0.0, 0.22, 0.45),    # 1: Canopy top
            (-0.24, 0.0, 0.35),   # 2: Left fuselage mid
            (0.24, 0.0, 0.35),    # 3: Right fuselage mid
            (0.0, -0.14, 0.35),   # 4: Belly mid
            (-1.35, -0.05, -0.65),# 5: Left wingtip
            (1.35, -0.05, -0.65), # 6: Right wingtip
            (-0.32, 0.05, -0.95), # 7: Left engine exhaust
            (0.32, 0.05, -0.95),  # 8: Right engine exhaust
            (0.0, -0.15, -0.95),  # 9: Rear belly
            (-0.45, 0.55, -0.88), # 10: Left twin tail fin
            (0.45, 0.55, -0.88),  # 11: Right twin tail fin
        ]
        faces = [
            (0, 1, 2), (0, 3, 1), (0, 2, 4), (0, 4, 3),
            (1, 7, 2), (1, 3, 8), (2, 5, 7), (3, 8, 6),
            (2, 4, 5), (3, 6, 4), (4, 9, 5), (4, 6, 9),
            (2, 7, 10), (3, 11, 8), (1, 8, 7), (4, 7, 9), (4, 9, 8)
        ]

    elif model_type == "saturn":
        name = "RINGED EXOPLANET SATURN"
        accent = (250, 204, 21)
        # UV Sphere + Tilted Planetary Rings
        stacks, slices = 10, 16
        for i in range(stacks + 1):
            phi = math.pi * i / stacks
            y = 0.62 * math.cos(phi)
            r = 0.62 * math.sin(phi)
            for j in range(slices):
                theta = 2.0 * math.pi * j / slices
                verts.append((r * math.cos(theta), y, r * math.sin(theta)))

        for i in range(stacks):
            for j in range(slices):
                jn = (j + 1) % slices
                a = i * slices + j
                b = a + 1 if j + 1 < slices else i * slices
                c = (i + 1) * slices + j
                d = (i + 1) * slices + jn
                faces.append((a, c, d))
                faces.append((a, d, b))

        # Add outer planetary ring quad strips
        base_idx = len(verts)
        r_in, r_out = 0.85, 1.42
        ring_segs = 24
        for j in range(ring_segs):
            ang = 2.0 * math.pi * j / ring_segs
            verts.append((r_in * math.cos(ang), 0.02, r_in * math.sin(ang)))
            verts.append((r_out * math.cos(ang), -0.02, r_out * math.sin(ang)))
        for j in range(ring_segs):
            jn = (j + 1) % ring_segs
            i0 = base_idx + j * 2
            i1 = i0 + 1
            i2 = base_idx + jn * 2
            i3 = i2 + 1
            faces.append((i0, i1, i3))
            faces.append((i0, i3, i2))

    elif model_type == "dna":
        name = "QUANTUM DNA DOUBLE HELIX"
        accent = (16, 185, 129)
        steps = 22
        for i in range(steps):
            t = (i / float(steps - 1)) * 2.0 - 1.0
            y = t * 1.15
            ang = t * math.pi * 2.2
            r = 0.52
            x1, z1 = r * math.cos(ang), r * math.sin(ang)
            x2, z2 = -x1, -z1
            verts.append((x1 - 0.08, y, z1))
            verts.append((x1 + 0.08, y, z1))
            verts.append((x2 - 0.08, y, z2))
            verts.append((x2 + 0.08, y, z2))
            if i > 0:
                p = (i - 1) * 4
                c = i * 4
                faces.append((p, c, c + 1))
                faces.append((p, c + 1, p + 1))
                faces.append((p + 2, c + 2, c + 3))
                faces.append((p + 2, c + 3, p + 3))
            if i % 2 == 0:
                c = i * 4
                faces.append((c, c + 2, c + 3))
                faces.append((c, c + 3, c + 1))

    else:
        name = "HYPER-CORE OCTAHEDRON REACTOR"
        accent = (244, 63, 94)
        verts = [
            (0.0, 1.25, 0.0),    # 0 Top
            (0.0, -1.25, 0.0),   # 1 Bottom
            (1.10, 0.0, 0.0),    # 2 Right
            (-1.10, 0.0, 0.0),   # 3 Left
            (0.0, 0.0, 1.10),    # 4 Front
            (0.0, 0.0, -1.10),   # 5 Back
            (0.55, 0.55, 0.55),  # 6 Inner cube corners
            (-0.55, 0.55, 0.55),
            (0.55, -0.55, 0.55),
            (-0.55, -0.55, 0.55),
            (0.55, 0.55, -0.55),
            (-0.55, 0.55, -0.55),
            (0.55, -0.55, -0.55),
            (-0.55, -0.55, -0.55)
        ]
        faces = [
            (0, 4, 2), (0, 2, 5), (0, 5, 3), (0, 3, 4),
            (1, 2, 4), (1, 5, 2), (1, 3, 5), (1, 4, 3),
            (6, 7, 9), (6, 9, 8), (10, 12, 13), (10, 13, 11),
            (6, 10, 11), (6, 11, 7), (8, 9, 13), (8, 13, 12)
        ]

    edge_set = set()
    for a, b, c in faces:
        edge_set.add(tuple(sorted((a, b))))
        edge_set.add(tuple(sorted((b, c))))
        edge_set.add(tuple(sorted((c, a))))

    return name, np.array(verts, dtype=np.float32), list(edge_set), faces, accent


# ===========================================================================
# 2. SCI-FI LASER SCANNING, HUM & MECHANICAL LOCK AUDIO ENGINE
# ===========================================================================
def build_hologram_audio_wav(stages: list, total_dur: float, out_wav: str):
    """Synthesizes deep sci-fi reactor hum, rising laser scan frequency, and reality lock boom."""
    rate = 22050
    n = int(total_dur * rate)
    t = np.linspace(0, total_dur, n, endpoint=False, dtype=np.float32)

    # Futuristic cyber pulse bass + harmonic shimmer
    bpm = 118.0
    beat_dur = 60.0 / bpm
    b_pos = (t % beat_dur) / beat_dur
    pulse = np.sin(2.0 * np.pi * 55.0 * t) * (0.18 + 0.14 * np.exp(-b_pos * 6.0))
    shimmer = np.sin(2.0 * np.pi * 220.0 * t + 1.5 * np.sin(2.0 * np.pi * 3.0 * t)) * 0.06
    audio = pulse + shimmer

    for t_start, t_scan_start, t_lock, t_end in stages:
        # Rising laser scan frequency sweep during materialization
        s0 = int(t_scan_start * rate)
        s1 = min(n, int(t_lock * rate))
        if s0 < s1:
            ts = np.linspace(0, (s1 - s0) / float(rate), s1 - s0, endpoint=False, dtype=np.float32)
            prog = ts / max(0.01, (s1 - s0) / float(rate))
            freq = 180.0 + 620.0 * (prog ** 1.4)
            laser = np.sin(2.0 * np.pi * freq * ts) * (0.14 * np.sin(np.pi * prog))
            audio[s0:s1] += laser

        # Heavy sci-fi sub-bass lock + bright crystalline chord at 100% Reality Lock!
        sl = int(t_lock * rate)
        l_len = min(n - sl, int(0.95 * rate))
        if l_len > 0:
            tl = np.linspace(0, l_len / float(rate), l_len, endpoint=False, dtype=np.float32)
            boom = np.sin(2.0 * np.pi * (110.0 * np.exp(-tl * 7.0) + 42.0) * tl) * np.exp(-tl * 4.5) * 0.55
            chord = (
                0.35 * np.sin(2.0 * np.pi * 523.25 * tl) +
                0.30 * np.sin(2.0 * np.pi * 659.25 * tl) +
                0.25 * np.sin(2.0 * np.pi * 1046.50 * tl)
            ) * np.exp(-tl * 4.0) * 0.30
            audio[sl:sl + l_len] += (boom + chord)

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
# 3. 3D PROJECTION, LASER SCANNER & HOLOGRAM MORPH RENDERER
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

class Hologram3DRenderer:
    def __init__(self, w: int, h: int):
        self.w = w
        self.h = h
        self.is_vertical = (h > w)
        self.rng = random.Random(2026)
        self.sparks = [
            [self.rng.uniform(0, w), self.rng.uniform(0, h), self.rng.uniform(25, 65)]
            for _ in range(36)
        ]

    def render_frame(self, model_data: tuple, stage_prog: float, global_t: float) -> bytes:
        m_name, base_verts, edges, faces, accent = model_data
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0

        frame = Image.new("RGB", (self.w, self.h), (5, 8, 18))
        draw = ImageDraw.Draw(frame)

        # 1. Perspective 3D Hologram Floor Grid & Ambient Sparks
        horizon_y = int(self.h * 0.62)
        cx = self.w * 0.5
        cy = self.h * 0.47

        for i in range(-8, 9):
            x_top = cx + i * int(34 * sc)
            x_bot = cx + i * int(135 * sc)
            draw.line([(x_top, horizon_y), (x_bot, self.h)], fill=(18, 34, 66), width=1)

        for r_i in range(1, 8):
            frac = ((r_i / 7.0) + (global_t * 0.35) % (1.0 / 7.0))
            ry = horizon_y + int((self.h - horizon_y) * (frac ** 1.6))
            draw.line([(0, ry), (self.w, ry)], fill=(18, 34, 66), width=1)

        for sp in self.sparks:
            sx = (sp[0] + math.sin(global_t + sp[1]) * 14) % self.w
            sy = (sp[1] - global_t * sp[2]) % self.h
            draw.ellipse([sx - 2, sy - 2, sx + 2, sy + 2], fill=accent)

        # 2. Rotate 3D Vertices in Real-Time (Yaw + Gentle Pitch Tilt)
        yaw = global_t * 1.45
        pitch = 0.38 + 0.14 * math.sin(global_t * 0.9)

        cy_a, sy_a = math.cos(yaw), math.sin(yaw)
        cp_a, sp_a = math.cos(pitch), math.sin(pitch)

        rot_y = np.array([[cy_a, 0, sy_a], [0, 1, 0], [-sy_a, 0, cy_a]], dtype=np.float32)
        rot_x = np.array([[1, 0, 0], [0, cp_a, -sp_a], [0, sp_a, cp_a]], dtype=np.float32)
        v_rot = (base_verts @ rot_y.T) @ rot_x.T

        # Perspective projection to 2D screen coordinates
        fov = min(self.w, self.h) * (0.62 if self.is_vertical else 0.56)
        cam_z = 3.2
        z_depth = cam_z - v_rot[:, 2]
        px = cx + (v_rot[:, 0] / z_depth) * fov
        py = cy - (v_rot[:, 1] / z_depth) * fov

        min_py = float(np.min(py))
        max_py = float(np.max(py))
        span_py = max(10.0, max_py - min_py)

        # Morph threshold: 0.0 -> 0.25 pure wireframe; 0.25 -> 0.85 laser sweeps down; 0.85 -> 1.0 full reality
        if stage_prog < 0.25:
            scan_y = min_py - 20.0
            phase_label = "PHASE 1: WIREFRAME BLUEPRINT"
        elif stage_prog < 0.85:
            scan_frac = (stage_prog - 0.25) / 0.60
            scan_y = min_py + span_py * scan_frac
            phase_label = "PHASE 2: LASER MATERIALIZING..."
        else:
            scan_y = max_py + 40.0
            phase_label = "PHASE 3: 100% REALITY LOCKED!"

        # 3. Draw Solid Shaded 3D Faces ABOVE the Laser Scan Line (Painter's Algorithm Depth Sort)
        light_dir = np.array([0.4, 0.7, 0.6], dtype=np.float32)
        light_dir /= np.linalg.norm(light_dir)

        face_depths = []
        for idx_f, (a, b, c) in enumerate(faces):
            avg_z = (v_rot[a, 2] + v_rot[b, 2] + v_rot[c, 2]) / 3.0
            face_depths.append((avg_z, a, b, c))
        face_depths.sort(key=lambda x: x[0])

        for _, a, b, c in face_depths:
            f_cy = (py[a] + py[b] + py[c]) / 3.0
            if f_cy <= scan_y:
                # Compute 3D surface normal for metallic directional lighting
                e1 = v_rot[b] - v_rot[a]
                e2 = v_rot[c] - v_rot[a]
                norm = np.cross(e1, e2)
                n_len = np.linalg.norm(norm)
                if n_len > 1e-5:
                    norm /= n_len
                shade = 0.35 + 0.65 * abs(float(np.dot(norm, light_dir)))
                fill_col = (
                    min(255, int(accent[0] * shade)),
                    min(255, int(accent[1] * shade)),
                    min(255, int(accent[2] * shade))
                )
                outline_col = (
                    min(255, int(accent[0] * 0.9 + 40)),
                    min(255, int(accent[1] * 0.9 + 40)),
                    min(255, int(accent[2] * 0.9 + 40))
                )
                draw.polygon([(px[a], py[a]), (px[b], py[b]), (px[c], py[c])], fill=fill_col, outline=outline_col)

        # 4. Draw Glowing Cyan Blueprint Wireframe Edges BELOW the Laser Scan Line
        for a, b in edges:
            e_cy = (py[a] + py[b]) * 0.5
            if e_cy > scan_y:
                draw.line([(px[a], py[a]), (px[b], py[b])], fill=(34, 211, 238), width=2)

        # Draw Blueprint Vertex Nodes below scan line
        for i in range(len(px)):
            if py[i] > scan_y:
                draw.ellipse([px[i] - 2.5, py[i] - 2.5, px[i] + 2.5, py[i] + 2.5], fill=(255, 255, 255))

        # 5. Active Glowing Laser Scanner Beam during Materialization
        if 0.25 <= stage_prog < 0.85:
            lx0 = int(self.w * 0.14)
            lx1 = int(self.w * 0.86)
            draw.line([(lx0, scan_y), (lx1, scan_y)], fill=accent, width=max(4, int(5 * sc)))
            draw.line([(lx0, scan_y), (lx1, scan_y)], fill=(255, 255, 255), width=2)

        # 6. Futuristic Telemetry HUD & Build Progress Meter
        margin = int(self.w * 0.06)
        font_title = get_font(max(17, int(23 * sc)))
        font_sub = get_font(max(13, int(16 * sc)))

        draw.text((margin, int(self.h * 0.04)), m_name, font=font_title, fill=(248, 250, 252))
        draw.text((margin, int(self.h * 0.088)), phase_label, font=font_sub, fill=accent)

        # Bottom Progress Bar
        bar_y = int(self.h * 0.88)
        bar_h = max(14, int(18 * sc))
        bar_w = self.w - margin * 2
        pct = min(100, int(stage_prog * 100))

        draw.rounded_rectangle([margin, bar_y, margin + bar_w, bar_y + bar_h], radius=bar_h // 2, fill=(15, 23, 42), outline=(51, 65, 85), width=2)
        fw = int(bar_w * (pct / 100.0))
        if fw > 8:
            draw.rounded_rectangle([margin, bar_y, margin + fw, bar_y + bar_h], radius=bar_h // 2, fill=accent)

        pct_txt = f"REALITY CONSTRUCTION: {pct}%"
        pb = draw.textbbox((0, 0), pct_txt, font=font_sub)
        draw.text(((self.w - (pb[2] - pb[0])) // 2, bar_y - int(24 * sc)), pct_txt, font=font_sub, fill=(248, 250, 252))

        return frame.tobytes()


# ===========================================================================
# 4. RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_hologram_job(job_id: str, ratio: str, duration_sec: float):
    job = JOBS[job_id]
    try:
        out_w, out_h = (960, 540) if ratio == "16:9" else (540, 960)
        fps = 24
        dt = 1.0 / float(fps)

        model_keys = ["jet", "saturn", "dna", "core"]
        random.shuffle(model_keys)

        # Each 3D model morph takes 9 seconds (for 18s Short -> 2 models; for 90s -> 10 models)
        seg_dur = 9.0
        num_models = max(1, int(round(duration_sec / seg_dur)))
        actual_dur = num_models * seg_dur

        stages_audio = []
        for i in range(num_models):
            t0 = i * seg_dur
            stages_audio.append((t0, t0 + seg_dur * 0.25, t0 + seg_dur * 0.85, t0 + seg_dur))

        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_holo_sfx.wav")
        job["progress"] = 8
        job["message"] = "Synthesizing Sci-Fi Laser Scan & Reality Lock Audio..."
        build_hologram_audio_wav(stages_audio, actual_dur, wav_path)

        out_name = f"NEXORA_3D_HOLOGRAM_{int(actual_dur)}s_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
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

        renderer = Hologram3DRenderer(out_w, out_h)
        global_t = 0.0
        frames_per_seg = int(seg_dur * fps)

        for m_idx in range(num_models):
            m_data = build_3d_model(model_keys[m_idx % len(model_keys)])
            for f in range(frames_per_seg):
                s_prog = f / max(1, frames_per_seg - 1)
                raw_bytes = renderer.render_frame(m_data, s_prog, global_t)
                proc.stdin.write(raw_bytes)
                global_t += dt

            job["progress"] = 12 + int(((m_idx + 1) / num_models) * 85)
            job["message"] = f"Rendering 3D Hologram Morph {m_idx + 1}/{num_models}..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! 3D Hologram Blueprint Video Ready ({out_name})"

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
    duration_sec = max(9.0, min(600.0, float(data.get("duration", "18"))))

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing 3D Wireframe Projection Engine..."}
    threading.Thread(
        target=run_hologram_job,
        args=(job_id, ratio, duration_sec),
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
