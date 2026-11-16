
import os
import re
import io
import gc
import json
import time
import math
import uuid
import shutil
import asyncio
import random
import threading
import subprocess
import traceback

from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import imageio_ffmpeg

from PIL import (
    Image,
    ImageDraw,
    ImageFont,
    ImageFilter
)

from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    send_from_directory,
    abort
)

from werkzeug.utils import secure_filename


# ============================================================
# NEXORA AI ANIMATION STUDIO
# COMPLETE PROCEDURAL 2D ANIMATION BACKEND
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "nexora_data"
JOB_DIR = DATA_DIR / "jobs"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "videos"

for folder in [DATA_DIR, JOB_DIR, UPLOAD_DIR, OUTPUT_DIR]:
    folder.mkdir(parents=True, exist_ok=True)


app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

app.config["JSON_SORT_KEYS"] = False


# ============================================================
# SETTINGS
# ============================================================

DEFAULT_FPS = 24
DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 1280

MAX_DURATION = 180
MAX_WORKERS = 1

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

jobs = {}
jobs_lock = threading.Lock()

ALLOWED_AUDIO = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".flac",
    ".mp4",
    ".webm"
}


# ============================================================
# FONT SYSTEM
# ============================================================

def load_font(size=32, bold=False):

    candidates = []

    if bold:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        ])

    else:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ])

    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass

    return ImageFont.load_default()


# ============================================================
# JOB MANAGEMENT
# ============================================================

def update_job(job_id, **values):

    with jobs_lock:

        if job_id not in jobs:
            return

        jobs[job_id].update(values)

        jobs[job_id]["updated_at"] = time.time()


def get_job(job_id):

    with jobs_lock:
        return jobs.get(job_id)


# ============================================================
# SCRIPT PARSER
# ============================================================

ACTION_TAGS = {
    "WALK",
    "RUN",
    "JUMP",
    "DANCE",
    "WAVE",
    "POINT",
    "FIGHT",
    "SIT",
    "IDLE",
    "TALK",
    "WINDOW_EXIT",
    "WINDOW_ENTER",
    "TURN",
    "LOOK",
    "LOOK_UP",
    "LOOK_DOWN",
    "CLAP",
    "CRY",
    "LAUGH",
    "THINK",
    "SLEEP",
    "FALL",
    "STAND",
    "KICK",
    "PUNCH",
    "HUG",
    "HANDSHAKE",
    "PHONE",
    "READ",
    "WRITE",
    "RUN_AWAY",
    "ENTER",
    "EXIT",
    "KNEEL",
    "SLOW_WALK"
}


EMOTION_TAGS = {
    "HAPPY",
    "SAD",
    "ANGRY",
    "SURPRISED",
    "CONFUSED",
    "ROMANTIC",
    "HEROIC",
    "SCARED",
    "SHY",
    "CRYING",
    "LAUGHING",
    "NEUTRAL",
    "EXCITED",
    "SERIOUS",
    "DISAPPOINTED",
    "LOVE",
    "WORRIED",
    "SMILE"
}


OBJECT_TAGS = {
    "PAPER",
    "PHONE",
    "BOOK",
    "CHAIR",
    "TABLE",
    "WINDOW",
    "DOOR",
    "CAR",
    "FLOWER",
    "GIFT",
    "BAG",
    "SWORD",
    "BALL",
    "LAMP",
    "CUP"
}


SCENE_TAGS = {
    "NIGHT",
    "DAY",
    "ROOM",
    "OUTDOOR",
    "RAIN",
    "SUNSET",
    "SUNRISE",
    "CITY",
    "FOREST",
    "BEACH",
    "SCHOOL",
    "HOME",
    "DARK",
    "ROMANTIC",
    "CINEMATIC",
    "DREAM",
    "FLASHBACK",
    "HEROIC"
}


def parse_script(script):

    lines = []

    current_character = "Narrator"

    current_action = "IDLE"

    current_emotion = "NEUTRAL"

    current_scene = "ROOM"

    current_tags = []

    raw_lines = script.replace("\r", "").split("\n")

    for raw in raw_lines:

        line = raw.strip()

        if not line:
            continue

        tags = re.findall(r"\[([^\]]+)\]", line)

        clean_text = re.sub(
            r"\[[^\]]+\]",
            "",
            line
        ).strip()

        for tag in tags:

            tag = tag.strip().upper()

            if tag in ACTION_TAGS:
                current_action = tag

            elif tag in EMOTION_TAGS:
                current_emotion = tag

            elif tag in SCENE_TAGS:
                current_scene = tag

            elif tag in OBJECT_TAGS:
                current_tags.append(tag)

            elif tag not in ["CAMERA", "CUT", "FADE"]:
                if len(tag) < 40:
                    current_character = tag.title()

        if clean_text:

            # Character name format:
            # Aarav: Hello
            match = re.match(
                r"^([A-Za-z][A-Za-z0-9 _-]{0,25}):\s*(.*)$",
                clean_text
            )

            if match:

                current_character = match.group(1).strip()

                clean_text = match.group(2).strip()

            if clean_text:

                lines.append({
                    "character": current_character,
                    "text": clean_text,
                    "action": current_action,
                    "emotion": current_emotion,
                    "scene": current_scene,
                    "objects": list(current_tags)
                })

                current_action = "TALK"
                current_emotion = "NEUTRAL"
                current_tags = []

    if not lines:

        lines.append({
            "character": "Narrator",
            "text": script.strip() or "Welcome to NEXORA Animation Studio.",
            "action": "TALK",
            "emotion": "NEUTRAL",
            "scene": "ROOM",
            "objects": []
        })

    return lines


# ============================================================
# AUDIO / TEXT TO SPEECH
# ============================================================

def run_async(coro):

    try:
        return asyncio.run(coro)

    except RuntimeError:

        loop = asyncio.new_event_loop()

        try:
            return loop.run_until_complete(coro)

        finally:
            loop.close()


async def generate_tts(text, output_path, language):

    import edge_tts

    language = language.lower()

    if language == "hindi":

        voice = "hi-IN-SwaraNeural"

    else:

        voice = "en-US-JennyNeural"

    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate="+0%",
        volume="+0%"
    )

    await communicate.save(str(output_path))


def make_voice_audio(lines, output_path, language):

    try:

        texts = []

        for line in lines:

            text = line.get("text", "").strip()

            if text:
                texts.append(text)

        combined = ". ".join(texts)

        if not combined:
            return False

        run_async(
            generate_tts(
                combined,
                output_path,
                language
            )
        )

        return output_path.exists()

    except Exception as e:

        print("TTS ERROR:", str(e))

        return False


# ============================================================
# FFMPEG
# ============================================================

def get_ffmpeg():

    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_duration(file_path):

    ffmpeg = get_ffmpeg()

    command = [
        ffmpeg,
        "-i",
        str(file_path)
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    output = result.stderr

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)",
        output
    )

    if not match:
        return 0

    h, m, s, ms = match.groups()

    return (
        int(h) * 3600
        + int(m) * 60
        + int(s)
        + float("0." + ms)
    )


def encode_audio_video(
    silent_video,
    output_video,
    audio_file=None
):

    ffmpeg = get_ffmpeg()

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(silent_video)
    ]

    if audio_file and Path(audio_file).exists():

        command.extend([
            "-i",
            str(audio_file),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest"
        ])

    else:

        command.extend([
            "-an"
        ])

    command.extend([
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_video)
    ])

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        raise RuntimeError(
            "FFmpeg Encoding Failed: "
            + result.stderr[-2000:]
        )


# ============================================================
# COLOR SYSTEM
# ============================================================

def color_for_scene(scene):

    colors = {

        "ROOM": ((12, 19, 38), (29, 41, 70)),
        "NIGHT": ((4, 8, 25), (20, 35, 75)),
        "OUTDOOR": ((55, 115, 170), (180, 210, 220)),
        "DAY": ((70, 145, 220), (190, 225, 245)),
        "SUNSET": ((90, 45, 95), (245, 145, 100)),
        "SUNRISE": ((80, 100, 160), (245, 190, 120)),
        "RAIN": ((15, 25, 50), (45, 75, 110)),
        "CITY": ((10, 15, 35), (55, 65, 100)),
        "FOREST": ((5, 40, 25), (35, 100, 60)),
        "BEACH": ((25, 100, 160), (220, 195, 140)),
        "SCHOOL": ((50, 75, 110), (150, 180, 200)),
        "HOME": ((25, 30, 50), (75, 80, 105)),
        "DARK": ((0, 0, 0), (12, 12, 25)),
        "DREAM": ((35, 10, 70), (100, 70, 160)),
        "FLASHBACK": ((70, 60, 40), (160, 135, 90))
    }

    return colors.get(
        scene,
        colors["ROOM"]
    )


def make_gradient(width, height, scene):

    top, bottom = color_for_scene(scene)

    img = Image.new(
        "RGB",
        (width, height)
    )

    draw = ImageDraw.Draw(img)

    for y in range(height):

        ratio = y / max(1, height - 1)

        r = int(top[0] * (1-ratio) + bottom[0] * ratio)
        g = int(top[1] * (1-ratio) + bottom[1] * ratio)
        b = int(top[2] * (1-ratio) + bottom[2] * ratio)

        draw.line(
            [(0, y), (width, y)],
            fill=(r, g, b)
        )

    return img


# ============================================================
# BACKGROUND SCENES
# ============================================================

def draw_background(draw, width, height, scene, frame):

    scene = scene.upper()

    horizon = int(height * 0.72)

    if scene in ["ROOM", "HOME", "SCHOOL"]:

        draw.rectangle(
            [0, horizon, width, height],
            fill=(35, 35, 55)
        )

        # Floor lines
        for x in range(0, width, 90):

            draw.line(
                [(x, horizon), (x - 50, height)],
                fill=(65, 65, 85),
                width=2
            )

        # Window
        wx = int(width * 0.67)
        wy = int(height * 0.15)

        draw.rectangle(
            [wx, wy, wx + 170, wy + 220],
            fill=(20, 45, 85),
            outline=(140, 180, 220),
            width=6
        )

        draw.line(
            [(wx + 85, wy), (wx + 85, wy + 220)],
            fill=(140, 180, 220),
            width=5
        )

        draw.line(
            [(wx, wy + 110), (wx + 170, wy + 110)],
            fill=(140, 180, 220),
            width=5
        )

        # Lamp
        draw.ellipse(
            [
                width - 100,
                80,
                width - 40,
                140
            ],
            fill=(255, 210, 120)
        )

    elif scene == "CITY":

        draw.rectangle(
            [0, horizon, width, height],
            fill=(20, 22, 35)
        )

        for x in range(0, width, 100):

            building_height = 150 + (x * 7 % 220)

            draw.rectangle(
                [
                    x,
                    horizon - building_height,
                    x + 75,
                    horizon
                ],
                fill=(18, 24, 45)
            )

            for wy in range(
                horizon - building_height + 20,
                horizon - 15,
                45
            ):

                draw.rectangle(
                    [x + 12, wy, x + 25, wy + 15],
                    fill=(255, 205, 110)
                )

    elif scene == "FOREST":

        draw.rectangle(
            [0, horizon, width, height],
            fill=(12, 50, 30)
        )

        for x in range(20, width, 110):

            draw.rectangle(
                [
                    x + 25,
                    170,
                    x + 50,
                    horizon
                ],
                fill=(70, 45, 25)
            )

            draw.ellipse(
                [
                    x - 35,
                    80,
                    x + 110,
                    260
                ],
                fill=(15, 85, 45)
            )

    elif scene == "BEACH":

        draw.rectangle(
            [0, horizon - 80, width, height],
            fill=(205, 180, 120)
        )

        draw.rectangle(
            [0, horizon - 100, width, horizon],
            fill=(35, 125, 180)
        )

    else:

        draw.rectangle(
            [0, horizon, width, height],
            fill=(30, 35, 50)
        )

    # Rain particles
    if scene == "RAIN":

        random.seed(frame // 2)

        for _ in range(80):

            x = random.randint(0, width)
            y = random.randint(0, height)

            draw.line(
                [(x, y), (x - 8, y + 22)],
                fill=(130, 170, 220),
                width=2
            )


# ============================================================
# CHARACTER DRAWING ENGINE
# ============================================================

def draw_character(
    image,
    x,
    y,
    scale,
    name,
    style,
    action,
    emotion,
    frame,
    facing=1
):

    draw = ImageDraw.Draw(image)

    style = style.lower()

    t = frame / 8.0

    walk = math.sin(t) * 18
    bounce = abs(math.sin(t)) * 12

    if action in ["JUMP", "FALL"]:
        bounce = abs(math.sin(t)) * 70

    if action in ["SIT"]:
        y += int(40 * scale)

    x = int(x)
    y = int(y)

    head_size = int(70 * scale)

    body_width = int(50 * scale)
    body_height = int(130 * scale)

    skin = (235, 180, 145)
    shirt = (40, 170, 230)
    pants = (30, 40, 65)
    hair = (35, 25, 20)

    if style == "stickman":

        # Head
        draw.ellipse(
            [
                x - head_size // 2,
                y - head_size,
                x + head_size // 2,
                y
            ],
            outline=(245, 245, 255),
            width=max(3, int(5 * scale))
        )

        # Body
        draw.line(
            [
                (x, y),
                (x, y + int(110 * scale))
            ],
            fill=(245, 245, 255),
            width=max(3, int(6 * scale))
        )

        # Arms
        arm_move = int(walk * scale)

        draw.line(
            [
                (x, y + int(30 * scale)),
                (x - int(45 * scale) - arm_move, y + int(70 * scale))
            ],
            fill=(245, 245, 255),
            width=max(3, int(5 * scale))
        )

        draw.line(
            [
                (x, y + int(30 * scale)),
                (x + int(45 * scale) + arm_move, y + int(70 * scale))
            ],
            fill=(245, 245, 255),
            width=max(3, int(5 * scale))
        )

        # Legs
        leg_move = int(walk * scale)

        draw.line(
            [
                (x, y + int(110 * scale)),
                (x - int(30 * scale) + leg_move, y + int(175 * scale))
            ],
            fill=(245, 245, 255),
            width=max(3, int(5 * scale))
        )

        draw.line(
            [
                (x, y + int(110 * scale)),
                (x + int(30 * scale) - leg_move, y + int(175 * scale))
            ],
            fill=(245, 245, 255),
            width=max(3, int(5 * scale))
        )

    else:

        # Shadow
        draw.ellipse(
            [
                x - int(65 * scale),
                y + int(165 * scale),
                x + int(65 * scale),
                y + int(185 * scale)
            ],
            fill=(15, 15, 25)
        )

        # Legs
        leg_move = int(walk * scale)

        draw.line(
            [
                (x - int(12 * scale), y + int(100 * scale)),
                (x - int(25 * scale) + leg_move, y + int(180 * scale))
            ],
            fill=pants,
            width=max(5, int(22 * scale))
        )

        draw.line(
            [
                (x + int(12 * scale), y + int(100 * scale)),
                (x + int(25 * scale) - leg_move, y + int(180 * scale))
            ],
            fill=pants,
            width=max(5, int(22 * scale))
        )

        # Shoes
        draw.ellipse(
            [
                x - int(55 * scale),
                y + int(165 * scale),
                x - int(10 * scale),
                y + int(190 * scale)
            ],
            fill=(20, 20, 25)
        )

        draw.ellipse(
            [
                x + int(10 * scale),
                y + int(165 * scale),
                x + int(55 * scale),
                y + int(190 * scale)
            ],
            fill=(20, 20, 25)
        )

        # Body
        draw.rounded_rectangle(
            [
                x - body_width // 2,
                y,
                x + body_width // 2,
                y + body_height
            ],
            radius=int(18 * scale),
            fill=shirt
        )

        # Arms
        arm_y = y + int(25 * scale)

        arm_length = int(65 * scale)

        if action == "WAVE":
            left_end = (x - arm_length, arm_y + arm_length)
            right_end = (
                x + arm_length,
                arm_y - int(70 * scale) + int(math.sin(t * 3) * 15)
            )

        elif action == "POINT":

            left_end = (x - arm_length, arm_y + arm_length)
            right_end = (x + arm_length * 2, arm_y - 5)

        elif action in ["FIGHT", "PUNCH", "KICK"]:

            left_end = (
                x - arm_length,
                arm_y - int(15 * scale)
            )

            right_end = (
                x + arm_length,
                arm_y + int(20 * scale)
            )

        else:

            left_end = (
                x - arm_length,
                arm_y + int(walk * scale)
            )

            right_end = (
                x + arm_length,
                arm_y - int(walk * scale)
            )

        draw.line(
            [
                (x - body_width // 2, arm_y),
                left_end
            ],
            fill=skin,
            width=max(5, int(17 * scale))
        )

        draw.line(
            [
                (x + body_width // 2, arm_y),
                right_end
            ],
            fill=skin,
            width=max(5, int(17 * scale))
        )

        # Head
        head_top = y - head_size

        draw.ellipse(
            [
                x - head_size // 2,
                head_top,
                x + head_size // 2,
                y + int(12 * scale)
            ],
            fill=skin
        )

        # Hair
        draw.pieslice(
            [
                x - head_size // 2,
                head_top - int(12 * scale),
                x + head_size // 2,
                y
            ],
            180,
            360,
            fill=hair
        )

        # Eyes
        eye_y = head_top + int(35 * scale)

        eye_size = max(2, int(5 * scale))

        draw.ellipse(
            [
                x - int(18 * scale),
                eye_y,
                x - int(18 * scale) + eye_size,
                eye_y + eye_size
            ],
            fill=(15, 15, 20)
        )

        draw.ellipse(
            [
                x + int(12 * scale),
                eye_y,
                x + int(12 * scale) + eye_size,
                eye_y + eye_size
            ],
            fill=(15, 15, 20)
        )

        # Emotion mouth
        mouth_y = head_top + int(52 * scale)

        if emotion in ["HAPPY", "SMILE", "LAUGHING", "EXCITED"]:

            draw.arc(
                [
                    x - int(15 * scale),
                    mouth_y - int(5 * scale),
                    x + int(15 * scale),
                    mouth_y + int(15 * scale)
                ],
                0,
                180,
                fill=(100, 25, 25),
                width=max(2, int(3 * scale))
            )

        elif emotion in ["SAD", "CRYING", "DISAPPOINTED"]:

            draw.arc(
                [
                    x - int(15 * scale),
                    mouth_y,
                    x + int(15 * scale),
                    mouth_y + int(18 * scale)
                ],
                180,
                360,
                fill=(100, 25, 25),
                width=max(2, int(3 * scale))
            )

        elif emotion in ["SURPRISED", "SCARED"]:

            draw.ellipse(
                [
                    x - int(6 * scale),
                    mouth_y,
                    x + int(6 * scale),
                    mouth_y + int(12 * scale)
                ],
                fill=(80, 20, 20)
            )

        else:

            draw.line(
                [
                    (x - int(12 * scale), mouth_y + int(5 * scale)),
                    (x + int(12 * scale), mouth_y + int(5 * scale))
                ],
                fill=(100, 25, 25),
                width=max(2, int(3 * scale))
            )

        # Simple tears
        if emotion in ["CRYING", "SAD"]:

            draw.line(
                [
                    (x - int(18 * scale), eye_y + 5),
                    (x - int(18 * scale), eye_y + int(25 * scale))
                ],
                fill=(80, 180, 255),
                width=max(2, int(3 * scale))
            )

    # Character name
    font = load_font(
        max(12, int(17 * scale)),
        True
    )

    try:

        draw.text(
            (x, y + int(205 * scale)),
            name,
            font=font,
            fill=(240, 245, 255),
            anchor="mm"
        )

    except Exception:
        pass


# ============================================================
# OBJECT RENDERING
# ============================================================

def draw_objects(draw, objects, width, height, frame):

    if not objects:
        return

    for obj in objects:

        obj = obj.upper()

        if obj == "PAPER":

            x = int(width * 0.68)
            y = int(height * 0.55)

            draw.rectangle(
                [x, y, x + 90, y + 120],
                fill=(245, 240, 220),
                outline=(30, 30, 30),
                width=3
            )

            for i in range(4):

                draw.line(
                    [
                        (x + 12, y + 25 + i * 20),
                        (x + 75, y + 25 + i * 20)
                    ],
                    fill=(100, 100, 100),
                    width=2
                )

        elif obj == "PHONE":

            x = int(width * 0.68)
            y = int(height * 0.55)

            draw.rounded_rectangle(
                [x, y, x + 55, y + 105],
                radius=8,
                fill=(15, 15, 20),
                outline=(180, 190, 210),
                width=3
            )

            draw.rectangle(
                [x + 5, y + 10, x + 50, y + 85],
                fill=(50, 100, 160)
            )

        elif obj == "FLOWER":

            x = int(width * 0.7)
            y = int(height * 0.55)

            draw.line(
                [(x, y), (x, y + 100)],
                fill=(20, 150, 60),
                width=5
            )

            draw.ellipse(
                [x - 18, y - 18, x + 18, y + 18],
                fill=(255, 100, 150)
            )

        elif obj == "BOOK":

            x = int(width * 0.65)
            y = int(height * 0.56)

            draw.rectangle(
                [x, y, x + 100, y + 75],
                fill=(160, 70, 50),
                outline=(240, 220, 170),
                width=3
            )

        elif obj == "CUP":

            x = int(width * 0.7)
            y = int(height * 0.62)

            draw.rectangle(
                [x, y, x + 45, y + 55],
                fill=(230, 230, 240),
                outline=(80, 80, 90),
                width=3
            )


# ============================================================
# SUBTITLE SYSTEM
# ============================================================

def draw_subtitle(image, text, width, height):

    if not text:
        return

    draw = ImageDraw.Draw(image)

    font = load_font(
        max(20, int(width * 0.033)),
        True
    )

    max_chars = 38

    words = text.split()

    lines = []
    current = ""

    for word in words:

        test = (
            current + " " + word
        ).strip()

        if len(test) > max_chars:

            if current:
                lines.append(current)

            current = word

        else:

            current = test

    if current:
        lines.append(current)

    lines = lines[:3]

    line_height = int(width * 0.05)

    total_height = len(lines) * line_height

    y = height - total_height - int(height * 0.11)

    draw.rounded_rectangle(
        [
            20,
            y - 15,
            width - 20,
            y + total_height + 15
        ],
        radius=15,
        fill=(5, 8, 18)
    )

    for line in lines:

        draw.text(
            (width // 2, y),
            line,
            font=font,
            fill=(255, 255, 255),
            anchor="mt",
            stroke_width=1,
            stroke_fill=(0, 0, 0)
        )

        y += line_height


# ============================================================
# ANIMATION RENDERER
# ============================================================

def render_animation(
    lines,
    output_file,
    ratio,
    style,
    fps,
    job_id
):

    if ratio == "16:9":

        width = 1280
        height = 720

    else:

        width = 720
        height = 1280

    width = min(width, 1280)
    height = min(height, 1280)

    width = width - (width % 2)
    height = height - (height % 2)

    scene_duration = 3.5

    total_frames = int(
        len(lines) * scene_duration * fps
    )

    total_frames = min(
        total_frames,
        MAX_DURATION * fps
    )

    ffmpeg = get_ffmpeg()

    command = [
        ffmpeg,
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_file)
    ]

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )

    try:

        frame_index = 0

        for scene_index, line in enumerate(lines):

            scene = line.get("scene", "ROOM")

            action = line.get("action", "TALK")

            emotion = line.get("emotion", "NEUTRAL")

            character = line.get("character", "Character")

            objects = line.get("objects", [])

            duration_frames = int(
                scene_duration * fps
            )

            for local_frame in range(duration_frames):

                img = make_gradient(
                    width,
                    height,
                    scene
                )

                draw = ImageDraw.Draw(img)

                draw_background(
                    draw,
                    width,
                    height,
                    scene,
                    frame_index
                )

                draw_objects(
                    draw,
                    objects,
                    width,
                    height,
                    frame_index
                )

                if ratio == "16:9":

                    char_x = int(width * 0.5)
                    char_y = int(height * 0.48)
                    scale = 1.2

                else:

                    char_x = int(width * 0.5)
                    char_y = int(height * 0.48)
                    scale = 1.5

                # Character motion
                if action in [
                    "WALK",
                    "RUN",
                    "SLOW_WALK",
                    "RUN_AWAY"
                ]:

                    movement = (
                        local_frame
                        / max(1, duration_frames)
                    )

                    if action == "RUN":
                        movement *= 2

                    char_x = int(
                        width * (
                            0.2 + 0.6 * movement
                        )
                    )

                if action == "WINDOW_EXIT":

                    char_x = int(width * 0.75)

                if action == "WINDOW_ENTER":

                    char_x = int(width * 0.3)

                if action in ["EXIT", "RUN_AWAY"]:

                    char_x = int(
                        width * (
                            0.5 + local_frame / duration_frames
                        )
                    )

                if action == "ENTER":

                    char_x = int(
                        width * (
                            0.1 + 0.4 * local_frame / duration_frames
                        )
                    )

                draw_character(
                    img,
                    char_x,
                    char_y,
                    scale,
                    character,
                    style,
                    action,
                    emotion,
                    local_frame
                )

                # Cinematic scene title
                if scene_index == 0 and local_frame < fps:

                    title_font = load_font(
                        max(24, int(width * 0.05)),
                        True
                    )

                    draw = ImageDraw.Draw(img)

                    draw.text(
                        (width // 2, int(height * 0.08)),
                        "NEXORA ANIMATION",
                        font=title_font,
                        fill=(100, 220, 255),
                        anchor="mm"
                    )

                draw_subtitle(
                    img,
                    line.get("text", ""),
                    width,
                    height
                )

                # Convert to RGB
                frame = np.asarray(
                    img.convert("RGB"),
                    dtype=np.uint8
                )

                process.stdin.write(
                    frame.tobytes()
                )

                frame_index += 1

                if frame_index % 24 == 0:

                    progress = int(
                        frame_index
                        / max(1, total_frames)
                        * 90
                    )

                    update_job(
                        job_id,
                        progress=min(progress, 90),
                        status="rendering",
                        message="Rendering animation frames..."
                    )

                del frame
                del img

        process.stdin.close()

        return_code = process.wait()

        if return_code != 0:

            error = process.stderr.read().decode(
                "utf-8",
                errors="ignore"
            )

            raise RuntimeError(
                error[-3000:]
            )

    finally:

        if process.poll() is None:

            process.kill()

        gc.collect()


# ============================================================
# COMPLETE VIDEO JOB
# ============================================================

def process_animation_job(
    job_id,
    script,
    ratio,
    style,
    voice_mode,
    language,
    uploaded_voice
):

    work_dir = JOB_DIR / job_id

    work_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    silent_video = work_dir / "animation_silent.mp4"

    final_video = OUTPUT_DIR / f"{job_id}.mp4"

    tts_audio = work_dir / "generated_voice.mp3"

    try:

        update_job(
            job_id,
            status="rendering",
            progress=1,
            message="Reading your animation script..."
        )

        lines = parse_script(script)

        if not lines:
            raise RuntimeError("No valid scenes found.")

        update_job(
            job_id,
            status="rendering",
            progress=5,
            message="Preparing characters and scenes..."
        )

        audio_file = None

        # Uploaded full voice recording
        if uploaded_voice and Path(uploaded_voice).exists():

            audio_file = uploaded_voice

            update_job(
                job_id,
                progress=8,
                message="Using your uploaded voice recording..."
            )

        # Automatic AI voice
        elif voice_mode == "auto":

            update_job(
                job_id,
                progress=8,
                message="Generating AI narration..."
            )

            success = make_voice_audio(
                lines,
                tts_audio,
                language
            )

            if success:
                audio_file = tts_audio

        # Render video
        update_job(
            job_id,
            status="rendering",
            progress=12,
            message="Creating 2D animation..."
        )

        render_animation(
            lines=lines,
            output_file=silent_video,
            ratio=ratio,
            style=style,
            fps=DEFAULT_FPS,
            job_id=job_id
        )

        update_job(
            job_id,
            status="encoding",
            progress=92,
            message="Encoding final MP4 video..."
        )

        encode_audio_video(
            silent_video,
            final_video,
            audio_file
        )

        if not final_video.exists():
            raise RuntimeError("Final video file was not created.")

        if final_video.stat().st_size == 0:
            raise RuntimeError("Final video is empty.")

        video_url = f"/api/video/{job_id}"

        update_job(
            job_id,
            status="completed",
            progress=100,
            message="Animation completed successfully!",
            video_url=video_url,
            file_name=f"NEXORA_{job_id}.mp4",
            file_size=final_video.stat().st_size,
            duration=probe_duration(final_video)
        )

    except Exception as e:

        traceback.print_exc()

        update_job(
            job_id,
            status="failed",
            progress=0,
            message="Animation rendering failed.",
            error=str(e)
        )

    finally:

        try:

            if silent_video.exists():
                silent_video.unlink()

        except Exception:
            pass

        gc.collect()


# ============================================================
# FRONTEND
# ============================================================

@app.route("/", methods=["GET"])
def home():

    index_file = BASE_DIR / "index.html"

    if index_file.exists():

        return send_file(index_file)

    return jsonify({
        "name": "NEXORA AI Animation Studio",
        "status": "online",
        "message": "Backend is running. index.html was not found."
    })


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/api/ping", methods=["GET"])
def ping():

    return jsonify({
        "success": True,
        "status": "online",
        "app": "NEXORA AI Animation Studio",
        "version": "1.0.0",
        "time": datetime.utcnow().isoformat()
    })


# ============================================================
# START ANIMATION
# ============================================================

@app.route("/api/start", methods=["POST"])
def start_animation():

    try:

        script = request.form.get(
            "script",
            ""
        ).strip()

        ratio = request.form.get(
            "ratio",
            "9:16"
        )

        style = request.form.get(
            "style",
            "cartoon"
        )

        voice_mode = request.form.get(
            "voice_mode",
            "auto"
        )

        language = request.form.get(
            "lang_mode",
            "hindi"
        )

        if not script:

            return jsonify({
                "success": False,
                "error": "Please enter an animation script."
            }), 400

        if len(script) > 50000:

            return jsonify({
                "success": False,
                "error": "Script is too long. Maximum 50000 characters."
            }), 400

        if ratio not in ["9:16", "16:9"]:

            ratio = "9:16"

        if style not in ["cartoon", "stickman"]:

            style = "cartoon"

        if voice_mode not in ["auto", "manual"]:

            voice_mode = "auto"

        if language.lower() not in ["hindi", "english"]:

            language = "hindi"

        job_id = uuid.uuid4().hex[:12]

        uploaded_voice_path = None

        if "voice_file" in request.files:

            voice = request.files["voice_file"]

            if voice and voice.filename:

                original_name = secure_filename(
                    voice.filename
                )

                extension = Path(
                    original_name
                ).suffix.lower()

                if extension not in ALLOWED_AUDIO:

                    return jsonify({
                        "success": False,
                        "error": "Unsupported voice file format."
                    }), 400

                uploaded_voice_path = (
                    UPLOAD_DIR
                    / f"{job_id}{extension}"
                )

                voice.save(uploaded_voice_path)

        with jobs_lock:

            jobs[job_id] = {

                "job_id": job_id,

                "status": "queued",

                "progress": 0,

                "message": "Animation job added to queue.",

                "created_at": time.time(),

                "updated_at": time.time(),

                "video_url": None,

                "file_name": None,

                "error": None
            }

        executor.submit(
            process_animation_job,
            job_id,
            script,
            ratio,
            style,
            voice_mode,
            language,
            uploaded_voice_path
        )

        return jsonify({

            "success": True,

            "job_id": job_id,

            "status": "queued",

            "message": "Animation job started.",

            "status_url": f"/api/status/{job_id}",

            "video_url": None

        }), 202

    except Exception as e:

        traceback.print_exc()

        return jsonify({

            "success": False,

            "error": str(e)

        }), 500


# ============================================================
# JOB STATUS
# ============================================================

@app.route("/api/status/<job_id>", methods=["GET"])
def animation_status(job_id):

    job = get_job(job_id)

    if not job:

        return jsonify({

            "success": False,

            "error": "Job not found."

        }), 404

    return jsonify({

        "success": True,

        "job_id": job_id,

        "status": job.get("status"),

        "progress": job.get("progress", 0),

        "message": job.get("message", ""),

        "error": job.get("error"),

        "video_url": job.get("video_url"),

        "file_name": job.get("file_name"),

        "file_size": job.get("file_size"),

        "duration": job.get("duration")

    })


# ============================================================
# VIDEO DOWNLOAD / PLAY
# ============================================================

@app.route("/api/video/<job_id>", methods=["GET"])
def download_video(job_id):

    video_file = OUTPUT_DIR / f"{job_id}.mp4"

    if not video_file.exists():

        abort(404)

    return send_file(

        video_file,

        mimetype="video/mp4",

        as_attachment=(
            request.args.get("dl") == "1"
        ),

        download_name=f"NEXORA_{job_id}.mp4",

        conditional=True

    )


# ============================================================
# JOB INFORMATION
# ============================================================

@app.route("/api/job/<job_id>", methods=["GET"])
def job_information(job_id):

    job = get_job(job_id)

    if not job:

        return jsonify({
            "success": False,
            "error": "Job not found."
        }), 404

    return jsonify(job)


# ============================================================
# BASIC ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(error):

    return jsonify({

        "success": False,

        "error": "Requested resource was not found."

    }), 404


@app.errorhandler(413)
def file_too_large(error):

    return jsonify({

        "success": False,

        "error": "Uploaded file is too large. Maximum size is 100 MB."

    }), 413


@app.errorhandler(500)
def server_error(error):

    return jsonify({

        "success": False,

        "error": "Internal server error."

    }), 500


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 5000)
    )

    app.run(

        host="0.0.0.0",

        port=port,

        debug=False,

        threaded=True

    )
