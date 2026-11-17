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
# 1. BUILT-IN QUESTION BANK (SHUFFLES EVERY TIME SO QUESTIONS NEVER REPEAT)
# ===========================================================================
QUESTION_BANK = [
    {"q": "Which planet in our solar system has the most moons?", "opts": ["Jupiter", "Saturn", "Uranus", "Neptune"], "ans": 1},
    {"q": "What is the hardest natural substance on Earth?", "opts": ["Gold", "Iron", "Diamond", "Platinum"], "ans": 2},
    {"q": "Which country is known as the Land of the Rising Sun?", "opts": ["China", "South Korea", "Thailand", "Japan"], "ans": 3},
    {"q": "How many bones are there in an adult human body?", "opts": ["196", "206", "216", "226"], "ans": 1},
    {"q": "Which gas do plants absorb from the atmosphere?", "opts": ["Oxygen", "Nitrogen", "Carbon Dioxide", "Hydrogen"], "ans": 2},
    {"q": "What is the largest ocean on Earth?", "opts": ["Atlantic Ocean", "Indian Ocean", "Arctic Ocean", "Pacific Ocean"], "ans": 3},
    {"q": "Who painted the world-famous Mona Lisa?", "opts": ["Vincent van Gogh", "Leonardo da Vinci", "Pablo Picasso", "Michelangelo"], "ans": 1},
    {"q": "Which metal is liquid at room temperature?", "opts": ["Mercury", "Silver", "Copper", "Aluminum"], "ans": 0},
    {"q": "What is the smallest country in the world by area?", "opts": ["Monaco", "Maldives", "Vatican City", "San Marino"], "ans": 2},
    {"q": "Which organ in the human body pumps blood?", "opts": ["Brain", "Lungs", "Liver", "Heart"], "ans": 3},
    {"q": "What has keys that open no locks, space but no room?", "opts": ["A Map", "A Keyboard", "A Clock", "A Piano"], "ans": 1},
    {"q": "Which animal can sleep for up to three years?", "opts": ["Koala", "Sloth", "Snail", "Bat"], "ans": 2},
    {"q": "What is the speed of light approximately?", "opts": ["300,000 km/s", "150,000 km/s", "30,000 km/s", "3,000 km/s"], "ans": 0},
    {"q": "Which continent has the highest number of countries?", "opts": ["Asia", "Europe", "Africa", "South America"], "ans": 2},
    {"q": "What gets wetter the more it dries?", "opts": ["A Sponge", "A Towel", "Ice", "Clouds"], "ans": 1}
]

def parse_quiz_input(raw_text: str, count: int, shuffle_mode: bool):
    """
    Parses custom questions in format:
    Q: Question text? | OptA | OptB | OptC | OptD | CorrectIndex(1-4)
    Falls back to shuffled built-in bank if empty.
    """
    items = []
    for ln in str(raw_text or "").splitlines():
        ln = ln.strip()
        if not ln or "|" not in ln:
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) >= 6:
            q_txt = re.sub(r"^Q\d*\s*[:.\-]\s*", "", parts[0], flags=re.I).strip()
            opts = parts[1:5]
            try:
                ans_idx = max(0, min(3, int(re.sub(r"[^0-9]", "", parts[5])) - 1))
            except Exception:
                ans_idx = 0
            items.append({"q": q_txt, "opts": opts, "ans": ans_idx})

    if not items:
        items = [dict(x) for x in QUESTION_BANK]

    if shuffle_mode:
        random.shuffle(items)

    return items[:max(1, min(30, count))]


# ===========================================================================
# 2. VOICEOVER + CLOCK TICK-TOCK & DING REVEAL AUDIO ENGINE
# ===========================================================================
VOICE_MAP = {
    "MALE_ENGLISH": "en-US-AndrewNeural",
    "FEMALE_ENGLISH": "en-US-AvaNeural",
    "MALE_HINDI": "hi-IN-MadhurNeural",
    "FEMALE_HINDI": "hi-IN-SwaraNeural"
}

async def _tts_save(text: str, voice_id: str, mp3_path: str):
    comm = edge_tts.Communicate(text, voice_id, rate="+5%")
    await comm.save(mp3_path)

def generate_tts_wav(text: str, lang_mode: str, is_female: bool, out_wav: str) -> float:
    clean_txt = re.sub(r"[^a-zA-Z0-9\s,?!.'\-]", "", text).strip() or "Next question"
    if lang_mode == "HINDI":
        vid = VOICE_MAP["FEMALE_HINDI"] if is_female else VOICE_MAP["MALE_HINDI"]
    else:
        vid = VOICE_MAP["FEMALE_ENGLISH"] if is_female else VOICE_MAP["MALE_ENGLISH"]

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
        dur = max(1.8, len(clean_txt.split()) * 0.36)
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", str(dur), out_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    with wave.open(out_wav, "rb") as wf:
        return max(1.5, wf.getnframes() / float(wf.getframerate()))

def build_timer_and_ding_wav(think_dur: float, out_wav: str):
    """Synthesizes a 5-second suspenseful clock tick-tock followed by a bright 'DING!' chime."""
    rate = 22050
    n = int(think_dur * rate)
    audio = np.zeros(n, dtype=np.float32)

    # Tick-tock every 0.5 seconds
    sec_step = 0.5
    t_mark = 0.25
    tick_idx = 0
    while t_mark < think_dur - 0.15:
        s_idx = int(t_mark * rate)
        dur_s = int(0.06 * rate)
        if s_idx + dur_s < n:
            tt = np.linspace(0, 0.06, dur_s, endpoint=False, dtype=np.float32)
            freq = 1150.0 if (tick_idx % 2 == 0) else 880.0
            tick = np.sin(2.0 * np.pi * freq * tt) * np.exp(-tt * 75.0) * 0.45
            audio[s_idx:s_idx + dur_s] += tick
        t_mark += sec_step
        tick_idx += 1

    # Soft tension hum
    t_full = np.linspace(0, think_dur, n, endpoint=False, dtype=np.float32)
    audio += 0.05 * np.sin(2.0 * np.pi * 110.0 * t_full)

    pcm = (np.clip(audio, -0.95, 0.95) * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())

def prepend_ding_to_wav(ans_wav: str, out_wav: str):
    """Adds a celebratory bell 'DING!' at the exact start of the answer reveal."""
    with wave.open(ans_wav, "rb") as wf:
        rate = wf.getframerate()
        ans = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    dur_d = int(0.65 * rate)
    td = np.linspace(0, 0.65, dur_d, endpoint=False, dtype=np.float32)
    ding = (
        0.55 * np.sin(2.0 * np.pi * 1046.50 * td) +
        0.35 * np.sin(2.0 * np.pi * 1318.51 * td) +
        0.25 * np.sin(2.0 * np.pi * 1567.98 * td)
    ) * np.exp(-td * 6.5) * 0.55

    if len(ans) < dur_d:
        ans = np.pad(ans, (0, dur_d - len(ans)))
    ans[:dur_d] = np.clip(ans[:dur_d] + ding, -0.95, 0.95)

    pcm = (ans * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf_out:
        wf_out.setnchannels(1)
        wf_out.setsampwidth(2)
        wf_out.setframerate(rate)
        wf_out.writeframes(pcm.tobytes())


# ===========================================================================
# 3. STUDIO QUIZ GRAPHICS & COUNTDOWN BAR RENDERER
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

def wrap_text_lines(draw: ImageDraw.Draw, text: str, font, max_w: int) -> list:
    words = text.split()
    lines, cur = [], ""
    for wd in words:
        test = (cur + " " + wd).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines

class QuizStudioRenderer:
    def __init__(self, w: int, h: int, total_q: int):
        self.w = w
        self.h = h
        self.total_q = total_q
        self.is_vertical = (h > w)

        # Rich Royal Indigo & Deep Slate Quiz Show Backdrop
        grad = np.zeros((h, w, 3), dtype=np.uint8)
        top_c, bot_c = (18, 24, 54), (7, 10, 24)
        for ch in range(3):
            col_line = np.linspace(top_c[ch], bot_c[ch], h, dtype=np.uint8)[:, None]
            grad[:, :, ch] = np.repeat(col_line, w, axis=1)
        self.base_bg = Image.fromarray(grad, mode="RGB")

    def render_frame(self, q_idx: int, q_obj: dict, phase: str, phase_prog: float, global_t: float) -> bytes:
        frame = self.base_bg.copy()
        draw = ImageDraw.Draw(frame)
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0

        # Animated subtle diagonal background accent lines
        offset = int((global_t * 35) % 80)
        for x in range(-self.h, self.w + self.h, 80):
            draw.line([(x + offset, 0), (x + offset - self.h // 2, self.h)], fill=(25, 34, 72), width=2)

        # 1. Top Badge: QUESTION X / N
        font_badge = get_font(max(15, int(18 * sc)))
        badge_txt = f"QUESTION {q_idx + 1} / {self.total_q}"
        bw_box = draw.textbbox((0, 0), badge_txt, font=font_badge)
        bw = bw_box[2] - bw_box[0] + 36
        bh = int(36 * sc)
        bx0 = (self.w - bw) // 2
        by0 = int(self.h * 0.05)
        draw.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=bh // 2, fill=(34, 211, 238))
        draw.text((bx0 + 18, by0 + int(7 * sc)), badge_txt, font=font_badge, fill=(6, 10, 24))

        # 2. Question Card
        card_margin = int(self.w * 0.06)
        q_top = int(self.h * 0.14)
        q_h = int(self.h * (0.24 if self.is_vertical else 0.25))
        draw.rounded_rectangle(
            [card_margin, q_top, self.w - card_margin, q_top + q_h],
            radius=16, fill=(15, 23, 42), outline=(56, 189, 248), width=3
        )

        font_q = get_font(max(20, int(26 * sc)))
        q_lines = wrap_text_lines(draw, q_obj["q"], font_q, self.w - card_margin * 2 - 40)
        line_h = int(font_q.size * 1.32)
        text_start_y = q_top + (q_h - len(q_lines) * line_h) // 2
        for li, ln in enumerate(q_lines):
            lb = draw.textbbox((0, 0), ln, font=font_q)
            lw = lb[2] - lb[0]
            draw.text(((self.w - lw) // 2, text_start_y + li * line_h), ln, font=font_q, fill=(248, 250, 252))

        # 3. 5-Second Countdown Timer Bar (Active during 'think' phase)
        timer_y = q_top + q_h + int(self.h * 0.03)
        timer_h = max(14, int(18 * sc))
        bar_w = self.w - card_margin * 2
        draw.rounded_rectangle([card_margin, timer_y, card_margin + bar_w, timer_y + timer_h], radius=timer_h // 2, fill=(30, 41, 59))

        if phase == "read":
            fill_frac = 1.0
            t_col = (56, 189, 248)
        elif phase == "think":
            fill_frac = max(0.0, 1.0 - phase_prog)
            t_col = (250, 204, 21) if fill_frac > 0.35 else (244, 63, 94)
        else:
            fill_frac = 1.0
            t_col = (16, 185, 129)

        cur_bw = int(bar_w * fill_frac)
        if cur_bw > 8:
            draw.rounded_rectangle([card_margin, timer_y, card_margin + cur_bw, timer_y + timer_h], radius=timer_h // 2, fill=t_col)

        # 4. Four Option Cards (A, B, C, D)
        opts = q_obj["opts"]
        ans_idx = q_obj["ans"]
        labels = ["A", "B", "C", "D"]
        font_opt = get_font(max(18, int(22 * sc)))
        font_lbl = get_font(max(17, int(20 * sc)))

        opts_top = timer_y + timer_h + int(self.h * 0.04)
        opts_area_h = int(self.h * 0.44)

        if self.is_vertical:
            # 1 column x 4 rows for 9:16 mobile screen
            gap = int(14 * sc)
            box_h = (opts_area_h - gap * 3) // 4
            for i in range(4):
                oy0 = opts_top + i * (box_h + gap)
                oy1 = oy0 + box_h
                ox0, ox1 = card_margin, self.w - card_margin
                self._draw_option_box(draw, ox0, oy0, ox1, oy1, labels[i], opts[i], i == ans_idx, phase, font_lbl, font_opt, sc)
        else:
            # 2 columns x 2 rows for 16:9 YouTube screen
            gap_x = int(20 * sc)
            gap_y = int(18 * sc)
            box_w = (bar_w - gap_x) // 2
            box_h = (opts_area_h - gap_y) // 2
            for i in range(4):
                col_i = i % 2
                row_i = i // 2
                ox0 = card_margin + col_i * (box_w + gap_x)
                ox1 = ox0 + box_w
                oy0 = opts_top + row_i * (box_h + gap_y)
                oy1 = oy0 + box_h
                self._draw_option_box(draw, ox0, oy0, ox1, oy1, labels[i], opts[i], i == ans_idx, phase, font_lbl, font_opt, sc)

        return frame.tobytes()

    def _draw_option_box(self, draw, x0, y0, x1, y1, letter, text, is_correct, phase, font_lbl, font_opt, sc):
        if phase == "reveal":
            if is_correct:
                bg_col = (6, 95, 70)
                border_col = (52, 211, 153)
                txt_col = (255, 255, 255)
                pill_col = (16, 185, 129)
            else:
                bg_col = (15, 23, 42)
                border_col = (51, 65, 85)
                txt_col = (100, 116, 139)
                pill_col = (51, 65, 85)
        else:
            bg_col = (15, 23, 42)
            border_col = (99, 102, 241)
            txt_col = (241, 245, 249)
            pill_col = (99, 102, 241)

        draw.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=bg_col, outline=border_col, width=3)

        # Option Letter Circle (A/B/C/D)
        bh = y1 - y0
        cr = int(min(bh * 0.34, 22 * sc))
        cx = x0 + cr + int(16 * sc)
        cy = y0 + bh // 2
        draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=pill_col)
        lb = draw.textbbox((0, 0), letter, font=font_lbl)
        draw.text((cx - (lb[2] - lb[0]) // 2, cy - (lb[3] - lb[1]) // 2 - 2), letter, font=font_lbl, fill=(255, 255, 255))

        # Option Text
        tb = draw.textbbox((0, 0), text, font=font_opt)
        draw.text((cx + cr + int(16 * sc), cy - (tb[3] - tb[1]) // 2 - 2), text, font=font_opt, fill=txt_col)


# ===========================================================================
# 4. QUIZ RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_quiz_job(job_id: str, ratio: str, lang_mode: str, is_female: bool,
                 q_count: int, shuffle_mode: bool, custom_raw: str):
    job = JOBS[job_id]
    try:
        questions = parse_quiz_input(custom_raw, q_count, shuffle_mode)
        out_w, out_h = (960, 540) if ratio == "16:9" else (540, 960)
        fps = 24
        dt = 1.0 / float(fps)
        total_q = len(questions)

        renderer = QuizStudioRenderer(out_w, out_h, total_q)
        out_name = f"NEXORA_QUIZ_SHOW_{total_q}Q_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        raw_vid_path = os.path.join(OUTPUT_DIR, f"{job_id}_silent.mp4")

        # 1. Pre-build audio segments for every question (Read -> 5s Tick-Tock -> Ding + Answer)
        seg_wavs = []
        timelines = []  # (q_idx, q_obj, read_dur, think_dur, ans_dur)

        timer_wav = os.path.join(AUDIO_DIR, f"{job_id}_timer5s.wav")
        think_dur = 5.0
        build_timer_and_ding_wav(think_dur, timer_wav)

        for idx, q_obj in enumerate(questions):
            job["progress"] = 5 + int(((idx + 1) / total_q) * 25)
            job["message"] = f"Recording Voiceover for Question {idx + 1}/{total_q}..."

            q_wav = os.path.join(AUDIO_DIR, f"{job_id}_q_{idx}.wav")
            read_dur = generate_tts_wav(q_obj["q"], lang_mode, is_female, q_wav)

            ans_text = f"The correct answer is {q_obj['opts'][q_obj['ans']]}."
            ans_raw_wav = os.path.join(AUDIO_DIR, f"{job_id}_araw_{idx}.wav")
            ans_ding_wav = os.path.join(AUDIO_DIR, f"{job_id}_ading_{idx}.wav")
            ans_dur = generate_tts_wav(ans_text, lang_mode, is_female, ans_raw_wav)
            prepend_ding_to_wav(ans_raw_wav, ans_ding_wav)

            seg_wavs.extend([q_wav, timer_wav, ans_ding_wav])
            timelines.append((idx, q_obj, read_dur, think_dur, ans_dur))

        master_wav = os.path.join(AUDIO_DIR, f"{job_id}_master.wav")
        concat_txt = os.path.join(AUDIO_DIR, f"{job_id}_list.txt")
        with open(concat_txt, "w") as cf:
            for sw in seg_wavs:
                cf.write(f"file '{sw}'\n")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", master_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )

        # 2. Render synchronized 24 FPS Quiz Video
        ffmpeg_vid_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", master_wav,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_vid_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        for idx, q_obj, read_dur, think_dur, ans_dur in timelines:
            for phase, p_dur in [("read", read_dur), ("think", think_dur), ("reveal", ans_dur)]:
                n_frames = max(1, int(p_dur * fps))
                for f in range(n_frames):
                    prog = f / max(1, n_frames - 1)
                    raw_bytes = renderer.render_frame(idx, q_obj, phase, prog, global_t)
                    proc.stdin.write(raw_bytes)
                    global_t += dt

            job["progress"] = 32 + int(((idx + 1) / total_q) * 65)
            job["message"] = f"Rendering Quiz Animation {idx + 1}/{total_q}..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! {total_q}-Question Quiz Video Ready ({out_name})"

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
    lang_mode = data.get("lang_mode", "ENGLISH")
    is_female = (data.get("voice_gender", "FEMALE") == "FEMALE")
    q_count = int(data.get("q_count", "5"))
    shuffle_mode = bool(data.get("shuffle", True))
    custom_raw = data.get("custom_raw", "")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing Interactive Quiz Studio..."}
    threading.Thread(
        target=run_quiz_job,
        args=(job_id, ratio, lang_mode, is_female, q_count, shuffle_mode, custom_raw),
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
