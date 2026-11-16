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
# 1. UNIVERSAL STORY DIRECTOR (ANY GENRE + LIP-SYNC POSE PAIRS)
# ===========================================================================
STYLES = {
    "golu_yash": (
        "3D stylized semi-realistic Indian anime manhwa art, Golu Yash viral reels aesthetic, "
        "expressive cinematic rim lighting, sharp detailed eyes, masterpiece"
    ),
    "manhwa_pro": (
        "bright pastel Indian romantic manhwa illustration, soft dreamy lighting, "
        "delicate lineart, gorgeous expressive anime eyes, 8k"
    ),
    "shinkai": (
        "Makoto Shinkai Japanese anime movie frame, vibrant dramatic lighting, "
        "expressive anime character acting, lens flare, ultra detailed scenery"
    ),
    "dark_horror": (
        "Dark Indian supernatural horror anime art, eerie moonlight, glowing red eyes, "
        "misty shadows, dramatic high-contrast rim lighting"
    )
}

VISUAL_RULES = [
    (r"\b(phone|mobile|screen|photo|wallpaper)\b", "holding a smartphone looking at glowing phone screen"),
    (r"\b(ots|shoulder|peeche)\b", "over-the-shoulder shot looking at smartphone screen"),
    (r"\b(beach|samundar|sea|waves)\b", "beside a scenic ocean beach with soft waves"),
    (r"\b(hair|baal|hand|haath)\b", "running hand softly through hair with expressive gesture"),
    (r"\b(lehenga|saree|traditional)\b", "wearing ornate traditional Indian outfit with gold jewelry"),
    (r"\b(bike|motorcycle|superbike)\b", "full body shot riding or standing beside a black sport motorcycle"),
    (r"\b(car|gaadi|thar|jeep)\b", "full body shot beside a sleek black SUV car"),
    (r"\b(haveli|mansion|home|ghar|room|kamra)\b", "inside or in front of a detailed Indian house"),
    (r"\b(college|school|canteen|class)\b", "inside a lively Indian college campus or canteen"),
    (r"\b(baarish|rain|storm)\b", "in pouring rain with wet reflections"),
    (r"\b(station|railway|train|platform)\b", "full body at a railway station platform"),
    (r"\b(sakura|phool|flower|rose)\b", "surrounded by falling pink flower petals"),
    (r"\b(shakti|power|fight|battle|sword|gun)\b", "full body dynamic action fight pose unleashing glowing energy"),
    (r"\b(fire|aag|explosion)\b", "surrounded by dramatic flames and sparks")
]

def extract_visual_english(text: str) -> str:
    low = text.lower()
    hits = [desc for pat, desc in VISUAL_RULES if re.search(pat, low)]
    # Also pass clean English words from caption directly so ANY custom topic works!
    clean_eng = re.sub(r"[^a-zA-Z0-9\s,.-]", "", text).strip()
    base = ", ".join(hits[:2]) if hits else ""
    return f"{base}, {clean_eng[:90]}".strip(", ")

def build_storyboard(raw_script: str, style_key: str = "golu_yash", wps: float = 2.6):
    lines = [ln.strip() for ln in str(raw_script or "").splitlines() if ln.strip()]
    beats = []
    current_scene = "cinematic Indian location with atmospheric lighting"
    cast_bible = {}
    style_tag = STYLES.get(style_key, STYLES["golu_yash"])

    for ln in lines:
        if re.match(r"^SCENE\s*\d*", ln, re.I):
            loc = re.sub(r"^SCENE\s*\d*\s*[:\-]?\s*", "", ln, flags=re.I).strip()
            if loc:
                current_scene = loc[:65]
            continue

        tags = []
        caption = re.sub(r"\[([^\]]+)\]", lambda m: (tags.append(m.group(1).strip()), "")[1], ln).strip()
        if not caption:
            continue

        speaker = "Narrator"
        camera = "AUTO"
        emotion = "expressive"
        action = "natural pose"

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
            if any(k in low_cap for k in ["ots", "phone screen", "photo dekh"]):
                camera = "OTS"
            elif any(k in low_cap for k in ["bike", "car", "walk", "run", "fight", "home", "ghar", "station", "college", "khada", "khadi"]):
                camera = "FULL"
            elif any(k in low_cap for k in ["shock", "aankhein", "dekh", "smile", "muskura", "boli", "bola", "kaha"]):
                camera = "CLOSEUP"
            else:
                camera = "HALF"

        sp_key = speaker.upper()
        is_speaking = (sp_key != "NARRATOR")

        if is_speaking and sp_key not in cast_bible:
            low_n = speaker.lower()
            if re.search(r"(bhoot|sakchunni|witch|ghost|demon)", low_n):
                cast_bible[sp_key] = "Bengali supernatural anime woman, glowing red eyes, long black hair, dark saree"
            elif re.search(r"(girl|meera|ananya|priya|aditi|neha|sara|riya|queen|mom|maa)", low_n):
                cast_bible[sp_key] = "gorgeous 20yo Indian anime girl, long dark hair, expressive large eyes, stylish modern outfit"
            else:
                cast_bible[sp_key] = "handsome 21yo Indian anime young man, messy black hair, sharp jawline, stylish streetwear jacket"

        char_desc = cast_bible.get(sp_key, "expressive anime character")
        vis_eng = extract_visual_english(caption)

        shot_desc = {
            "FULL": "full body shot from head to shoes",
            "WIDE": "wide cinematic shot showing full scenery",
            "HALF": "medium waist-up shot with hand gesture",
            "MEDIUM": "medium waist-up shot",
            "CLOSEUP": "dramatic close-up portrait focusing on face and lips",
            "OTS": "over-the-shoulder shot looking at smartphone screen",
            "ORBIT": "dynamic low-angle hero action shot"
        }.get(camera, "medium waist-up shot")

        # Pose A = Closed mouth / listening pose; Pose B = Open mouth speaking & tilted head for Lip-Sync!
        mouth_a = "mouth closed, attentive expression" if is_speaking else action
        mouth_b = "mouth open speaking actively, slight head tilt and expressive hand gesture" if is_speaking else "dynamic follow-through movement"

        prompt_a = f"{style_tag}, {shot_desc}, {char_desc}, {emotion}, {mouth_a}, {vis_eng}, {current_scene}, no text"
        prompt_b = f"{style_tag}, {shot_desc}, {char_desc}, {emotion}, {mouth_b}, {vis_eng}, {current_scene}, no text"

        combo = (current_scene + " " + caption + " " + style_key).lower()
        fx_mode = "none"
        if any(k in combo for k in ["sakura", "flower", "romantic", "love", "manhwa_pro"]):
            fx_mode = "butterfly_petals"
        elif any(k in combo for k in ["beach", "street", "promenade", "crowd", "market"]):
            fx_mode = "crowd_walk"
        elif any(k in combo for k in ["rain", "baarish", "storm"]):
            fx_mode = "rain"
        elif any(k in combo for k in ["fire", "fight", "horror", "bhoot", "battle"]):
            fx_mode = "embers"

        words = caption.split()
        duration = max(2.4, round(len(words) / max(1.2, wps), 2))

        beats.append({
            "index": len(beats) + 1,
            "speaker": speaker if is_speaking else "",
            "is_speaking": is_speaking,
            "camera": camera,
            "emotion": emotion.upper(),
            "fx_mode": fx_mode,
            "caption": caption,
            "words": words,
            "duration": duration,
            "prompt_a": prompt_a,
            "prompt_b": prompt_b
        })

    return beats


# ===========================================================================
# 2. ZERO-BLUE-SCREEN AI ART FETCHER
# ===========================================================================
def fetch_single_art(prompt: str, target_w: int, target_h: int, seed: int):
    req_w = 512 if target_h > target_w else 896
    req_h = 896 if target_h > target_w else 512
    encoded = urllib.parse.quote(prompt)

    urls = [
        f"https://image.pollinations.ai/prompt/{encoded}?width={req_w}&height={req_h}&seed={seed}&model=flux&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded}?width={req_w}&height={req_h}&seed={seed + 17}&model=turbo&nologo=true",
        f"https://pollinations.ai/p/{encoded}?width={req_w}&height={req_h}&seed={seed + 31}&nologo=true"
    ]

    headers = {
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127.0.{seed % 85}.0 Safari/537.36",
        "Accept": "image/jpeg,image/png,image/*,*/*;q=0.8"
    }

    for attempt in range(4):
        u = urls[attempt % len(urls)]
        try:
            req = urllib.request.Request(u, headers=headers)
            with urllib.request.urlopen(req, timeout=28) as resp:
                raw = resp.read()
                if len(raw) > 6000:
                    img = Image.open(BytesIO(raw)).convert("RGB")
                    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                    img = ImageEnhance.Color(img).enhance(1.14)
                    img = ImageEnhance.Contrast(img).enhance(1.06)
                    return img
        except Exception:
            time.sleep(1.0 + attempt * 0.7)

    return None


# ===========================================================================
# 3. SYLLABLE-SYNCED LIP MOVEMENT + HEAD BOBBING + CINEMATIC FX
# ===========================================================================
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

def render_reels_frame(img_a: Image.Image, img_b: Image.Image,
                       out_w: int, out_h: int, beat: dict, prog: float, global_t: float) -> bytes:
    is_speaking = beat.get("is_speaking", False)

    # Syllable-paced Lip-Sync Pulse (alternates closed/open mouth 4.5 times/sec when speaking!)
    if is_speaking:
        lip_pulse = 0.5 + 0.5 * math.sin(global_t * 26.0)
        head_nod_y = math.sin(global_t * 9.5) * 4.5
        head_tilt_x = math.cos(global_t * 6.0) * 3.0
    else:
        lip_pulse = max(0.0, min(1.0, (prog - 0.15) / 0.70))
        head_nod_y = math.sin(global_t * 3.0) * 3.5
        head_tilt_x = 0.0

    blended = Image.blend(img_a, img_b, lip_pulse)

    sw, sh = blended.size
    cam = beat["camera"]
    zoom = (1.03 + prog * 0.08) if cam in {"CLOSEUP", "OTS"} else (1.04 + prog * 0.06)

    cw = int(out_w / zoom)
    ch = int(out_h / zoom)
    dx = max(0, sw - cw)
    dy = max(0, sh - ch)
    pan_dir = prog if (beat["index"] % 2 == 1) else (1.0 - prog)
    x0 = max(0, min(dx, int(dx * (0.5 + (pan_dir - 0.5) * 0.70) + head_tilt_x)))
    y0 = max(0, min(dy, int(dy * 0.48 + head_nod_y)))

    base = blended.crop((x0, y0, x0 + cw, y0 + ch)).resize((out_w, out_h), Image.Resampling.BILINEAR).convert("RGBA")
    overlay = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    fx = beat.get("fx_mode", "none")

    if fx == "crowd_walk":
        pass_x = int(out_w * (1.25 - prog * 1.65))
        if -int(out_w * 0.45) < pass_x < int(out_w * 1.15):
            fg_blur = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
            bd = ImageDraw.Draw(fg_blur)
            bd.ellipse([pass_x, int(out_h * 0.14), pass_x + int(out_w * 0.34), int(out_h * 0.44)], fill=(18, 22, 32, 235))
            bd.rounded_rectangle([pass_x - int(out_w * 0.08), int(out_h * 0.40), pass_x + int(out_w * 0.44), out_h + 20], radius=65, fill=(14, 18, 28, 240))
            fg_blur = fg_blur.filter(ImageFilter.GaussianBlur(radius=18))
            overlay = Image.alpha_composite(overlay, fg_blur)
            draw = ImageDraw.Draw(overlay)

    elif fx == "butterfly_petals":
        for i in range(18):
            px = int((i * 97 + global_t * (42 + (i % 4) * 10)) % (out_w + 40)) - 20
            py = int((i * 67 + global_t * (32 + (i % 3) * 9) + math.sin(global_t * 2.2 + i) * 16) % out_h)
            draw.ellipse([px - 6, py - 3, px + 6, py + 3], fill=(244, 114, 182, 185))

        bx = int(out_w * (0.35 + math.sin(global_t * 1.6) * 0.25))
        by = int(out_h * (0.42 + math.cos(global_t * 2.1) * 0.12))
        wing_flap = int(14 * abs(math.sin(global_t * 12.0))) + 4
        bf_layer = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
        bfd = ImageDraw.Draw(bf_layer)
        bfd.ellipse([bx - wing_flap - 8, by - 14, bx, by + 10], fill=(56, 189, 248, 210))
        bfd.ellipse([bx, by - 14, bx + wing_flap + 8, by + 10], fill=(37, 99, 235, 210))
        bf_layer = bf_layer.filter(ImageFilter.GaussianBlur(radius=3))
        overlay = Image.alpha_composite(overlay, bf_layer)
        draw = ImageDraw.Draw(overlay)

    elif fx == "rain":
        for i in range(45):
            rx = int((i * 73 + global_t * 160) % (out_w + 60)) - 30
            ry = int((i * 97 + global_t * 760) % (out_h + 40)) - 20
            draw.line([(rx, ry), (rx - 8, ry + 22)], fill=(186, 230, 253, 145), width=2)

    elif fx == "embers":
        for i in range(22):
            ex = int((i * 89 + math.sin(global_t * 2 + i) * 26) % out_w)
            ey = int((out_h - ((i * 61 + global_t * 95) % out_h)))
            draw.ellipse([ex - 3, ey - 3, ex + 3, ey + 3], fill=(251, 146, 60, 190))

    words = beat.get("words", [])
    if words:
        is_portrait = out_h > out_w
        active_idx = min(len(words) - 1, int(prog * len(words)))
        font_size = max(21, int(out_w * 0.044)) if is_portrait else max(23, int(out_w * 0.026))
        font = get_font(font_size)
        space_w = int(font_size * 0.35)
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

        line_h = int(font_size * 1.32)
        total_h = len(lines) * line_h
        top_y = int(out_h * 0.78) - (total_h // 2)

        for l_idx, tokens in enumerate(lines):
            row_w = sum(tk[2] for tk in tokens) + space_w * (len(tokens) - 1)
            cx = (out_w - row_w) // 2
            cy = top_y + l_idx * line_h

            for wd, idx, ww in tokens:
                is_curr = (idx == active_idx)
                col = (250, 204, 21) if is_curr else (255, 255, 255)
                draw.text(
                    (cx, cy),
                    wd,
                    font=font,
                    fill=(*col, 255),
                    stroke_width=max(3, font_size // 10),
                    stroke_fill=(6, 10, 18, 240)
                )
                cx += ww + space_w

    return Image.alpha_composite(base, overlay).convert("RGB").tobytes()


# ===========================================================================
# 4. PIPELINE & ROUTES
# ===========================================================================
def encode_mp4_from_job(job_id: str):
    job = JOBS[job_id]
    try:
        job["status"] = "rendering"
        job["progress"] = 65
        job["message"] = "Encoding Lip-Sync & Head Motion Video..."

        beats = job["raw_beats"]
        out_w, out_h = job["out_w"], job["out_h"]
        fps = 18
        total = len(beats)

        out_name = f"NEXORA_STUDIO_{job['ratio'].replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        ffmpeg_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
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
                raw_bytes = render_reels_frame(img_a, img_b, out_w, out_h, b, prog, global_t)
                proc.stdin.write(raw_bytes)
                global_t += 1.0 / fps

            job["progress"] = 65 + int(((i + 1) / total) * 34)
            job["message"] = f"Rendering Scene ({i + 1}/{total})..."

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
        gen_w, gen_h = int(out_w * 1.12), int(out_h * 1.12)
        total = len(beats)
        job["raw_beats"] = beats
        job["out_w"] = out_w
        job["out_h"] = out_h
        job["ratio"] = ratio
        job["beats"] = [None] * total

        done_count = 0

        def fetch_shot_pair(idx, b_obj):
            time.sleep((idx % 2) * 0.6)
            seed_base = 9000 + idx * 79
            art_a = fetch_single_art(b_obj["prompt_a"], gen_w, gen_h, seed=seed_base)
            if art_a is None:
                art_a = fetch_single_art("Indian anime character cinematic scene, 8k", gen_w, gen_h, seed=seed_base + 3)
            if art_a is None:
                art_a = Image.new("RGB", (gen_w, gen_h), (24, 28, 42))

            art_b = fetch_single_art(b_obj["prompt_b"], gen_w, gen_h, seed=seed_base + 9)
            if art_b is None:
                art_b = art_a.copy()

            ts = int(time.time())
            file_a = f"{job_id}_s{idx + 1}_a_{ts}.jpg"
            file_b = f"{job_id}_s{idx + 1}_b_{ts}.jpg"
            art_a.save(os.path.join(CACHE_DIR, file_a), quality=88)
            art_b.save(os.path.join(CACHE_DIR, file_b), quality=88)
            return idx, file_a, file_b

        with ThreadPoolExecutor(max_workers=2) as pool:
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
                job["message"] = f"AI Scenes & Lip-Sync Poses Ready: {done_count}/{total}..."

        if auto_render:
            encode_mp4_from_job(job_id)
        else:
            job["status"] = "scenes_ready"
            job["progress"] = 65
            job["message"] = "All Scenes Ready! Review/Regen or Render Final MP4."

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
    JOBS[job_id] = {"status": "running", "progress": 2, "message": "Generating Scenes & Lip-Sync Poses...", "beats": []}
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
    gen_w, gen_h = int(job["out_w"] * 1.12), int(job["out_h"] * 1.12)
    seed_base = int(time.time()) % 99999
    art_a = fetch_single_art(b["prompt_a"], gen_w, gen_h, seed=seed_base)
    if art_a is None:
        return jsonify({"error": "Server busy, try again"}), 503
    art_b = fetch_single_art(b["prompt_b"], gen_w, gen_h, seed=seed_base + 7) or art_a.copy()
    file_a = f"{job_id}_s{shot_idx + 1}_a_{seed_base}.jpg"
    file_b = f"{job_id}_s{shot_idx + 1}_b_{seed_base}.jpg"
    art_a.save(os.path.join(CACHE_DIR, file_a), quality=88)
    art_b.save(os.path.join(CACHE_DIR, file_b), quality=88)
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
    gen_w, gen_h = int(job["out_w"] * 1.12), int(job["out_h"] * 1.12)
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
