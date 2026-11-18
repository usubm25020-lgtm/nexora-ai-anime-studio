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
# 1. CLASSIFIED CCTV SCENARIOS & ANOMALY LOGS
# ===========================================================================
CCTV_SCENARIOS = [
    {
        "cam": "CAM-04 • SUBWAY TUNNEL B",
        "env": "tunnel",
        "log1": "[03:14:02] MOTION SENSOR TRIGGERED IN SECTOR 4...",
        "log2": "[WARNING] UNKNOWN ENTITY DETECTED — DO NOT APPROACH",
        "anomaly": "shadow_walker"
    },
    {
        "cam": "CAM-09 • BLACKWOOD FOREST",
        "env": "forest",
        "log1": "[02:47:19] THERMAL SIGNATURE SPIKE NEAR PERIMETER...",
        "log2": "[ALERT] SUBJECT IS STARING DIRECTLY AT CAMERA",
        "anomaly": "twin_eyes"
    },
    {
        "cam": "CAM-02 • ABANDONED WARD 7",
        "env": "corridor",
        "log1": "[04:01:55] SIGNAL INTERFERENCE — FREQUENCY 19.4 HZ...",
        "log2": "[CRITICAL] CONTAINMENT BREACH — FEED COMPROMISED",
        "anomaly": "glitch_phantom"
    },
    {
        "cam": "CAM-11 • ARCTIC OUTPOST 9",
        "env": "outpost",
        "log1": "[01:12:40] BLIZZARD VISIBILITY 12% — RADAR PING...",
        "log2": "[DANGER] MASSIVE SILHOUETTE APPROACHING GATE",
        "anomaly": "tall_stalker"
    }
]

# ===========================================================================
# 2. CCTV STATIC, HEARTBEAT, GLITCH & ANOMALY ALARM AUDIO SYNTHESIZER
# ===========================================================================
def build_cctv_audio_wav(events: list, total_dur: float, out_wav: str):
    """Synthesizes eerie night-vision hum, static bursts, heartbeat, and anomaly alarm."""
    rate = 22050
    n = int(total_dur * rate)
    t = np.linspace(0, total_dur, n, endpoint=False, dtype=np.float32)

    rng = np.random.default_rng(666)
    # 60Hz electrical camera hum + low eerie wind
    hum = 0.045 * np.sin(2.0 * np.pi * 60.0 * t) + 0.035 * np.sin(2.0 * np.pi * 42.0 * t)
    wind = rng.uniform(-1.0, 1.0, n).astype(np.float32)
    # Simple low-pass smoothing for eerie wind
    wind = np.convolve(wind, np.ones(45, dtype=np.float32) / 45.0, mode="same") * 0.14

    audio = hum + wind

    for t_start, t_detect, t_end in events:
        # 1. Subtle mechanical camera zoom whir at t_start
        s0 = int(t_start * rate)
        w_len = min(n - s0, int(0.45 * rate))
        if w_len > 0:
            tw = np.linspace(0, w_len / float(rate), w_len, endpoint=False, dtype=np.float32)
            whir = np.sin(2.0 * np.pi * (240.0 + 180.0 * tw) * tw) * np.sin(np.pi * tw / 0.45) * 0.12
            audio[s0:s0 + w_len] += whir

        # 2. Intense heartbeat + static glitch + radar warning beep when anomaly appears!
        sd = int(t_detect * rate)
        d_len = min(n - sd, int((t_end - t_detect) * rate))
        if d_len > 0:
            td = np.linspace(0, d_len / float(rate), d_len, endpoint=False, dtype=np.float32)
            # Fast heartbeat thump (lub-dub every 0.65s)
            hb_cycle = td % 0.65
            lub = np.sin(2.0 * np.pi * 48.0 * hb_cycle) * np.exp(-hb_cycle * 22.0) * 0.48
            dub_t = np.maximum(0.0, hb_cycle - 0.16)
            dub = np.sin(2.0 * np.pi * 44.0 * dub_t) * np.exp(-dub_t * 25.0) * (hb_cycle > 0.16) * 0.38

            # Radar alert ping at detection start
            ping_len = min(d_len, int(0.85 * rate))
            tp = td[:ping_len]
            beep = np.sin(2.0 * np.pi * 960.0 * tp) * ((tp % 0.22) < 0.09) * 0.22

            # Static burst
            st_len = min(d_len, int(0.25 * rate))
            static_burst = rng.uniform(-1.0, 1.0, st_len).astype(np.float32) * np.exp(-td[:st_len] * 10.0) * 0.32

            audio[sd:sd + d_len] += (lub + dub)
            audio[sd:sd + ping_len] += beep
            audio[sd:sd + st_len] += static_burst

    fade_s = min(int(0.6 * rate), n // 4)
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
# 3. NIGHT-VISION CCTV SCENE, ANOMALY ENTITY & GLITCH RENDERER
# ===========================================================================
_FONT_CACHE = {}
def get_font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"
    ]:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[size] = f
                return f
            except Exception:
                pass
    return ImageFont.load_default()

class CCTVAnomalyRenderer:
    def __init__(self, w: int, h: int):
        self.w = w
        self.h = h
        self.is_vertical = (h > w)
        self.rng = np.random.default_rng(1337)

        # Precompute radial IR flashlight vignette (brighter center, dark corners)
        yy, xx = np.ogrid[0:h, 0:w]
        nx = (xx - w * 0.5) / (w * 0.52)
        ny = (yy - h * 0.48) / (h * 0.52)
        dist2 = nx * nx + ny * ny
        self.vignette = np.clip(1.15 - 0.65 * dist2, 0.18, 1.0).astype(np.float32)

        # Precompute horizontal CRT scanlines
        self.scanlines = np.ones((h, 1, 1), dtype=np.float32)
        self.scanlines[1::2, 0, 0] = 0.80

    def _draw_environment(self, draw: ImageDraw.Draw, env: str, sc: float):
        cx, cy = self.w * 0.5, self.h * 0.46
        if env == "tunnel":
            # Perspective subway tunnel rings & tracks
            for r in range(1, 9):
                rad_x = int(r * 68 * sc)
                rad_y = int(r * 58 * sc)
                shade = max(16, 48 - r * 4)
                draw.ellipse([cx - rad_x, cy - rad_y, cx + rad_x, cy + rad_y], outline=(10, shade, 18), width=3)
            draw.line([(cx - 40 * sc, cy + 30 * sc), (self.w * 0.18, self.h)], fill=(22, 68, 32), width=3)
            draw.line([(cx + 40 * sc, cy + 30 * sc), (self.w * 0.82, self.h)], fill=(22, 68, 32), width=3)

        elif env == "forest":
            # Dark silhouettes of twisted trees & fog horizon
            draw.rectangle([0, int(cy), self.w, self.h], fill=(8, 26, 14))
            for tx_frac, tw in [(0.08, 28), (0.22, 18), (0.76, 22), (0.91, 32), (0.38, 12), (0.64, 14)]:
                tx = int(self.w * tx_frac)
                draw.rectangle([tx - int(tw * sc * 0.5), 0, tx + int(tw * sc * 0.5), self.h], fill=(4, 14, 8))

        elif env == "corridor":
            # Hospital/facility perspective hallway walls & doors
            box_w, box_h = int(130 * sc), int(160 * sc)
            x0, y0 = int(cx - box_w * 0.5), int(cy - box_h * 0.5)
            x1, y1 = int(cx + box_w * 0.5), int(cy + box_h * 0.5)
            draw.rectangle([x0, y0, x1, y1], outline=(18, 64, 30), width=2)
            draw.line([(0, 0), (x0, y0)], fill=(16, 56, 26), width=2)
            draw.line([(self.w, 0), (x1, y0)], fill=(16, 56, 26), width=2)
            draw.line([(0, self.h), (x0, y1)], fill=(16, 56, 26), width=2)
            draw.line([(self.w, self.h), (x1, y1)], fill=(16, 56, 26), width=2)

        else:
            # Outpost fence & radar tower silhouette
            draw.rectangle([0, int(self.h * 0.65), self.w, self.h], fill=(10, 30, 18))
            for fx in range(0, self.w, int(45 * sc)):
                draw.line([(fx, int(self.h * 0.45)), (fx, int(self.h * 0.72))], fill=(18, 64, 32), width=2)
            draw.line([(0, int(self.h * 0.52)), (self.w, int(self.h * 0.52))], fill=(18, 64, 32), width=2)

    def render_frame(self, scenario: dict, seg_prog: float, global_t: float) -> bytes:
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0
        # Slight handheld/wind camera micro-shake
        shake_x = int(math.sin(global_t * 17.0) * 2.0 * sc)
        shake_y = int(math.cos(global_t * 13.0) * 2.0 * sc)

        frame = Image.new("RGB", (self.w, self.h), (5, 16, 9))
        draw = ImageDraw.Draw(frame)

        self._draw_environment(draw, scenario["env"], sc)

        # Anomaly emerges at 38% of the clip and lunges/moves closer!
        detected = (seg_prog >= 0.38)
        cx = int(self.w * 0.5 + math.sin(global_t * 1.4) * 35 * sc) + shake_x
        cy = int(self.h * 0.48) + shake_y

        if detected:
            d_prog = (seg_prog - 0.38) / 0.62
            scale_ent = 0.65 + d_prog * 0.95
            ent_h = int(150 * sc * scale_ent)
            ent_w = int(56 * sc * scale_ent)

            # Dark shadowy humanoid silhouette
            ex0, ey0 = cx - ent_w // 2, cy - ent_h // 2
            ex1, ey1 = cx + ent_w // 2, cy + ent_h // 2
            draw.ellipse([cx - int(20 * sc * scale_ent), ey0, cx + int(20 * sc * scale_ent), ey0 + int(38 * sc * scale_ent)], fill=(2, 6, 4))
            draw.polygon([(ex0, ey1), (ex0 + int(10 * sc), ey0 + int(36 * sc * scale_ent)), (ex1 - int(10 * sc), ey0 + int(36 * sc * scale_ent)), (ex1, ey1)], fill=(2, 6, 4))

            # Piercing glowing white/red anomaly eyes
            eye_y = ey0 + int(18 * sc * scale_ent)
            eye_sep = max(4, int(8 * sc * scale_ent))
            eye_r = max(2, int(3.0 * sc * scale_ent))
            eye_col = (255, 60, 60) if d_prog > 0.55 else (220, 255, 230)
            draw.ellipse([cx - eye_sep - eye_r, eye_y - eye_r, cx - eye_sep + eye_r, eye_y + eye_r], fill=eye_col)
            draw.ellipse([cx + eye_sep - eye_r, eye_y - eye_r, cx + eye_sep + eye_r, eye_y + eye_r], fill=eye_col)

            # Red Target-Lock Tracking Brackets around Anomaly!
            pad = int(18 * sc)
            bx0, by0, bx1, by1 = ex0 - pad, ey0 - pad, ex1 + pad, ey1 + pad
            br_len = int(18 * sc)
            b_col = (244, 63, 94)
            for (px, py, dx, dy) in [(bx0, by0, 1, 1), (bx1, by0, -1, 1), (bx0, by1, 1, -1), (bx1, by1, -1, -1)]:
                draw.line([(px, py), (px + dx * br_len, py)], fill=b_col, width=3)
                draw.line([(px, py), (px, py + dy * br_len)], fill=b_col, width=3)

            font_lock = get_font(max(12, int(14 * sc)))
            draw.text((bx0, by0 - int(20 * sc)), f"ANOMALY LOCK [{int(d_prog * 99)}%]", font=font_lock, fill=b_col)

        # Apply Night-Vision IR Vignette, CRT Scanlines & Sensor Grain via NumPy
        arr = np.asarray(frame, dtype=np.float32)
        arr *= self.vignette[:, :, None]
        arr *= self.scanlines

        # Add live sensor ISO grain
        noise = self.rng.integers(-14, 15, size=(self.h, self.w, 1), dtype=np.int16)
        arr = np.clip(arr + noise, 0, 255).astype(np.uint8)

        # Glitch horizontal slice shift right when anomaly is first detected!
        if 0.38 <= seg_prog <= 0.44 or (detected and int(global_t * 14) % 9 == 0):
            for _ in range(4):
                gy = int(self.rng.integers(0, max(1, self.h - 24)))
                gh = int(self.rng.integers(6, 22))
                shift = int(self.rng.integers(-28, 29))
                arr[gy:gy + gh] = np.roll(arr[gy:gy + gh], shift, axis=1)

        frame = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(frame)

        # CCTV HUD Overlays (REC dot, Live Timestamp, Camera ID, Typewriter Log)
        margin = int(self.w * 0.045)
        font_hud = get_font(max(14, int(18 * sc)))
        font_log = get_font(max(13, int(16 * sc)))

        # Blinking Red REC Indicator
        rec_y = int(self.h * 0.035)
        if int(global_t * 2.5) % 2 == 0:
            draw.ellipse([margin, rec_y + 3, margin + int(14 * sc), rec_y + int(17 * sc)], fill=(239, 68, 68))
        draw.text((margin + int(22 * sc), rec_y), "REC  •  NIGHT VISION IR", font=font_hud, fill=(220, 252, 231))

        # Running Millisecond Timecode
        ms = int((global_t % 1.0) * 100)
        secs = int(global_t) % 60
        mins = (int(global_t) // 60) % 60
        tc_txt = f"03:{14 + mins:02d}:{secs:02d}:{ms:02d}"
        tb = draw.textbbox((0, 0), tc_txt, font=font_hud)
        draw.text((self.w - margin - (tb[2] - tb[0]), rec_y), tc_txt, font=font_hud, fill=(134, 239, 172))

        # Camera Sector Label
        draw.text((margin, int(self.h * 0.082)), scenario["cam"], font=font_log, fill=(74, 222, 128))

        # Bottom Classified Typewriter Terminal Log
        active_log = scenario["log2"] if detected else scenario["log1"]
        local_p = (seg_prog - 0.38) / 0.25 if detected else (seg_prog / 0.25)
        char_cnt = max(1, int(len(active_log) * min(1.0, local_p)))
        shown_log = active_log[:char_cnt] + ("_" if int(global_t * 6) % 2 == 0 else "")

        log_y = int(self.h * 0.87)
        draw.rectangle([margin - 6, log_y - 6, self.w - margin + 6, log_y + int(34 * sc)], fill=(3, 10, 6), outline=(244, 63, 94) if detected else (34, 197, 94), width=2)
        draw.text((margin + 4, log_y + int(4 * sc)), shown_log, font=font_log, fill=(254, 202, 202) if detected else (187, 247, 208))

        return frame.tobytes()


# ===========================================================================
# 4. RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_cctv_job(job_id: str, ratio: str, duration_sec: float):
    job = JOBS[job_id]
    try:
        out_w, out_h = (960, 540) if ratio == "16:9" else (540, 960)
        fps = 24
        dt = 1.0 / float(fps)

        scenarios = list(CCTV_SCENARIOS)
        random.shuffle(scenarios)

        seg_dur = 9.0
        num_clips = max(1, int(round(duration_sec / seg_dur)))
        actual_dur = num_clips * seg_dur

        audio_events = []
        for i in range(num_clips):
            t0 = i * seg_dur
            audio_events.append((t0, t0 + seg_dur * 0.38, t0 + seg_dur))

        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_cctv_sfx.wav")
        job["progress"] = 8
        job["message"] = "Synthesizing Night-Vision Static, Heartbeat & Anomaly Alarm..."
        build_cctv_audio_wav(audio_events, actual_dur, wav_path)

        out_name = f"NEXORA_CCTV_ANOMALY_{int(actual_dur)}s_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
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

        renderer = CCTVAnomalyRenderer(out_w, out_h)
        global_t = 0.0
        frames_per_clip = int(seg_dur * fps)

        for c_idx in range(num_clips):
            scn = scenarios[c_idx % len(scenarios)]
            for f in range(frames_per_clip):
                s_prog = f / max(1, frames_per_clip - 1)
                raw_bytes = renderer.render_frame(scn, s_prog, global_t)
                proc.stdin.write(raw_bytes)
                global_t += dt

            job["progress"] = 12 + int(((c_idx + 1) / num_clips) * 85)
            job["message"] = f"Rendering CCTV Footage {c_idx + 1}/{num_clips}..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Night-Vision CCTV Footage Ready ({out_name})"

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

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Hacking Night-Vision Security Feeds..."}
    threading.Thread(
        target=run_cctv_job,
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
