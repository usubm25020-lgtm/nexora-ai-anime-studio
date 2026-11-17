import os
import re
import time
import math
import uuid
import wave
import random
import urllib.parse
import urllib.request
import asyncio
import subprocess
import threading
import numpy as np

import edge_tts
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
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
IMG_DIR = os.path.join(BASE_DIR, "scene_images")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
JOBS = {}

# ===========================================================================
# 1. DEEP HORROR NARRATOR VOICE + THUNDER, RAIN & DARK DRONE MIXER
# ===========================================================================
VOICE_MAP = {
    "MALE_HINDI": "hi-IN-MadhurNeural",
    "FEMALE_HINDI": "hi-IN-SwaraNeural",
    "MALE_ENGLISH": "en-US-ChristopherNeural",
    "FEMALE_ENGLISH": "en-US-AriaNeural"
}

async def _tts_save(text: str, voice_id: str, mp3_path: str):
    # Deeper pitch & slightly slower suspenseful pacing for horror narration
    comm = edge_tts.Communicate(text, voice_id, rate="-8%", pitch="-6Hz")
    await comm.save(mp3_path)

def generate_horror_tts(text: str, lang_mode: str, is_female: bool, out_wav: str) -> float:
    clean_txt = re.sub(r"[^a-zA-Z0-9\s,?!.'\-]", "", text).strip() or "Something is watching in the dark."
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
        dur = max(3.0, len(clean_txt.split()) * 0.45)
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", str(dur), out_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    with wave.open(out_wav, "rb") as wf:
        return max(2.5, wf.getnframes() / float(wf.getframerate()))

def convert_custom_audio_to_wav(src_path: str, out_wav: str) -> float:
    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", src_path, "-ar", "22050", "-ac", "1", out_wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )
    with wave.open(out_wav, "rb") as wf:
        return max(2.5, wf.getnframes() / float(wf.getframerate()))

def mix_horror_atmosphere(voice_wav: str, thunder_times: list, out_mixed_wav: str):
    """Synthesizes eerie dark tension drone, steady midnight rain, and thunder rumbles."""
    with wave.open(voice_wav, "rb") as wf:
        rate = wf.getframerate()
        voice = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    n = len(voice)
    t = np.linspace(0, n / float(rate), n, endpoint=False, dtype=np.float32)

    # Eerie dissonant horror drone (tritone + low sub-bass)
    lfo = 0.70 + 0.30 * np.sin(2.0 * np.pi * 0.14 * t)
    drone = (
        0.45 * np.sin(2.0 * np.pi * 55.0 * t) +
        0.30 * np.sin(2.0 * np.pi * 77.78 * t + 0.6) +  # Eerie tritone tension
        0.25 * np.sin(2.0 * np.pi * 110.0 * t + 1.2)
    ) * lfo * 0.14

    # Soft midnight rain texture
    rng = np.random.default_rng(666)
    noise = rng.normal(0, 1, n).astype(np.float32)
    kernel = np.ones(14, dtype=np.float32) / 14.0
    rain = np.convolve(noise, kernel, mode="same") * 0.065

    # Thunder rumbles synced with lightning flashes
    thunder_track = np.zeros(n, dtype=np.float32)
    for tt in thunder_times:
        s_idx = int(tt * rate)
        dur_s = int(2.2 * rate)
        if s_idx + dur_s < n:
            t_th = np.linspace(0, 2.2, dur_s, endpoint=False, dtype=np.float32)
            env = np.exp(-t_th * 1.9) * (1.0 - np.exp(-t_th * 18.0))
            rumble = np.convolve(rng.normal(0, 1, dur_s).astype(np.float32), np.ones(28, dtype=np.float32) / 28.0, mode="same")
            sub_boom = np.sin(2.0 * np.pi * 42.0 * t_th) * 0.45
            thunder_track[s_idx:s_idx + dur_s] += (rumble * 1.4 + sub_boom) * env * 0.45

    mixed = np.clip(voice * 1.08 + drone + rain + thunder_track, -0.96, 0.96)
    pcm = (mixed * 32767.0).astype(np.int16)
    with wave.open(out_mixed_wav, "wb") as wf_out:
        wf_out.setnchannels(1)
        wf_out.setsampwidth(2)
        wf_out.setframerate(rate)
        wf_out.writeframes(pcm.tobytes())


# ===========================================================================
# 2. CINEMATIC HORROR SCENE IMAGE GENERATOR (AI FETCH + PROCEDURAL FALLBACK)
# ===========================================================================
def build_procedural_horror_fallback(w: int, h: int, seed: int) -> Image.Image:
    """Creates an atmospheric dark foggy forest / haunted silhouette scene if AI image endpoint times out."""
    rng = random.Random(seed)
    grad = np.zeros((h, w, 3), dtype=np.uint8)
    top_c, bot_c = (14, 20, 32), (4, 6, 10)
    for ch in range(3):
        col_line = np.linspace(top_c[ch], bot_c[ch], h, dtype=np.uint8)[:, None]
        grad[:, :, ch] = np.repeat(col_line, w, axis=1)
    img = Image.fromarray(grad, mode="RGB").convert("RGBA")
    d = ImageDraw.Draw(img)

    # Eerie pale full moon behind fog
    mx, my = int(w * rng.uniform(0.3, 0.7)), int(h * 0.28)
    mr = int(min(w, h) * 0.14)
    d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(203, 213, 225, 95))

    # Twisted dead trees & haunted cabin silhouette
    ground_y = int(h * 0.78)
    for _ in range(9):
        tx = rng.randint(0, w)
        tw = rng.randint(8, 22)
        th = rng.randint(int(h * 0.35), int(h * 0.65))
        d.polygon([(tx - tw, ground_y), (tx - tw // 3, ground_y - th), (tx + tw // 3, ground_y - th), (tx + tw, ground_y)], fill=(6, 8, 12, 240))

    # Distant glowing eerie eyes in the dark
    ex, ey = int(w * 0.50), int(h * 0.64)
    d.ellipse([ex - 14, ey - 3, ex - 8, ey + 3], fill=(244, 63, 94, 210))
    d.ellipse([ex + 8, ey - 3, ex + 14, ey + 3], fill=(244, 63, 94, 210))

    d.rectangle([0, ground_y, w, h], fill=(5, 7, 10, 255))
    return img.convert("RGB")

def fetch_horror_scene_image(prompt_text: str, w: int, h: int, seed: int, out_path: str) -> Image.Image:
    """Fetches a dark cinematic horror illustration with a strict 10-second timeout + instant fallback."""
    full_prompt = (
        f"dark cinematic horror movie still, eerie midnight atmosphere, fog, moonlight, "
        f"photorealistic, moody shadows, {prompt_text[:120]}, 8k, no text, no watermark"
    )
    encoded = urllib.parse.quote(full_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width={w}&height={h}&seed={seed}&nologo=true"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
            with open(out_path, "wb") as f:
                f.write(data)
        img = Image.open(out_path).convert("RGB").resize((w, h), Image.Resampling.LANCZOS)
        # Enhance dark contrast for moody horror look
        img = ImageEnhance.Contrast(img).enhance(1.18)
        img = ImageEnhance.Brightness(img).enhance(0.85)
        return img
    except Exception:
        return build_procedural_horror_fallback(w, h, seed)


# ===========================================================================
# 3. 3D KEN BURNS PARALLAX, RAIN, LIGHTNING & SUBTITLE RENDERER
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

def render_horror_frame(scene_img: Image.Image, out_w: int, out_h: int, prog: float,
                        global_t: float, words: list, zoom_in: bool, lightning_alpha: float) -> bytes:
    # Smooth Ken Burns Camera Zoom & Pan (creates depth from 2D cinematic art)
    z = (1.0 + 0.14 * prog) if zoom_in else (1.14 - 0.14 * prog)
    cw = int(out_w / z)
    ch = int(out_h / z)
    pan_x = int((out_w - cw) * (0.35 + 0.30 * prog))
    pan_y = int((out_h - ch) * (0.45 + 0.10 * math.sin(prog * math.pi)))

    cropped = scene_img.crop((pan_x, pan_y, pan_x + cw, pan_y + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR).convert("RGBA")
    draw = ImageDraw.Draw(cropped)

    # Midnight falling rain streaks
    for i in range(45):
        rx = int((i * 89 + global_t * 160) % (out_w + 60)) - 30
        ry = int((i * 113 + global_t * 780) % (out_h + 40)) - 20
        draw.line([(rx, ry), (rx - 7, ry + 20)], fill=(186, 230, 253, 95), width=1)

    # Lightning flash overlay
    if lightning_alpha > 0.04:
        flash = Image.new("RGBA", (out_w, out_h), (226, 232, 240, int(165 * lightning_alpha)))
        cropped = Image.alpha_composite(cropped, flash)
        draw = ImageDraw.Draw(cropped)

    # Bottom dark gradient scrim for crisp readable horror captions
    scrim_h = int(out_h * 0.28)
    scrim = Image.new("RGBA", (out_w, scrim_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    for row in range(scrim_h):
        al = int((row / float(scrim_h)) * 215)
        sdraw.line([(0, row), (out_w, row)], fill=(4, 6, 10, al))
    cropped.alpha_composite(scrim, (0, out_h - scrim_h))

    # Word-by-word highlighted suspense captions
    if words:
        draw_t = ImageDraw.Draw(cropped)
        active_idx = min(len(words) - 1, int(prog * len(words)))
        font_size = max(21, int(out_w * 0.044)) if out_h > out_w else max(23, int(out_w * 0.025))
        font = get_font(font_size)
        space_w = int(font_size * 0.35)
        max_line_w = int(out_w * 0.86)

        lines, curr_line, curr_w = [], [], 0
        for idx, wd in enumerate(words):
            bbox = draw_t.textbbox((0, 0), wd, font=font)
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

        line_h = int(font_size * 1.35)
        top_y = int(out_h * 0.86) - (len(lines) * line_h) // 2

        for l_idx, tokens in enumerate(lines):
            row_w = sum(tk[2] for tk in tokens) + space_w * (len(tokens) - 1)
            cx_t = (out_w - row_w) // 2
            cy_t = top_y + l_idx * line_h
            for wd, idx, ww in tokens:
                col = (244, 63, 94) if (idx == active_idx) else (241, 245, 249)
                draw_t.text((cx_t, cy_t), wd, font=font, fill=(*col, 255), stroke_width=3, stroke_fill=(0, 0, 0, 255))
                cx_t += ww + space_w

    return cropped.convert("RGB").tobytes()


# ===========================================================================
# 4. HORROR STORY PARSER & PIPELINE
# ===========================================================================
def parse_horror_scenes(raw_story: str):
    lines = [ln.strip() for ln in str(raw_story or "").splitlines() if ln.strip()]
    scenes = []
    for ln in lines:
        tags = []
        caption = re.sub(r"\[([^\]]+)\]", lambda m: (tags.append(m.group(1).strip()), "")[1], ln).strip()
        caption = re.sub(r"[^\w\s,?!.'\-]", "", caption).strip()
        if not caption:
            continue
        is_female = any("female" in t.lower() or "girl" in t.lower() for t in tags)
        scenes.append({
            "caption": caption,
            "words": caption.split(),
            "is_female": is_female
        })
    return scenes

def run_horror_job(job_id: str, story_text: str, ratio: str, voice_mode: str, lang_mode: str, custom_wav_path):
    job = JOBS[job_id]
    try:
        scenes = parse_horror_scenes(story_text)
        if not scenes:
            job["status"] = "error"
            job["message"] = "Please write or paste your horror/mystery story paragraphs first!"
            return

        out_w, out_h = (960, 540) if ratio == "16:9" else (540, 960)
        fps = 24
        total = len(scenes)

        job["message"] = "Recording Deep Horror Narration & Thunder Audio..."
        job["progress"] = 8

        raw_wav = os.path.join(AUDIO_DIR, f"{job_id}_voice.wav")
        final_wav = os.path.join(AUDIO_DIR, f"{job_id}_horror_mix.wav")
        scene_durations = []

        if voice_mode == "manual" and custom_wav_path and os.path.exists(custom_wav_path):
            total_audio_dur = convert_custom_audio_to_wav(custom_wav_path, raw_wav)
            total_words = sum(max(1, len(s["words"])) for s in scenes)
            for s in scenes:
                share = max(1, len(s["words"])) / float(total_words)
                scene_durations.append(max(2.5, total_audio_dur * share))
        else:
            seg_wavs = []
            for idx, s in enumerate(scenes):
                seg_path = os.path.join(AUDIO_DIR, f"{job_id}_seg_{idx}.wav")
                dur = generate_horror_tts(s["caption"], lang_mode, s["is_female"], seg_path)
                scene_durations.append(dur)
                seg_wavs.append(seg_path)
                job["progress"] = 8 + int(((idx + 1) / total) * 18)

            concat_txt = os.path.join(AUDIO_DIR, f"{job_id}_list.txt")
            with open(concat_txt, "w") as cf:
                for sw in seg_wavs:
                    cf.write(f"file '{sw}'\n")
            subprocess.run(
                [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", raw_wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )

        # Schedule dramatic lightning & thunder strikes at key scene transitions
        thunder_times = []
        t_acc = 0.0
        for i, d in enumerate(scene_durations):
            if i % 2 == 1:
                thunder_times.append(t_acc + 0.35)
            t_acc += d

        mix_horror_atmosphere(raw_wav, thunder_times, final_wav)

        # Generate / fetch dark cinematic artwork for each scene
        scene_images = []
        for idx, s in enumerate(scenes):
            job["message"] = f"Generating Dark Horror Artwork ({idx + 1}/{total})..."
            job["progress"] = 28 + int(((idx + 1) / total) * 28)
            img_path = os.path.join(IMG_DIR, f"{job_id}_sc_{idx}.jpg")
            img = fetch_horror_scene_image(s["caption"], out_w, out_h, seed=1000 + idx * 37, out_path=img_path)
            scene_images.append(img)

        out_name = f"NEXORA_HORROR_STORY_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
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
        for i, s in enumerate(scenes):
            dur = scene_durations[i]
            n_frames = max(1, int(dur * fps))
            img = scene_images[i]
            zoom_in = (i % 2 == 0)

            for f in range(n_frames):
                prog = f / max(1, n_frames - 1)
                # Check if lightning flash is active right now
                l_alpha = 0.0
                for tt in thunder_times:
                    dt_l = global_t - tt
                    if 0.0 <= dt_l <= 0.32:
                        l_alpha = max(l_alpha, 1.0 - (dt_l / 0.32))

                raw_bytes = render_horror_frame(img, out_w, out_h, prog, global_t, s["words"], zoom_in, l_alpha)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps

            job["progress"] = 58 + int(((i + 1) / total) * 40)
            job["message"] = f"Rendering Horror Scene {i + 1}/{total}..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Horror Video Ready ({out_name})"

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
        story_text = request.form.get("story", "")
        ratio = request.form.get("ratio", "16:9")
        voice_mode = request.form.get("voice_mode", "auto")
        lang_mode = request.form.get("lang_mode", "HINDI")
        if "voice_file" in request.files and request.files["voice_file"].filename:
            vf = request.files["voice_file"]
            custom_wav_path = os.path.join(AUDIO_DIR, f"{job_id}_voice_{vf.filename}")
            vf.save(custom_wav_path)
    else:
        data = request.get_json(force=True)
        story_text = data.get("story", "")
        ratio = data.get("ratio", "16:9")
        voice_mode = data.get("voice_mode", "auto")
        lang_mode = data.get("lang_mode", "HINDI")

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Initializing Dark Horror Story Studio..."}
    threading.Thread(
        target=run_horror_job,
        args=(job_id, story_text, ratio, voice_mode, lang_mode, custom_wav_path),
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
