import os
import re
import time
import math
import uuid
import wave
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
# 1. EMOTIONAL VOICEOVER + 432Hz AMBIENT SYNTH & HEARTBEAT BASS MIXER
# ===========================================================================
VOICE_MAP = {
    "MALE_HINDI": "hi-IN-MadhurNeural",
    "FEMALE_HINDI": "hi-IN-SwaraNeural",
    "MALE_ENGLISH": "en-IN-PrabhatNeural",
    "FEMALE_ENGLISH": "en-IN-NeerjaNeural"
}

async def _tts_save(text: str, voice_id: str, mp3_path: str):
    comm = edge_tts.Communicate(text, voice_id, rate="-4%", pitch="-2Hz")
    await comm.save(mp3_path)

def generate_tts_wav(text: str, is_female: bool, lang_mode: str, out_wav: str) -> float:
    clean_txt = re.sub(r"[^a-zA-Z0-9\s,?!.'\-]", "", text).strip() or "Listen carefully"
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
        dur = max(2.2, len(clean_txt.split()) * 0.40)
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

def mix_emotional_soundscape(voice_wav: str, universe: str, out_mixed_wav: str, fps: int) -> list:
    with wave.open(voice_wav, "rb") as wf:
        rate = wf.getframerate()
        voice = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    n = len(voice)
    t = np.linspace(0, n / float(rate), n, endpoint=False, dtype=np.float32)

    if universe == "crimson_heart":
        f1, f2, f3 = 108.0, 162.0, 256.0
    elif universe == "quantum_cyan":
        f1, f2, f3 = 128.0, 192.0, 288.0
    else:
        f1, f2, f3 = 114.0, 171.0, 216.0

    lfo = 0.75 + 0.25 * np.sin(2 * np.pi * 0.18 * t)
    pad = (
        0.45 * np.sin(2 * np.pi * f1 * t) +
        0.30 * np.sin(2 * np.pi * f2 * t + 0.8) +
        0.25 * np.sin(2 * np.pi * f3 * t + 1.6)
    ) * lfo * 0.11

    beat_phase = (t % 1.1)
    hb1 = np.exp(-beat_phase * 28.0) * np.sin(2 * np.pi * 55.0 * beat_phase)
    hb2 = np.exp(-np.maximum(0.0, beat_phase - 0.18) * 32.0) * np.sin(2 * np.pi * 48.0 * np.maximum(0.0, beat_phase - 0.18)) * (beat_phase > 0.18)
    heartbeat = (hb1 + 0.7 * hb2) * 0.14

    fade_len = min(int(0.4 * rate), n // 4)
    if fade_len > 0:
        pad[:fade_len] *= np.linspace(0, 1, fade_len)
        pad[-fade_len:] *= np.linspace(1, 0, fade_len)

    mixed = np.clip(voice * 1.05 + pad + heartbeat, -0.96, 0.96)
    pcm = (mixed * 32767.0).astype(np.int16)
    with wave.open(out_mixed_wav, "wb") as wf_out:
        wf_out.setnchannels(1)
        wf_out.setsampwidth(2)
        wf_out.setframerate(rate)
        wf_out.writeframes(pcm.tobytes())

    step = max(1, int(rate / fps))
    env, prev = [], 0.0
    for i in range(0, n, step):
        chunk = voice[i:i + step]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        val = min(1.0, max(0.0, (rms - 0.012) * 9.5))
        cur = prev * 0.35 + val * 0.65
        env.append(cur)
        prev = cur
    return env if env else [0.0]


# ===========================================================================
# 2. ULTRA-FAST QUANTUM 3D PARTICLE & RIBBON ENGINE (PRE-CACHED GLOW SPRITES)
# ===========================================================================
class QuantumVisualEngine:
    def __init__(self, w: int, h: int, universe: str):
        self.w = w
        self.h = h
        self.universe = universe
        self.cx = int(w * 0.50)
        self.cy = int(h * 0.42)

        if universe == "crimson_heart":
            self.bg_top = (16, 4, 12)
            self.bg_bot = (4, 2, 6)
            self.c_primary = (244, 63, 94)
            self.c_secondary = (251, 146, 60)
            self.c_core = (255, 228, 230)
        elif universe == "quantum_cyan":
            self.bg_top = (4, 16, 28)
            self.bg_bot = (2, 5, 12)
            self.c_primary = (34, 211, 238)
            self.c_secondary = (168, 85, 247)
            self.c_core = (224, 242, 254)
        else:
            self.bg_top = (18, 13, 6)
            self.bg_bot = (5, 4, 2)
            self.c_primary = (250, 204, 21)
            self.c_secondary = (56, 189, 248)
            self.c_core = (254, 249, 195)

        grad = np.zeros((h, w, 3), dtype=np.uint8)
        for ch in range(3):
            col_line = np.linspace(self.bg_top[ch], self.bg_bot[ch], h, dtype=np.uint8)[:, None]
            grad[:, :, ch] = np.repeat(col_line, w, axis=1)
        self.base_bg = Image.fromarray(grad, mode="RGB").convert("RGBA")

        # Pre-render the glowing core sprite ONCE so we never run slow blur per frame!
        glow_sz = int(min(w, h) * 0.55)
        g_img = Image.new("RGBA", (glow_sz, glow_sz), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g_img)
        gc = glow_sz // 2
        gd.ellipse([gc - int(glow_sz * 0.34), gc - int(glow_sz * 0.34), gc + int(glow_sz * 0.34), gc + int(glow_sz * 0.34)], fill=(*self.c_primary, 110))
        gd.ellipse([gc - int(glow_sz * 0.16), gc - int(glow_sz * 0.16), gc + int(glow_sz * 0.16), gc + int(glow_sz * 0.16)], fill=(*self.c_core, 235))
        self.glow_sprite = g_img.filter(ImageFilter.GaussianBlur(radius=14))

        # 180 3D Quantum Stardust Particles (fast & silky smooth on free CPU)
        rng = np.random.default_rng(1337)
        self.num_pts = 180
        self.theta = rng.uniform(0, 2 * np.pi, self.num_pts)
        self.phi = rng.uniform(0, np.pi, self.num_pts)
        self.radius = rng.uniform(0.30, 1.05, self.num_pts)
        self.speed = rng.uniform(0.45, 1.35, self.num_pts)
        self.sizes = rng.uniform(1.4, 3.8, self.num_pts)

        # Precompute unit circle angles for 3D ribbon
        self.u_arr = np.linspace(0, 2.0 * np.pi, 76, endpoint=True)

    def render_frame(self, t: float, audio_energy: float, words: list, prog: float) -> bytes:
        scale = min(self.w, self.h) * 0.36
        pulse = 1.0 + audio_energy * 0.36 + math.sin(t * 5.5) * 0.05

        frame = self.base_bg.copy()

        # Paste pre-rendered glowing core with dynamic audio pulse scaling
        core_scale = 0.85 + audio_energy * 0.45
        gs_w = max(20, int(self.glow_sprite.width * core_scale))
        gs_h = max(20, int(self.glow_sprite.height * core_scale))
        scaled_glow = self.glow_sprite.resize((gs_w, gs_h), Image.Resampling.BILINEAR)
        frame.alpha_composite(scaled_glow, (self.cx - gs_w // 2, self.cy - gs_h // 2))

        draw = ImageDraw.Draw(frame)

        # 1. Vectorized 3D Torus-Knot & Harmonic Ribbons (3 interlocking trails for silky motion)
        for trail_idx, dt_off in enumerate((0.12, 0.06, 0.0)):
            tt = max(0.0, t - dt_off)
            rot_y = tt * 0.65
            rot_x = math.sin(tt * 0.42) * 0.55
            cos_ry, sin_ry = math.cos(rot_y), math.sin(rot_y)
            cos_rx, sin_rx = math.cos(rot_x), math.sin(rot_x)
            freq_mod = 3.0 + audio_energy * 2.0

            r_wave = (0.72 + 0.24 * np.sin(freq_mod * self.u_arr + tt * 3.2) * (0.4 + audio_energy)) * pulse
            x3 = r_wave * np.cos(self.u_arr)
            y3 = r_wave * np.sin(self.u_arr)
            z3 = 0.38 * np.sin(4.0 * self.u_arr - tt * 2.6) * (0.5 + audio_energy)

            xr = x3 * cos_ry + z3 * sin_ry
            zr = -x3 * sin_ry + z3 * cos_ry
            yr = y3 * cos_rx - zr * sin_rx
            zf = y3 * sin_rx + zr * cos_rx

            persp = 2.4 / (2.4 + zf)
            sx = self.cx + xr * scale * persp
            sy = self.cy + yr * scale * persp
            pts1 = list(zip(sx.tolist(), sy.tolist()))

            sx2 = self.cx - xr * scale * 0.72 * persp
            sy2 = self.cy + yr * scale * 0.72 * persp
            pts2 = list(zip(sx2.tolist(), sy2.tolist()))

            al_1 = 60 + trail_idx * 70
            al_2 = 50 + trail_idx * 60
            draw.line(pts1, fill=(*self.c_primary, al_1), width=2 + trail_idx)
            draw.line(pts2, fill=(*self.c_secondary, al_2), width=1 + trail_idx)

        # 2. Audio-Reactive Shockwave Rings
        for ring_i in range(3):
            ring_phase = ((t * 0.85 + ring_i * 0.33) % 1.0)
            rr = scale * (0.25 + ring_phase * 1.15) * (1.0 + audio_energy * 0.18)
            alpha = int((1.0 - ring_phase) * (95 + audio_energy * 110))
            if alpha > 6:
                draw.ellipse(
                    [self.cx - rr, self.cy - rr, self.cx + rr, self.cy + rr],
                    outline=(*self.c_primary, min(255, alpha)),
                    width=2
                )

        # 3. 3D Quantum Stardust Particle Field
        ang_t = self.theta + t * self.speed * (0.55 + audio_energy * 0.45)
        r_cur = self.radius * scale * (0.88 + audio_energy * 0.32)
        px_3d = r_cur * np.sin(self.phi) * np.cos(ang_t)
        py_3d = r_cur * np.cos(self.phi) * 0.85
        pz_3d = r_cur * np.sin(self.phi) * np.sin(ang_t)

        persp_p = 420.0 / (420.0 + pz_3d)
        sx_p = self.cx + px_3d * persp_p
        sy_p = self.cy + py_3d * persp_p

        for idx in range(self.num_pts):
            if pz_3d[idx] > -260:
                sz = self.sizes[idx] * persp_p[idx] * (1.0 + audio_energy * 0.42)
                x_p, y_p = float(sx_p[idx]), float(sy_p[idx])
                if 0 <= x_p < self.w and 0 <= y_p < self.h:
                    al = int(min(245, max(55, 175 * persp_p[idx])))
                    col = self.c_core if (idx % 5 == 0) else (self.c_primary if (idx % 2 == 0) else self.c_secondary)
                    draw.ellipse([x_p - sz, y_p - sz, x_p + sz, y_p + sz], fill=(*col, al))

        # 4. Hypnotic Word-by-Word Kinetic Typography
        if words:
            active_idx = min(len(words) - 1, int(prog * len(words)))
            font_size = max(23, int(self.w * 0.046)) if self.h > self.w else max(25, int(self.w * 0.028))
            font = get_font(font_size)
            space_w = int(font_size * 0.36)
            max_line_w = int(self.w * 0.84)

            lines, curr_line, curr_w = [], [], 0
            for idx, wd in enumerate(words):
                bbox = draw.textbbox((0, 0), wd, font=font)
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

            line_h = int(font_size * 1.38)
            top_y = int(self.h * 0.82) - (len(lines) * line_h) // 2

            for l_idx, tokens in enumerate(lines):
                row_w = sum(tk[2] for tk in tokens) + space_w * (len(tokens) - 1)
                cx_t = (self.w - row_w) // 2
                cy_t = top_y + l_idx * line_h
                for wd, idx, ww in tokens:
                    if idx == active_idx:
                        col = self.c_primary
                    elif idx < active_idx:
                        col = (255, 255, 255)
                    else:
                        col = (148, 163, 184)
                    draw.text((cx_t, cy_t), wd, font=font, fill=(*col, 255), stroke_width=3, stroke_fill=(4, 6, 12, 255))
                    cx_t += ww + space_w

        return frame.convert("RGB").tobytes()


# ===========================================================================
# 3. SCRIPT PARSER & RENDER PIPELINE
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

def parse_story_beats(raw_script: str):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip()]
    beats = []
    for ln in lines:
        if re.match(r"^SCENE\s*\d*", ln, re.I):
            continue
        tags = []
        caption = re.sub(r"\[([^\]]+)\]", lambda m: (tags.append(m.group(1).strip()), "")[1], ln).strip()
        # Keep clean Latin/Hinglish letters so font & TTS never stall
        caption = re.sub(r"[^\w\s,?!.'\-]", "", caption).strip()
        if not caption:
            continue
        speaker = tags[0] if tags else "Narrator"
        is_female = bool(re.search(r"(girl|meera|ananya|priya|aditi|neha|sara|riya|female)", speaker.lower()))
        beats.append({
            "caption": caption,
            "words": caption.split(),
            "is_female": is_female
        })
    return beats

def run_quantum_job(job_id: str, script_text: str, ratio: str, universe: str,
                    voice_mode: str, lang_mode: str, custom_wav_path):
    job = JOBS[job_id]
    try:
        beats = parse_story_beats(script_text)
        if not beats:
            job["status"] = "error"
            job["message"] = "Please write or paste your story lines first!"
            return

        out_w, out_h = (540, 960) if ratio == "9:16" else (960, 540)
        fps = 20
        total = len(beats)

        job["message"] = "Synthesizing Emotional Voiceover & 432Hz Soundscape..."
        job["progress"] = 10

        raw_wav = os.path.join(AUDIO_DIR, f"{job_id}_raw.wav")
        mixed_wav = os.path.join(AUDIO_DIR, f"{job_id}_mixed.wav")
        beat_durations = []

        if voice_mode == "manual" and custom_wav_path and os.path.exists(custom_wav_path):
            total_audio_dur = convert_custom_audio_to_wav(custom_wav_path, raw_wav)
            total_words = sum(max(1, len(b["words"])) for b in beats)
            for b in beats:
                share = max(1, len(b["words"])) / float(total_words)
                beat_durations.append(max(1.8, total_audio_dur * share))
        else:
            seg_wavs = []
            for idx, b in enumerate(beats):
                seg_path = os.path.join(AUDIO_DIR, f"{job_id}_seg_{idx}.wav")
                dur = generate_tts_wav(b["caption"], b["is_female"], lang_mode, seg_path)
                beat_durations.append(dur)
                seg_wavs.append(seg_path)
                job["progress"] = 10 + int(((idx + 1) / total) * 20)

            concat_txt = os.path.join(AUDIO_DIR, f"{job_id}_list.txt")
            with open(concat_txt, "w") as cf:
                for sw in seg_wavs:
                    cf.write(f"file '{sw}'\n")
            subprocess.run(
                [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", raw_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )

        energy_env = mix_emotional_soundscape(raw_wav, universe, mixed_wav, fps)

        engine = QuantumVisualEngine(out_w, out_h, universe)
        out_name = f"NEXORA_QUANTUM_{universe.upper()}_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-i", mixed_wav,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "21",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        frame_cursor = 0
        for i, b in enumerate(beats):
            dur = beat_durations[i]
            n_frames = max(1, int(dur * fps))
            for f in range(n_frames):
                prog = f / max(1, n_frames - 1)
                energy = energy_env[frame_cursor] if frame_cursor < len(energy_env) else 0.0
                raw_bytes = engine.render_frame(global_t, energy, b["words"], prog)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps
                frame_cursor += 1

            job["progress"] = 32 + int(((i + 1) / total) * 65)
            job["message"] = f"Rendering Quantum Physics Beat {i + 1}/{total}..."

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
    custom_wav_path = None

    if request.content_type and "multipart/form-data" in request.content_type:
        script_text = request.form.get("script", "")
        ratio = request.form.get("ratio", "9:16")
        universe = request.form.get("universe", "golden_soul")
        voice_mode = request.form.get("voice_mode", "auto")
        lang_mode = request.form.get("lang_mode", "HINDI")
        if "voice_file" in request.files and request.files["voice_file"].filename:
            vf = request.files["voice_file"]
            custom_wav_path = os.path.join(AUDIO_DIR, f"{job_id}_voice_{vf.filename}")
            vf.save(custom_wav_path)
    else:
        data = request.get_json(force=True)
        script_text = data.get("script", "")
        ratio = data.get("ratio", "9:16")
        universe = data.get("universe", "golden_soul")
        voice_mode = data.get("voice_mode", "auto")
        lang_mode = data.get("lang_mode", "HINDI")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing Fast 3D Quantum Engine..."}
    threading.Thread(
        target=run_quantum_job,
        args=(job_id, script_text, ratio, universe, voice_mode, lang_mode, custom_wav_path),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    return jsonify(JOBS.get(job_id, {"status": "error", "message": "Server restarted — please click Render again!"}))

@app.route("/api/video/<job_id>")
def api_video(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("file_path") or not os.path.exists(job["file_path"]):
        return "Not ready", 404
    return send_file(job["file_path"], as_attachment=(request.args.get("dl") == "1"), mimetype="video/mp4")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
