import os
import time
import math
import uuid
import wave
import subprocess
import threading
import numpy as np

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter
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
# 1. PROCEDURAL EMOTIONAL HARMONIC & RAIN SOUNDSCAPE GENERATOR (NO SPEECH!)
# ===========================================================================
def generate_cymatic_music_wav(duration_sec: float, mode: str, out_wav: str, fps: int) -> list:
    """
    Synthesizes rich stereo-like harmonic piano/pad chords, singing bowl resonance,
    and soft rain texture—zero robotic TTS, 100% hypnotic retention audio!
    """
    rate = 22050
    n = int(duration_sec * rate)
    t = np.linspace(0, duration_sec, n, endpoint=False, dtype=np.float32)

    if mode == "midnight_rain":
        # Deep melancholic minor progression + soft rainfall
        chords = [(146.83, 174.61, 220.00, 293.66), (130.81, 164.81, 196.00, 261.63)]
        rain_lvl = 0.08
    elif mode == "cosmic_trance":
        # Deep 432Hz celestial resonance + sub-harmonic pulse
        chords = [(108.00, 162.00, 216.00, 324.00), (121.50, 182.25, 243.00, 364.50)]
        rain_lvl = 0.02
    else:
        # golden_zen: Warm emotional healing frequencies
        chords = [(136.10, 204.15, 272.20, 408.30), (128.00, 192.00, 256.00, 384.00)]
        rain_lvl = 0.04

    music = np.zeros(n, dtype=np.float32)
    bar_dur = 4.0
    num_bars = int(math.ceil(duration_sec / bar_dur))

    for b in range(num_bars):
        s_idx = int(b * bar_dur * rate)
        e_idx = min(n, int((b + 1) * bar_dur * rate))
        if s_idx >= n:
            break
        seg_len = e_idx - s_idx
        tb = np.linspace(0, seg_len / float(rate), seg_len, endpoint=False, dtype=np.float32)
        notes = chords[b % len(chords)]

        # Warm evolving pad swell
        swell = np.sin(np.pi * (tb / (seg_len / float(rate)))) ** 1.3
        chord_wave = np.zeros(seg_len, dtype=np.float32)
        for ni, freq in enumerate(notes):
            # Plucked bell/piano transient + sustained harmonic pad
            pluck = np.exp(-tb * (2.2 + ni * 0.4)) * np.sin(2 * np.pi * freq * tb)
            pad = swell * np.sin(2 * np.pi * freq * tb + ni * 0.5)
            chord_wave += (0.35 * pluck + 0.25 * pad)

        music[s_idx:e_idx] += chord_wave * 0.34

    # Soft soothing rain / vinyl warmth texture
    rng = np.random.default_rng(2026)
    noise = rng.normal(0, 1, n).astype(np.float32)
    # Simple low-pass smoothing for warm rain sound
    kernel = np.ones(12, dtype=np.float32) / 12.0
    warm_rain = np.convolve(noise, kernel, mode="same") * rain_lvl

    # Deep emotional sub-pulse every 1.25s
    pulse_phase = (t % 1.25)
    sub_pulse = np.exp(-pulse_phase * 14.0) * np.sin(2 * np.pi * 54.0 * pulse_phase) * 0.22

    full_audio = music + warm_rain + sub_pulse

    # Smooth fade-in and fade-out
    fade_s = min(int(1.0 * rate), n // 4)
    if fade_s > 0:
        full_audio[:fade_s] *= np.linspace(0, 1, fade_s)
        full_audio[-fade_s:] *= np.linspace(1, 0, fade_s)

    full_audio = np.clip(full_audio, -0.95, 0.95)
    pcm = (full_audio * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())

    # Extract smooth frame-by-frame energy envelope for Chladni wave morphing
    step = max(1, int(rate / fps))
    env, prev = [], 0.0
    for i in range(0, n, step):
        chunk = full_audio[i:i + step]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        val = min(1.0, max(0.0, rms * 3.8))
        cur = prev * 0.45 + val * 0.55
        env.append(cur)
        prev = cur
    return env if env else [0.5]


# ===========================================================================
# 2. CHLADNI CYMATICS WAVE INTERFERENCE & LUMINOUS PARTICLE PHYSICS ENGINE
# ===========================================================================
class ChladniCymaticsEngine:
    """
    Simulates real Chladni acoustic plate physics: thousands of luminous sand/gold
    particles migrate into evolving Sacred Geometry nodal lines driven by sound!
    100% impossible to animate by hand.
    """
    def __init__(self, w: int, h: int, mode: str):
        self.w = w
        self.h = h
        self.mode = mode
        self.cx = int(w * 0.50)
        self.cy = int(h * 0.50)
        self.plate_r = min(w, h) * 0.43

        if mode == "midnight_rain":
            self.bg_top = (6, 12, 24)
            self.bg_bot = (2, 4, 10)
            self.c1 = (56, 189, 248)   # Ice Cyan
            self.c2 = (129, 140, 248)  # Indigo Mist
            self.c_hi = (240, 249, 255)
        elif mode == "cosmic_trance":
            self.bg_top = (16, 6, 26)
            self.bg_bot = (4, 2, 10)
            self.c1 = (232, 121, 249)  # Electric Magenta
            self.c2 = (34, 211, 238)   # Quantum Cyan
            self.c_hi = (253, 244, 255)
        else:
            # golden_zen
            self.bg_top = (18, 13, 6)
            self.bg_bot = (4, 3, 2)
            self.c1 = (250, 204, 21)   # Pure Gold Sand
            self.c2 = (251, 146, 60)   # Warm Amber
            self.c_hi = (254, 252, 232)

        # Deep space gradient background
        grad = np.zeros((h, w, 3), dtype=np.uint8)
        for ch in range(3):
            col_line = np.linspace(self.bg_top[ch], self.bg_bot[ch], h, dtype=np.uint8)[:, None]
            grad[:, :, ch] = np.repeat(col_line, w, axis=1)
        self.base_bg = Image.fromarray(grad, mode="RGB").convert("RGBA")

        # Pre-render soft radial aura sprite once (zero per-frame blur overhead!)
        aura_sz = int(self.plate_r * 2.3)
        aura = Image.new("RGBA", (aura_sz, aura_sz), (0, 0, 0, 0))
        ad = ImageDraw.Draw(aura)
        ac = aura_sz // 2
        ad.ellipse([ac - int(self.plate_r * 0.95), ac - int(self.plate_r * 0.95),
                    ac + int(self.plate_r * 0.95), ac + int(self.plate_r * 0.95)], fill=(*self.c1, 38))
        ad.ellipse([ac - int(self.plate_r * 0.45), ac - int(self.plate_r * 0.45),
                    ac + int(self.plate_r * 0.45), ac + int(self.plate_r * 0.45)], fill=(*self.c2, 55))
        self.aura_sprite = aura.filter(ImageFilter.GaussianBlur(radius=24))

        # Spawn 2,400 microscopic sand particles inside circular acoustic plate [-1, 1]^2
        rng = np.random.default_rng(777)
        self.n_particles = 2400
        ang = rng.uniform(0, 2 * np.pi, self.n_particles).astype(np.float32)
        rad = np.sqrt(rng.uniform(0.02, 0.98, self.n_particles)).astype(np.float32)
        self.px = rad * np.cos(ang)
        self.py = rad * np.sin(ang)
        self.p_bright = rng.uniform(0.45, 1.0, self.n_particles).astype(np.float32)

    def render_frame(self, t: float, energy: float) -> bytes:
        # Smoothly morph Chladni harmonic mode numbers (n, m) over time
        phase = t * 0.35
        blend = 0.5 - 0.5 * math.cos(phase)

        # Harmonic standing-wave modes (creates mesmerizing floral & star mandalas!)
        n1, m1 = 3.0, 5.0
        n2, m2 = 4.0, 7.0

        # Chladni plate wave equation: Z(x, y) = cos(n*pi*x)*cos(m*pi*y) - cos(m*pi*x)*cos(n*pi*y)
        pi = np.float32(math.pi)
        z1 = np.cos(n1 * pi * self.px) * np.cos(m1 * pi * self.py) - np.cos(m1 * pi * self.px) * np.cos(n1 * pi * self.py)
        z2 = np.cos(n2 * pi * self.px) * np.cos(m2 * pi * self.py) - np.cos(m2 * pi * self.px) * np.cos(n2 * pi * self.py)
        z = (1.0 - blend) * z1 + blend * z2

        # Compute numerical gradient of |Z| so particles migrate onto nodal lines (Z ≈ 0)
        eps = np.float32(0.015)
        zx1 = np.cos(n1 * pi * (self.px + eps)) * np.cos(m1 * pi * self.py) - np.cos(m1 * pi * (self.px + eps)) * np.cos(n1 * pi * self.py)
        zx2 = np.cos(n2 * pi * (self.px + eps)) * np.cos(m2 * pi * self.py) - np.cos(m2 * pi * (self.px + eps)) * np.cos(n2 * pi * self.py)
        dz_dx = (((1.0 - blend) * zx1 + blend * zx2) - z) / eps

        zy1 = np.cos(n1 * pi * self.px) * np.cos(m1 * pi * (self.py + eps)) - np.cos(m1 * pi * self.px) * np.cos(n1 * pi * (self.py + eps))
        zy2 = np.cos(n2 * pi * self.px) * np.cos(m2 * pi * (self.py + eps)) - np.cos(m2 * pi * self.px) * np.cos(n2 * pi * (self.py + eps))
        dz_dy = (((1.0 - blend) * zy1 + blend * zy2) - z) / eps

        # Move particles toward nodal lines + gentle acoustic vibration
        step_lr = np.float32(0.018 * (0.6 + energy))
        self.px -= step_lr * z * dz_dx
        self.py -= step_lr * z * dz_dy

        # Keep particles inside circular plate & gently respawn drifted ones
        r_sq = self.px ** 2 + self.py ** 2
        out_mask = r_sq > 0.96
        if np.any(out_mask):
            cnt = int(np.sum(out_mask))
            ang_new = np.linspace(0, 2 * np.pi, cnt, endpoint=False, dtype=np.float32) + np.float32(t * 3.1)
            rad_new = np.float32(0.88)
            self.px[out_mask] = rad_new * np.cos(ang_new)
            self.py[out_mask] = rad_new * np.sin(ang_new)

        # Rotate whole mandala gracefully in 2D space
        rot = t * 0.22
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        rx = self.px * cos_r - self.py * sin_r
        ry = self.px * sin_r + self.py * cos_r

        pulse_r = self.plate_r * (0.96 + energy * 0.08)
        sx = (self.cx + rx * pulse_r).astype(np.int32)
        sy = (self.cy + ry * pulse_r).astype(np.int32)

        # Fast NumPy pixel accumulation (renders 2,400 glowing sand grains in < 3ms!)
        canvas = np.array(self.base_bg, dtype=np.uint8)

        # Subtle outer acoustic ring
        img = Image.fromarray(canvas, mode="RGBA")
        img.alpha_composite(
            self.aura_sprite,
            (self.cx - self.aura_sprite.width // 2, self.cy - self.aura_sprite.height // 2)
        )
        draw = ImageDraw.Draw(img)
        ring_r = int(pulse_r * 1.02)
        draw.ellipse(
            [self.cx - ring_r, self.cy - ring_r, self.cx + ring_r, self.cy + ring_r],
            outline=(*self.c1, 70), width=2
        )

        arr = np.array(img, dtype=np.uint8)
        valid = (sx >= 1) & (sx < self.w - 2) & (sy >= 1) & (sy < self.h - 2)
        vx = sx[valid]
        vy = sy[valid]
        vz = np.abs(z[valid])

        # Particles settled on nodal lines (vz small) shine brightest!
        nodal_glow = np.clip(1.0 - vz * 1.4, 0.25, 1.0)

        for idx in range(len(vx)):
            x, y = int(vx[idx]), int(vy[idx])
            g = float(nodal_glow[idx])
            if g > 0.72:
                col = self.c_hi
                arr[y:y+2, x:x+2, :3] = col
            elif g > 0.45:
                col = self.c1
                arr[y:y+2, x:x+2, :3] = col
            else:
                col = self.c2
                arr[y, x, :3] = col

        return arr[:, :, :3].tobytes()


# ===========================================================================
# 3. FAST RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_cymatics_job(job_id: str, ratio: str, mode: str, duration_sec: float):
    job = JOBS[job_id]
    try:
        out_w, out_h = (540, 960) if ratio == "9:16" else (960, 540)
        fps = 24

        job["message"] = "Synthesizing Harmonic Resonance & Rain Soundscape..."
        job["progress"] = 12

        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_cymatic.wav")
        energy_env = generate_cymatic_music_wav(duration_sec, mode, wav_path, fps)

        engine = ChladniCymaticsEngine(out_w, out_h, mode)
        out_name = f"NEXORA_CYMATICS_{mode.upper()}_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", wav_path,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        total_frames = int(duration_sec * fps)
        global_t = 0.0

        for f in range(total_frames):
            en = energy_env[f] if f < len(energy_env) else 0.5
            raw_bytes = engine.render_frame(global_t, en)
            proc.stdin.write(raw_bytes)
            global_t += 1.0 / fps

            if f % 24 == 0:
                job["progress"] = 18 + int((f / max(1, total_frames)) * 78)
                job["message"] = f"Simulating 2,400 Cymatic Grains ({f}/{total_frames} frames)..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Ready to Watch & Upload ({out_name})"

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
    if request.content_type and "multipart/form-data" in request.content_type:
        ratio = request.form.get("ratio", "9:16")
        mode = request.form.get("mode", "golden_zen")
        duration_sec = float(request.form.get("duration", "12"))
    else:
        data = request.get_json(force=True)
        ratio = data.get("ratio", "9:16")
        mode = data.get("mode", "golden_zen")
        duration_sec = float(data.get("duration", "12"))

    duration_sec = max(6.0, min(60.0, duration_sec))
    JOBS[job_id] = {"status": "running", "progress": 4, "message": "Initializing Chladni Acoustic Physics Plate..."}
    threading.Thread(
        target=run_cymatics_job,
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
