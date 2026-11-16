import os
import re
import time
import math
import uuid
import subprocess
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
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
CACHE_DIR = os.path.join(BASE_DIR, "ai_cache")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
JOBS = {}

# ===========================================================================
# 1. AI STORY DIRECTOR & CHARACTER CONSISTENCY ENGINE
# ===========================================================================
STYLES = {
    "golu_yash": (
        "3D stylized semi-realistic anime manhwa art, Golu Yash viral reels aesthetic, "
        "expressive cinematic lighting, subsurface skin glow, sharp detailed jewel eyes, "
        "dramatic rim light, volumetric atmosphere, depth of field bokeh, 8k masterpiece"
    ),
    "manhwa_pro": (
        "Masterpiece Korean manhwa webtoon illustration, crisp ink lineart, "
        "soft airbrush shading, glowing eyes, cinematic framing, 8k resolution"
    ),
    "shinkai": (
        "Makoto Shinkai anime movie still, breathtaking scenic sky, volumetric sunbeams, "
        "lens flare, ultra detailed background and emotional character art"
    ),
    "dark_horror": (
        "Dark supernatural Indian horror anime style, eerie moonlight, glowing red eyes, "
        "misty shadows, dramatic high-contrast rim lighting, thriller anime frame"
    )
}

def build_storyboard(raw_script: str, style_key: str = "golu_yash", wps: float = 2.6):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip()]
    beats = []
    current_scene = "moody cinematic Indian city street at night with glowing lights"
    cast_bible = {}
    style_prompt = STYLES.get(style_key, STYLES["golu_yash"])

    for ln in lines:
        if re.match(r"^SCENE\s*\d*", ln, re.I):
            loc = re.sub(r"^SCENE\s*\d*\s*[:\-]?\s*", "", ln, flags=re.I).strip()
            if loc:
                current_scene = loc
            continue

        tags = []
        caption = re.sub(r"\[([^\]]+)\]", lambda m: (tags.append(m.group(1).strip()), "")[1], ln).strip()
        if not caption:
            continue

        speaker = "Narrator"
        camera = "MEDIUM"
        emotion = "NEUTRAL"
        action = "expressive pose"

        for tg in tags:
            u = tg.upper()
            if u in {"WIDE", "MEDIUM", "CLOSEUP", "OTS", "ORBIT"}:
                camera = u
            elif u in {"HAPPY", "SAD", "SHOCK", "ROMANTIC", "CONFUSED", "FEAR", "CHILL", "ANGRY", "HEROIC"}:
                emotion = u
            elif u in {"WALK", "RUN", "HUG", "FIGHT", "DANCE", "PROPOSE", "SIT", "JUMP", "WAVE", "LOOK"}:
                action = u.lower()
            else:
                speaker = tg

        sp_key = speaker.upper()
        if sp_key != "NARRATOR" and sp_key not in cast_bible:
            low_name = speaker.lower()
            if re.search(r"(bhoot|sakchunni|witch|ghost|demon|chudail)", low_name):
                cast_bible[sp_key] = (
                    "mystical Bengali supernatural anime woman with glowing red eyes, long flowing jet-black hair, "
                    "traditional dark saree with gold jewelry, eerie ethereal aura"
                )
            elif re.search(r"(ai|bot|robot|cyber|android|hologram)", low_name):
                cast_bible[sp_key] = (
                    "futuristic translucent cyan holographic anime AI character, glowing neon circuit eyes, sleek cyber jacket"
                )
            elif re.search(r"(girl|meera|ananya|priya|aditi|neha|sara|riya|queen|princess)", low_name):
                cast_bible[sp_key] = (
                    "gorgeous 20 year old Indian anime girl with long silky dark brown hair, wispy face-framing strands, "
                    "expressive large dark eyes, stylish modern outfit"
                )
            else:
                cast_bible[sp_key] = (
                    "handsome 21 year old Indian anime young man with messy textured black hair, sharp jawline, "
                    "expressive eyes, stylish dark streetwear hoodie"
                )

        char_desc = cast_bible.get(sp_key, "cinematic anime protagonist in dramatic lighting")

        # Automatic Visual Props & Assets from Dialogue
        low_cap = caption.lower()
        extras = []
        if any(w in low_cap for w in ["bike", "motorcycle", "scooter"]):
            extras.append("parked sleek sport motorcycle with glowing headlight beside him")
        if any(w in low_cap for w in ["khat", "letter", "chitthi", "paper"]):
            extras.append("holding an open handwritten paper letter in hands")
        if any(w in low_cap for w in ["bouquet", "flower", "rose", "phool"]):
            extras.append("holding a luxury red rose bouquet")
        if any(w in low_cap for w in ["coffee", "chai", "cup"]):
            extras.append("holding a steaming hot coffee cup")
        if any(w in low_cap for w in ["umbrella", "chhatri"]):
            extras.append("holding a transparent umbrella in the rain")
        if any(w in low_cap for w in ["phone", "mobile", "battery", "website"]):
            extras.append("looking at a glowing smartphone screen")
        if any(w in low_cap for w in ["cake", "birthday"]):
            extras.append("birthday cake with glowing candles on table")
        if any(w in low_cap for w in ["gift", "present", "box", "tohfa"]):
            extras.append("holding a luxury wrapped gift box with ribbon")

        extras_str = (", " + ", ".join(extras)) if extras else ""

        framing = {
            "WIDE": "wide cinematic establishing shot showing full environment and character",
            "MEDIUM": "cinematic medium waist-up shot with dynamic perspective",
            "CLOSEUP": "dramatic close-up portrait focusing on facial emotion and eyes",
            "OTS": "cinematic over-the-shoulder shot of two characters facing each other",
            "ORBIT": "dynamic low-angle hero shot with dramatic lighting"
        }.get(camera, "cinematic medium waist-up shot")

        prompt = (
            f"{style_prompt}, {framing}, {char_desc}, emotion: {emotion.lower()}, action: {action}{extras_str}, "
            f"story moment: {caption}, background setting: {current_scene}, "
            f"no text, no subtitles, no watermark, clean full-bleed artwork"
        )

        combo = (current_scene + " " + caption).lower()
        weather = "bokeh"
        if any(k in combo for k in ["rain", "baarish", "storm", "wet", "monsoon"]):
            weather = "rain"
        elif any(k in combo for k in ["sakura", "flower", "bouquet", "love", "romantic", "shaadi", "cafe", "sunset"]):
            weather = "petals"
        elif any(k in combo for k in ["fire", "fight", "angry", "danger", "horror", "bhoot"]):
            weather = "embers"

        words = caption.split()
        duration = max(2.2, round(len(words) / max(1.2, wps), 2))

        beats.append({
            "index": len(beats) + 1,
            "scene": current_scene,
            "speaker": "" if sp_key == "NARRATOR" else speaker,
            "camera": camera,
            "emotion": emotion,
            "weather": weather,
            "caption": caption,
            "words": words,
            "duration": duration,
            "prompt": prompt
        })

    return beats


# ===========================================================================
# 2. REAL AI ANIME ART GENERATOR (CLOUD SERVER — FAST & UNBLOCKED)
# ===========================================================================
def generate_ai_art(prompt: str, width: int, height: int, seed: int) -> Image.Image:
    gen_w = int(width * 1.16)
    gen_h = int(height * 1.16)
    encoded = urllib.parse.quote(prompt)

    urls = [
        f"https://image.pollinations.ai/prompt/{encoded}?width={gen_w}&height={gen_h}&seed={seed}&model=flux&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded}?width={gen_w}&height={gen_h}&seed={seed + 11}&model=turbo&nologo=true"
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "image/*,*/*;q=0.8"
    }

    for u in urls:
        for _ in range(3):
            try:
                req = urllib.request.Request(u, headers=headers)
                with urllib.request.urlopen(req, timeout=45) as resp:
                    raw = resp.read()
                    if len(raw) > 5000:
                        img = Image.open(BytesIO(raw)).convert("RGB")
                        img = img.resize((gen_w, gen_h), Image.Resampling.LANCZOS)
                        img = ImageEnhance.Color(img).enhance(1.16)
                        img = ImageEnhance.Contrast(img).enhance(1.08)
                        return img
            except Exception:
                time.sleep(1.0)

    fb = Image.new("RGB", (gen_w, gen_h), (8, 12, 26))
    d = ImageDraw.Draw(fb)
    for y in range(gen_h):
        t = y / max(1, gen_h)
        d.line([(0, y), (gen_w, y)], fill=(int(10 + t * 35), int(18 + t * 48), int(45 + t * 90)))
    return fb


# ===========================================================================
# 3. 2.5D MULTI-LAYER DEPTH PARALLAX + WEATHER FX + GAMING CAPTIONS
# ===========================================================================
WORD_PALETTE = [
    (34, 211, 238),   # Electric Cyan
    (251, 191, 36),   # Cyber Gold
    (251, 113, 133),  # Neon Rose
    (167, 139, 250),  # Ultra Violet
    (52, 211, 153)    # Emerald Neon
]

_FONT_CACHE = {}
def get_font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/system/fonts/Roboto-Bold.ttf"
    ]:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[size] = f
                return f
            except Exception:
                pass
    return ImageFont.load_default()

def split_depth_layers(src_img: Image.Image):
    bg_layer = src_img.filter(ImageFilter.GaussianBlur(radius=5))
    bg_layer = ImageEnhance.Brightness(bg_layer).enhance(0.88)

    w, h = src_img.size
    mask = Image.new("L", (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse([int(w * 0.08), int(h * 0.06), int(w * 0.92), int(h * 0.98)], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=28))
    return bg_layer, src_img, mask

def render_25d_frame(bg_layer: Image.Image, fg_layer: Image.Image, mask: Image.Image,
                     out_w: int, out_h: int, beat: dict, prog: float, global_t: float) -> bytes:
    sw, sh = fg_layer.size
    cam = beat["camera"]
    emo = beat["emotion"]

    if cam == "CLOSEUP":
        fg_zoom = 1.04 + prog * 0.13
    elif cam == "WIDE":
        fg_zoom = 1.15 - prog * 0.11
    else:
        fg_zoom = 1.05 + prog * 0.09

    bg_zoom = 1.01 + (fg_zoom - 1.0) * 0.45

    shake_x, shake_y = 0, 0
    if emo in {"SHOCK", "FEAR", "ANGRY"} and prog < 0.25:
        decay = 1.0 - (prog / 0.25)
        shake_x = int(math.sin(global_t * 58) * 10 * decay)
        shake_y = int(math.cos(global_t * 50) * 7 * decay)

    breath_y = math.sin(global_t * 3.2) * 5.0
    talk_pulse = abs(math.sin(global_t * 11.0)) * 3.0 if beat.get("speaker") else 0.0

    def crop_layer(img, z, pan_mult):
        cw = int(out_w / z)
        ch = int(out_h / z)
        dx = max(0, sw - cw)
        dy = max(0, sh - ch)
        pan_dir = prog if (beat["index"] % 2 == 1) else (1.0 - prog)
        x0 = int(dx * (0.5 + (pan_dir - 0.5) * pan_mult)) + shake_x
        y0 = int(dy * 0.46 + (breath_y - talk_pulse) * pan_mult) + shake_y
        x0 = max(0, min(dx, x0))
        y0 = max(0, min(dy, y0))
        return img.crop((x0, y0, x0 + cw, y0 + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR)

    bg_frame = crop_layer(bg_layer, bg_zoom, 0.35)
    fg_frame = crop_layer(fg_layer, fg_zoom, 0.85)
    mask_frame = crop_layer(mask, fg_zoom, 0.85)

    composed = Image.composite(fg_frame, bg_frame, mask_frame).convert("RGBA")
    overlay = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    weather = beat.get("weather", "bokeh")
    if weather == "rain":
        for i in range(52):
            rx = int((i * 73 + global_t * 160) % (out_w + 60)) - 30
            ry = int((i * 97 + global_t * 760) % (out_h + 40)) - 20
            draw.line([(rx, ry), (rx - 8, ry + 24)], fill=(186, 230, 253, 145), width=2)
    elif weather == "petals":
        for i in range(26):
            px = int((i * 97 + global_t * (45 + (i % 4) * 11)) % (out_w + 40)) - 20
            py = int((i * 67 + global_t * (34 + (i % 3) * 10) + math.sin(global_t * 2.2 + i) * 18) % out_h)
            col = (251, 113, 133, 195) if i % 2 == 0 else (253, 164, 175, 170)
            draw.ellipse([px - 6, py - 3, px + 6, py + 3], fill=col)
    elif weather == "embers":
        for i in range(28):
            ex = int((i * 89 + math.sin(global_t * 2 + i) * 26) % out_w)
            ey = int((out_h - ((i * 61 + global_t * 95) % out_h)))
            draw.ellipse([ex - 3, ey - 3, ex + 3, ey + 3], fill=(251, 146, 60, 190))
    else:
        for i in range(22):
            bx = int((i * 113 + math.sin(global_t + i) * 24) % out_w)
            by = int((i * 79 - global_t * 26) % out_h)
            alpha = int(75 + abs(math.sin(global_t * 2.4 + i)) * 145)
            draw.ellipse([bx - 3, by - 3, bx + 3, by + 3], fill=(56, 189, 248, alpha))

    words = beat.get("words", [])
    if words:
        is_portrait = out_h > out_w
        active_idx = min(len(words) - 1, int(prog * len(words)))
        font_size = max(24, int(out_w * 0.048)) if is_portrait else max(26, int(out_w * 0.028))
        font = get_font(font_size)
        space_w = int(font_size * 0.36)
        max_line_w = int(out_w * 0.86)

        lines = []
        curr_line = []
        curr_w = 0
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

        line_h = int(font_size * 1.34)
        total_h = len(lines) * line_h
        bottom_pad = int(out_h * 0.085)
        top_y = out_h - bottom_pad - total_h

        max_actual_w = max(sum(tk[2] for tk in ln) + space_w * (len(ln) - 1) for ln in lines)
        box_w = min(int(out_w * 0.92), max_actual_w + 36)
        box_h = total_h + 22
        box_x = (out_w - box_w) // 2
        box_y = top_y - 10

        draw.rounded_rectangle(
            [box_x, box_y, box_x + box_w, box_y + box_h],
            radius=18,
            fill=(6, 10, 22, 220),
            outline=(34, 211, 238, 205),
            width=2
        )

        speaker = beat.get("speaker", "")
        if speaker:
            b_font = get_font(max(14, int(font_size * 0.48)))
            sp_label = f"⚡ {speaker.upper()}"
            sb = draw.textbbox((0, 0), sp_label, font=b_font)
            sw = (sb[2] - sb[0]) + 22
            sh = (sb[3] - sb[1]) + 10
            sx = (out_w - sw) // 2
            sy = box_y - sh - 6
            draw.rounded_rectangle([sx, sy, sx + sw, sy + sh], radius=99, fill=(9, 13, 26, 238), outline=(34, 211, 238, 225), width=2)
            draw.text((sx + 11, sy + 4), sp_label, font=b_font, fill=(56, 189, 248, 255))

        for l_idx, tokens in enumerate(lines):
            row_w = sum(tk[2] for tk in tokens) + space_w * (len(tokens) - 1)
            cx = (out_w - row_w) // 2
            cy = top_y + l_idx * line_h

            for wd, idx, ww in tokens:
                is_curr = (idx == active_idx)
                col = WORD_PALETTE[idx % len(WORD_PALETTE)] if is_curr else (255, 255, 255)
                draw.text(
                    (cx, cy),
                    wd,
                    font=font,
                    fill=(*col, 255),
                    stroke_width=max(3, font_size // 10),
                    stroke_fill=(2, 4, 10, 255)
                )
                cx += ww + space_w

    final_rgb = Image.alpha_composite(composed, overlay).convert("RGB")
    return final_rgb.tobytes()


# ===========================================================================
# 4. BACKGROUND VIDEO PIPELINE (FFMPEG REAL H.264 MP4 ENCODER)
# ===========================================================================
def run_pipeline(job_id: str, script_text: str, ratio: str, style_key: str, wps: float):
    job = JOBS[job_id]
    try:
        beats = build_storyboard(script_text, style_key, wps)
        if not beats:
            job["status"] = "error"
            job["message"] = "Script is empty! Paste your story script first."
            return

        out_w, out_h = (720, 1280) if ratio == "9:16" else (1280, 720)
        fps = 24
        total = len(beats)
        job["beats"] = [None] * total
        raw_arts = [None] * total

        # Step 1: Parallel Cloud AI Image Generation
        job["message"] = f"Generating {total} AI 3D-Anime Scenes in Parallel..."
        done_count = 0

        def task(idx, b_obj):
            art = generate_ai_art(b_obj["prompt"], out_w, out_h, seed=5000 + idx * 47)
            p_name = f"{job_id}_shot_{idx + 1}.jpg"
            art.save(os.path.join(CACHE_DIR, p_name), quality=85)
            return idx, art, p_name

        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = [pool.submit(task, i, b) for i, b in enumerate(beats)]
            for fut in as_completed(futs):
                idx, art, p_name = fut.result()
                raw_arts[idx] = art
                job["beats"][idx] = {
                    "index": idx + 1,
                    "speaker": beats[idx]["speaker"] or "NARRATOR",
                    "caption": beats[idx]["caption"],
                    "preview_file": p_name
                }
                done_count += 1
                job["progress"] = int((done_count / total) * 55)
                job["message"] = f"AI Scenes Ready: {done_count}/{total}..."

        # Step 2: Stream 2.5D Frames directly into FFmpeg H.264 MP4 Encoder
        out_name = f"NEXORA_AI_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}",
            "-pix_fmt", "rgb24",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "21",
            "-pix_fmt", "yuv420p",
            out_path
        ]

        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        for i, (b, art) in enumerate(zip(beats, raw_arts)):
            bg_l, fg_l, msk = split_depth_layers(art)
            n_frames = max(1, int(b["duration"] * fps))
            for f in range(n_frames):
                prog = f / max(1, n_frames - 1)
                raw_bytes = render_25d_frame(bg_l, fg_l, msk, out_w, out_h, b, prog, global_t)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps

            job["progress"] = 55 + int(((i + 1) / total) * 43)
            job["message"] = f"Encoding 2.5D Motion & Captions ({i + 1}/{total})..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Ready to Watch & Download ({out_name})"

    except Exception as exc:
        job["status"] = "error"
        job["message"] = f"Server Error: {exc}"


# ===========================================================================
# 5. API & FRONTEND ROUTES
# ===========================================================================
@app.route("/")
def serve_home():
    return send_file(os.path.join(BASE_DIR, "index.html"))

@app.route("/api/ping")
def api_ping():
    return jsonify({"ok": True, "engine": "NEXORA Cloud AI Studio"})

@app.route("/api/start", methods=["POST", "OPTIONS"])
def api_start():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(force=True)
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "running", "progress": 2, "message": "AI Director analyzing script...", "beats": []}
    threading.Thread(
        target=run_pipeline,
        args=(job_id, data.get("script", ""), data.get("ratio", "9:16"), data.get("style", "golu_yash"), float(data.get("wps", 2.6))),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    st = JOBS.get(job_id, {"status": "error", "message": "Job not found"}).copy()
    st["beats"] = [b for b in st.get("beats", []) if b is not None]
    return jsonify(st)

@app.route("/api/preview/<filename>")
def api_preview(filename):
    path = os.path.join(CACHE_DIR, os.path.basename(filename))
    if not os.path.exists(path):
        return "", 404
    return send_file(path, mimetype="image/jpeg")

@app.route("/api/video/<job_id>")
def api_video(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("file_path") or not os.path.exists(job["file_path"]):
        return "Video not ready", 404
    as_dl = request.args.get("dl") == "1"
    return send_file(job["file_path"], as_attachment=as_dl, mimetype="video/mp4")

if __name__ == "__main__":
    print("\n====================================================")
    print(" NEXORA CLOUD AI STUDIO IS LIVE ON PORT 5000!")
    print("====================================================\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
