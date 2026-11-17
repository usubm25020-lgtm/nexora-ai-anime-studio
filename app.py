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
# 1. TENSION PULSE, AI DISCOVERY CHIMES & ESCAPE VICTORY AUDIO SYNTHESIZER
# ===========================================================================
def generate_maze_audio_wav(duration_sec: float, escape_events: list, gen_events: list, out_wav: str):
    """Synthesizes rhythmic cyber-tension pulse, generation chimes, and victory fanfare."""
    rate = 22050
    n = int(duration_sec * rate)
    t = np.linspace(0, duration_sec, n, endpoint=False, dtype=np.float32)

    bpm = 126.0
    beat_dur = 60.0 / bpm
    beat_pos = (t % beat_dur) / beat_dur

    # Cyber-synth pulse bass
    sub = np.sin(2.0 * np.pi * (55.0 + 45.0 * np.exp(-beat_pos * 20.0)) * (t % beat_dur)) * np.exp(-beat_pos * 8.0) * 0.30

    # Arpeggio tension notes
    notes = [220.0, 261.63, 329.63, 392.00, 440.00, 392.00, 329.63, 261.63]
    step_idx = (t / (beat_dur * 0.5)).astype(np.int32)
    step_pos = (t % (beat_dur * 0.5)) / (beat_dur * 0.5)
    freqs = np.array([notes[i % len(notes)] for i in step_idx], dtype=np.float32)
    arp = np.sin(2.0 * np.pi * freqs * t) * np.exp(-step_pos * 7.0) * 0.14

    audio = sub + arp

    # Generation upgrade sweep sound
    for gt in gen_events:
        s_idx = int(gt * rate)
        dur = 0.40
        e_idx = min(n, s_idx + int(dur * rate))
        if s_idx < e_idx:
            tg = np.linspace(0, (e_idx - s_idx) / float(rate), e_idx - s_idx, endpoint=False, dtype=np.float32)
            swp = np.sin(2.0 * np.pi * (280.0 + 420.0 * tg) * tg) * np.exp(-tg * 6.0) * 0.28
            audio[s_idx:e_idx] += swp

    # Celebratory chime whenever an AI agent escapes the maze!
    for et in escape_events:
        s_idx = int(et * rate)
        dur = 0.55
        e_idx = min(n, s_idx + int(dur * rate))
        if s_idx < e_idx:
            te = np.linspace(0, (e_idx - s_idx) / float(rate), e_idx - s_idx, endpoint=False, dtype=np.float32)
            chime = (
                0.45 * np.sin(2.0 * np.pi * 880.0 * te) +
                0.35 * np.sin(2.0 * np.pi * 1174.66 * te) +
                0.25 * np.sin(2.0 * np.pi * 1760.0 * te)
            ) * np.exp(-te * 5.5) * 0.38
            audio[s_idx:e_idx] += chime

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
# 2. PROCEDURAL MAZE & REINFORCEMENT-LEARNING SWARM ENGINE
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

class MazeAILearningEngine:
    """
    Generates a complex braided maze and simulates generations of AI agents
    evolving via pheromone/Q-value trail reinforcement until they master the exit!
    """
    def __init__(self, w: int, h: int, difficulty: str, num_agents: int):
        self.w = w
        self.h = h
        self.is_vertical = (h > w)
        self.num_agents = num_agents
        self.rng = random.Random(int(time.time()) % 100000)

        # Grid dimensions (odd numbers for crisp wall-carving)
        if difficulty == "insane":
            self.cols, self.rows = (21, 31) if self.is_vertical else (35, 21)
        elif difficulty == "hard":
            self.cols, self.rows = (17, 25) if self.is_vertical else (29, 17)
        else:
            self.cols, self.rows = (13, 19) if self.is_vertical else (23, 13)

        self.maze = self._carve_maze()
        self.start_cell = (1, 1)
        self.goal_cell = (self.cols - 2, self.rows - 2)

        # Compute true shortest-path distance map from Goal using BFS (for AI reward & HUD %)
        self.dist_map = self._compute_bfs_distances()
        self.max_dist = max(1, self.dist_map[self.start_cell[1]][self.start_cell[0]])

        # AI Collective Memory Map (pheromone / learned Q-score per cell)
        self.memory_grid = np.zeros((self.rows, self.cols), dtype=np.float32)

        self.generation = 1
        self.total_escaped = 0
        self.best_progress_pct = 0
        self.escape_events = []
        self.gen_events = []
        self.gen_timer = 0.0
        self.gen_duration = 7.5  # Evolve to next smarter generation every 7.5 seconds

        self._precompute_maze_bg()
        self._spawn_generation()

    def _carve_maze(self):
        # 1 = Wall, 0 = Open Corridor
        grid = [[1] * self.cols for _ in range(self.rows)]
        stack = [(1, 1)]
        grid[1][1] = 0

        while stack:
            cx, cy = stack[-1]
            neighbors = []
            for dx, dy in [(0, -2), (0, 2), (-2, 0), (2, 0)]:
                nx, ny = cx + dx, cy + dy
                if 1 <= nx < self.cols - 1 and 1 <= ny < self.rows - 1 and grid[ny][nx] == 1:
                    neighbors.append((nx, ny, cx + dx // 2, cy + dy // 2))
            if neighbors:
                nx, ny, mx, my = self.rng.choice(neighbors)
                grid[my][mx] = 0
                grid[ny][nx] = 0
                stack.append((nx, ny))
            else:
                stack.pop()

        # Add a few tactical loops so AI agents can discover shortcuts!
        for _ in range((self.cols * self.rows) // 28):
            rx = self.rng.randrange(2, self.cols - 2)
            ry = self.rng.randrange(2, self.rows - 2)
            if grid[ry][rx] == 1:
                if (grid[ry][rx - 1] == 0 and grid[ry][rx + 1] == 0) or (grid[ry - 1][rx] == 0 and grid[ry + 1][rx] == 0):
                    grid[ry][rx] = 0
        return grid

    def _compute_bfs_distances(self):
        dist = [[9999] * self.cols for _ in range(self.rows)]
        gx, gy = self.goal_cell
        dist[gy][gx] = 0
        q = [(gx, gy)]
        head = 0
        while head < len(q):
            cx, cy = q[head]
            head += 1
            for dx, dy in [(0, -1), (0, 1), (-1, 0), (1, 0)]:
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < self.cols and 0 <= ny < self.rows and self.maze[ny][nx] == 0:
                    if dist[ny][nx] > dist[cy][cx] + 1:
                        dist[ny][nx] = dist[cy][cx] + 1
                        q.append((nx, ny))
        return dist

    def _precompute_maze_bg(self):
        top_y = int(self.h * 0.13)
        bot_y = int(self.h * 0.93)
        left_x = int(self.w * 0.05)
        right_x = int(self.w * 0.95)

        avail_w = right_x - left_x
        avail_h = bot_y - top_y
        self.cell_sz = min(avail_w / float(self.cols), avail_h / float(self.rows))
        self.ox = (self.w - self.cols * self.cell_sz) * 0.5
        self.oy = top_y + (avail_h - self.rows * self.cell_sz) * 0.5

        bg = Image.new("RGB", (self.w, self.h), (6, 9, 18))
        d = ImageDraw.Draw(bg)

        for r in range(self.rows):
            for c in range(self.cols):
                x0 = self.ox + c * self.cell_sz
                y0 = self.oy + r * self.cell_sz
                x1 = x0 + self.cell_sz
                y1 = y0 + self.cell_sz
                if self.maze[r][c] == 1:
                    d.rectangle([x0, y0, x1, y1], fill=(24, 34, 58), outline=(38, 52, 84))
                else:
                    d.rectangle([x0, y0, x1, y1], fill=(10, 14, 26))

        # Highlight START (Cyan) and EXIT GOAL (Emerald)
        sx, sy = self.start_cell
        d.rectangle([
            self.ox + sx * self.cell_sz + 2, self.oy + sy * self.cell_sz + 2,
            self.ox + (sx + 1) * self.cell_sz - 2, self.oy + (sy + 1) * self.cell_sz - 2
        ], fill=(14, 116, 144))

        gx, gy = self.goal_cell
        d.rectangle([
            self.ox + gx * self.cell_sz + 2, self.oy + gy * self.cell_sz + 2,
            self.ox + (gx + 1) * self.cell_sz - 2, self.oy + (gy + 1) * self.cell_sz - 2
        ], fill=(16, 185, 129))

        self.maze_bg = bg

    def _spawn_generation(self):
        self.gen_timer = 0.0
        self.agents = []
        sx, sy = self.start_cell
        palette = [(34, 211, 238), (250, 204, 21), (244, 63, 94), (168, 85, 247), (52, 211, 153)]

        for i in range(self.num_agents):
            self.agents.append({
                "cx": sx,
                "cy": sy,
                "px": float(sx),
                "py": float(sy),
                "tx": sx,
                "ty": sy,
                "move_prog": 1.0,
                "speed": self.rng.uniform(5.5, 8.2) + min(3.5, (self.generation - 1) * 0.45),
                "visited": {(sx, sy)},
                "path": [(sx, sy)],
                "escaped": False,
                "dead_end_hits": 0,
                "color": palette[i % len(palette)]
            })

    def step_simulation(self, global_t: float, dt: float):
        self.gen_timer += dt
        if self.gen_timer >= self.gen_duration:
            self.generation += 1
            self.gen_events.append(global_t)
            # Evaporate old memory slightly so AI keeps refining optimal path
            self.memory_grid *= 0.85
            self._spawn_generation()
            return

        gx, gy = self.goal_cell
        # Intelligence increases every generation!
        smartness = min(0.95, 0.25 + (self.generation - 1) * 0.14)

        for ag in self.agents:
            if ag["escaped"]:
                continue

            ag["move_prog"] += ag["speed"] * dt
            if ag["move_prog"] >= 1.0:
                ag["cx"], ag["cy"] = ag["tx"], ag["ty"]
                ag["px"], ag["py"] = float(ag["cx"]), float(ag["cy"])
                ag["move_prog"] = 0.0

                cx, cy = ag["cx"], ag["cy"]
                if (cx, cy) == (gx, gy):
                    ag["escaped"] = True
                    self.total_escaped += 1
                    self.best_progress_pct = 100
                    self.escape_events.append(global_t)
                    # Reinforce entire winning path strongly in collective AI memory!
                    for px_c, py_c in ag["path"]:
                        self.memory_grid[py_c, px_c] = min(10.0, self.memory_grid[py_c, px_c] + 2.5)
                    continue

                # Track closest progress toward exit
                cur_d = self.dist_map[cy][cx]
                pct = int(max(0, min(99, ((self.max_dist - cur_d) / float(self.max_dist)) * 100)))
                if pct > self.best_progress_pct:
                    self.best_progress_pct = pct

                # Reward cell slightly for getting closer
                self.memory_grid[cy, cx] = min(10.0, self.memory_grid[cy, cx] + 0.08)

                # Choose next corridor cell
                options = []
                for dx, dy in [(0, -1), (0, 1), (-1, 0), (1, 0)]:
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows and self.maze[ny][nx] == 0:
                        options.append((nx, ny))

                if options:
                    unvisited = [o for o in options if o not in ag["visited"]]
                    candidates = unvisited if unvisited else options
                    if not unvisited:
                        # Penalize dead-end cell so future agents avoid it!
                        self.memory_grid[cy, cx] = max(-5.0, self.memory_grid[cy, cx] - 0.65)

                    if self.rng.random() < smartness:
                        # Pick cell with highest learned memory + heuristic proximity
                        best_c = max(
                            candidates,
                            key=lambda c_xy: self.memory_grid[c_xy[1], c_xy[0]] - self.dist_map[c_xy[1]][c_xy[0]] * 0.18 + self.rng.uniform(-0.4, 0.4)
                        )
                    else:
                        best_c = self.rng.choice(candidates)

                    ag["tx"], ag["ty"] = best_c
                    ag["visited"].add(best_c)
                    ag["path"].append(best_c)
            else:
                # Smooth interpolation between grid cells
                f = ag["move_prog"]
                ag["px"] = ag["cx"] + (ag["tx"] - ag["cx"]) * f
                ag["py"] = ag["cy"] + (ag["ty"] - ag["cy"]) * f

    def render_frame(self) -> bytes:
        frame = self.maze_bg.copy()
        draw = ImageDraw.Draw(frame)
        sc = self.w / 960.0 if not self.is_vertical else self.w / 540.0

        # 1. Draw Learned AI Pheromone Heatmap Trail
        for r in range(self.rows):
            for c in range(self.cols):
                val = float(self.memory_grid[r, c])
                if val > 0.25 and self.maze[r][c] == 0:
                    intensity = min(1.0, val / 5.0)
                    pad = self.cell_sz * 0.26
                    x0 = self.ox + c * self.cell_sz + pad
                    y0 = self.oy + r * self.cell_sz + pad
                    x1 = self.ox + (c + 1) * self.cell_sz - pad
                    y1 = self.oy + (r + 1) * self.cell_sz - pad
                    col = (int(16 + 36 * intensity), int(85 + 145 * intensity), int(129 + 110 * intensity))
                    draw.rectangle([x0, y0, x1, y1], fill=col)

        # 2. Draw Active AI Agents
        r_ag = max(2.5, self.cell_sz * 0.28)
        for ag in self.agents:
            if ag["escaped"]:
                continue
            ax = self.ox + (ag["px"] + 0.5) * self.cell_sz
            ay = self.oy + (ag["py"] + 0.5) * self.cell_sz
            draw.ellipse([ax - r_ag, ay - r_ag, ax + r_ag, ay + r_ag], fill=ag["color"], outline=(255, 255, 255), width=1)

        # 3. Top Live Telemetry HUD
        font_hdr = get_font(max(16, int(21 * sc)))
        font_sub = get_font(max(12, int(15 * sc)))

        hdr_txt = f"AI GENERATION {self.generation}  •  ESCAPED: {self.total_escaped}"
        draw.text((int(self.w * 0.05), int(self.h * 0.028)), hdr_txt, font=font_hdr, fill=(248, 250, 252))

        sub_txt = f"BEST AI PROGRESS: {self.best_progress_pct}% TO EXIT  •  SWARM SIZE: {self.num_agents}"
        draw.text((int(self.w * 0.05), int(self.h * 0.072)), sub_txt, font=font_sub, fill=(52, 211, 153))

        return frame.tobytes()


# ===========================================================================
# 3. RENDER PIPELINE & API ROUTES
# ===========================================================================
def run_maze_job(job_id: str, ratio: str, difficulty: str, agents_cnt: int, duration_sec: float):
    job = JOBS[job_id]
    try:
        out_w, out_h = (960, 540) if ratio == "16:9" else (540, 960)
        fps = 24
        total_frames = int(duration_sec * fps)
        dt = 1.0 / float(fps)

        sim = MazeAILearningEngine(out_w, out_h, difficulty, agents_cnt)
        out_name = f"NEXORA_AI_MAZE_{difficulty.upper()}_{int(duration_sec)}s_{ratio.replace(':', 'x')}_{int(time.time())}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        raw_vid_path = os.path.join(OUTPUT_DIR, f"{job_id}_silent.mp4")
        wav_path = os.path.join(AUDIO_DIR, f"{job_id}_maze_audio.wav")

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
            sim.step_simulation(global_t, dt)
            raw_bytes = sim.render_frame()
            proc.stdin.write(raw_bytes)
            global_t += dt

            if f % 48 == 0:
                job["progress"] = 8 + int(prog * 78)
                job["message"] = f"Training AI Gen {sim.generation} ({int(prog * 100)}%)..."

        proc.stdin.close()
        proc.wait()

        job["progress"] = 88
        job["message"] = "Synthesizing AI Evolution Audio & Escape Chimes..."
        generate_maze_audio_wav(duration_sec, sim.escape_events, sim.gen_events, wav_path)

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
        job["message"] = f"Completed! AI Maze Learning Video Ready ({out_name})"

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
    difficulty = data.get("difficulty", "hard")
    agents_cnt = int(data.get("agents", "60"))
    duration_sec = max(15.0, min(600.0, float(data.get("duration", "90"))))

    JOBS[job_id] = {"status": "running", "progress": 3, "message": "Carving Impossible Maze & Spawning AI Swarm..."}
    threading.Thread(
        target=run_maze_job,
        args=(job_id, ratio, difficulty, agents_cnt, duration_sec),
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
