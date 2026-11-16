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
# 1. HINGLISH-TO-CINEMATIC-ENGLISH AI SCENE DIRECTOR
# ===========================================================================
STYLES = {
    "golu_yash": (
        "3D stylized semi-realistic anime manhwa art, Golu Yash viral reels aesthetic, "
        "expressive cinematic lighting, subsurface skin glow, sharp detailed eyes, "
        "dramatic rim light, volumetric atmosphere, depth of field bokeh, masterpiece"
    ),
    "manhwa_pro": (
        "Masterpiece Korean manhwa webtoon illustration, crisp ink lineart, "
        "soft airbrush shading, glowing jewel eyes, cinematic framing, high detail"
    ),
    "shinkai": (
        "Makoto Shinkai anime film still, breathtaking scenic sky, volumetric sunbeams, "
        "lens flare, ultra detailed background and emotional character art"
    ),
    "dark_horror": (
        "Dark supernatural Indian horror anime art, eerie moonlight, glowing red eyes, "
        "misty shadows, dramatic high-contrast rim lighting, thriller anime frame"
    )
}

HINGLISH_VISUAL_MAP = [
    (r"\b(bike|motorcycle|scooter)\b", "beside a sleek parked sport motorcycle with glowing headlight"),
    (r"\b(haveli|mansion|bangla|bhoot bangla)\b", "in front of an ancient spooky Indian mansion with glowing windows"),
    (r"\b(jungle|forest|jungli|rasta|road)\b", "on a dark misty forest road surrounded by tall trees"),
    (r"\b(baarish|rain|bheeg)\b", "in heavy pouring rain with wet reflections and raindrops"),
    (r"\b(phone|mobile|torch)\b", "holding a glowing smartphone flashlight in the dark"),
    (r"\b(khidki|window)\b", "looking up at a dark mansion window with glowing red eyes peering out"),
    (r"\b(darwaza|door|gate)\b", "standing in front of a massive wooden mansion door opening with dramatic light"),
    (r"\b(khat|letter|chitthi)\b", "reading an old handwritten paper letter"),
    (r"\b(station|railway|train|platform)\b", "at a vintage railway station platform at sunset"),
    (r"\b(sakura|phool|flower|bouquet|rose)\b", "surrounded by falling pink cherry blossom petals"),
    (r"\b(cafe|coffee|chai)\b", "inside a warm aesthetic cafe with glowing fairy lights"),
    (r"\b(rooftop|chhat)\b", "standing on a high building rooftop overlooking city lights"),
    (r"\b(hologram|cyber|ai)\b", "surrounded by glowing blue futuristic holographic light rings"),
    (r"\b(dhuan|smoke|saya|shadow|bhoot|demon)\b", "confronting a menacing dark shadowy demon aura"),
    (r"\b(shakti|power|fight|raakh|attack)\b", "unleashing glowing blue and crimson magical energy aura"),
    (r"\b(subah|morning|kiran|sunrise|sunlight)\b", "bathed in warm golden morning sunrise rays")
]

def translate_hinglish_to_visual_english(text: str) -> str:
    low = text.lower()
    matched_actions = []
    for pattern, eng_desc in HINGLISH_VISUAL_MAP:
        if re.search(pattern, low):
            matched_actions.append(eng_desc)
    if not matched_actions:
        return "expressive dramatic storytelling moment with emotional facial expression"
    return ", ".join(matched_actions)

def build_storyboard(raw_script: str, style_key: str = "golu_yash", wps: float = 2.6):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip()]
    beats = []
    current_scene = "dark misty forest road at midnight"
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
                    "mystical Bengali supernatural anime woman with glowing crimson eyes, long flowing jet-black hair, "
                    "traditional dark saree with gold jewelry, eerie ethereal aura"
                )
            elif re.search(r"(ai|bot|robot|cyber|android|hologram)", low_name):
                cast_bible[sp_key] = (
                    "futuristic translucent cyan holographic anime AI character, glowing neon eyes, sleek cyber jacket"
                )
            elif re.search(r"(girl|meera|ananya|priya|aditi|neha|sara|riya|queen|princess)", low_name):
                cast_bible[sp_key] = (
                    "gorgeous 20 year old Indian anime girl with long silky dark brown hair, wispy face-framing strands, "
                    "expressive large dark eyes, stylish modern outfit"
                )
            else:
                cast_bible[sp_key] = (
                    "handsome 21 year old Indian anime young man with messy textured black hair, sharp jawline, "
                    "expressive dark eyes, stylish dark streetwear hoodie"
                )

        char_desc = cast_bible.get(sp_key, "mysterious cinematic anime narrator silhouette in dramatic lighting")
        visual_action_eng = translate_hinglish_to_visual_english(caption)

        framing = {
            "WIDE": "wide cinematic establishing shot showing full scenery and character",
            "MEDIUM": "cinematic medium waist-up shot with dynamic depth",
            "CLOSEUP": "dramatic close-up portrait focusing on facial emotion and eyes",
            "OTS": "cinematic over-the-shoulder shot of two characters facing each other",
            "ORBIT": "dynamic low-angle hero action shot with dramatic lighting"
        }.get(camera, "cinematic medium waist-up shot")

        prompt = (
            f"{style_prompt}, {framing}, {char_desc}, emotion: {emotion.lower()}, {action}, "
            f"{visual_action_eng}, environment setting: {current_scene}, "
            f"no text, no subtitles, no watermark, clean full-bleed anime illustration"
        )

        combo = (current_scene + " " + caption).lower()
        weather = "bokeh"
        if any(k in combo for k in ["rain", "baarish", "storm", "wet", "monsoon"]):
            weather = "rain"
        elif any(k in combo for k in ["sakura", "flower", "bouquet", "love", "romantic", "shaadi", "cafe", "sunset"]):
            weather = "petals"
        elif any(k in combo for k in ["fire", "fight", "angry", "danger", "horror", "bhoot", "shakti", "raakh"]):
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
# 2. RATE-LIMIT-SAFE REAL AI ART GENERATOR (NEVER RETURNS BLANK BLUE!)
# ===========================================================================
def generate_ai_art(prompt: str, width: int, height: int, seed: int) -> Image.Image:
    gen_w = int(width * 1.15)
    gen_h = int(height * 1.15)
    encoded = urllib.parse.quote(prompt)

    urls = [
        f"https://image.pollinations.ai/prompt/{encoded}?width={gen_w}&height={gen_h}&seed={seed}&model=flux&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded}?width={gen_w}&height={gen_h}&seed={seed + 19}&model=turbo&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded}?width={gen_w}&height={gen_h}&seed={seed + 37}&nologo=true"
    ]

    headers = {
        "User-Agent": f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.{seed % 99}.0 Safari/537.36",
        "Accept": "image/jpeg,image/png,image/*,*/*;q=0.8"
    }

    for attempt in range(4):
        url = urls[attempt % len(urls)]
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=55) as resp:
                raw = resp.read()
                if len(raw) > 8000:
                    img = Image.open(BytesIO(raw)).convert("RGB")
                    img = img.resize((gen_w, gen_h), Image.Resampling.LANCZOS)
                    img = ImageEnhance.Color(img).enhance(1.16)
                    img = ImageEnhance.Contrast(img).enhance(1.08)
                    return img
        except Exception:
            time.sleep(2.2 + attempt * 1.5)

    # Fallback only if all retries fail
    fb = Image.new("RGB", (gen_w, gen_h), (8, 12, 26))
    d = ImageDraw.Draw(fb)
    for y in range(gen_h):
        t = y / max(1, gen_h)
        d.line([(0, y), (gen_w, y)], fill=(int(12 + t * 35), int(20 + t * 48), int(48 + t * 90)))
    return fb


# ===========================================================================
# 3. 2.5D MULTI-LAYER DEPTH PARALLAX + WEATHER FX + GAMING CAPTIONS
# ===========================================================================
WORD_PALETTE = [
    (34, 211, 238),
    (251, 191, 36),
    (251, 113, 133),
    (167, 139, 250),
    (52, 211, 153)
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
    bg_layer = src_img.filter(ImageFilter.GaussianBlur(radius=4))
    bg_layer = ImageEnhance.Brightness(bg_layer).enhance(0.90)

    w, h = src_img.size
    mask = Image.new("L", (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse([int(w * 0.06), int(h * 0.05), int(w * 0.94), int(h * 0.98)], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=28))
    return bg_layer, src_img, mask

def render_25d_frame(bg_layer: Image.Image, fg_layer: Image.Image, mask: Image.Image,
                     out_w: int, out_h: int, beat: dict, prog: float, global_t: float) -> bytes:
    sw, sh = fg_layer.size
    cam = beat["camera"]
    emo = beat["emotion"]

    if cam == "CLOSEUP":
        fg_zoom = 1.04 + prog * 0.12
    elif cam == "WIDE":
        fg_zoom = 1.14 - prog * 0.10
    else:
        fg_zoom = 1.05 + prog * 0.08

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
# 4. TWO-STAGE PIPELINE: (A) GENERATE ALL SCENES -> (B) RENDER MP4 VIDEO
# ===========================================================================
def encode_mp4_from_job(job_id: str):
    job = JOBS[job_id]
    try:
        job["status"] = "rendering"
        job["progress"] = 60
        job["message"] = "Encoding 2.5D Depth Motion & Captions into MP4..."

        beats = job["raw_beats"]
        out_w, out_h = job["out_w"], job["out_h"]
        fps = 24
        total = len(beats)

        out_name = f"NEXORA_AI_{job['ratio'].replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        for i, b in enumerate(beats):
            img_path = os.path.join(CACHE_DIR, job["beats"][i]["preview_file"])
            art = Image.open(img_path).convert("RGB")
            bg_l, fg_l, msk = split_depth_layers(art)

            n_frames = max(1, int(b["duration"] * fps))
            for f in range(n_frames):
                prog = f / max(1, n_frames - 1)
                raw_bytes = render_25d_frame(bg_l, fg_l, msk, out_w, out_h, b, prog, global_t)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps

            job["progress"] = 60 + int(((i + 1) / total) * 39)
            job["message"] = f"Rendering 2.5D Motion ({i + 1}/{total})..."

        proc.stdin.close()
        proc.wait()

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Ready to Watch & Download ({out_name})"
    except Exception as exc:
        job["status"] = "error"
        job["message"] = f"Render Error: {exc}"


def generate_scenes_pipeline(job_id: str, script_text: str, ratio: str, style_key: str, wps: float, auto_render: bool = True):
    job = JOBS[job_id]
    try:
        beats = build_storyboard(script_text, style_key, wps)
        if not beats:
            job["status"] = "error"
            job["message"] = "Script is empty! Paste your story script first."
            return

        out_w, out_h = (720, 1280) if ratio == "9:16" else (1280, 720)
        total = len(beats)
        job["raw_beats"] = beats
        job["out_w"] = out_w
        job["out_h"] = out_h
        job["ratio"] = ratio
        job["beats"] = [None] * total

        # Rate-limit safe parallel pool (max_workers=2 with staggered start so zero shots get blocked!)
        done_count = 0

        def fetch_one(idx, b_obj):
            time.sleep((idx % 2) * 0.9)
            art = generate_ai_art(b_obj["prompt"], out_w, out_h, seed=7000 + idx * 61)
            p_name = f"{job_id}_shot_{idx + 1}_{int(time.time())}.jpg"
            art.save(os.path.join(CACHE_DIR, p_name), quality=88)
            return idx, p_name

        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(fetch_one, i, b) for i, b in enumerate(beats)]
            for fut in as_completed(futs):
                idx, p_name = fut.result()
                job["beats"][idx] = {
                    "index": idx + 1,
                    "speaker": beats[idx]["speaker"] or "NARRATOR",
                    "caption": beats[idx]["caption"],
                    "preview_file": p_name
                }
                done_count += 1
                job["progress"] = int((done_count / total) * 58)
                job["message"] = f"AI Scene Artwork Ready: {done_count}/{total}..."

        if auto_render:
            encode_mp4_from_job(job_id)
        else:
            job["status"] = "scenes_ready"
            job["progress"] = 60
            job["message"] = "All AI Scenes Ready! Review/Swap any shot or click Render Final MP4."

    except Exception as exc:
        job["status"] = "error"
        job["message"] = f"Error: {exc}"


# ===========================================================================
# 5. API ROUTES (INCLUDING SINGLE-SHOT REGENERATE & CUSTOM IMAGE UPLOAD)
# ===========================================================================
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
    data = request.get_json(force=True)
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "running", "progress": 2, "message": "Translating Hinglish script to AI Visual Scenes...", "beats": []}
    threading.Thread(
        target=generate_scenes_pipeline,
        args=(job_id, data.get("script", ""), data.get("ratio", "9:16"), data.get("style", "golu_yash"), float(data.get("wps", 2.6)), bool(data.get("auto_render", True))),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id})

@app.route("/api/regen_shot/<job_id>/<int:shot_idx>", methods=["POST"])
def api_regen_shot(job_id, shot_idx):
    job = JOBS.get(job_id)
    if not job or shot_idx < 0 or shot_idx >= len(job.get("raw_beats", [])):
        return jsonify({"error": "Invalid shot"}), 400
    b = job["raw_beats"][shot_idx]
    art = generate_ai_art(b["prompt"], job["out_w"], job["out_h"], seed=int(time.time()) % 99999)
    p_name = f"{job_id}_shot_{shot_idx + 1}_{int(time.time())}.jpg"
    art.save(os.path.join(CACHE_DIR, p_name), quality=88)
    job["beats"][shot_idx]["preview_file"] = p_name
    return jsonify({"ok": True, "preview_file": p_name})

@app.route("/api/upload_shot/<job_id>/<int:shot_idx>", methods=["POST"])
def api_upload_shot(job_id, shot_idx):
    job = JOBS.get(job_id)
    if not job or "file" not in request.files:
        return jsonify({"error": "Missing file"}), 400
    f = request.files["file"]
    img = Image.open(f.stream).convert("RGB")
    gen_w = int(job["out_w"] * 1.15)
    gen_h = int(job["out_h"] * 1.15)
    img = img.resize((gen_w, gen_h), Image.Resampling.LANCZOS)
    p_name = f"{job_id}_custom_{shot_idx + 1}_{int(time.time())}.jpg"
    img.save(os.path.join(CACHE_DIR, p_name), quality=90)
    job["beats"][shot_idx]["preview_file"] = p_name
    return jsonify({"ok": True, "preview_file": p_name})

@app.route("/api/render_mp4/<job_id>", methods=["POST"])
def api_render_mp4(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    threading.Thread(target=encode_mp4_from_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    st = JOBS.get(job_id, {"status": "error", "message": "Job not found"}).copy()
    st.pop("raw_beats", None)
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
        return "Not ready", 404
    return send_file(job["file_path"], as_attachment=(request.args.get("dl") == "1"), mimetype="video/mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
