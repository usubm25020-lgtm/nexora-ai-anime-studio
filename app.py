import os
import time
import math
import uuid
import wave
import subprocess
import threading
import numpy as np

import imageio_ffmpeg
from PIL import Image
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
# 1. HYPNOTIC BASS-DROP & PHONK PULSE AUDIO GENERATOR (KEEPS VIEWERS HOOKED)
# ===========================================================================
def generate_hypnotic_bass_wav(duration_sec: float, style: str, out_wav: str, fps: int) -> list:
    rate = 22050
    n = int(duration_sec * rate)
    t = np.linspace(0, duration_sec, n, endpoint=False, dtype=np.float32)

    bpm = 128.0 if style == "cyber_vortex" else 110.0
    beat_dur = 60.0 / bpm
    beat_pos = (t % beat_dur) / beat_dur

    # Punchy 808 Sub-Bass Kick on every beat
    kick_freq = 48.0 + 110.0 * np.exp(-beat_pos * 18.0)
    kick = np.sin(2.0 * np.pi * kick_freq * (t % beat_dur)) * np.exp(-beat_pos * 7.5) * 0.65

    # Dark atmospheric synth lead
    if style == "golden_fractal":
        notes = [110.0, 130.81, 146.83, 164.81]
    elif style == "illusion_bw":
        notes = [98.0, 116.54, 130.81, 146.83]
    else:
        notes = [82.41, 98.00, 110.00, 123.47]

    bar_idx = (t / (beat_dur * 2.0)).astype(np.int32)
    freq_arr = np.array([notes[i % len(notes)] for i in bar_idx], dtype=np.float32)
    synth = (
        0.22 * np.sin(2.0 * np.pi * freq_arr * t) +
        0.12 * np.sin(2.0 * np.pi * freq_arr * 2.0 * t + np.sin(t * 3.0))
    )

    # High-hat crisp tick on off-beats
    rng = np.random.default_rng(99)
    noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
    hat_env = np.exp(-((t + beat_dur * 0.5) % beat_dur) * 45.0)
    hihat = noise * hat_env * 0.16

    audio = np.clip(kick + synth + hihat, -0.95, 0.95)
    fade_s = min(int(0.4 * rate), n // 4)
    if fade_s > 0:
        audio[:fade_s] *= np.linspace(0, 1, fade_s)
        audio[-fade_s:] *= np.linspace(1, 0, fade_s)

    pcm = (audio * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())

    step = max(1, int(rate / fps))
    env = []
    for i in range(0, n, step):
        chunk = kick[i:i + step]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        env.append(min(1.0, rms * 2.8))
    return env if env else [0.5]


# ===========================================================================
# 2. FULL-SCREEN 3D INFINITE ZOOM TUNNEL & FRACTAL ILLUSION ENGINE
# ===========================================================================
class InfiniteVortexEngine:
    """
    Renders full-screen 3D ray-warped infinite zoom tunnels and optical illusions
    using vectorized polar coordinate shaders. 100% impossible to draw manually!
    """
    def __init__(self, w: int, h: int, style: str):
        self.w = w
        self.h = h
        self.style = style

        # Normalized coordinate grid [-1, 1] with aspect ratio correction
        aspect = w / float(h)
        y, x = np.ogrid[-1.0:1.0:h*1j, -aspect:aspect:w*1j]
        x = x.astype(np.float32)
        y = y.astype(np.float32)

        self.r = np.sqrt(x * x + y * y) + np.float32(1e-4)
        self.theta = np.arctan2(y, x)
        # Inverse depth (1/r) creates true 3D infinite perspective tunnel!
        self.inv_r = np.float32(1.0) / (self.r + np.float32(0.06))
        # Soft center glow & edge vignette masks
        self.center_glow = np.exp(-self.r * np.float32(3.5))
        self.vignette = np.clip(np.float32(1.18) - self.r * np.float32(0.32), 0.35, 1.0)

    def render_frame(self, t: float, beat_energy: float) -> bytes:
        t32 = np.float32(t)
        pulse = np.float32(1.0 + beat_energy * 0.25)

        if self.style == "illusion_bw":
            # Mind-bending High-Contrast Optical Hypnosis Tunnel
            twist = self.theta + self.inv_r * np.float32(0.45) * np.sin(t32 * np.float32(0.7)) + t32 * np.float32(0.9)
            depth = self.inv_r * np.float32(2.6) - t32 * np.float32(4.5)
            pattern = np.sin(twist * np.float32(8.0)) * np.cos(depth * np.float32(3.2))
            val = np.clip((pattern * np.float32(2.2) + np.float32(0.5)) * self.vignette, 0.0, 1.0)
            c = (val * np.float32(255.0)).astype(np.uint8)
            rgb = np.stack([c, c, c], axis=2)
            return rgb.tobytes()

        elif self.style == "golden_fractal":
            # Infinite Sacred Golden Kaleidoscope Zoom
            twist = self.theta * np.float32(6.0) + np.sin(self.inv_r * np.float32(1.4) - t32 * np.float32(2.2)) * np.float32(1.8)
            depth = self.inv_r * np.float32(2.2) - t32 * np.float32(3.6)
            w1 = np.sin(twist + depth * np.float32(2.5))
            w2 = np.cos(self.theta * np.float32(12.0) - depth * np.float32(3.0))
            energy = np.abs(w1 + w2) * np.float32(0.5) * pulse
            energy = np.power(np.clip(energy, 0.0, 1.0), np.float32(1.3))

            r_ch = np.clip((energy * np.float32(255.0) + self.center_glow * np.float32(180.0)) * self.vignette, 0, 255).astype(np.uint8)
            g_ch = np.clip((energy * np.float32(195.0) + self.center_glow * np.float32(140.0)) * self.vignette, 0, 255).astype(np.uint8)
            b_ch = np.clip((np.power(energy, np.float32(2.4)) * np.float32(110.0) + self.center_glow * np.float32(90.0)) * self.vignette, 0, 255).astype(np.uint8)
            return np.stack([r_ch, g_ch, b_ch], axis=2).tobytes()

        else:
            # cyber_vortex: Neon Cyan & Magenta Warp-Speed Wormhole
            swirl = self.theta + self.inv_r * np.float32(0.55) * np.cos(t32 * np.float32(0.65)) - t32 * np.float32(0.8)
            depth = self.inv_r * np.float32(2.4) - t32 * np.float32(4.8)

            grid_a = np.abs(np.sin(swirl * np.float32(6.0)))
            grid_b = np.abs(np.sin(depth * np.float32(2.8)))
            # Crisp glowing neon plasma lines inside 3D wormhole
            neon_lines = np.exp(-np.minimum(grid_a, grid_b) * np.float32(6.5)) * pulse
            plasma = (np.float32(0.5) + np.float32(0.5) * np.sin(depth + swirl * np.float32(2.0)))

            r_ch = np.clip((neon_lines * plasma * np.float32(245.0) + self.center_glow * np.float32(140.0)) * self.vignette, 0, 255).astype(np.uint8)
            g_ch = np.clip((neon_lines * (np.float32(1.0) - plasma * np.float32(0.4)) * np.float32(225.0) + self.center_glow * np.float32(210.0)) * self.vignette, 0, 255).astype(np.uint8)
            b_ch = np.clip((neon_lines * np.float32(255.0) + np.float32(28.0) + self.center_glow * np.float32(255.0)) * self.vignette, 0, 255).astype(np.uint8)
            return np.stack([r_ch, g_ch, b_ch], axis=2).tobytes()


# ===========================================================================
# 3. FAST RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_vortex_job(job_id: str, ratio: str, style: str, duration_sec: float):
    job = JOBS[job_id]
    try:
        out_w, out_h = (540, 960) if ratio == "9:16" else (960, 540)
        fps = 24

        job["message"] = "Synthesizing Hypnotic Bass & Warp Audio..."
        job["progress"] = 12

        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_vortex.wav")
        beat_env = generate_hypnotic_bass_wav(duration_sec, style, wav_path, fps)

        engine = InfiniteVortexEngine(out_w, out_h, style)
        out_name = f"NEXORA_INFINITE_ZOOM_{style.upper()}_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
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

        total_frames = int(duration_sec * fps)
        global_t = 0.0

        for f in range(total_frames):
            en = beat_env[f] if f < len(beat_env) else 0.5
            raw_bytes = engine.render_frame(global_t, en)
            proc.stdin.write(raw_bytes)
            global_t += 1.0 / fps

            if f % 24 == 0:
                job["progress"] = 18 + int((f / max(1, total_frames)) * 78)
                job["message"] = f"Warping 3D Infinite Tunnel ({f}/{total_frames} frames)..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Ready to Upload ({out_name})"

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
    style = data.get("style", "cyber_vortex")
    duration_sec = max(6.0, min(60.0, float(data.get("duration", "12"))))

    JOBS[job_id] = {"status": "running", "progress": 4, "message": "Initializing 3D Infinite Zoom Shader..."}
    threading.Thread(
        target=run_vortex_job,
        args=(job_id, ratio, style, duration_sec),
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
