
import os
import re
import gc
import math
import uuid
import time
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

from PIL import Image, ImageDraw, ImageFont

from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    abort
)

from werkzeug.utils import secure_filename


# ============================================================
# NEXORA AI ANIMATION STUDIO
# COMPLETE PYTHON BACKEND
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

# Render-friendly settings
DEFAULT_FPS = 18
MAX_DURATION = 180
MAX_WORKERS = 1

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

jobs = {}
jobs_lock = threading.Lock()
render_lock = threading.Lock()

ALLOWED_AUDIO = {
    ".mp3", ".wav", ".m4a", ".aac",
    ".ogg", ".flac", ".mp4", ".webm"
}


# ============================================================
# FONT SYSTEM
# ============================================================

def load_font(size=32, bold=False):

    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ]

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
        if job_id in jobs:
            jobs[job_id].update(values)
            jobs[job_id]["updated_at"] = time.time()


def get_job(job_id):

    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


# ============================================================
# SCRIPT TAGS
# ============================================================

ACTION_TAGS = {
    "WALK", "RUN", "JUMP", "DANCE", "WAVE",
    "POINT", "FIGHT", "SIT", "IDLE", "TALK",
    "WINDOW_EXIT", "WINDOW_ENTER", "TURN",
    "LOOK", "LOOK_UP", "LOOK_DOWN", "CLAP",
    "CRY", "LAUGH", "THINK", "SLEEP",
    "FALL", "STAND", "KICK", "PUNCH",
    "HUG", "HANDSHAKE", "PHONE", "READ",
    "WRITE", "RUN_AWAY", "ENTER", "EXIT",
    "KNEEL", "SLOW_WALK"
}

EMOTION_TAGS = {
    "HAPPY", "SAD", "ANGRY", "SURPRISED",
    "CONFUSED", "ROMANTIC", "HEROIC",
    "SCARED", "SHY", "CRYING", "LAUGHING",
    "NEUTRAL", "EXCITED", "SERIOUS",
    "DISAPPOINTED", "LOVE", "WORRIED", "SMILE"
}

OBJECT_TAGS = {
    "PAPER", "PHONE", "BOOK", "CHAIR",
    "TABLE", "WINDOW", "DOOR", "CAR",
    "FLOWER", "GIFT", "BAG", "SWORD",
    "BALL", "LAMP", "CUP"
}

SCENE_TAGS = {
    "NIGHT", "DAY", "ROOM", "OUTDOOR",
    "RAIN", "SUNSET", "SUNRISE", "CITY",
    "FOREST", "BEACH", "SCHOOL", "HOME",
    "DARK", "DREAM", "FLASHBACK"
}


def parse_script(script):

    scenes = []

    current_character = "Narrator"
    current_action = "TALK"
    current_emotion = "NEUTRAL"
    current_scene = "ROOM"
    current_objects = []

    for raw_line in script.replace("\r", "").split("\n"):

        line = raw_line.strip()

        if not line:
            continue

        tags = re.findall(r"\[([^\]]+)\]", line)

        text_line = re.sub(
            r"\[[^\]]+\]",
            "",
            line
        ).strip()

        for raw_tag in tags:

            tag = raw_tag.strip().upper()

            if tag in ACTION_TAGS:
                current_action = tag

            elif tag in EMOTION_TAGS:
                current_emotion = tag

            elif tag in SCENE_TAGS:
                current_scene = tag

            elif tag in OBJECT_TAGS:
                if tag not in current_objects:
                    current_objects.append(tag)

            elif tag not in {
                "FADE", "CUT", "CAMERA",
                "THE END", "END"
            }:
                if len(tag) <= 35:
                    current_character = tag.title()

        if not text_line:
            continue

        # Supports: Aarav: Hello
        match = re.match(
            r"^([A-Za-z][A-Za-z0-9 _-]{0,25}):\s*(.*)$",
            text_line
        )

        if match:
            current_character = match.group(1).strip()
            text_line = match.group(2).strip()

        if text_line:

            scenes.append({
                "character": current_character,
                "text": text_line,
                "action": current_action,
                "emotion": current_emotion,
                "scene": current_scene,
                "objects": list(current_objects)
            })

            current_action = "TALK"
            current_emotion = "NEUTRAL"
            current_objects = []

    if not scenes:

        scenes.append({
            "character": "Narrator",
            "text": script.strip() or "Welcome to NEXORA.",
            "action": "TALK",
            "emotion": "NEUTRAL",
            "scene": "ROOM",
            "objects": []
        })

    return scenes


# ============================================================
# AUDIO GENERATION
# ============================================================

def run_async(coroutine):

    return asyncio.run(coroutine)


async def generate_tts(text, output_path, language):

    import edge_tts

    if language.lower() == "hindi":
        voice = "hi-IN-SwaraNeural"
    else:
        voice = "en-US-JennyNeural"

    communicator = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate="+0%",
        volume="+0%"
    )

    await communicator.save(str(output_path))


def make_voice_audio(scenes, output_path, language):

    try:

        dialogue = []

        for scene in scenes:
            value = scene.get("text", "").strip()
            if value:
                dialogue.append(value)

        full_text = ". ".join(dialogue)

        if not full_text:
            return False

        run_async(
            generate_tts(
                full_text,
                output_path,
                language
            )
        )

        return (
            Path(output_path).exists()
            and Path(output_path).stat().st_size > 0
        )

    except Exception:

        traceback.print_exc()
        return False


# ============================================================
# FFMPEG
# ============================================================

def get_ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_duration(file_path):

    command = [
        get_ffmpeg(),
        "-i",
        str(file_path)
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30
    )

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)",
        result.stderr
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

    command = [
        get_ffmpeg(),
        "-y",
        "-i",
        str(silent_video)
    ]

    if audio_file and Path(audio_file).exists():

        command.extend([
            "-i", str(audio_file),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest"
        ])

    else:
        command.append("-an")

    command.extend([
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_video)
    ])

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=600
    )

    if result.returncode != 0:

        raise RuntimeError(
            "Video encoding failed: "
            + result.stderr[-1500:]
        )


# ============================================================
# BACKGROUND COLORS
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

    return colors.get(scene, colors["ROOM"])


def make_gradient(width, height, scene):

    top, bottom = color_for_scene(scene)

    y = np.linspace(
        0,
        1,
        height,
        dtype=np.float32
    )[:, None, None]

    top_color = np.array(
        top,
        dtype=np.float32
    )[None, None, :]

    bottom_color = np.array(
        bottom,
        dtype=np.float32
    )[None, None, :]

    gradient = (
        top_color * (1 - y)
        + bottom_color * y
    )

    gradient = np.broadcast_to(
        gradient,
        (height, width, 3)
    )

    return Image.fromarray(
        gradient.astype(np.uint8),
        mode="RGB"
    )


# ============================================================
# SCENE BACKGROUNDS
# ============================================================

def draw_background(draw, width, height, scene, frame):

    scene = scene.upper()

    horizon = int(height * 0.72)

    if scene in ["ROOM", "HOME", "SCHOOL"]:

        draw.rectangle(
            [0, horizon, width, height],
            fill=(35, 35, 55)
        )

        for x in range(0, width, 90):

            draw.line(
                [(x, horizon), (x - 50, height)],
                fill=(65, 65, 85),
                width=2
            )

        wx = int(width * 0.67)
        wy = int(height * 0.15)

        draw.rectangle(
            [wx, wy, wx + 130, wy + 180],
            fill=(20, 45, 85),
            outline=(140, 180, 220),
            width=5
        )

        draw.line(
            [(wx + 65, wy), (wx + 65, wy + 180)],
            fill=(140, 180, 220),
            width=4
        )

        draw.line(
            [(wx, wy + 90), (wx + 130, wy + 90)],
            fill=(140, 180, 220),
            width=4
        )

        draw.ellipse(
            [width - 100, 70, width - 40, 130],
            fill=(255, 210, 120)
        )

    elif scene == "CITY":

        draw.rectangle(
            [0, horizon, width, height],
            fill=(20, 22, 35)
        )

        for x in range(0, width, 90):

            building_height = 130 + (x * 7 % 180)

            draw.rectangle(
                [
                    x,
                    horizon - building_height,
                    x + 70,
                    horizon
                ],
                fill=(18, 24, 45)
            )

            for wy in range(
                horizon - building_height + 20,
                horizon - 15,
                40
            ):

                draw.rectangle(
                    [x + 12, wy, x + 24, wy + 12],
                    fill=(255, 205, 110)
                )

    elif scene == "FOREST":

        draw.rectangle(
            [0, horizon, width, height],
            fill=(12, 50, 30)
        )

        for x in range(20, width, 100):

            draw.rectangle(
                [x + 25, 170, x + 45, horizon],
                fill=(70, 45, 25)
            )

            draw.ellipse(
                [x - 35, 80, x + 95, 260],
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

    if scene == "RAIN":

        random.seed(frame // 2)

        for _ in range(45):

            x = random.randint(0, width)
            y = random.randint(0, height)

            draw.line(
                [(x, y), (x - 7, y + 18)],
                fill=(130, 170, 220),
                width=2
            )


# ============================================================
# CHARACTER ENGINE
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
    frame
):

    draw = ImageDraw.Draw(image)

    style = style.lower()

    movement = math.sin(frame / 8.0) * 14

    if action == "RUN":
        movement *= 2

    bounce = abs(math.sin(frame / 8.0)) * 9

    if action == "JUMP":
        bounce = abs(math.sin(frame / 5.0)) * 60

    if action == "SIT":
        y += int(35 * scale)

    x = int(x)
    y = int(y - bounce)

    head_size = int(65 * scale)

    skin = (235, 180, 145)
    shirt = (40, 170, 230)
    pants = (30, 40, 65)
    hair = (35, 25, 20)

    line_width = max(3, int(5 * scale))

    if style == "stickman":

        draw.ellipse(
            [
                x - head_size // 2,
                y - head_size,
                x + head_size // 2,
                y
            ],
            outline=(245, 245, 255),
            width=line_width
        )

        draw.line(
            [(x, y), (x, y + int(110 * scale))],
            fill=(245, 245, 255),
            width=line_width
        )

        arm_move = int(movement * scale)

        if action == "WAVE":
            left_end = (
                x - int(45 * scale),
                y + int(70 * scale)
            )
            right_end = (
                x + int(45 * scale),
                y - int(45 * scale)
            )

        elif action == "POINT":
            left_end = (
                x - int(45 * scale),
                y + int(70 * scale)
            )
            right_end = (
                x + int(90 * scale),
                y + int(10 * scale)
            )

        else:
            left_end = (
                x - int(45 * scale) - arm_move,
                y + int(70 * scale)
            )
            right_end = (
                x + int(45 * scale) + arm_move,
                y + int(70 * scale)
            )

        draw.line(
            [(x, y + int(30 * scale)), left_end],
            fill=(245, 245, 255),
            width=line_width
        )

        draw.line(
            [(x, y + int(30 * scale)), right_end],
            fill=(245, 245, 255),
            width=line_width
        )

        draw.line(
            [
                (x, y + int(110 * scale)),
                (x - int(30 * scale) + arm_move, y + int(175 * scale))
            ],
            fill=(245, 245, 255),
            width=line_width
        )

        draw.line(
            [
                (x, y + int(110 * scale)),
                (x + int(30 * scale) - arm_move, y + int(175 * scale))
            ],
            fill=(245, 245, 255),
            width=line_width
        )

    else:

        draw.ellipse(
            [
                x - int(55 * scale),
                y + int(160 * scale),
                x + int(55 * scale),
                y + int(180 * scale)
            ],
            fill=(15, 15, 25)
        )

        leg_move = int(movement * scale)

        draw.line(
            [
                (x - int(12 * scale), y + int(100 * scale)),
                (x - int(25 * scale) + leg_move, y + int(180 * scale))
            ],
            fill=pants,
            width=max(5, int(20 * scale))
        )

        draw.line(
            [
                (x + int(12 * scale), y + int(100 * scale)),
                (x + int(25 * scale) - leg_move, y + int(180 * scale))
            ],
            fill=pants,
            width=max(5, int(20 * scale))
        )

        draw.ellipse(
            [
                x - int(50 * scale),
                y + int(165 * scale),
                x - int(8 * scale),
                y + int(190 * scale)
            ],
            fill=(20, 20, 25)
        )

        draw.ellipse(
            [
                x + int(8 * scale),
                y + int(165 * scale),
                x + int(50 * scale),
                y + int(190 * scale)
            ],
            fill=(20, 20, 25)
        )

        draw.rounded_rectangle(
            [
                x - int(25 * scale),
                y,
                x + int(25 * scale),
                y + int(130 * scale)
            ],
            radius=int(12 * scale),
            fill=shirt
        )

        arm_y = y + int(25 * scale)
        arm_length = int(60 * scale)

        if action == "WAVE":

            left_end = (
                x - arm_length,
                arm_y + arm_length
            )

            right_end = (
                x + arm_length,
                arm_y - int(60 * scale)
                + int(math.sin(frame / 3) * 12)
            )

        elif action == "POINT":

            left_end = (
                x - arm_length,
                arm_y + arm_length
            )

            right_end = (
                x + arm_length * 2,
                arm_y - 5
            )

        elif action in ["FIGHT", "PUNCH", "KICK"]:

            left_end = (
                x - arm_length,
                arm_y - 12
            )

            right_end = (
                x + arm_length,
                arm_y + 15
            )

        else:

            left_end = (
                x - arm_length,
                arm_y + int(movement * scale)
            )

            right_end = (
                x + arm_length,
                arm_y - int(movement * scale)
            )

        draw.line(
            [(x - int(25 * scale), arm_y), left_end],
            fill=skin,
            width=max(5, int(15 * scale))
        )

        draw.line(
            [(x + int(25 * scale), arm_y), right_end],
            fill=skin,
            width=max(5, int(15 * scale))
        )

        head_top = y - head_size

        draw.ellipse(
            [
                x - head_size // 2,
                head_top,
                x + head_size // 2,
                y + int(10 * scale)
            ],
            fill=skin
        )

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

        eye_y = head_top + int(33 * scale)
        eye_size = max(2, int(5 * scale))

        draw.ellipse(
            [
                x - int(17 * scale),
                eye_y,
                x - int(17 * scale) + eye_size,
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

        mouth_y = head_top + int(50 * scale)

        if emotion in [
            "HAPPY", "SMILE", "LAUGHING", "EXCITED"
        ]:

            draw.arc(
                [
                    x - int(14 * scale),
                    mouth_y - int(5 * scale),
                    x + int(14 * scale),
                    mouth_y + int(15 * scale)
                ],
                0,
                180,
                fill=(100, 25, 25),
                width=max(2, int(3 * scale))
            )

        elif emotion in [
            "SAD", "CRYING", "DISAPPOINTED"
        ]:

            draw.arc(
                [
                    x - int(14 * scale),
                    mouth_y,
                    x + int(14 * scale),
                    mouth_y + int(16 * scale)
                ],
                180,
                360,
                fill=(100, 25, 25),
                width=max(2, int(3 * scale))
            )

        elif emotion in ["SURPRISED", "SCARED"]:

            draw.ellipse(
                [
                    x - int(5 * scale),
                    mouth_y,
                    x + int(5 * scale),
                    mouth_y + int(12 * scale)
                ],
                fill=(80, 20, 20)
            )

        else:

            draw.line(
                [
                    (x - int(11 * scale), mouth_y + int(5 * scale)),
                    (x + int(11 * scale), mouth_y + int(5 * scale))
                ],
                fill=(100, 25, 25),
                width=max(2, int(3 * scale))
            )

        if emotion in ["CRYING", "SAD"]:

            draw.line(
                [
                    (x - int(17 * scale), eye_y + 5),
                    (x - int(17 * scale), eye_y + int(22 * scale))
                ],
                fill=(80, 180, 255),
                width=max(2, int(3 * scale))
            )

    font = load_font(
        max(12, int(16 * scale)),
        True
    )

    draw.text(
        (x, y + int(205 * scale)),
        name,
        font=font,
        fill=(240, 245, 255),
        anchor="mm"
    )


# ============================================================
# OBJECTS
# ============================================================

def draw_objects(draw, objects, width, height):

    for obj in objects:

        x = int(width * 0.68)
        y = int(height * 0.55)

        if obj == "PAPER":

            draw.rectangle(
                [x, y, x + 80, y + 105],
                fill=(245, 240, 220),
                outline=(30, 30, 30),
                width=3
            )

            for i in range(4):

                draw.line(
                    [
                        (x + 10, y + 20 + i * 20),
                        (x + 68, y + 20 + i * 20)
                    ],
                    fill=(100, 100, 100),
                    width=2
                )

        elif obj == "PHONE":

            draw.rounded_rectangle(
                [x, y, x + 48, y + 95],
                radius=7,
                fill=(15, 15, 20),
                outline=(180, 190, 210),
                width=3
            )

            draw.rectangle(
                [x + 5, y + 10, x + 43, y + 80],
                fill=(50, 100, 160)
            )

        elif obj == "FLOWER":

            draw.line(
                [(x, y), (x, y + 80)],
                fill=(20, 150, 60),
                width=5
            )

            draw.ellipse(
                [x - 16, y - 16, x + 16, y + 16],
                fill=(255, 100, 150)
            )

        elif obj == "BOOK":

            draw.rectangle(
                [x, y, x + 85, y + 65],
                fill=(160, 70, 50),
                outline=(240, 220, 170),
                width=3
            )

        elif obj == "CUP":

            draw.rectangle(
                [x, y, x + 40, y + 48],
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
        max(18, int(width * 0.032)),
        True
    )

    words = text.split()

    lines = []
    current = ""

    for word in words:

        test = (current + " " + word).strip()

        if len(test) > 34:

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

    y = height - total_height - int(height * 0.10)

    draw.rounded_rectangle(
        [
            18,
            y - 12,
            width - 18,
            y + total_height + 12
        ],
        radius=12,
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
# RENDER ENGINE
# ============================================================

def render_animation(
    scenes,
    output_file,
    ratio,
    style,
    fps,
    job_id
):

    if ratio == "16:9":
        width = 960
        height = 540
    else:
        width = 540
        height = 960

    width -= width % 2
    height -= height % 2

    scene_duration = 3.0

    duration = min(
        len(scenes) * scene_duration,
        MAX_DURATION
    )

    total_frames = max(
        1,
        int(duration * fps)
    )

    command = [
        get_ffmpeg(),
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_file)
    ]

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        bufsize=1024 * 1024
    )

    frame_index = 0

    try:

        for scene_index, scene_data in enumerate(scenes):

            scene_name = scene_data.get("scene", "ROOM")
            action = scene_data.get("action", "TALK")
            emotion = scene_data.get("emotion", "NEUTRAL")
            character = scene_data.get("character", "Character")
            objects = scene_data.get("objects", [])
            dialogue = scene_data.get("text", "")

            frames_per_scene = int(
                scene_duration * fps
            )

            for local_frame in range(frames_per_scene):

                img = make_gradient(
                    width,
                    height,
                    scene_name
                )

                draw = ImageDraw.Draw(img)

                draw_background(
                    draw,
                    width,
                    height,
                    scene_name,
                    frame_index
                )

                draw_objects(
                    draw,
                    objects,
                    width,
                    height
                )

                if ratio == "16:9":
                    char_x = int(width * 0.50)
                    char_y = int(height * 0.46)
                    scale = 1.0
                else:
                    char_x = int(width * 0.50)
                    char_y = int(height * 0.45)
                    scale = 1.15

                if action in [
                    "WALK", "RUN", "SLOW_WALK", "RUN_AWAY"
                ]:

                    movement = (
                        local_frame / max(1, frames_per_scene)
                    )

                    if action == "RUN":
                        movement *= 1.5

                    char_x = int(
                        width * (0.20 + 0.60 * movement)
                    )

                elif action == "WINDOW_EXIT":
                    char_x = int(width * 0.75)

                elif action == "WINDOW_ENTER":
                    char_x = int(width * 0.30)

                elif action in ["EXIT", "RUN_AWAY"]:
                    char_x = int(
                        width * (
                            0.50
                            + 0.40 * local_frame / frames_per_scene
                        )
                    )

                elif action == "ENTER":
                    char_x = int(
                        width * (
                            0.10
                            + 0.40 * local_frame / frames_per_scene
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

                if scene_index == 0 and local_frame < fps:

                    title_font = load_font(
                        max(20, int(width * 0.045)),
                        True
                    )

                    draw.text(
                        (width // 2, int(height * 0.07)),
                        "NEXORA ANIMATION",
                        font=title_font,
                        fill=(100, 220, 255),
                        anchor="mm"
                    )

                draw_subtitle(
                    img,
                    dialogue,
                    width,
                    height
                )

                frame = np.asarray(
                    img.convert("RGB"),
                    dtype=np.uint8
                )

                try:
                    process.stdin.write(frame.tobytes())
                except (BrokenPipeError, OSError):
                    raise RuntimeError(
                        "FFmpeg stopped while receiving video frames."
                    )

                frame_index += 1

                if frame_index % 12 == 0:

                    progress = int(
                        10 + (
                            frame_index / total_frames
                        ) * 78
                    )

                    update_job(
                        job_id,
                        status="rendering",
                        progress=min(progress, 88),
                        message="Rendering animation frames..."
                    )

                del frame
                del img

        process.stdin.close()

        return_code = process.wait(timeout=600)

        if return_code != 0:

            raise RuntimeError(
                f"FFmpeg rendering failed with exit code: {return_code}"
            )

    finally:

        if process.poll() is None:
            process.kill()

        gc.collect()


# ============================================================
# COMPLETE ANIMATION JOB
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
    work_dir.mkdir(parents=True, exist_ok=True)

    silent_video = work_dir / "silent.mp4"
    generated_audio = work_dir / "voice.mp3"
    final_video = OUTPUT_DIR / f"{job_id}.mp4"

    try:

        with render_lock:

            update_job(
                job_id,
                status="rendering",
                progress=2,
                message="Preparing animation..."
            )

            scenes = parse_script(script)

            if not scenes:
                raise RuntimeError("No scenes found in script.")

            audio_file = None

            if uploaded_voice and Path(uploaded_voice).exists():

                audio_file = uploaded_voice

                update_job(
                    job_id,
                    progress=5,
                    message="Using uploaded voice recording..."
                )

            elif voice_mode == "auto":

                update_job(
                    job_id,
                    progress=5,
                    message="Generating AI voice..."
                )

                success = make_voice_audio(
                    scenes,
                    generated_audio,
                    language
                )

                if success:
                    audio_file = generated_audio

            update_job(
                job_id,
                status="rendering",
                progress=10,
                message="Creating animation scenes..."
            )

            render_animation(
                scenes=scenes,
                output_file=silent_video,
                ratio=ratio,
                style=style,
                fps=DEFAULT_FPS,
                job_id=job_id
            )

            update_job(
                job_id,
                status="encoding",
                progress=90,
                message="Encoding final MP4..."
            )

            encode_audio_video(
                silent_video,
                final_video,
                audio_file
            )

            if not final_video.exists():
                raise RuntimeError("Final MP4 was not created.")

            if final_video.stat().st_size == 0:
                raise RuntimeError("Final MP4 is empty.")

            duration = probe_duration(final_video)

            update_job(
                job_id,
                status="completed",
                progress=100,
                message="Animation completed successfully!",
                video_url=f"/api/video/{job_id}",
                file_name=f"NEXORA_{job_id}.mp4",
                file_size=final_video.stat().st_size,
                duration=duration
            )

    except Exception as error:

        traceback.print_exc()

        update_job(
            job_id,
            status="failed",
            progress=0,
            message="Animation rendering failed.",
            error=str(error)
        )

    finally:

        try:
            if silent_video.exists():
                silent_video.unlink()
        except Exception:
            pass

        gc.collect()


# ============================================================
# FRONTEND ROUTE
# ============================================================

@app.route("/", methods=["GET"])
def home():

    index_file = BASE_DIR / "index.html"

    if index_file.exists():
        return send_file(index_file)

    return jsonify({
        "name": "NEXORA AI Animation Studio",
        "status": "online",
        "message": "Backend running. index.html not found."
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
        "version": "2.0",
        "time": datetime.utcnow().isoformat()
    })


# ============================================================
# START RENDER
# ============================================================

@app.route("/api/start", methods=["POST"])
def start_animation():

    try:

        script = request.form.get(
            "script", ""
        ).strip()

        ratio = request.form.get(
            "ratio", "9:16"
        )

        style = request.form.get(
            "style", "cartoon"
        )

        voice_mode = request.form.get(
            "voice_mode", "auto"
        )

        language = request.form.get(
            "lang_mode", "hindi"
        )

        if not script:

            return jsonify({
                "success": False,
                "error": "Please enter an animation script."
            }), 400

        if len(script) > 50000:

            return jsonify({
                "success": False,
                "error": "Script is too long."
            }), 400

        if ratio not in ["9:16", "16:9"]:
            ratio = "9:16"

        if style not in ["cartoon", "stickman"]:
            style = "cartoon"

        if voice_mode not in ["auto", "manual"]:
            voice_mode = "auto"

        if language.lower() not in ["hindi", "english"]:
            language = "hindi"

        # Avoid excessive queued jobs on a small server
        with jobs_lock:

            active_jobs = [
                item for item in jobs.values()
                if item.get("status") in [
                    "queued", "rendering", "encoding"
                ]
            ]

            if len(active_jobs) >= 2:

                return jsonify({
                    "success": False,
                    "error": "Render queue is busy. Please wait for the current video."
                }), 429

        job_id = uuid.uuid4().hex[:12]

        uploaded_voice_path = None

        if "voice_file" in request.files:

            voice = request.files["voice_file"]

            if voice and voice.filename:

                filename = secure_filename(
                    voice.filename
                )

                extension = Path(filename).suffix.lower()

                if extension not in ALLOWED_AUDIO:

                    return jsonify({
                        "success": False,
                        "error": "Unsupported voice file format."
                    }), 400

                uploaded_voice_path = (
                    UPLOAD_DIR / f"{job_id}{extension}"
                )

                voice.save(uploaded_voice_path)

        with jobs_lock:

            jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "progress": 0,
                "message": "Waiting for render worker...",
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
            "message": "Animation job accepted.",
            "status_url": f"/api/status/{job_id}",
            "video_url": None
        }), 202

    except Exception as error:

        traceback.print_exc()

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ============================================================
# CHECK RENDER STATUS
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
# VIDEO PLAY / DOWNLOAD
# ============================================================

@app.route("/api/video/<job_id>", methods=["GET"])
def download_video(job_id):

    video_file = OUTPUT_DIR / f"{job_id}.mp4"

    if not video_file.exists():
        abort(404)

    download = request.args.get("dl") == "1"

    return send_file(
        video_file,
        mimetype="video/mp4",
        as_attachment=download,
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
# ERROR HANDLERS
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
        "error": "File too large. Maximum upload size is 100 MB."
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
