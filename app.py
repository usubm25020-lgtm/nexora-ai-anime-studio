
import os
import re
import uuid
import math
import time
import shutil
import random
import threading
import subprocess

import numpy as np
from PIL import Image, ImageDraw

from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename

# ==========================================
# NEXORA 2D ANIMATION ENGINE
# Backend: Python + Flask + Pillow + FFmpeg
# Hosting: Render
# ==========================================

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "renders")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

JOBS = {}
JOBS_LOCK = threading.Lock()

FPS = 24

# ==========================================
# CHARACTER CONFIGURATION
# ==========================================

CHARACTERS = {
    "Aarav": {
        "color": (245, 205, 175),
        "shirt": (35, 105, 190),
        "pants": (35, 38, 50)
    },
    "Maya": {
        "color": (245, 195, 165),
        "shirt": (205, 75, 135),
        "pants": (45, 40, 65)
    },
    "Default": {
        "color": (235, 190, 155),
        "shirt": (40, 120, 200),
        "pants": (35, 35, 45)
    }
}


def clamp(value, low, high):
    return max(low, min(high, value))


def smoothstep(t):
    t = clamp(t, 0, 1)
    return t * t * (3 - 2 * t)


def lerp(a, b, t):
    return a + (b - a) * t


# ==========================================
# SCRIPT INTELLIGENCE
# ==========================================

ACTION_TAGS = [
    "WALK", "RUN", "JUMP", "DANCE",
    "WAVE", "POINT", "FIGHT", "SIT",
    "IDLE", "TALK", "HAPPY", "SAD",
    "ANGRY", "SURPRISED"
]


def parse_script(script):
    lines = []

    for raw in script.splitlines():
        raw = raw.strip()

        if not raw:
            continue

        tags = re.findall(r"\[([^\]]+)\]", raw)
        clean_text = re.sub(r"\[[^\]]+\]", "", raw).strip()

        character = "Aarav"
        action = "TALK"
        emotion = "NEUTRAL"

        for tag in tags:
            tag_upper = tag.upper()

            if tag in CHARACTERS:
                character = tag

            elif tag_upper in ACTION_TAGS:
                action = tag_upper

            elif tag_upper in [
                "HAPPY", "SAD", "ANGRY",
                "SURPRISED", "NEUTRAL"
            ]:
                emotion = tag_upper

        # Automatic action detection
        if not tags:
            text = clean_text.lower()

            if any(x in text for x in ["runs", "running", "দৌড়"]):
                action = "RUN"
            elif any(x in text for x in ["walks", "walking", "হাঁটে"]):
                action = "WALK"
            elif any(x in text for x in ["jumps", "jumping", "লাফ"]):
                action = "JUMP"
            elif any(x in text for x in ["sad", "দুঃখ", "কষ্ট"]):
                emotion = "SAD"
            elif any(x in text for x in ["angry", "রাগ"]):
                emotion = "ANGRY"

        lines.append({
            "text": clean_text,
            "character": character,
            "action": action,
            "emotion": emotion
        })

    return lines or [{
        "text": "Welcome to NEXORA Animation Studio",
        "character": "Aarav",
        "action": "IDLE",
        "emotion": "HAPPY"
    }]


# ==========================================
# SCENE BACKGROUNDS
# ==========================================

def draw_background(draw, width, height, scene, t):
    if scene == "night":
        top = (8, 12, 35)
        bottom = (35, 40, 75)

    elif scene == "room":
        top = (190, 205, 225)
        bottom = (120, 135, 155)

    else:
        top = (100, 175, 235)
        bottom = (205, 225, 240)

    for y in range(height):
        p = y / height
        color = tuple(
            int(lerp(top[i], bottom[i], p))
            for i in range(3)
        )
        draw.line((0, y, width, y), fill=color)

    if scene == "night":
        random.seed(42)

        for _ in range(70):
            x = random.randint(0, width)
            y = random.randint(0, int(height * 0.55))
            r = random.choice([1, 2, 3])
            draw.ellipse(
                (x-r, y-r, x+r, y+r),
                fill=(255, 255, 210)
            )

        draw.ellipse(
            (width * .76, height * .09,
             width * .86, height * .19),
            fill=(245, 240, 210)
        )

    elif scene == "room":
        draw.rectangle(
            (width * .08, height * .12,
             width * .92, height * .75),
            fill=(205, 215, 225)
        )

        draw.rectangle(
            (width * .12, height * .17,
             width * .36, height * .45),
            fill=(90, 155, 205)
        )

        draw.rectangle(
            (width * .12, height * .45,
             width * .36, height * .47),
            fill=(240, 240, 245)
        )

        draw.rectangle(
            (width * .55, height * .48,
             width * .88, height * .72),
            fill=(115, 75, 60)
        )

    else:
        # Distant buildings
        random.seed(20)

        for i in range(9):
            x = i * width / 8
            h = random.randint(
                int(height * .15),
                int(height * .35)
            )

            draw.rectangle(
                (x, height * .58 - h,
                 x + width * .11, height * .58),
                fill=(105, 125, 150)
            )

        # Road
        draw.rectangle(
            (0, height * .72, width, height),
            fill=(65, 70, 80)
        )

        for x in range(0, width, 140):
            draw.rectangle(
                (x, height * .85,
                 x + 65, height * .86),
                fill=(240, 220, 160)
            )


# ==========================================
# CHARACTER ANIMATION ENGINE
# ==========================================

def draw_character(draw, cx, cy, scale, action,
                   emotion, t, config, facing=1):

    skin = config["color"]
    shirt = config["shirt"]
    pants = config["pants"]

    phase = t * 6

    # Movement system
    bounce = 0
    body_angle = 0
    arm_swing = math.sin(phase) * 0.35
    leg_swing = math.sin(phase) * 0.45

    if action == "WALK":
        bounce = abs(math.sin(phase)) * 5 * scale
        arm_swing = math.sin(phase) * 0.55
        leg_swing = math.sin(phase) * 0.65

    elif action == "RUN":
        bounce = abs(math.sin(phase * 1.4)) * 12 * scale
        arm_swing = math.sin(phase * 1.4) * 0.9
        leg_swing = math.sin(phase * 1.4) * 1.0
        body_angle = -0.12

    elif action == "JUMP":
        bounce = abs(math.sin(t * 3)) * 65 * scale

    elif action == "DANCE":
        bounce = abs(math.sin(phase * 1.5)) * 13 * scale
        arm_swing = math.sin(phase * 1.5) * 1.1
        leg_swing = math.sin(phase * 1.5) * 0.8

    elif action == "SIT":
        leg_swing = 0
        bounce = -18 * scale

    elif action == "FIGHT":
        arm_swing = math.sin(phase * 2) * 1.2
        body_angle = math.sin(phase * 2) * 0.12

    cy -= bounce

    # Body geometry
    head_r = 24 * scale
    head_x = cx
    head_y = cy - 105 * scale

    shoulder_y = cy - 70 * scale
    hip_y = cy + 12 * scale

    # Head
    draw.ellipse(
        (
            head_x - head_r,
            head_y - head_r,
            head_x + head_r,
            head_y + head_r
        ),
        fill=skin,
        outline=(35, 30, 30),
        width=max(2, int(2 * scale))
    )

    # Hair
    draw.arc(
        (
            head_x - head_r,
            head_y - head_r - 5 * scale,
            head_x + head_r,
            head_y + head_r * .45
        ),
        180, 350,
        fill=(35, 25, 25),
        width=max(4, int(7 * scale))
    )

    # Eyes
    eye_y = head_y - 3 * scale

    if emotion == "SAD":
        draw.line(
            (head_x - 12*scale, eye_y,
             head_x - 5*scale, eye_y + 3*scale),
            fill=(20, 20, 25), width=3
        )
        draw.line(
            (head_x + 5*scale, eye_y + 3*scale,
             head_x + 12*scale, eye_y),
            fill=(20, 20, 25), width=3
        )

    else:
        draw.ellipse(
            (head_x - 13*scale, eye_y - 3*scale,
             head_x - 8*scale, eye_y + 4*scale),
            fill=(20, 20, 25)
        )
        draw.ellipse(
            (head_x + 8*scale, eye_y - 3*scale,
             head_x + 13*scale, eye_y + 4*scale),
            fill=(20, 20, 25)
        )

    # Eyebrows
    if emotion == "ANGRY":
        draw.line(
            (head_x - 16*scale, eye_y - 9*scale,
             head_x - 5*scale, eye_y - 5*scale),
            fill=(35, 25, 25), width=3
        )
        draw.line(
            (head_x + 5*scale, eye_y - 5*scale,
             head_x + 16*scale, eye_y - 9*scale),
            fill=(35, 25, 25), width=3
        )

    # Mouth
    if emotion == "HAPPY":
        draw.arc(
            (head_x - 9*scale, head_y + 3*scale,
             head_x + 9*scale, head_y + 17*scale),
            0, 180, fill=(100, 25, 30), width=3
        )

    elif emotion == "SAD":
        draw.arc(
            (head_x - 8*scale, head_y + 12*scale,
             head_x + 8*scale, head_y + 24*scale),
            180, 360, fill=(100, 25, 30), width=3
        )

    elif emotion == "SURPRISED":
        draw.ellipse(
            (head_x - 4*scale, head_y + 7*scale,
             head_x + 4*scale, head_y + 16*scale),
            outline=(100, 25, 30), width=2
        )

    else:
        draw.line(
            (head_x - 7*scale, head_y + 11*scale,
             head_x + 7*scale, head_y + 11*scale),
            fill=(100, 25, 30), width=2
        )

    # Body
    body_top = shoulder_y
    body_bottom = hip_y

    draw.line(
        (cx, body_top, cx, body_bottom),
        fill=shirt,
        width=max(8, int(23 * scale))
    )

    # Shoulder points
    left_shoulder = (cx - 14*scale, shoulder_y)
    right_shoulder = (cx + 14*scale, shoulder_y)

    # Arms
    arm_length = 43 * scale

    left_arm_angle = arm_swing
    right_arm_angle = -arm_swing

    if action == "WAVE":
        right_arm_angle = -1.9 + math.sin(t * 5) * .2

    elif action == "POINT":
        right_arm_angle = -1.45

    elif action == "DANCE":
        left_arm_angle = -1.8
        right_arm_angle = -1.8

    elif action == "FIGHT":
        left_arm_angle = -1.1
        right_arm_angle = -2.0

    def arm(start, angle, direction):
        elbow = (
            start[0] + math.sin(angle) * arm_length,
            start[1] + math.cos(angle) * arm_length
        )

        hand = (
            elbow[0] + math.sin(angle) * arm_length * .75,
            elbow[1] + math.cos(angle) * arm_length * .75
        )

        draw.line(
            (start[0], start[1], elbow[0], elbow[1]),
            fill=skin, width=max(5, int(8*scale))
        )

        draw.line(
            (elbow[0], elbow[1], hand[0], hand[1]),
            fill=skin, width=max(5, int(7*scale))
        )

        draw.ellipse(
            (hand[0]-4*scale, hand[1]-4*scale,
             hand[0]+4*scale, hand[1]+4*scale),
            fill=skin
        )

    arm(left_shoulder, left_arm_angle, -1)
    arm(right_shoulder, right_arm_angle, 1)

    # Legs
    leg_length = 47 * scale

    left_angle = leg_swing
    right_angle = -leg_swing

    if action == "SIT":
        left_angle = 1.3
        right_angle = 1.3

    def leg(x, angle):
        knee = (
            x + math.sin(angle) * leg_length,
            hip_y + math.cos(angle) * leg_length
        )

        foot = (
            knee[0] + math.sin(angle) * leg_length,
            knee[1] + math.cos(angle) * leg_length
        )

        draw.line(
            (x, hip_y, knee[0], knee[1]),
            fill=pants, width=max(7, int(12*scale))
        )

        draw.line(
            (knee[0], knee[1], foot[0], foot[1]),
            fill=pants, width=max(7, int(11*scale))
        )

        draw.line(
            (foot[0], foot[1],
             foot[0] + 13*scale, foot[1]),
            fill=(25, 25, 30),
            width=max(5, int(8*scale))
        )

    leg(cx - 9*scale, left_angle)
    leg(cx + 9*scale, right_angle)


# ==========================================
# SCENE SELECTION
# ==========================================

def detect_scene(script):
    text = script.lower()

    if any(x in text for x in [
        "bedroom", "room", "house", "ঘর", "বাড়ি"
    ]):
        return "room"

    if any(x in text for x in [
        "night", "midnight", "dark", "রাত"
    ]):
        return "night"

    return "outdoor"


# ==========================================
# FRAME RENDERER
# ==========================================

def render_frame(width, height, beat, scene, t, total):
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    draw_background(draw, width, height, scene, t)

    action = beat["action"]
    emotion = beat["emotion"]

    progress = t / max(total, 0.01)

    # Character movement
    if action in ["WALK", "RUN"]:
        cx = width * .5 + math.sin(t * 1.5) * width * .12
    else:
        cx = width * .5

    cy = height * .72

    character_name = beat["character"]
    config = CHARACTERS.get(
        character_name,
        CHARACTERS["Default"]
    )

    scale = min(width / 720, height / 1280) * 1.15

    draw_character(
        draw,
        cx,
        cy,
        scale,
        action,
        emotion,
        t,
        config
    )

    # Subtitle
    text = beat["text"]

    if text:
        font_size = max(16, int(width * .035))

        # Basic text wrapping
        words = text.split()
        lines = []
        current = ""

        for word in words:
            test = (current + " " + word).strip()

            if len(test) > 35:
                lines.append(current)
                current = word
            else:
                current = test

        if current:
            lines.append(current)

        y = int(height * .88)

        for line in lines[:3]:
            # Background strip
            draw.rounded_rectangle(
                (
                    width * .06,
                    y - font_size,
                    width * .94,
                    y + font_size * 1.2
                ),
                radius=12,
                fill=(10, 10, 15)
            )

            draw.text(
                (width * .08, y - font_size * .6),
                line,
                fill=(255, 255, 255)
            )

            y += font_size * 2

    # NEXORA watermark
    draw.text(
        (20, 20),
        "NEXORA STUDIO",
        fill=(210, 225, 255)
    )

    return img


# ==========================================
# FFMPEG VIDEO EXPORT
# ==========================================

def find_ffmpeg():
    candidates = [
        shutil.which("ffmpeg"),
        "/usr/bin/ffmpeg"
    ]

    for item in candidates:
        if item and os.path.exists(item):
            return item

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def export_video(frames_dir, output_path, fps):
    ffmpeg = find_ffmpeg()

    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg not found. Install imageio-ffmpeg."
        )

    command = [
        ffmpeg,
        "-y",
        "-framerate", str(fps),
        "-i", os.path.join(frames_dir, "frame_%06d.jpg"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        "-crf", "22",
        "-movflags", "+faststart",
        output_path
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:])


# ==========================================
# ANIMATION JOB
# ==========================================

def run_animation_job(job_id, data):
    try:
        script = data["script"]
        ratio = data.get("ratio", "9:16")

        if ratio == "16:9":
            width, height = 1280, 720
        else:
            width, height = 720, 1280

        beats = parse_script(script)
        scene = detect_scene(script)

        # Duration per dialogue beat
        beat_duration = 3.5
        total_frames = len(beats) * beat_duration * FPS

        frames_dir = os.path.join(
            OUTPUT_DIR, job_id + "_frames"
        )

        os.makedirs(frames_dir, exist_ok=True)

        frame_number = 0

        for beat_index, beat in enumerate(beats):
            for local_frame in range(int(beat_duration * FPS)):
                t = local_frame / FPS

                frame = render_frame(
                    width,
                    height,
                    beat,
                    scene,
                    t,
                    beat_duration
                )

                frame_path = os.path.join(
                    frames_dir,
                    f"frame_{frame_number:06d}.jpg"
                )

                frame.save(
                    frame_path,
                    "JPEG",
                    quality=88
                )

                frame_number += 1

                progress = int(
                    frame_number / total_frames * 90
                )

                with JOBS_LOCK:
                    JOBS[job_id]["progress"] = progress
                    JOBS[job_id]["status"] = "rendering"

        output_path = os.path.join(
            OUTPUT_DIR, job_id + ".mp4"
        )

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "encoding"
            JOBS[job_id]["progress"] = 92

        export_video(frames_dir, output_path, FPS)

        shutil.rmtree(frames_dir, ignore_errors=True)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "completed"
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["video"] = output_path

    except Exception as error:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = str(error)


# ==========================================
# API ROUTES
# ==========================================

@app.route("/")
def home():
    return jsonify({
        "app": "NEXORA 2D Animation Engine",
        "version": "2.0",
        "status": "online"
    })


@app.route("/api/ping")
def ping():
    return jsonify({
        "success": True,
        "engine": "NEXORA",
        "version": "2.0"
    })


@app.route("/api/start", methods=["POST"])
def start_render():
    script = request.form.get("script", "").strip()

    if not script:
        return jsonify({
            "success": False,
            "error": "Please enter an animation script."
        }), 400

    job_id = str(uuid.uuid4())

    data = {
        "script": script,
        "ratio": request.form.get("ratio", "9:16")
    }

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "video": None,
            "error": None
        }

    thread = threading.Thread(
        target=run_animation_job,
        args=(job_id, data),
        daemon=True
    )

    thread.start()

    return jsonify({
        "success": True,
        "job_id": job_id,
        "message": "NEXORA rendering started."
    })


@app.route("/api/status/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

        if not job:
            return jsonify({
                "success": False,
                "error": "Job not found."
            }), 404

        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "error": job["error"],
            "video_url": (
                f"/api/video/{job_id}"
                if job["status"] == "completed"
                else None
            )
        })


@app.route("/api/video/<job_id>")
def download_video(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

        if not job or job["status"] != "completed":
            return jsonify({
                "success": False,
                "error": "Video is not ready."
            }), 404

        path = job["video"]

    if not path or not os.path.exists(path):
        return jsonify({
            "success": False,
            "error": "Video file not found."
        }), 404

    return send_file(
        path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name="NEXORA_Animation.mp4"
    )


# ==========================================
# SERVER START
# ==========================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True
    )
