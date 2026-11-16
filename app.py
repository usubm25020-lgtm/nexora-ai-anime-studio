
import os
import uuid
import subprocess
import tempfile
from pathlib import Path

from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    send_from_directory
)

import imageio_ffmpeg

# =========================================
# NEXORA ANIMATION STUDIO BACKEND
# =========================================

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = Path(tempfile.gettempdir()) / "nexora_jobs"

TEMP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)

app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# =========================================
# HOME
# =========================================

@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


# =========================================
# HEALTH CHECK
# =========================================

@app.route("/api/ping", methods=["GET"])
def ping():
    return jsonify({
        "success": True,
        "application": "NEXORA AI Animation Studio",
        "backend": "Online",
        "video_converter": "Available",
        "ffmpeg": os.path.exists(FFMPEG)
    })


# =========================================
# VIDEO CONVERSION
# =========================================

@app.route("/api/convert", methods=["POST"])
def convert_video():

    if "video" not in request.files:
        return jsonify({
            "success": False,
            "error": "No video file received."
        }), 400

    uploaded = request.files["video"]

    if not uploaded.filename:
        return jsonify({
            "success": False,
            "error": "Empty filename."
        }), 400

    job_id = str(uuid.uuid4())

    input_path = TEMP_DIR / f"{job_id}.webm"
    output_path = TEMP_DIR / f"{job_id}.mp4"

    try:

        uploaded.save(input_path)

        if not input_path.exists() or input_path.stat().st_size == 0:
            return jsonify({
                "success": False,
                "error": "Uploaded video is empty."
            }), 400

        command = [
            FFMPEG,
            "-y",
            "-i", str(input_path),

            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",

            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",

            "-an",
            str(output_path)
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600
        )

        if result.returncode != 0:
            error = result.stderr.decode(
                "utf-8", errors="replace"
            )[-2500:]

            return jsonify({
                "success": False,
                "error": "FFmpeg conversion failed.",
                "details": error
            }), 500

        if not output_path.exists():
            return jsonify({
                "success": False,
                "error": "MP4 output was not created."
            }), 500

        if output_path.stat().st_size == 0:
            return jsonify({
                "success": False,
                "error": "Generated MP4 is empty."
            }), 500

        return send_file(
            output_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="NEXORA-Animation.mp4"
        )

    except subprocess.TimeoutExpired:

        return jsonify({
            "success": False,
            "error": "Conversion timeout. Try a shorter video."
        }), 504

    except Exception as error:

        app.logger.exception("Video conversion error")

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500

    finally:

        # Allow the response to finish before deleting files.
        # Files are stored in temporary storage and may be removed
        # automatically when the server restarts.


# =========================================
# JOB / SERVER INFORMATION
# =========================================

@app.route("/api/job/<job_id>", methods=["GET"])
def job_info(job_id):

    return jsonify({
        "job_id": job_id,
        "status": "This endpoint is reserved for future rendering jobs."
    })


# =========================================
# ERROR HANDLERS
# =========================================

@app.errorhandler(413)
def file_too_large(error):
    return jsonify({
        "success": False,
        "error": "Video exceeds the 500 MB upload limit."
    }), 413


@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "success": False,
        "error": "API endpoint not found."
    }), 404


@app.errorhandler(500)
def server_error(error):
    return jsonify({
        "success": False,
        "error": "Internal server error."
    }), 500


# =========================================
# START SERVER
# =========================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
