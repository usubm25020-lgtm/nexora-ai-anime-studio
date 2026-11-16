
import os
import re
import gc
import io
import json
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
import edge_tts

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from flask import Flask, request, jsonify, send_file, abort


# ============================================================
# NEXORA AI ANIMATION STUDIO
# Complete Backend - Version 1
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "nexora_data"
JOB_DIR = DATA_DIR / "jobs"
VIDEO_DIR = DATA_DIR / "videos"
UPLOAD_DIR = DATA_DIR / "uploads"

for folder in [DATA_DIR, JOB_DIR, VIDEO_DIR, UPLOAD_DIR]:
    folder.mkdir(parents=True, exist_ok=True)


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

FPS = 18
MAX_DURATION = 180
MAX_WORKERS = 1

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

jobs = {}
jobs_lock = threading.Lock()
render_lock = threading.Lock()


# ============================================================
# FONTS
# ============================================================

def load_font(size, bold=False):

    candidates = []

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
        return jobs.get(job_id)


# ============================================================
# AI STORY DIRECTOR
# ============================================================

EMOTIONS = [
    "happy",
    "sad",
    "angry",
    "surprised",
    "scared",
    "thinking",
    "neutral"
]

ACTION_KEYWORDS = {

    "walk": [
        "walk", "walking", "হাঁট", "चल", "चलना"
    ],

    "run": [
        "run", "running", "দৌড়", "दौड़"
    ],

    "wave": [
        "wave", "waving", "হাত নাড়", "हाथ हिला"
    ],

    "sit": [
        "sit", "sitting", "বসে", "बैठ"
    ],

    "think": [
        "think", "thinking", "ভাব", "सोच"
    ],

    "talk": [
        "say", "speak", "talk", "বল", "बोल"
    ],

    "point": [
        "point", "ইশারা", "इशारा"
    ],

    "idle": []
}


def detect_emotion(text):

    value = text.lower()

    mapping = {

        "happy": [
            "happy", "smile", "laugh",
            "খুশি", "হাসি",
            "खुश", "हंस"
        ],

        "sad": [
            "sad", "cry", "tears",
            "দুঃখ", "কাঁদ",
            "दुख", "रो"
        ],

        "angry": [
            "angry", "shout", "furious",
            "রাগ", "চিৎকার",
            "गुस्सा", "चिल्ला"
        ],

        "surprised": [
            "surprised", "shock",
            "অবাক", "চমকে",
            "हैरान", "चौंक"
        ],

        "scared": [
            "scared", "fear", "afraid",
            "ভয়", "ভীত",
            "डर", "भय"
        ],

        "thinking": [
            "thinking", "wonder",
            "ভাবছে", "চিন্তা",
            "सोच", "विचार"
        ]
    }

    for emotion, words in mapping.items():

        if any(word in value for word in words):
            return emotion

    return "neutral"


def detect_action(text):

    value = text.lower()

    for action, keywords in ACTION_KEYWORDS.items():

        if any(word in value for word in keywords):
            return action

    return "talk"


def clean_dialogue(text):

    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\([^)]*\)", "", text)

    return text.strip()


def parse_script(script):

    lines = []

    current_speaker = "Aarav"

    raw_lines = script.replace("\r", "").split("\n")

    for raw in raw_lines:

        raw = raw.strip()

        if not raw:
            continue

        speaker = current_speaker
        dialogue = raw
        emotion = None
        action = None

        # [Aarav] [HAPPY] [WALK]
        speaker_match = re.match(
            r"^\[([A-Za-z0-9 _-]+)\]\s*(.*)$",
            dialogue
        )

        if speaker_match:

            possible = speaker_match.group(1).strip()

            if possible.lower() not in EMOTIONS and possible.lower() not in ACTION_KEYWORDS:

                speaker = possible
                current_speaker = speaker
                dialogue = speaker_match.group(2).strip()

        # Emotion tags
        emotion_match = re.search(
            r"\[(HAPPY|SAD|ANGRY|SURPRISED|SCARED|THINKING|NEUTRAL)\]",
            dialogue,
            re.IGNORECASE
        )

        if emotion_match:
            emotion = emotion_match.group(1).lower()

        # Action tags
        action_match = re.search(
            r"\[(WALK|RUN|WAVE|SIT|THINK|TALK|POINT|IDLE)\]",
            dialogue,
            re.IGNORECASE
        )

        if action_match:
            action = action_match.group(1).lower()

        # Name: dialogue
        colon_match = re.match(
            r"^([A-Za-z][A-Za-z0-9 _-]{0,25}):\s*(.*)$",
            dialogue
        )

        if colon_match:

            speaker = colon_match.group(1).strip()
            current_speaker = speaker
            dialogue = colon_match.group(2).strip()

        dialogue = clean_dialogue(dialogue)

        if not dialogue:
            continue

        emotion = emotion or detect_emotion(dialogue)
        action = action or detect_action(dialogue)

        lines.append({

            "speaker": speaker,
            "dialogue": dialogue,
            "emotion": emotion,
            "action": action,
            "duration": 3.0

        })

    return lines


# ============================================================
# VOICE ENGINE
# ============================================================

VOICE_MAP = {

    "hindi": "hi-IN-SwaraNeural",
    "english": "en-US-JennyNeural"

}


async def create_voice(text, output_path, lang_mode):

    voice = VOICE_MAP.get(
        lang_mode,
        VOICE_MAP["hindi"]
    )

    communicate = edge_tts.Communicate(
        text,
        voice
    )

    await communicate.save(str(output_path))


def generate_voice(text, output_path, lang_mode):

    asyncio.run(
        create_voice(
            text,
            output_path,
            lang_mode
        )
    )


# ============================================================
# VIDEO UTILITIES
# ============================================================

def get_ffmpeg():

    return imageio_ffmpeg.get_ffmpeg_exe()


def get_media_duration(path):

    ffmpeg = get_ffmpeg()

    command = [
        ffmpeg,
        "-i", str(path),
        "-f", "null",
        "-"
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        result.stderr
    )

    if not match:
        return 3.0

    hours, minutes, seconds = match.groups()

    return (
        int(hours) * 3600 +
        int(minutes) * 60 +
        float(seconds)
    )


# ============================================================
# SCENE ENGINE
# ============================================================

def create_background(width, height, scene_index=0):

    image = Image.new(
        "RGB",
        (width, height)
    )

    pixels = image.load()

    palettes = [

        ((17, 24, 49), (50, 39, 89)),

        ((22, 37, 61), (16, 72, 87)),

        ((43, 27, 54), (90, 43, 65)),

        ((18, 34, 46), (34, 64, 73))

    ]

    top, bottom = palettes[
        scene_index % len(palettes)
    ]

    for y in range(height):

        t = y / max(1, height - 1)

        color = tuple(
            int(top[i] * (1 - t) + bottom[i] * t)
            for i in range(3)
        )

        for x in range(width):
            pixels[x, y] = color

    draw = ImageDraw.Draw(image, "RGBA")

    # Distant lights
    random.seed(scene_index + 100)

    for _ in range(24):

        x = random.randint(0, width)
        y = random.randint(0, int(height * 0.55))

        radius = random.randint(2, 8)

        draw.ellipse(
            (
                x-radius,
                y-radius,
                x+radius,
                y+radius
            ),
            fill=(120, 190, 255, 30)
        )

    # Ground
    ground_y = int(height * 0.83)

    draw.rectangle(
        (0, ground_y, width, height),
        fill=(9, 13, 25)
    )

    draw.line(
        (0, ground_y, width, ground_y),
        fill=(100, 180, 210, 90),
        width=2
    )

    return image


# ============================================================
# HUMAN CHARACTER RIG
# ============================================================

def draw_limb(draw, p1, p2, color, thickness):

    draw.line(
        [p1, p2],
        fill=color,
        width=thickness
    )

    radius = thickness // 2

    for point in [p1, p2]:

        x, y = point

        draw.ellipse(
            (
                x-radius,
                y-radius,
                x+radius,
                y+radius
            ),
            fill=color
        )


def draw_character(
    image,
    x,
    ground_y,
    frame,
    emotion="neutral",
    action="talk",
    character_index=0,
    scale=1.0
):

    draw = ImageDraw.Draw(image, "RGBA")

    # Character palette
    palettes = [

        {
            "skin": (224, 170, 132),
            "hair": (35, 27, 25),
            "shirt": (36, 145, 180),
            "pants": (31, 39, 65),
            "shoe": (12, 15, 22)
        },

        {
            "skin": (190, 135, 103),
            "hair": (20, 19, 22),
            "shirt": (135, 74, 160),
            "pants": (35, 42, 56),
            "shoe": (12, 15, 22)
        },

        {
            "skin": (235, 190, 157),
            "hair": (74, 45, 29),
            "shirt": (190, 100, 74),
            "pants": (34, 43, 62),
            "shoe": (12, 15, 22)
        }

    ]

    colors = palettes[
        character_index % len(palettes)
    ]

    skin = colors["skin"]
    hair = colors["hair"]
    shirt = colors["shirt"]
    pants = colors["pants"]
    shoe = colors["shoe"]

    s = scale

    def P(px, py):
        return (
            int(x + px * s),
            int(ground_y + py * s)
        )

    # Movement
    walk = math.sin(frame * 0.16)
    bob = abs(math.sin(frame * 0.16)) * 5

    if action == "walk":
        leg_motion = walk * 20
        arm_motion = walk * 16

    elif action == "run":
        leg_motion = walk * 34
        arm_motion = walk * 28
        bob *= 1.7

    else:
        leg_motion = 0
        arm_motion = math.sin(frame * 0.07) * 3

    if action == "wave":
        arm_motion = -25 + math.sin(frame * 0.24) * 18

    if action == "think":
        arm_motion = -12

    body_bob = bob if action in ["walk", "run"] else math.sin(frame * 0.07) * 2

    # Character dimensions
    head_y = -175 - body_bob

    shoulder_y = -115 - body_bob
    waist_y = -60 - body_bob

    # Legs
    hip_left = P(-15, -60 - body_bob)
    hip_right = P(15, -60 - body_bob)

    left_knee = P(
        -20 + leg_motion * 0.4,
        -30 - body_bob
    )

    right_knee = P(
        20 - leg_motion * 0.4,
        -30 - body_bob
    )

    left_foot = P(
        -22 + leg_motion,
        -5
    )

    right_foot = P(
        22 - leg_motion,
        -5
    )

    # Feet
    draw_limb(draw, left_knee, left_foot, pants, int(15*s))
    draw_limb(draw, right_knee, right_foot, pants, int(15*s))

    draw_limb(
        draw,
        left_foot,
        P(-12 + leg_motion, 0),
        shoe,
        int(13*s)
    )

    draw_limb(
        draw,
        right_foot,
        P(32 - leg_motion, 0),
        shoe,
        int(13*s)
    )

    # Torso
    torso = [

        P(-25, -125 - body_bob),
        P(25, -125 - body_bob),
        P(20, -60 - body_bob),
        P(-20, -60 - body_bob)

    ]

    draw.polygon(
        torso,
        fill=shirt
    )

    # Neck
    draw.rectangle(
        (
            P(-7, -135 - body_bob),
            P(7, -118 - body_bob)
        ),
        fill=skin
    )

    # Arms
    left_shoulder = P(-24, -115 - body_bob)
    right_shoulder = P(24, -115 - body_bob)

    left_elbow = P(
        -43 - arm_motion * 0.3,
        -82 - body_bob
    )

    right_elbow = P(
        43 + arm_motion * 0.3,
        -82 - body_bob
    )

    left_hand = P(
        -48 - arm_motion * 0.5,
        -53 - body_bob
    )

    right_hand = P(
        48 + arm_motion,
        -53 - body_bob
    )

    if action == "wave":

        right_elbow = P(40, -150 - body_bob)
        right_hand = P(
            55 + math.sin(frame * 0.25) * 8,
            -185 - body_bob
        )

    if action == "think":

        right_elbow = P(35, -95 - body_bob)
        right_hand = P(16, -165 - body_bob)

    if action == "point":

        right_elbow = P(55, -115 - body_bob)
        right_hand = P(100, -145 - body_bob)

    draw_limb(
        draw,
        left_shoulder,
        left_elbow,
        shirt,
        int(13*s)
    )

    draw_limb(
        draw,
        left_elbow,
        left_hand,
        skin,
        int(10*s)
    )

    draw_limb(
        draw,
        right_shoulder,
        right_elbow,
        shirt,
        int(13*s)
    )

    draw_limb(
        draw,
        right_elbow,
        right_hand,
        skin,
        int(10*s)
    )

    # Head
    hx = x
    hy = ground_y + head_y * s

    radius_x = int(28*s)
    radius_y = int(35*s)

    draw.ellipse(
        (
            hx-radius_x,
            hy-radius_y,
            hx+radius_x,
            hy+radius_y
        ),
        fill=skin
    )

    # Hair
    draw.pieslice(
        (
            hx-radius_x,
            hy-radius_y-7*s,
            hx+radius_x,
            hy+radius_y
        ),
        180,
        360,
        fill=hair
    )

    draw.ellipse(
        (
            hx-radius_x,
            hy-radius_y,
            hx+radius_x,
            hy-radius_y+15*s
        ),
        fill=hair
    )

    # Eyes
    eye_y = hy - 3*s

    eye_color = (30, 25, 25)

    if emotion == "angry":

        draw.line(
            (hx-17*s, eye_y-6*s, hx-6*s, eye_y-2*s),
            fill=eye_color,
            width=max(1, int(3*s))
        )

        draw.line(
            (hx+6*s, eye_y-2*s, hx+17*s, eye_y-6*s),
            fill=eye_color,
            width=max(1, int(3*s))
        )

    else:

        draw.ellipse(
            (
                hx-15*s,
                eye_y-3*s,
                hx-8*s,
                eye_y+5*s
            ),
            fill=eye_color
        )

        draw.ellipse(
            (
                hx+8*s,
                eye_y-3*s,
                hx+15*s,
                eye_y+5*s
            ),
            fill=eye_color
        )

    # Mouth
    mouth_y = hy + 15*s

    if emotion == "happy":

        draw.arc(
            (
                hx-12*s,
                mouth_y-9*s,
                hx+12*s,
                mouth_y+9*s
            ),
            10,
            170,
            fill=(105, 35, 38),
            width=max(1, int(3*s))
        )

    elif emotion == "sad":

        draw.arc(
            (
                hx-12*s,
                mouth_y,
                hx+12*s,
                mouth_y+12*s
            ),
            190,
            350,
            fill=(105, 35, 38),
            width=max(1, int(3*s))
        )

    elif emotion == "surprised":

        draw.ellipse(
            (
                hx-5*s,
                mouth_y-4*s,
                hx+5*s,
                mouth_y+7*s
            ),
            fill=(90, 30, 35)
        )

    else:

        mouth_open = (
            abs(math.sin(frame * 0.65)) * 5
            if action == "talk"
            else 1
        )

        draw.line(
            (
                hx-8*s,
                mouth_y,
                hx+8*s,
                mouth_y+mouth_open
            ),
            fill=(90, 35, 35),
            width=max(1, int(3*s))
        )


# ============================================================
# SUBTITLE ENGINE
# ============================================================

def draw_subtitle(image, text):

    draw = ImageDraw.Draw(image)

    width, height = image.size

    font = load_font(
        max(18, int(width * 0.033)),
        bold=True
    )

    max_chars = 42

    words = text.split()
    lines = []
    current = ""

    for word in words:

        test = (current + " " + word).strip()

        if len(test) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = test

    if current:
        lines.append(current)

    lines = lines[:3]

    line_height = int(width * 0.055)

    total_height = len(lines) * line_height + 30

    box_y = height - total_height - 30

    draw.rounded_rectangle(
        (
            20,
            box_y,
            width - 20,
            height - 18
        ),
        radius=15,
        fill=(0, 0, 0, 175)
    )

    for i, line in enumerate(lines):

        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font
        )

        text_width = bbox[2] - bbox[0]

        tx = (width - text_width) // 2
        ty = box_y + 12 + i * line_height

        draw.text(
            (tx+2, ty+2),
            line,
            font=font,
            fill=(0, 0, 0)
        )

        draw.text(
            (tx, ty),
            line,
            font=font,
            fill=(255, 255, 255)
        )


# ============================================================
# CINEMATIC RENDER ENGINE
# ============================================================

def render_scene_frame(
    width,
    height,
    frame,
    scene_index,
    scene,
    character_positions
):

    image = create_background(
        width,
        height,
        scene_index
    )

    # Cinematic glow
    overlay = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 0)
    )

    glow = ImageDraw.Draw(overlay)

    glow.ellipse(
        (
            width * 0.2,
            height * 0.1,
            width * 0.8,
            height * 0.8
        ),
        fill=(50, 110, 180, 16)
    )

    overlay = overlay.filter(
        ImageFilter.GaussianBlur(55)
    )

    image = Image.alpha_composite(
        image.convert("RGBA"),
        overlay
    ).convert("RGB")

    speaker = scene["speaker"]

    char_index = character_positions[speaker]

    center_x = width // 2

    if len(character_positions) > 1:

        if char_index % 2 == 0:
            center_x = int(width * 0.34)
        else:
            center_x = int(width * 0.68)

    ground_y = int(height * 0.84)

    scale = min(width / 540, height / 960) * 1.5

    draw_character(
        image,
        center_x,
        ground_y,
        frame,
        emotion=scene["emotion"],
        action=scene["action"],
        character_index=char_index,
        scale=scale
    )

    draw_subtitle(
        image,
        scene["dialogue"]
    )

    return image


def write_raw_video(output_path, scenes, width, height, job_id):

    ffmpeg = get_ffmpeg()

    command = [

        ffmpeg,

        "-y",

        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(FPS),

        "-i", "-",

        "-an",

        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",

        "-movflags", "+faststart",

        str(output_path)

    ]

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )

    speakers = list(dict.fromkeys(
        scene["speaker"] for scene in scenes
    ))

    positions = {
        name: index
        for index, name in enumerate(speakers)
    }

    total_frames = sum(
        max(1, int(scene["duration"] * FPS))
        for scene in scenes
    )

    current_frame = 0

    try:

        for scene_index, scene in enumerate(scenes):

            scene_frames = max(
                1,
                int(scene["duration"] * FPS)
            )

            for local_frame in range(scene_frames):

                image = render_scene_frame(
                    width,
                    height,
                    local_frame,
                    scene_index,
                    scene,
                    positions
                )

                process.stdin.write(
                    np.asarray(
                        image,
                        dtype=np.uint8
                    ).tobytes()
                )

                current_frame += 1

                if current_frame % 10 == 0:

                    progress = int(
                        55 +
                        (current_frame / max(1, total_frames)) * 35
                    )

                    update_job(
                        job_id,
                        progress=progress,
                        message="Rendering animation..."
                    )

        process.stdin.close()

        stderr = process.stderr.read()
        return_code = process.wait()

        if return_code != 0:
            raise RuntimeError(
                stderr.decode(
                    "utf-8",
                    errors="ignore"
                )[-3000:]
            )

    except Exception:

        try:
            process.kill()
        except Exception:
            pass

        raise


# ============================================================
# AUDIO ENGINE
# ============================================================

def combine_audio_files(audio_files, output_path):

    if not audio_files:
        return None

    ffmpeg = get_ffmpeg()

    list_file = output_path.parent / (
        output_path.stem + "_audio_list.txt"
    )

    with open(list_file, "w", encoding="utf-8") as file:

        for audio in audio_files:

            escaped = str(audio).replace("'", "'\\''")

            file.write(
                "file '" + escaped + "'\n"
            )

    command = [

        ffmpeg,
        "-y",

        "-f", "concat",
        "-safe", "0",

        "-i", str(list_file),

        "-c:a", "aac",
        "-b:a", "128k",

        str(output_path)

    ]

    subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True
    )

    return output_path


def mux_video_audio(video_path, audio_path, output_path):

    ffmpeg = get_ffmpeg()

    command = [

        ffmpeg,
        "-y",

        "-i", str(video_path),
        "-i", str(audio_path),

        "-map", "0:v:0",
        "-map", "1:a:0",

        "-c:v", "copy",
        "-c:a", "aac",

        "-shortest",
        "-movflags", "+faststart",

        str(output_path)

    ]

    subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True
    )


# ============================================================
# COMPLETE ANIMATION JOB
# ============================================================

def process_animation_job(job_id, script, ratio, style, voice_mode, lang_mode, voice_file=None):

    work_dir = JOB_DIR / job_id

    work_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    try:

        update_job(
            job_id,
            status="processing",
            progress=3,
            message="Understanding your story..."
        )

        scenes = parse_script(script)

        if not scenes:
            raise ValueError(
                "No valid dialogue found in your script."
            )

        if voice_mode == "manual" and voice_file:

            uploaded_voice = UPLOAD_DIR / (
                job_id + Path(voice_file.filename).suffix
            )

            voice_file.save(uploaded_voice)

            # Manual voice is used as a single narration track.
            audio_files = [uploaded_voice]

            for scene in scenes:
                scene["duration"] = 3.0

        else:

            audio_files = []

            for index, scene in enumerate(scenes):

                update_job(
                    job_id,
                    progress=8 + int(
                        index / max(1, len(scenes)) * 20
                    ),
                    message="Generating dialogue voice..."
                )

                audio_path = work_dir / (
                    f"voice_{index:03d}.mp3"
                )

                generate_voice(
                    scene["dialogue"],
                    audio_path,
                    lang_mode
                )

                audio_files.append(audio_path)

                scene["duration"] = max(
                    2.5,
                    get_media_duration(audio_path)
                )

        total_duration = sum(
            scene["duration"]
            for scene in scenes
        )

        if total_duration > MAX_DURATION:

            raise ValueError(
                f"Your animation is too long. Maximum duration is {MAX_DURATION} seconds."
            )

        update_job(
            job_id,
            progress=35,
            message="Preparing animation scenes..."
        )

        if ratio == "16:9":

            width = 960
            height = 540

        else:

            width = 540
            height = 960

        silent_video = work_dir / "silent.mp4"
        audio_output = work_dir / "audio.m4a"

        final_video = VIDEO_DIR / (
            job_id + ".mp4"
        )

        write_raw_video(
            silent_video,
            scenes,
            width,
            height,
            job_id
        )

        update_job(
            job_id,
            progress=93,
            message="Combining video and audio..."
        )

        combine_audio_files(
            audio_files,
            audio_output
        )

        mux_video_audio(
            silent_video,
            audio_output,
            final_video
        )

        update_job(
            job_id,
            status="completed",
            progress=100,
            message="Animation completed!",
            video_url=f"/api/video/{job_id}",
            duration=round(total_duration, 2),
            scenes=len(scenes)
        )

    except Exception as error:

        traceback.print_exc()

        update_job(
            job_id,
            status="failed",
            progress=0,
            message=str(error),
            error=str(error)
        )

    finally:

        gc.collect()


# ============================================================
# API ROUTES
# ============================================================

@app.route("/")
def home():

    index_file = BASE_DIR / "index.html"

    if not index_file.exists():
        return "NEXORA AI Animation Studio backend is running."

    return send_file(index_file)


@app.route("/api/ping")
def ping():

    return jsonify({

        "status": "online",
        "app": "NEXORA AI Animation Studio",
        "version": "1.0.0",
        "engine": "Human 2D Rig Animation",
        "fps": FPS

    })


@app.route("/api/start", methods=["POST"])
def start_animation():

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

    lang_mode = request.form.get(
        "lang_mode",
        "hindi"
    )

    voice_file = request.files.get(
        "voice_file"
    )

    if not script:

        return jsonify({
            "error": "Please enter your story or dialogue."
        }), 400

    if ratio not in ["9:16", "16:9"]:

        ratio = "9:16"

    if lang_mode not in ["hindi", "english"]:

        lang_mode = "hindi"

    if voice_mode not in ["auto", "manual"]:

        voice_mode = "auto"

    with jobs_lock:

        active_jobs = sum(

            1 for job in jobs.values()

            if job.get("status") in [
                "queued",
                "processing"
            ]

        )

        if active_jobs >= 2:

            return jsonify({

                "error": "Server queue is full. Please wait."

            }), 429

        job_id = str(uuid.uuid4())

        jobs[job_id] = {

            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Waiting for animation engine...",
            "created_at": time.time(),
            "updated_at": time.time()

        }

    executor.submit(

        process_animation_job,

        job_id,
        script,
        ratio,
        style,
        voice_mode,
        lang_mode,
        voice_file

    )

    return jsonify({

        "success": True,
        "job_id": job_id,
        "message": "Animation job created."

    })


@app.route("/api/status/<job_id>")
def animation_status(job_id):

    job = get_job(job_id)

    if not job:

        return jsonify({
            "error": "Job not found."
        }), 404

    return jsonify(job)


@app.route("/api/job/<job_id>")
def animation_job(job_id):

    job = get_job(job_id)

    if not job:

        return jsonify({
            "error": "Job not found."
        }), 404

    return jsonify(job)


@app.route("/api/video/<job_id>")
def get_video(job_id):

    video_path = VIDEO_DIR / (
        job_id + ".mp4"
    )

    if not video_path.exists():

        return jsonify({
            "error": "Video is not ready."
        }), 404

    return send_file(

        video_path,

        mimetype="video/mp4",

        as_attachment=False,

        download_name="NEXORA_Animation.mp4"

    )


@app.route("/api/download/<job_id>")
def download_video(job_id):

    video_path = VIDEO_DIR / (
        job_id + ".mp4"
    )

    if not video_path.exists():

        return jsonify({
            "error": "Video is not ready."
        }), 404

    return send_file(

        video_path,

        mimetype="video/mp4",

        as_attachment=True,

        download_name="NEXORA_Animation.mp4"

    )


@app.route("/api/health")
def health():

    return jsonify({

        "status": "healthy",
        "time": datetime.utcnow().isoformat(),
        "active_jobs": sum(

            1 for job in jobs.values()

            if job.get("status") in [
                "queued",
                "processing"
            ]

        )

    })


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(413)
def file_too_large(error):

    return jsonify({

        "error": "Uploaded file is too large. Maximum is 100 MB."

    }), 413


@app.errorhandler(404)
def not_found(error):

    return jsonify({

        "error": "Requested resource was not found."

    }), 404


@app.errorhandler(500)
def internal_error(error):

    return jsonify({

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
