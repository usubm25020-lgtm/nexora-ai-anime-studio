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
# 1. CRISP & ULTRA-FAST AI SCENE DIRECTOR (ZERO TIMEOUTS)
# ===========================================================================
STYLES = {
    "golu_yash": "3D stylized semi-realistic anime manhwa art, Golu Yash viral reels aesthetic, cinematic rim lighting, sharp detailed eyes, 8k",
    "manhwa_pro": "Korean manhwa webtoon illustration, crisp ink lineart, soft airbrush shading, glowing eyes, cinematic lighting",
    "shinkai": "Makoto Shinkai anime movie frame, vibrant scenic sky, volumetric sunbeams, ultra detailed background",
    "dark_horror": "Dark Indian supernatural horror anime art, eerie moonlight, glowing red eyes, misty shadows, dramatic lighting"
}

VISUAL_RULES = [
    (r"\b(bike|motorcycle)\b", "full body shot beside a black sport motorcycle"),
    (r"\b(haveli|mansion|home|ghar)\b", "in front of a grand spooky Indian mansion at night"),
    (r"\b(jungle|forest|road|rasta)\b", "on a dark misty forest road"),
    (r"\b(baarish|rain)\b", "in pouring rain with wet reflections"),
    (r"\b(phone|mobile|torch)\b", "holding a glowing smartphone flashlight"),
    (r"\b(khidki|window)\b", "looking up at a mansion window with glowing eyes"),
    (r"\b(darwaza|door)\b", "standing before a huge wooden door opening"),
    (r"\b(khat|letter)\b", "reading a handwritten paper letter"),
    (r"\b(station|railway|platform)\b", "full body standing at an old railway station platform"),
    (r"\b(sakura|phool|flower|rose)\b", "surrounded by falling pink sakura petals"),
    (r"\b(rooftop|chhat)\b", "full body standing on a building rooftop at night"),
    (r"\b(hologram|cyber|ai)\b", "surrounded by glowing blue holographic light rings"),
    (r"\b(dhuan|smoke|saya|shadow|bhoot)\b", "facing a dark shadowy demon aura"),
    (r"\b(shakti|power|fight|raakh)\b", "full body action pose unleashing glowing blue magical energy"),
    (r"\b(subah|morning|kiran|sunrise)\b", "bathed in warm golden morning sunrise rays")
]

def extract_visual_english(text: str) -> str:
    low = text.lower()
    hits = [desc for pat, desc in VISUAL_RULES if re.search(pat, low)]
    return ", ".join(hits[:2]) if hits else "expressive dramatic storytelling pose"

def build_storyboard(raw_script: str, style_key: str = "golu_yash", wps: float = 2.6):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip()]
    beats = []
    current_scene = "moody Indian city street at night"
    cast_bible = {}
    style_tag = STYLES.get(style_key, STYLES["golu_yash"])

    for ln in lines:
        if re.match(r"^SCENE\s*\d*", ln, re.I):
            loc = re.sub(r"^SCENE\s*\d*\s*[:\-]?\s*", "", ln, flags=re.I).strip()
            if loc:
                current_scene = loc[:60]
            continue

        tags = []
        caption = re.sub(r"\[([^\]]+)\]", lambda m: (tags.append(m.group(1).strip()), "")[1], ln).strip()
        if not caption:
            continue

        speaker = "Narrator"
        camera = "AUTO"
        emotion = "expressive"
        action = "dynamic pose"

        for tg in tags:
            u = tg.upper()
            if u in {"WIDE", "FULL", "MEDIUM", "HALF", "CLOSEUP", "OTS", "ORBIT"}:
                camera = u
            elif u in {"HAPPY", "SAD", "SHOCK", "ROMANTIC", "CONFUSED", "FEAR", "CHILL", "ANGRY", "HEROIC"}:
                emotion = u.lower()
            elif u in {"WALK", "RUN", "HUG", "FIGHT", "DANCE", "PROPOSE", "SIT", "JUMP", "WAVE", "LOOK"}:
                action = u.lower()
            else:
                speaker = tg

        low_cap = caption.lower()
        if camera == "AUTO":
            if any(k in low_cap for k in ["bike", "walk", "run", "fight", "home", "ghar", "haveli", "road", "station", "rooftop", "khada", "khadi"]):
                camera = "FULL"
            elif any(k in low_cap for k in ["shock", "aankhein", "dekh", "fear"]):
                camera = "CLOSEUP"
            else:
                camera = "HALF"

        sp_key = speaker.upper()
        if sp_key != "NARRATOR" and sp_key not in cast_bible:
            low_n = speaker.lower()
            if re.search(r"(bhoot|sakchunni|witch|ghost|demon)", low_n):
                cast_bible[sp_key] = "Bengali supernatural anime woman, glowing red eyes, long black hair, dark saree"
            elif re.search(r"(girl|meera|ananya|priya|aditi|neha|sara|riya)", low_n):
                cast_bible[sp_key] = "gorgeous 20yo Indian anime girl, long dark brown hair, large expressive eyes, stylish jacket and jeans"
            else:
                cast_bible[sp_key] = "handsome 21yo Indian anime boy, messy black hair, sharp jawline, dark streetwear hoodie and cargo pants"

        char_desc = cast_bible.get(sp_key, "cinematic anime protagonist")
        vis_eng = extract_visual_english(caption)

        shot_desc = {
            "FULL": "full body shot from head to shoes",
            "WIDE": "wide full body shot showing full scenery",
            "HALF": "half body waist-up shot with hand gesture",
            "MEDIUM": "half body waist-up shot with hand gesture",
            "CLOSEUP": "dramatic close-up face portrait",
            "OTS": "over-the-shoulder two characters talking",
            "ORBIT": "dynamic low-angle hero action shot"
        }.get(camera, "half body waist-up shot")

        # Keep prompt concise (< 280 chars) so Pollinations API responds in 3-5 seconds!
        prompt_a = f"{style_tag}, {shot_desc}, {char_desc}, {emotion}, {action}, {vis_eng}, {current_scene}, no text"
        prompt_b = f"{style_tag}, {shot_desc}, {char_desc}, {emotion}, dynamic movement follow-through gesture, {vis_eng}, {current_scene}, no text"

        combo = (current_scene + " " + caption).lower()
        weather = "bokeh"
        if any(k in combo for k in ["rain", "baarish", "storm", "wet"]):
            weather = "rain"
        elif any(k in combo for k in ["sakura", "flower", "bouquet", "love", "romantic", "sunset"]):
            weather = "petals"
        elif any(k in combo for k in ["fire", "fight", "angry", "danger", "horror", "bhoot", "shakti"]):
            weather = "embers"

        words = caption.split()
        duration = max(2.2, round(len(words) / max(1.2, wps), 2))

        beats.append({
            "index": len(beats) + 1,
            "speaker": "" if sp_key == "NARRATOR" else speaker,
            "camera": camera,
            "emotion": emotion.upper(),
            "weather": weather,
            "caption": caption,
            "words": words,
            "duration": duration,
            "prompt_a": prompt_a,
            "prompt_b": prompt_b
        })

    return beats


# ===========================================================================
# 2. FAST & UNBREAKABLE AI ART FETCHER (512x896 -> UPSCALE, ZERO BLUE SCREEN)
# ===========================================================================
def fetch_single_art(prompt: str, target_w: int, target_h: int, seed: int) -> Image.Image:
    # Fast generation resolution (prevents Render & Pollinations timeouts)
    req_w = 512 if target_h > target_w else 896
    req_h = 896 if target_h > target_w else 512
    encoded = urllib.parse.quote(prompt)

    urls = [
        f"https://image.pollinations.ai/prompt/{encoded}?width={req_w}&height={req_h}&seed={seed}&model=flux&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded}?width={req_w}&height={req_h}&seed={seed + 13}&model=turbo&nologo=true",
        f"https://pollinations.ai/p/{encoded}?width={req_w}&height={req_h}&seed={seed + 29}&nologo=true"
    ]

    headers = {
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127.0.{seed % 80}.0 Safari/537.36",
        "Accept": "image/jpeg,image/png,image/*,*/*;q=0.8"
    }

    for attempt in range(5):
        u = urls[attempt % len(urls)]
        try:
            req = urllib.request.Request(u, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if len(raw) > 6000:
                    img = Image.open(BytesIO(raw)).convert("RGB")
                    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                    img = ImageEnhance.Color(img).enhance(1.15)
                    img = ImageEnhance.Contrast(img).enhance(1.08)
                    return img
        except Exception:
            time.sleep(1.2 + attempt * 0.8)

    # Last-resort simplified prompt so an image ALWAYS arrives
    simple_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote('3D anime character cinematic scene, 8k')}?width={req_w}&height={req_h}&seed={seed}&nologo=true"
    try:
        req = urllib.request.Request(simple_url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
            if len(raw) > 5000:
                return Image.open(BytesIO(raw)).convert("RGB").resize((target_w, target_h), Image.Resampling.LANCZOS)
    except Exception:
        pass

    fb = Image.new("RGB", (target_w, target_h), (12, 18, 36))
    return fb


# ===========================================================================
# 3. DUAL-KEYFRAME LIMB/POSE MORPH + 2.5D PARALLAX + GAMING CAPTIONS
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

def render_action_frame(img_a: Image.Image, img_b: Image.Image,
                        out_w: int, out_h: int, beat: dict, prog: float, global_t: float) -> bytes:
    # Smoothly morph Pose A (start action) -> Pose B (follow-through action) so body & hands visibly move!
    morph_alpha = max(0.0, min(1.0, (prog - 0.15) / 0.70))
    blended = Image.blend(img_a, img_b, morph_alpha)

    sw, sh = blended.size
    cam = beat["camera"]
    emo = beat["emotion"]

    if cam == "CLOSEUP":
        zoom = 1.03 + prog * 0.11
    elif cam in {"WIDE", "FULL"}:
        zoom = 1.12 - prog * 0.09
    else:
        zoom = 1.04 + prog * 0.08

    shake_x, shake_y = 0, 0
    if emo in {"SHOCK", "FEAR", "ANGRY"} and prog < 0.25:
        decay = 1.0 - (prog / 0.25)
        shake_x = int(math.sin(global_t * 54) * 8 * decay)
        shake_y = int(math.cos(global_t * 48) * 6 * decay)

    breath_y = math.sin(global_t * 3.4) * 4.0
    cw = int(out_w / zoom)
    ch = int(out_h / zoom)
    dx = max(0, sw - cw)
    dy = max(0, sh - ch)
    pan_dir = prog if (beat["index"] % 2 == 1) else (1.0 - prog)
    x0 = max(0, min(dx, int(dx * (0.5 + (pan_dir - 0.5) * 0.85)) + shake_x))
    y0 = max(0, min(dy, int(dy * 0.48 + breath_y) + shake_y))

    base = blended.crop((x0, y0, x0 + cw, y0 + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR).convert("RGBA")
    overlay = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    weather = beat.get("weather", "bokeh")
    if weather == "rain":
        for i in range(46):
            rx = int((i * 73 + global_t * 160) % (out_w + 60)) - 30
            ry = int((i * 97 + global_t * 760) % (out_h + 40)) - 20
            draw.line([(rx, ry), (rx - 8, ry + 22)], fill=(186, 230, 253, 145), width=2)
    elif weather == "petals":
        for i in range(24):
            px = int((i * 97 + global_t * (45 + (i % 4) * 11)) % (out_w + 40)) - 20
            py = int((i * 67 + global_t * (34 + (i % 3) * 10) + math.sin(global_t * 2.2 + i) * 18) % out_h)
            col = (251, 113, 133, 195) if i % 2 == 0 else (253, 164, 175, 170)
            draw.ellipse([px - 5, py - 3, px + 5, py + 3], fill=col)
    elif weather == "embers":
        for i in range(24):
            ex = int((i * 89 + math.sin(global_t * 2 + i) * 26) % out_w)
            ey = int((out_h - ((i * 61 + global_t * 95) % out_h)))
            draw.ellipse([ex - 3, ey - 3, ex + 3, ey + 3], fill=(251, 146, 60, 190))
    else:
        for i in range(18):
            bx = int((i * 113 + math.sin(global_t + i) * 24) % out_w)
            by = int((i * 79 - global_t * 26) % out_h)
            alpha = int(75 + abs(math.sin(global_t * 2.4 + i)) * 145)
            draw.ellipse([bx - 3, by - 3, bx + 3, by + 3], fill=(56, 189, 248, alpha))

    words = beat.get("words", [])
    if words:
        is_portrait = out_h > out_w
        active_idx = min(len(words) - 1, int(prog * len(words)))
        font_size = max(21, int(out_w * 0.048)) if is_portrait else max(23, int(out_w * 0.028))
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
        box_w = min(int(out_w * 0.92), max_actual_w + 32)
        box_h = total_h + 20
        box_x = (out_w - box_w) // 2
        box_y = top_y - 10

        draw.rounded_rectangle(
            [box_x, box_y, box_x + box_w, box_y + box_h],
            radius=16,
            fill=(6, 10, 22, 220),
            outline=(34, 211, 238, 205),
            width=2
        )

        speaker = beat.get("speaker", "")
        if speaker:
            b_font = get_font(max(13, int(font_size * 0.48)))
            sp_label = f"⚡ {speaker.upper()}"
            sb = draw.textbbox((0, 0), sp_label, font=b_font)
            sw = (sb[2] - sb[0]) + 20
            sh = (sb[3] - sb[1]) + 10
            sx = (out_w - sw) // 2
            sy = box_y - sh - 6
            draw.rounded_rectangle([sx, sy, sx + sw, sy + sh], radius=99, fill=(9, 13, 26, 238), outline=(34, 211, 238, 225), width=2)
            draw.text((sx + 10, sy + 4), sp_label, font=b_font, fill=(56, 189, 248, 255))

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

    return Image.alpha_composite(base, overlay).convert("RGB").tobytes()


# ===========================================================================
# 4. FAST PIPELINE (DUAL POSE KEYFRAMES + ULTRAFAST MP4 ENCODER)
# ===========================================================================
def encode_mp4_from_job(job_id: str):
    job = JOBS[job_id]
    try:
        job["status"] = "rendering"
        job["progress"] = 65
        job["message"] = "Encoding Multi-Pose Action Motion & Captions..."

        beats = job["raw_beats"]
        out_w, out_h = job["out_w"], job["out_h"]
        fps = 18
        total = len(beats)

        out_name = f"NEXORA_AI_{job['ratio'].replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", out_path
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        for i, b in enumerate(beats):
            path_a = os.path.join(CACHE_DIR, job["beats"][i]["preview_file"])
            path_b = os.path.join(CACHE_DIR, job["beats"][i].get("pose_b_file", job["beats"][i]["preview_file"]))
            img_a = Image.open(path_a).convert("RGB")
            img_b = Image.open(path_b).convert("RGB") if os.path.exists(path_b) else img_a

            n_frames = max(1, int(b["duration"] * fps))
            for f in range(n_frames):
                prog = f / max(1, n_frames - 1)
                raw_bytes = render_action_frame(img_a, img_b, out_w, out_h, b, prog, global_t)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps

            job["progress"] = 65 + int(((i + 1) / total) * 34)
            job["message"] = f"Rendering Action Video ({i + 1}/{total})..."

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
            job["message"] = "Script is empty!"
            return

        out_w, out_h = (576, 1024) if ratio == "9:16" else (1024, 576)
        gen_w, gen_h = int(out_w * 1.14), int(out_h * 1.14)
        total = len(beats)
        job["raw_beats"] = beats
        job["out_w"] = out_w
        job["out_h"] = out_h
        job["ratio"] = ratio
        job["beats"] = [None] * total

        done_count = 0

        def fetch_shot_pair(idx, b_obj):
            time.sleep((idx % 3) * 0.45)
            seed_base = 8000 + idx * 71
            art_a = fetch_single_art(b_obj["prompt_a"], gen_w, gen_h, seed=seed_base)
            art_b = fetch_single_art(b_obj["prompt_b"], gen_w, gen_h, seed=seed_base + 5)
            ts = int(time.time())
            file_a = f"{job_id}_s{idx + 1}_a_{ts}.jpg"
            file_b = f"{job_id}_s{idx + 1}_b_{ts}.jpg"
            art_a.save(os.path.join(CACHE_DIR, file_a), quality=86)
            art_b.save(os.path.join(CACHE_DIR, file_b), quality=86)
            return idx, file_a, file_b

        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = [pool.submit(fetch_shot_pair, i, b) for i, b in enumerate(beats)]
            for fut in as_completed(futs):
                idx, file_a, file_b = fut.result()
                job["beats"][idx] = {
                    "index": idx + 1,
                    "speaker": beats[idx]["speaker"] or "NARRATOR",
                    "caption": beats[idx]["caption"],
                    "preview_file": file_a,
                    "pose_b_file": file_b
                }
                done_count += 1
                job["progress"] = int((done_count / total) * 62)
                job["message"] = f"AI Action Scenes Ready: {done_count}/{total}..."

        if auto_render:
            encode_mp4_from_job(job_id)
        else:
            job["status"] = "scenes_ready"
            job["progress"] = 65
            job["message"] = "All Action Scenes Ready! Review/Regenerate any shot or click Render Final MP4."

    except Exception as exc:
        job["status"] = "error"
        job["message"] = f"Error: {exc}"


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
    JOBS[job_id] = {"status": "running", "progress": 2, "message": "Fast-Generating Full-Body & Action Keyframes...", "beats": []}
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
    gen_w, gen_h = int(job["out_w"] * 1.14), int(job["out_h"] * 1.14)
    seed_base = int(time.time()) % 99999
    art_a = fetch_single_art(b["prompt_a"], gen_w, gen_h, seed=seed_base)
    art_b = fetch_single_art(b["prompt_b"], gen_w, gen_h, seed=seed_base + 5)
    file_a = f"{job_id}_s{shot_idx + 1}_a_{seed_base}.jpg"
    file_b = f"{job_id}_s{shot_idx + 1}_b_{seed_base}.jpg"
    art_a.save(os.path.join(CACHE_DIR, file_a), quality=86)
    art_b.save(os.path.join(CACHE_DIR, file_b), quality=86)
    job["beats"][shot_idx]["preview_file"] = file_a
    job["beats"][shot_idx]["pose_b_file"] = file_b
    return jsonify({"ok": True, "preview_file": file_a})

@app.route("/api/upload_shot/<job_id>/<int:shot_idx>", methods=["POST"])
def api_upload_shot(job_id, shot_idx):
    job = JOBS.get(job_id)
    if not job or "file" not in request.files:
        return jsonify({"error": "Missing file"}), 400
    f = request.files["file"]
    img = Image.open(f.stream).convert("RGB")
    gen_w, gen_h = int(job["out_w"] * 1.14), int(job["out_h"] * 1.14)
    img = img.resize((gen_w, gen_h), Image.Resampling.LANCZOS)
    p_name = f"{job_id}_custom_{shot_idx + 1}_{int(time.time())}.jpg"
    img.save(os.path.join(CACHE_DIR, p_name), quality=90)
    job["beats"][shot_idx]["preview_file"] = p_name
    job["beats"][shot_idx]["pose_b_file"] = p_name
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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
