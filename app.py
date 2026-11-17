import os
import re
import time
import math
import uuid
import wave
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
# 1. BUILT-IN HIGH-RPM VIRAL DATASETS + CUSTOM CSV/TEXT DATA PARSER
# ===========================================================================
PRESET_DATASETS = {
    "tech_giants": {
        "title": "TOP 10 MOST VALUABLE TECH COMPANIES (2000 - 2026)",
        "unit": "$B",
        "years": [2000, 2005, 2010, 2015, 2020, 2023, 2026],
        "series": {
            "Microsoft":  [510, 280, 240, 440, 1680, 2790, 3450],
            "Apple":      [16,  55,  295, 590, 2250, 2990, 3620],
            "NVIDIA":     [5,   8,   9,   18,  320,  1220, 3890],
            "Alphabet":   [0,   115, 190, 520, 1180, 1750, 2380],
            "Amazon":     [25,  20,  80,  310, 1630, 1570, 2290],
            "Meta":       [0,   0,   25,  290, 770,  910,  1540],
            "Tesla":      [0,   0,   2,   32,  660,  790,  1120],
            "TSMC":       [35,  48,  65,  115, 480,  540,  980],
            "Samsung":    [45,  95,  135, 180, 410,  390,  520],
            "Cisco":      [420, 110, 115, 138, 190,  205,  240],
            "Intel":      [380, 150, 118, 162, 205,  195,  145],
            "IBM":        [185, 130, 182, 135, 112,  148,  210]
        }
    },
    "smartphones": {
        "title": "GLOBAL SMARTPHONE MARKET SHARE RACE (2008 - 2026)",
        "unit": "M Units",
        "years": [2008, 2011, 2014, 2017, 2020, 2023, 2026],
        "series": {
            "Nokia":      [61,  77,  15,  8,   12,  10,  8],
            "BlackBerry": [23,  51,  9,   2,   0,   0,   0],
            "Apple":      [13,  93,  192, 215, 206, 234, 248],
            "Samsung":    [10,  94,  318, 317, 256, 226, 242],
            "Xiaomi":     [0,   1,   61,  92,  148, 146, 178],
            "OPPO":       [0,   2,   30,  111, 112, 103, 116],
            "Vivo":       [0,   1,   26,  95,  110, 94,  108],
            "Huawei":     [2,   20,  75,  153, 189, 35,  68],
            "Motorola":   [12,  18,  24,  28,  34,  46,  58],
            "Realme":     [0,   0,   0,   0,   42,  45,  54]
        }
    },
    "economies": {
        "title": "LARGEST ECONOMIES IN THE WORLD BY GDP (1980 - 2026)",
        "unit": "$T",
        "years": [1980, 1990, 2000, 2010, 2018, 2022, 2026],
        "series": {
            "USA":        [2.86, 5.96, 10.25, 15.05, 20.53, 25.74, 30.80],
            "China":      [0.30, 0.39, 1.21,  6.08,  13.89, 17.96, 21.40],
            "Japan":      [1.12, 3.19, 4.97,  5.76,  5.04,  4.23,  4.45],
            "Germany":    [0.95, 1.77, 1.95,  3.40,  3.98,  4.08,  4.75],
            "India":      [0.19, 0.32, 0.47,  1.68,  2.70,  3.39,  4.68],
            "UK":         [0.60, 1.09, 1.66,  2.49,  2.87,  3.09,  3.62],
            "France":     [0.70, 1.27, 1.36,  2.64,  2.79,  2.78,  3.22],
            "Brazil":     [0.24, 0.46, 0.65,  2.21,  1.92,  1.92,  2.38],
            "Italy":      [0.48, 1.18, 1.15,  2.13,  2.09,  2.05,  2.41],
            "Canada":     [0.27, 0.59, 0.74,  1.61,  1.72,  2.14,  2.35]
        }
    }
}

BAR_PALETTE = [
    (34, 211, 238),   # Cyan
    (250, 204, 21),   # Gold
    (244, 63, 94),    # Rose
    (16, 185, 129),   # Emerald
    (168, 85, 247),   # Violet
    (249, 115, 22),   # Orange
    (56, 189, 248),   # Sky
    (232, 121, 249),  # Magenta
    (52, 211, 153),   # Mint
    (251, 191, 36),   # Amber
    (99, 102, 241),   # Indigo
    (248, 113, 113)   # Coral
]

def parse_custom_csv_data(raw_text: str, custom_title: str, custom_unit: str):
    """
    Parses user-pasted data in format:
    YEARS: 2015, 2018, 2021, 2024, 2026
    NameA: 10, 25, 40, 75, 120
    NameB: 30, 35, 50, 60, 95
    """
    lines = [ln.strip() for ln in str(raw_text or "").splitlines() if ln.strip()]
    years = []
    series = {}
    for ln in lines:
        if ":" not in ln:
            continue
        key, vals_str = ln.split(":", 1)
        key = key.strip()
        nums = []
        for tok in vals_str.split(","):
            tok_clean = re.sub(r"[^0-9.\-]", "", tok.strip())
            if tok_clean:
                nums.append(float(tok_clean))
        if key.upper() == "YEARS" and len(nums) >= 2:
            years = [int(x) for x in nums]
        elif len(nums) >= 2:
            series[key] = nums

    if len(years) >= 2 and len(series) >= 3:
        n_pts = len(years)
        clean_series = {}
        for k, arr in series.items():
            if len(arr) < n_pts:
                arr = arr + [arr[-1]] * (n_pts - len(arr))
            clean_series[k] = arr[:n_pts]
        return {
            "title": (custom_title or "GLOBAL DATA RANKING RACE").upper(),
            "unit": custom_unit or "",
            "years": years,
            "series": clean_series
        }
    return None


# ===========================================================================
# 2. UPBEAT DOCUMENTARY / DATA-RACE BACKGROUND MUSIC GENERATOR
# ===========================================================================
def generate_documentary_music_wav(duration_sec: float, overtake_times: list, out_wav: str):
    """Generates clean corporate/documentary pulse music + subtle 'whoosh' on rank #1 overtakes!"""
    rate = 22050
    n = int(duration_sec * rate)
    t = np.linspace(0, duration_sec, n, endpoint=False, dtype=np.float32)

    bpm = 116.0
    beat_dur = 60.0 / bpm
    beat_pos = (t % beat_dur) / beat_dur

    # Soft documentary kick & marimba arpeggio
    kick = np.sin(2.0 * np.pi * (52.0 + 60.0 * np.exp(-beat_pos * 18.0)) * (t % beat_dur)) * np.exp(-beat_pos * 9.0) * 0.32

    arp_notes = [261.63, 329.63, 392.00, 523.25, 293.66, 349.23, 440.00, 587.33]
    step_idx = (t / (beat_dur * 0.5)).astype(np.int32)
    step_pos = (t % (beat_dur * 0.5)) / (beat_dur * 0.5)
    freqs = np.array([arp_notes[i % len(arp_notes)] for i in step_idx], dtype=np.float32)
    marimba = np.sin(2.0 * np.pi * freqs * t) * np.exp(-step_pos * 6.5) * 0.18

    # Warm pad
    pad = (0.06 * np.sin(2.0 * np.pi * 130.81 * t) + 0.04 * np.sin(2.0 * np.pi * 196.00 * t))

    audio = kick + marimba + pad

    # Crisp bell chime whenever a new leader takes #1!
    for ot in overtake_times:
        s_idx = int(ot * rate)
        dur = 0.45
        e_idx = min(n, s_idx + int(dur * rate))
        if s_idx < e_idx:
            tb = np.linspace(0, (e_idx - s_idx) / float(rate), e_idx - s_idx, endpoint=False, dtype=np.float32)
            chime = (0.5 * np.sin(2.0 * np.pi * 880.0 * tb) + 0.3 * np.sin(2.0 * np.pi * 1318.5 * tb)) * np.exp(-tb * 8.0) * 0.35
            audio[s_idx:e_idx] += chime

    fade_s = min(int(0.8 * rate), n // 4)
    if fade_s > 0:
        audio[:fade_s] *= np.linspace(0, 1, fade_s)
        audio[-fade_s:] *= np.linspace(1, 0, fade_s)

    audio = np.clip(audio, -0.95, 0.95)
    pcm = (audio * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


# ===========================================================================
# 3. 30 FPS SMOOTH INTERPOLATED BAR CHART RACE RENDERER
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

class BarChartRaceRenderer:
    def __init__(self, w: int, h: int, dataset: dict, fps: int = 24):
        self.w = w
        self.h = h
        self.title = dataset["title"]
        self.unit = dataset["unit"]
        self.years = dataset["years"]
        self.series = dataset["series"]
        self.fps = fps
        self.is_vertical = (h > w)
        self.top_n = 10 if not self.is_vertical else 9

        # Assign consistent brand colors per item
        self.colors = {}
        for idx, name in enumerate(self.series.keys()):
            self.colors[name] = BAR_PALETTE[idx % len(BAR_PALETTE)]

        # Smooth spring-damped Y-positions so rank overtakes glide like butter!
        self.smooth_rank = {name: float(idx) for idx, name in enumerate(self.series.keys())}
        self.prev_leader = None
        self.overtake_times = []

        # Precompute clean dark-navy studio grid background
        grad = np.zeros((h, w, 3), dtype=np.uint8)
        top_c, bot_c = (12, 18, 34), (5, 8, 16)
        for ch in range(3):
            col_line = np.linspace(top_c[ch], bot_c[ch], h, dtype=np.uint8)[:, None]
            grad[:, :, ch] = np.repeat(col_line, w, axis=1)
        self.base_bg = Image.fromarray(grad, mode="RGB")

    def _interpolate_values(self, prog: float):
        """Cubic-eased interpolation across the dataset timeline."""
        n_segs = len(self.years) - 1
        scaled = prog * n_segs
        idx = min(n_segs - 1, int(scaled))
        frac = scaled - idx
        # Smooth ease
        e = frac * frac * (3.0 - 2.0 * frac)

        cur_year = self.years[idx] + (self.years[idx + 1] - self.years[idx]) * frac
        vals = {}
        for name, arr in self.series.items():
            v0, v1 = arr[idx], arr[idx + 1]
            vals[name] = v0 + (v1 - v0) * e
        return cur_year, vals

    def render_frame(self, prog: float, global_t: float) -> bytes:
        cur_year, vals = self._interpolate_values(prog)

        # Sort items descending by current value
        ranked = sorted(vals.items(), key=lambda kv: kv[1], reverse=True)
        leader_name = ranked[0][0]
        if self.prev_leader is not None and leader_name != self.prev_leader:
            self.overtake_times.append(global_t)
        self.prev_leader = leader_name

        max_val = max(1e-3, ranked[0][1])

        # Smoothly glide vertical rank slots (exponential smoothing)
        for target_idx, (name, _) in enumerate(ranked):
            cur_r = self.smooth_rank[name]
            self.smooth_rank[name] = cur_r + (target_idx - cur_r) * 0.22

        frame = self.base_bg.copy()
        draw = ImageDraw.Draw(frame)

        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0

        # 1. Header Title & Subtitle
        font_title = get_font(max(18, int(24 * sc)))
        font_sub = get_font(max(13, int(15 * sc)))
        draw.text((int(self.w * 0.05), int(self.h * 0.04)), self.title[:48], font=font_title, fill=(248, 250, 252))
        draw.text((int(self.w * 0.05), int(self.h * 0.095)), f"LIVE RANKING TIMELINE  •  UNIT: {self.unit}", font=font_sub, fill=(34, 211, 238))

        # 2. Chart Layout Coordinates
        left_margin = int(self.w * (0.24 if self.is_vertical else 0.18))
        right_margin = int(self.w * 0.14)
        chart_w = max(80, self.w - left_margin - right_margin)
        top_y = int(self.h * 0.16)
        bot_y = int(self.h * 0.88)
        slot_h = (bot_y - top_y) / float(self.top_n)
        bar_h = max(14, int(slot_h * 0.68))

        # Dynamic Vertical Scale Grid Lines
        font_grid = get_font(max(11, int(13 * sc)))
        for g_frac in (0.25, 0.50, 0.75, 1.0):
            gx = left_margin + int(chart_w * g_frac)
            draw.line([(gx, top_y - 6), (gx, bot_y)], fill=(30, 41, 59), width=1)
            g_val = max_val * g_frac
            g_lbl = f"{g_val:,.0f}" if g_val >= 10 else f"{g_val:.1f}"
            draw.text((gx - 14, top_y - 22), g_lbl, font=font_grid, fill=(100, 116, 139))

        # 3. Draw Bars in Reverse Rank Order so Overtaking Bars Slide Cleanly On Top
        font_lbl = get_font(max(13, int(17 * sc)))
        font_val = get_font(max(13, int(16 * sc)))
        font_rk = get_font(max(11, int(13 * sc)))

        visible_items = [(name, val) for name, val in ranked if self.smooth_rank[name] < self.top_n + 0.4]
        visible_items.sort(key=lambda kv: self.smooth_rank[kv[0]], reverse=True)

        for name, val in visible_items:
            r_pos = self.smooth_rank[name]
            y0 = int(top_y + r_pos * slot_h + (slot_h - bar_h) * 0.5)
            y1 = y0 + bar_h
            bw = max(4, int((val / max_val) * chart_w))
            col = self.colors[name]

            # Bar shadow + main rounded bar + bright leading edge highlight
            draw.rounded_rectangle([left_margin, y0, left_margin + bw, y1], radius=6, fill=col)
            if bw > 10:
                draw.rectangle([left_margin + bw - 5, y0 + 2, left_margin + bw, y1 - 2], fill=(255, 255, 255))

            # Entity Name on Left
            nb = draw.textbbox((0, 0), name, font=font_lbl)
            nw = nb[2] - nb[0]
            nh = nb[3] - nb[1]
            draw.text((left_margin - nw - 12, y0 + (bar_h - nh) // 2 - 2), name, font=font_lbl, fill=(241, 245, 249))

            # Rank Badge inside left of bar
            rk_num = int(round(r_pos)) + 1
            if bw > 28:
                draw.text((left_margin + 8, y0 + (bar_h - nh) // 2), f"#{rk_num}", font=font_rk, fill=(10, 15, 28))

            # Live Value Counter on Right of Bar
            val_txt = f"{val:,.1f} {self.unit}".strip() if val < 100 else f"{val:,.0f} {self.unit}".strip()
            draw.text((left_margin + bw + 10, y0 + (bar_h - nh) // 2 - 2), val_txt, font=font_val, fill=col)

        # 4. Giant Cinematic Year / Timeline Watermark in Bottom-Right
        font_year = get_font(max(36, int(58 * sc)))
        yr_str = str(int(round(cur_year)))
        yb = draw.textbbox((0, 0), yr_str, font=font_year)
        yw, yh = yb[2] - yb[0], yb[3] - yb[1]
        draw.text((self.w - yw - int(self.w * 0.05), self.h - yh - int(self.h * 0.06)), yr_str, font=font_year, fill=(226, 232, 240))

        # Bottom Timeline Progress Bar
        pb_y = int(self.h * 0.94)
        draw.rectangle([int(self.w * 0.05), pb_y, int(self.w * 0.95), pb_y + 5], fill=(30, 41, 59))
        draw.rectangle([int(self.w * 0.05), pb_y, int(self.w * (0.05 + 0.90 * prog)), pb_y + 5], fill=(34, 211, 238))

        return frame.tobytes()


# ===========================================================================
# 4. RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_data_race_job(job_id: str, ratio: str, preset_key: str, duration_sec: float,
                      custom_csv: str, custom_title: str, custom_unit: str):
    job = JOBS[job_id]
    try:
        dataset = None
        if preset_key == "custom":
            dataset = parse_custom_csv_data(custom_csv, custom_title, custom_unit)
            if not dataset:
                job["status"] = "error"
                job["message"] = "Invalid Custom Data! Use YEARS: 2020, 2023, 2026 and at least 3 rows."
                return
        else:
            dataset = PRESET_DATASETS.get(preset_key, PRESET_DATASETS["tech_giants"])

        out_w, out_h = (960, 540) if ratio == "16:9" else (540, 960)
        fps = 24
        total_frames = int(duration_sec * fps)
        dt = 1.0 / float(fps)

        renderer = BarChartRaceRenderer(out_w, out_h, dataset, fps=fps)

        out_name = f"NEXORA_DATA_RACE_{preset_key.upper()}_{int(duration_sec)}s_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        raw_vid_path = os.path.join(OUTPUT_DIR, f"{job_id}_silent.mp4")
        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_doc_music.wav")

        ffmpeg_vid_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", raw_vid_path
        ]
        proc = subprocess.Popen(ffmpeg_vid_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        for f in range(total_frames):
            prog = f / max(1, total_frames - 1)
            raw_bytes = renderer.render_frame(prog, global_t)
            proc.stdin.write(raw_bytes)
            global_t += dt

            if f % 48 == 0:
                job["progress"] = 8 + int((f / max(1, total_frames)) * 78)
                job["message"] = f"Animating Data Race Timeline ({int(prog * 100)}%)..."

        proc.stdin.close()
        proc.wait()

        job["progress"] = 88
        job["message"] = "Synthesizing Documentary Pulse Music & Leader Overtake Chimes..."
        generate_documentary_music_wav(duration_sec, renderer.overtake_times, wav_path)

        job["progress"] = 95
        job["message"] = "Mastering YouTube High-RPM MP4..."
        subprocess.run([
            FFMPEG_BIN, "-y",
            "-i", raw_vid_path,
            "-i", wav_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest", out_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(raw_vid_path):
            os.remove(raw_vid_path)

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! High-RPM Data Video Ready ({out_name})"

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
    ratio = data.get("ratio", "16:9")
    preset = data.get("preset", "tech_giants")
    duration_sec = max(15.0, min(600.0, float(data.get("duration", "120"))))
    custom_csv = data.get("custom_csv", "")
    custom_title = data.get("custom_title", "")
    custom_unit = data.get("custom_unit", "")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing High-RPM Data Ranking Engine..."}
    threading.Thread(
        target=run_data_race_job,
        args=(job_id, ratio, preset, duration_sec, custom_csv, custom_title, custom_unit),
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
