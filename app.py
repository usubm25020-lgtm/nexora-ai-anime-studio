import os
import time
import math
import uuid
import wave
import random
import subprocess
import threading
import numpy as np

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont
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
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
JOBS = {}

# ===========================================================================
# 1. FACTIONS & COUNTRIES ROSTER
# ===========================================================================
FACTIONS_DATA = {
    "countries": [
        ("INDIA",   (255, 153, 51)),
        ("USA",     (59, 130, 246)),
        ("RUSSIA",  (239, 68, 68)),
        ("CHINA",   (250, 204, 21)),
        ("JAPAN",   (244, 114, 182)),
        ("GERMANY", (168, 85, 247)),
        ("BRAZIL",  (16, 185, 129)),
        ("UK",      (34, 211, 238)),
        ("FRANCE",  (99, 102, 241)),
        ("TURKEY",  (248, 113, 113)),
        ("EGYPT",   (217, 119, 6)),
        ("CANADA",  (225, 29, 72))
    ],
    "empires": [
        ("CRIMSON", (244, 63, 94)),
        ("CYAN",    (34, 211, 238)),
        ("GOLD",    (250, 204, 21)),
        ("EMERALD", (16, 185, 129)),
        ("VIOLET",  (168, 85, 247)),
        ("ORANGE",  (249, 115, 22)),
        ("COBALT",  (59, 130, 246)),
        ("MAGENTA", (232, 121, 249))
    ]
}

# ===========================================================================
# 2. WAR DRUMS, CONQUEST BOOM & ELIMINATION AUDIO SYNTHESIZER
# ===========================================================================
def generate_battle_audio_wav(duration_sec: float, elim_events: list, out_wav: str):
    """Synthesizes rhythmic epic war drums, ambient tension, and elimination horns/booms."""
    rate = 22050
    n = int(duration_sec * rate)
    t = np.linspace(0, duration_sec, n, endpoint=False, dtype=np.float32)

    bpm = 122.0
    beat_dur = 60.0 / bpm
    beat_pos = (t % beat_dur) / beat_dur
    beat_num = (t / beat_dur).astype(np.int32)

    # Epic War Taiko Drum pattern (BOOM - boom - boom-boom)
    is_accent = ((beat_num % 4) == 0).astype(np.float32)
    drum_freq = (54.0 + 42.0 * np.exp(-beat_pos * 18.0)) * (1.0 + 0.12 * is_accent)
    drums = np.sin(2.0 * np.pi * drum_freq * (t % beat_dur)) * np.exp(-beat_pos * (6.5 - 1.5 * is_accent)) * (0.34 + 0.14 * is_accent)

    # Subtle metallic war percussion
    rng = np.random.default_rng(404)
    noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
    perc_env = np.exp(-((t + beat_dur * 0.5) % beat_dur) * 38.0)
    perc = noise * perc_env * 0.10

    # Deep war tension drone
    drone = (0.05 * np.sin(2.0 * np.pi * 65.41 * t) + 0.03 * np.sin(2.0 * np.pi * 98.00 * t))

    audio = drums + perc + drone

    # Dramatic Sub-Bass Impact + Brass Horn whenever a country/empire is eliminated!
    for ev_t, is_final in elim_events:
        s_idx = int(ev_t * rate)
        dur = 1.1 if is_final else 0.55
        e_idx = min(n, s_idx + int(dur * rate))
        if s_idx < e_idx:
            seg_len = e_idx - s_idx
            te = np.linspace(0, seg_len / float(rate), seg_len, endpoint=False, dtype=np.float32)
            boom = np.sin(2.0 * np.pi * (95.0 * np.exp(-te * 6.0) + 38.0) * te) * np.exp(-te * 4.5) * 0.65
            horn = (
                0.35 * np.sin(2.0 * np.pi * 196.00 * te) +
                0.25 * np.sin(2.0 * np.pi * 293.66 * te)
            ) * np.exp(-te * 3.5) * 0.35
            audio[s_idx:e_idx] += (boom + horn)

    fade_s = min(int(0.8 * rate), n // 4)
    if fade_s > 0:
        audio[:fade_s] *= np.linspace(0, 1, fade_s)
        audio[-fade_s:] *= np.linspace(1, 0, fade_s)

    audio = np.clip(audio, -0.95, 0.95)
    pcm = (audio * 32767.0).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


# ===========================================================================
# 3. PROCEDURAL CONTINENT & CELLULAR CONQUEST SIMULATION ENGINE
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

class TerritoryConquestEngine:
    """
    Generates realistic procedural continents & islands, spawns warring nations/empires,
    simulates dynamic frontline invasions, power surges, and live leaderboard rankings!
    """
    def __init__(self, w: int, h: int, roster_type: str, num_factions: int):
        self.w = w
        self.h = h
        self.is_vertical = (h > w)
        self.rng = random.Random(int(time.time()) % 100000)
        self.np_rng = np.random.default_rng(int(time.time()) % 100000)

        # Simulation Grid Size (Fast & Detailed)
        if self.is_vertical:
            self.gw, self.gh = 120, 150
            self.map_box = (int(w * 0.04), int(h * 0.08), int(w * 0.96), int(h * 0.68))
        else:
            self.gw, self.gh = 165, 125
            self.map_box = (int(w * 0.03), int(h * 0.11), int(w * 0.68), int(h * 0.92))

        self.mw = self.map_box[2] - self.map_box[0]
        self.mh = self.map_box[3] - self.map_box[1]

        # Build Procedural Continents + Islands Land Mask (0 = Ocean, 1 = Land)
        self.land_mask = self._generate_world_map()
        self.total_land = max(1, int(np.sum(self.land_mask)))

        # Setup Factions (1..N)
        pool = list(FACTIONS_DATA.get(roster_type, FACTIONS_DATA["countries"]))
        self.rng.shuffle(pool)
        chosen = pool[:max(4, min(len(pool), num_factions))]

        self.num_factions = len(chosen)
        self.names = [""] + [c[0] for c in chosen]
        self.colors = np.zeros((self.num_factions + 1, 3), dtype=np.uint8)
        self.colors[0] = (10, 22, 40)  # Deep Ocean Blue
        for idx, (_, col) in enumerate(chosen, start=1):
            self.colors[idx] = col

        # Grid stores faction ID (0 = ocean, -1 = neutral land, 1..N = faction)
        self.grid = np.where(self.land_mask, -1, 0).astype(np.int16)
        self.power = np.ones(self.num_factions + 1, dtype=np.float32)
        self.alive = [False] + [True] * self.num_factions
        self.capitals = {}
        self.elim_events = []
        self.banner_text = ""
        self.banner_timer = 0.0

        self._spawn_capitals()

    def _generate_world_map(self) -> np.ndarray:
        """Creates multi-continent + archipelago terrain using layered radial blobs & waves."""
        yy, xx = np.ogrid[0:self.gh, 0:self.gw]
        nx = (xx / float(self.gw)) * 2.0 - 1.0
        ny = (yy / float(self.gh)) * 2.0 - 1.0

        heightmap = np.zeros((self.gh, self.gw), dtype=np.float32)
        # 5 major continental tectonic plates + 6 smaller islands
        for i in range(11):
            cx = self.np_rng.uniform(-0.68, 0.68)
            cy = self.np_rng.uniform(-0.68, 0.68)
            rad = self.np_rng.uniform(0.32, 0.62) if i < 5 else self.np_rng.uniform(0.14, 0.25)
            d2 = ((nx - cx) ** 2 + (ny - cy) ** 2) / (rad ** 2)
            heightmap += np.exp(-d2 * 1.65)

        # Add coastline irregularity
        wave = (
            0.22 * np.sin(nx * 7.5 + self.np_rng.uniform(0, 6)) * np.cos(ny * 7.5) +
            0.14 * np.cos(nx * 14.0 - ny * 12.0)
        )
        heightmap += wave
        # Keep ocean border around edges
        edge_fade = (1.0 - np.power(np.abs(nx), 4.0)) * (1.0 - np.power(np.abs(ny), 4.0))
        land = (heightmap * edge_fade) > 0.58
        return land

    def _spawn_capitals(self):
        land_coords = np.argwhere(self.land_mask)
        self.np_rng.shuffle(land_coords)
        picked = []
        min_dist_sq = ((self.gw + self.gh) * 0.07) ** 2

        for y, x in land_coords:
            if all((y - py) ** 2 + (x - px) ** 2 >= min_dist_sq for py, px in picked):
                picked.append((int(y), int(x)))
                if len(picked) == self.num_factions:
                    break

        while len(picked) < self.num_factions and len(land_coords) > len(picked):
            y, x = land_coords[len(picked)]
            picked.append((int(y), int(x)))

        for fid, (cy, cx) in enumerate(picked, start=1):
            self.capitals[fid] = (cx, cy)
            # Claim initial 3x3 territory around capital
            y0, y1 = max(0, cy - 2), min(self.gh, cy + 3)
            x0, x1 = max(0, cx - 2), min(self.gw, cx + 3)
            patch = self.grid[y0:y1, x0:x1]
            self.grid[y0:y1, x0:x1] = np.where(patch != 0, fid, 0)

    def step_war(self, global_t: float, dt: float, prog: float):
        if self.banner_timer > 0:
            self.banner_timer = max(0.0, self.banner_timer - dt)

        # Periodically shift military momentum so underdogs can make dramatic comebacks!
        if self.rng.random() < 0.08:
            alive_ids = [i for i in range(1, self.num_factions + 1) if self.alive[i]]
            if alive_ids:
                boosted = self.rng.choice(alive_ids)
                self.power[boosted] = min(2.4, self.power[boosted] + 0.45)
        self.power = 1.0 + (self.power - 1.0) * 0.96

        # Run multiple invasion waves per frame so frontlines move briskly
        waves = 4 if prog < 0.7 else 6
        for _ in range(waves):
            # Pick random shift direction (including naval island-hopping across short straits!)
            jump = 2 if self.rng.random() < 0.14 else 1
            dy, dx = self.rng.choice([(-jump, 0), (jump, 0), (0, -jump), (0, jump), (-1, -1), (1, 1), (-1, 1), (1, -1)])

            shifted = np.roll(self.grid, shift=(dy, dx), axis=(0, 1))
            # Attacker must be an active faction (> 0), defender must be land (!= 0) and different
            can_attack = (shifted > 0) & (self.grid != 0) & (shifted != self.grid)

            # Neutral land (-1) is conquered quickly; enemy territory requires winning combat roll
            rand_mat = self.np_rng.random((self.gh, self.gw), dtype=np.float32)
            atk_power = self.power[np.maximum(0, shifted)]
            def_power = np.where(self.grid == -1, 0.35, self.power[np.maximum(0, self.grid)])

            win_prob = 0.28 * (atk_power / (atk_power + def_power))
            conquered = can_attack & (rand_mat < win_prob)
            self.grid = np.where(conquered, shifted, self.grid)

        # Check eliminations & declare champion
        counts = np.bincount(np.maximum(0, self.grid).ravel(), minlength=self.num_factions + 1)
        alive_now = []
        for fid in range(1, self.num_factions + 1):
            if self.alive[fid]:
                if counts[fid] == 0:
                    self.alive[fid] = False
                    self.elim_events.append((global_t, False))
                    self.banner_text = f"{self.names[fid]} ELIMINATED!"
                    self.banner_timer = 1.8
                else:
                    alive_now.append(fid)

        if len(alive_now) == 1 and not getattr(self, "victory_announced", False):
            self.victory_announced = True
            win_id = alive_now[0]
            self.elim_events.append((global_t, True))
            self.banner_text = f"👑 {self.names[win_id]} CONQUERED THE WORLD!"
            self.banner_timer = 5.0

    def render_frame(self, global_t: float) -> bytes:
        frame = Image.new("RGB", (self.w, self.h), (7, 11, 20))
        draw = ImageDraw.Draw(frame)
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0

        # 1. Render Colored Territory Map with Crisp Black Borders
        color_idx = np.where(self.grid == -1, 0, self.grid)
        rgb_grid = self.colors[color_idx]
        # Neutral uncolonized land gets dark slate-gray tone
        neutral_mask = (self.grid == -1)
        rgb_grid[neutral_mask] = (34, 42, 58)

        # Draw dark frontline borders where adjacent pixels belong to different factions
        b_up = self.grid != np.roll(self.grid, 1, axis=0)
        b_lt = self.grid != np.roll(self.grid, 1, axis=1)
        border_mask = (b_up | b_lt) & (self.grid != 0)
        rgb_grid[border_mask] = (12, 16, 26)

        map_img = Image.fromarray(rgb_grid, mode="RGB").resize((self.mw, self.mh), Image.Resampling.NEAREST)
        frame.paste(map_img, (self.map_box[0], self.map_box[1]))
        draw.rectangle(self.map_box, outline=(56, 189, 248), width=2)

        # Draw Capital Stars & Territory Names on Map
        font_cap = get_font(max(11, int(13 * sc)))
        counts = np.bincount(np.maximum(0, self.grid).ravel(), minlength=self.num_factions + 1)
        for fid in range(1, self.num_factions + 1):
            if not self.alive[fid]:
                continue
            ys, xs = np.where(self.grid == fid)
            if len(xs) > 18:
                mx = self.map_box[0] + int((float(np.mean(xs)) / self.gw) * self.mw)
                my = self.map_box[1] + int((float(np.mean(ys)) / self.gh) * self.mh)
                lbl = self.names[fid]
                tb = draw.textbbox((0, 0), lbl, font=font_cap)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                draw.text((mx - tw // 2, my - th // 2), lbl, font=font_cap, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))

        # 2. Header Banner & Elimination Alerts
        font_hdr = get_font(max(16, int(21 * sc)))
        alive_cnt = sum(1 for i in range(1, self.num_factions + 1) if self.alive[i])
        hdr_txt = f"WORLD CONQUEST BATTLE ROYALE  •  {alive_cnt}/{self.num_factions} ALIVE"
        draw.text((int(self.w * 0.03), int(self.h * 0.025)), hdr_txt, font=font_hdr, fill=(248, 250, 252))

        if self.banner_timer > 0 and self.banner_text:
            font_al = get_font(max(15, int(19 * sc)))
            ab = draw.textbbox((0, 0), self.banner_text, font=font_al)
            aw = ab[2] - ab[0] + 28
            ax0 = (self.map_box[0] + self.map_box[2] - aw) // 2
            ay0 = self.map_box[1] + 14
            draw.rounded_rectangle([ax0, ay0, ax0 + aw, ay0 + int(34 * sc)], radius=8, fill=(15, 23, 42), outline=(250, 204, 21), width=2)
            draw.text((ax0 + 14, ay0 + int(6 * sc)), self.banner_text, font=font_al, fill=(250, 204, 21))

        # 3. Live Territory Control Leaderboard
        ranking = []
        for fid in range(1, self.num_factions + 1):
            pct = (counts[fid] / float(self.total_land)) * 100.0
            ranking.append((fid, pct))
        ranking.sort(key=lambda x: x[1], reverse=True)

        if self.is_vertical:
            lb_x0, lb_y0 = int(self.w * 0.04), int(self.h * 0.70)
            lb_w = int(self.w * 0.92)
            row_h = int(26 * sc)
            max_rows = min(8, len(ranking))
        else:
            lb_x0, lb_y0 = int(self.w * 0.70), int(self.h * 0.11)
            lb_w = int(self.w * 0.27)
            row_h = int(34 * sc)
            max_rows = min(12, len(ranking))

        font_lb = get_font(max(12, int(14 * sc)))
        for idx in range(max_rows):
            fid, pct = ranking[idx]
            ry = lb_y0 + idx * row_h
            col = tuple(int(c) for c in self.colors[fid])
            is_dead = (pct <= 0.01)

            draw.rounded_rectangle([lb_x0, ry, lb_x0 + lb_w, ry + row_h - 4], radius=6, fill=(15, 23, 42))
            if not is_dead:
                fill_w = max(6, int((pct / 100.0) * lb_w))
                draw.rounded_rectangle([lb_x0, ry, lb_x0 + fill_w, ry + row_h - 4], radius=6, fill=col)

            txt_col = (100, 116, 139) if is_dead else (255, 255, 255)
            status_str = "OUT" if is_dead else f"{pct:.1f}%"
            draw.text((lb_x0 + 8, ry + int(5 * sc)), f"#{idx + 1} {self.names[fid]}", font=font_lb, fill=txt_col, stroke_width=2 if not is_dead else 0, stroke_fill=(0, 0, 0))
            sb = draw.textbbox((0, 0), status_str, font=font_lb)
            draw.text((lb_x0 + lb_w - (sb[2] - sb[0]) - 8, ry + int(5 * sc)), status_str, font=font_lb, fill=txt_col, stroke_width=2 if not is_dead else 0, stroke_fill=(0, 0, 0))

        return frame.tobytes()


# ===========================================================================
# 4. RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_territory_job(job_id: str, ratio: str, roster: str, factions_cnt: int, duration_sec: float):
    job = JOBS[job_id]
    try:
        out_w, out_h = (960, 540) if ratio == "16:9" else (540, 960)
        fps = 24
        total_frames = int(duration_sec * fps)
        dt = 1.0 / float(fps)

        sim = TerritoryConquestEngine(out_w, out_h, roster, factions_cnt)
        out_name = f"NEXORA_CONQUEST_{roster.upper()}_{int(duration_sec)}s_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        raw_vid_path = os.path.join(OUTPUT_DIR, f"{job_id}_silent.mp4")
        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_war_drums.wav")

        ffmpeg_vid_cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", raw_vid_path
        ]
        proc = subprocess.Popen(ffmpeg_vid_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        global_t = 0.0
        for f in range(total_frames):
            prog = f / max(1, total_frames - 1)
            sim.step_war(global_t, dt, prog)
            raw_bytes = sim.render_frame(global_t)
            proc.stdin.write(raw_bytes)
            global_t += dt

            if f % 48 == 0:
                job["progress"] = 8 + int(prog * 78)
                job["message"] = f"Simulating Territory Battle Royale ({int(prog * 100)}%)..."

        proc.stdin.close()
        proc.wait()

        job["progress"] = 88
        job["message"] = "Synthesizing Epic War Drums & Elimination Horns..."
        generate_battle_audio_wav(duration_sec, sim.elim_events, wav_path)

        job["progress"] = 95
        job["message"] = "Mastering Final MP4..."
        subprocess.run([
            FFMPEG_BIN, "-y",
            "-i", raw_vid_path,
            "-i", wav_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest", out_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(raw_vid_path):
            os.remove(raw_vid_path)

        job["status"] = "done"
        job["progress"] = 100
        job["file_path"] = out_path
        job["file_name"] = out_name
        job["message"] = f"Completed! Territory Battle Royale Ready ({out_name})"

    except Exception as exc:
        job["status"] = "error"
        job["message"] = f"Simulation Error: {exc}"

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
    data = request.get_json(force=True) if request.is_json else request.form
    ratio = data.get("ratio", "16:9")
    roster = data.get("roster", "countries")
    factions_cnt = int(data.get("factions", "10"))
    duration_sec = max(15.0, min(600.0, float(data.get("duration", "90"))))

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Generating Procedural World Map & Capitals..."}
    threading.Thread(
        target=run_territory_job,
        args=(job_id, ratio, roster, factions_cnt, duration_sec),
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
