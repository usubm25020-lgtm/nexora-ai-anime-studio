import os
import re
import gc
import math
import time
import uuid
import wave
import shutil
import asyncio
import tempfile
import subprocess
import threading
from typing import Dict, List, Any, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, request, jsonify, send_file

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

# ==============================================================================
# 1. CONFIGURATION & EPHEMERAL STORAGE (ZERO DISK BLOAT ON RENDER)
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
TMP_ROOT = os.path.join(BASE_DIR, "tmp_work")

for d in (OUTPUT_DIR, UPLOAD_DIR, TMP_ROOT):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_JOB_IDS = set()
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,40}$")


def is_safe_id(val: str) -> bool:
    return bool(val and SAFE_ID_RE.match(val))


def purge_old_files(max_age_sec: int = 300):
    """Automatically deletes files older than 5 minutes so Render storage never fills up."""
    now = time.time()
    for folder in (OUTPUT_DIR, UPLOAD_DIR):
        try:
            for fname in os.listdir(folder):
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_sec:
                    os.remove(fpath)
        except Exception:
            pass


HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None


# ==============================================================================
# 2. TYPOGRAPHY & SPEECH CLEANER
# ==============================================================================
_FONT_CACHE: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}


def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    key = ("bold" if bold else "reg", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    paths = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for path in paths:
        if os.path.exists(path):
            try:
                fnt = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = fnt
                return fnt
            except Exception:
                pass
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    words = str(text or "").split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def clean_speech_text(text: str) -> str:
    s = str(text or "")
    rules = [
        (r"\bkm/h\b", "kilometers per hour"),
        (r"\bkm/s\b", "kilometers per second"),
        (r"\bm/s\b", "meters per second"),
        (r"\bcm/year\b", "centimeters per year"),
        (r"°C\b", " degrees Celsius"),
        (r"°F\b", " degrees Fahrenheit"),
        (r"°\b", " degrees"),
        (r"\bkPa\b", "kilopascals"),
        (r"—", ", "),
        (r"&", "and"),
    ]
    for pat, rep in rules:
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)
    return s.strip()


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


# ==============================================================================
# 3. INTELLIGENCE & CONSEQUENCE ENGINE
# ==============================================================================
SCENARIOS: Dict[str, List[Dict[str, Any]]] = {
    "earth_stop": [
        {
            "stage": "01 // INITIAL EVENT",
            "clock": "T + 00:00:01",
            "mode": "earth_rotation",
            "headline": "Angular Momentum Halts at 1,670 km/h",
            "narration": "At the equator, Earth spins at 1,670 kilometers per hour. If the crust locked in one second, inertia would keep the atmosphere and oceans hurtling eastward at supersonic speed.",
            "telemetry": {"Crust Speed": "0 km/h", "Air Shear": "1,670 km/h", "Peak Zone": "Equator"},
            "hidden": "Only observers standing within a few kilometers of the North and South Poles would survive the initial deceleration.",
            "hyp": True,
        },
        {
            "stage": "02 // ATMOSPHERIC SHOCKWAVE",
            "clock": "T + 00:04:00",
            "mode": "dual_reality",
            "headline": "Super-Hurricane Winds Scour Continents",
            "narration": "Winds four times faster than the strongest recorded tornado strip landscapes clean, generating immense aerodynamic friction heat and continent-wide firestorms.",
            "telemetry": {"Wind Velocity": "> 450 m/s", "Pressure": "130 kPa", "Friction Heat": "Extreme"},
            "hidden": "Atmospheric friction rapidly superheats the lower troposphere and triggers massive electrostatic lightning.",
            "hyp": True,
        },
        {
            "stage": "03 // POLAR OCEAN SURGE",
            "clock": "T + 12:00:00",
            "mode": "consequence_map",
            "headline": "Mega-Tsunamis & Polar Ocean Migration",
            "narration": "Without centrifugal force bulging water around the equator, global oceans rush toward both poles, flooding northern and southern continents while exposing an equatorial supercontinent.",
            "telemetry": {"Equatorial Drop": "-21 km", "Ocean Basins": "2 Polar Seas", "Tsunami Scale": "Planetary"},
            "hidden": "Earth's equatorial radius is 21 kilometers wider than its poles; without spin, gravity pulls water directly poleward.",
            "hyp": True,
        },
        {
            "stage": "04 // SIX-MONTH DAY & NIGHT",
            "clock": "T + 180 DAYS",
            "mode": "dual_reality",
            "headline": "One Full Year Per Day-Night Cycle",
            "narration": "A single day now lasts an entire year as Earth orbits the Sun. One hemisphere bakes under six months of relentless daylight while the dark side plunges into deep freeze.",
            "telemetry": {"Solar Cycle": "365 Days", "Day Temp": "> 60°C", "Night Temp": "< -50°C"},
            "hidden": "Slowed liquid core convection progressively weakens Earth's protective magnetic field over time.",
            "hyp": True,
        },
        {
            "stage": "05 // SCIENTIFIC REALITY",
            "clock": "VERIFIED PHYSICS",
            "mode": "timeline_science",
            "headline": "Real Earth Slows by Just 1.8 ms Per Century",
            "narration": "In reality, no force can stop Earth instantaneously. Lunar tidal friction lengthens our day by just 1.8 milliseconds per century, keeping our planet safely spinning.",
            "telemetry": {"Real Slowdown": "1.8 ms / Century", "Energy Needed": "2.13e29 J", "Status": "Safe"},
            "hidden": "Halting Earth's spin would require absorbing more energy than the Sun outputs in six full days.",
            "hyp": False,
        },
    ],
    "moon_disappear": [
        {
            "stage": "01 // GRAVITATIONAL RELEASE",
            "clock": "T + 00:00:01.3",
            "mode": "solar_system",
            "headline": "Sudden Darkness & Tidal Release",
            "narration": "Because light takes 1.28 seconds to travel from the Moon to Earth, the night sky turns pitch black after 1.3 seconds as lunar gravity releases Earth's oceans.",
            "telemetry": {"Light Lag": "1.28 sec", "Night Brightness": "-95%", "Tidal Source": "Solar Only"},
            "hidden": "Gravitational waves propagate at light speed, relaxing Earth's tidal bulge the exact instant the Moon vanishes.",
            "hyp": True,
        },
        {
            "stage": "02 // TIDAL COLLAPSE",
            "clock": "T + 24 HOURS",
            "mode": "consequence_map",
            "headline": "Global Ocean Tides Shrink by 75%",
            "narration": "Without lunar gravity, ocean tides drop to one-quarter of their normal height, driven solely by the distant Sun. Intertidal ecosystems lose their daily nutrient cycle.",
            "telemetry": {"Tidal Drop": "-75%", "Solar Share": "25%", "Ocean Mixing": "Weakened"},
            "hidden": "Lunar tides drive deep-ocean thermal mixing that brings vital nutrients to surface phytoplankton.",
            "hyp": True,
        },
        {
            "stage": "03 // AXIAL INSTABILITY",
            "clock": "T + 100,000 YEARS",
            "mode": "dual_reality",
            "headline": "Chaotic Axial Tilt & Extreme Ice Ages",
            "narration": "Without the Moon's gravitational anchor, Earth's 23.5-degree axial tilt wobbles chaotically up to 45 degrees, triggering extreme climate swings and massive ice ages.",
            "telemetry": {"Normal Tilt": "23.5° Stable", "Chaos Tilt": "0° to 45°+", "Seasons": "Extreme"},
            "hidden": "Mars has no large stabilizing moon, which is why its axial tilt has swung wildly over millions of years.",
            "hyp": True,
        },
        {
            "stage": "04 // SCIENTIFIC REALITY",
            "clock": "VERIFIED PHYSICS",
            "mode": "timeline_science",
            "headline": "The Moon Recedes Safely at 3.8 cm Per Year",
            "narration": "Apollo lunar laser reflectors prove the Moon drifts away by just 3.8 centimeters annually—a gradual orbital dance that keeps Earth's climate stabilized for eons.",
            "telemetry": {"Drift Rate": "3.8 cm / yr", "Distance": "384,400 km", "Orbit": "Stabilized"},
            "hidden": "In roughly 600 million years, the Moon will appear too small to completely cover the Sun during an eclipse.",
            "hyp": False,
        },
    ],
    "asteroid_impact": [
        {
            "stage": "01 // TERMINAL APPROACH",
            "clock": "T - 30 DAYS",
            "mode": "asteroid_trajectory",
            "headline": "10-km Asteroid Locked on Earth Corridor",
            "narration": "Survey telescopes track a 10-kilometer carbonaceous asteroid accelerating under Earth's gravity toward an entry velocity of 20 kilometers per second.",
            "telemetry": {"Diameter": "10.0 km", "Velocity": "20 km/s", "Energy": "~100M Megatons"},
            "hidden": "At 20 kilometers per second, the asteroid crosses Earth's breathable atmosphere in under two seconds.",
            "hyp": True,
        },
        {
            "stage": "02 // HYPERVELOCITY IMPACT",
            "clock": "T + 00:00:02",
            "mode": "asteroid_impact",
            "headline": "Thermal Flash & 100-km Transient Crater",
            "narration": "Upon contact, kinetic energy converts instantaneously into intense thermal radiation and shockwaves, vaporizing rock and excavating a crater over 100 kilometers wide.",
            "telemetry": {"Peak Temp": "> 10,000 K", "Seismic": "Mag 11+", "Ejecta": "Sub-Orbital"},
            "hidden": "Superheated rock droplets launched into sub-orbital trajectories re-enter globally, heating the upper atmosphere.",
            "hyp": True,
        },
        {
            "stage": "03 // IMPACT WINTER",
            "clock": "T + 02 YEARS",
            "mode": "dual_reality",
            "headline": "Global Temperature Plunges by 18°C",
            "narration": "Stratospheric soot and sulfate aerosols block up to 90 percent of sunlight for years, halting photosynthesis and plunging continents into an extended impact winter.",
            "telemetry": {"Sunlight": "-90%", "Cooling": "-18°C Avg", "Photosynthesis": "Halted"},
            "hidden": "Sulfur aerosols vaporized from carbonate crust amplify global cooling far more than dust alone.",
            "hyp": True,
        },
        {
            "stage": "04 // PLANETARY DEFENSE",
            "clock": "VERIFIED PHYSICS",
            "mode": "timeline_science",
            "headline": "NASA DART Proved Kinetic Deflection Works",
            "narration": "In September 2022, NASA's DART spacecraft shifted the orbit of asteroid Dimorphos by 33 minutes—proving humanity now possesses real planetary defense capability.",
            "telemetry": {"DART Mission": "Sept 2022", "Orbit Shift": "-33 Min", "10km Threats": "Zero"},
            "hidden": "Over 95% of near-Earth asteroids larger than 1 kilometer are already mapped, with zero threat this century.",
            "hyp": False,
        },
    ],
}


def build_scenes_from_topic(topic: str, duration_sec: int) -> Tuple[str, List[Dict[str, Any]]]:
    clean = (topic or "").strip() or "What If Earth Suddenly Stopped Rotating?"
    low = clean.lower()

    if any(k in low for k in ["stop", "rotating", "rotation", "spin"]):
        raw = SCENARIOS["earth_stop"]
    elif any(k in low for k in ["moon", "lunar", "tide"]):
        raw = SCENARIOS["moon_disappear"]
    elif any(k in low for k in ["asteroid", "meteor", "comet", "impact"]):
        raw = SCENARIOS["asteroid_impact"]
    else:
        raw = [
            {
                "stage": "01 // BASELINE EQUILIBRIUM",
                "clock": "T + 00:00:00",
                "mode": "earth_rotation",
                "headline": f"Initial State: {clean[:36]}",
                "narration": f"Every natural system maintains balance until a boundary condition shifts. To examine {clean}, we first measure the fundamental forces holding reality steady.",
                "telemetry": {"System": "Equilibrium", "Driver": "Energy & Mass", "State": "Baseline"},
                "hidden": "Small shifts in initial boundary conditions often amplify non-linearly across coupled physical systems.",
                "hyp": True,
            },
            {
                "stage": "02 // IMMEDIATE REACTION",
                "clock": "T + 00:05:00",
                "mode": "dual_reality",
                "headline": "Primary Physical Shock & Energy Transfer",
                "narration": "Within the opening moments, conservation of energy and momentum govern how pressure, thermal energy, and motion redistribute across the environment.",
                "telemetry": {"Response": "Instant", "Comparison": "Dual Reality", "Stability": "Disrupted"},
                "hidden": "Secondary pressure waves frequently propagate faster through dense media than visible surface changes.",
                "hyp": True,
            },
            {
                "stage": "03 // GLOBAL CASCADE",
                "clock": "T + 24 HOURS",
                "mode": "consequence_map",
                "headline": "Atmospheric & Oceanic Cascade Effects",
                "narration": "Over the following hours, localized disruptions propagate through atmospheric circulation, ocean currents, and global planetary cycles.",
                "telemetry": {"Scale": "Global", "Feedback": "Active", "Equilibrium": "Shifting"},
                "hidden": "Interconnected climate and energy systems experience cascading latency across hemispheres.",
                "hyp": True,
            },
            {
                "stage": "04 // SCIENTIFIC REALITY",
                "clock": "VERIFIED PHYSICS",
                "mode": "timeline_science",
                "headline": "What Verified Science Confirms Today",
                "narration": "Separating hypothetical modeling from established physical laws demonstrates how finely balanced constants keep our planet habitable.",
                "telemetry": {"Basis": "First Principles", "Model": "Verified", "Status": "Complete"},
                "hidden": "Controlled scientific simulations allow researchers to test extreme scenarios safely.",
                "hyp": False,
            },
        ]

    dur_total = max(25, min(600, int(duration_sec)))
    n = len(raw)
    base_d = round(dur_total / float(n), 2)
    scenes: List[Dict[str, Any]] = []
    cur = 0.0
    for i, item in enumerate(raw):
        d = round(dur_total - cur, 2) if i == n - 1 else base_d
        scenes.append({
            **item,
            "index": i + 1,
            "start_sec": round(cur, 2),
            "duration_sec": max(5.0, d),
        })
        cur += d
    return clean, scenes


# ==============================================================================
# 4. 3D SHADED REALITY & CINEMATIC RENDERER
# ==============================================================================
class RealityCinematicRenderer:
    """Renders 3D ray-shaded planets, glowing atmospheres, and sleek glassmorphism HUD."""

    PALETTES = {
        "nexus_obsidian": {
            "bg_top": (10, 20, 46),
            "bg_bot": (3, 5, 13),
            "cyan": (34, 211, 238),
            "violet": (168, 85, 247),
            "amber": (251, 191, 36),
            "crimson": (244, 63, 94),
            "emerald": (16, 185, 129),
            "panel": (10, 17, 34),
            "text": (248, 250, 252),
            "muted": (148, 163, 184),
        },
        "deep_cosmos": {
            "bg_top": (20, 11, 48),
            "bg_bot": (2, 3, 10),
            "cyan": (56, 189, 248),
            "violet": (192, 132, 252),
            "amber": (250, 204, 21),
            "crimson": (251, 113, 133),
            "emerald": (52, 211, 153),
            "panel": (15, 13, 38),
            "text": (248, 250, 252),
            "muted": (148, 163, 184),
        },
    }

    def __init__(self, width: int, height: int, style_key: str = "nexus_obsidian"):
        self.w = width
        self.h = height
        self.is_vertical = height > width
        self.sc = min(width, height) / 540.0
        self.pal = self.PALETTES.get(style_key, self.PALETTES["nexus_obsidian"])

        rng = np.random.default_rng(2027)
        self.stars = np.column_stack([
            rng.uniform(0, width, 110),
            rng.uniform(0, height, 110),
            rng.uniform(0.7, 2.3, 110),
            rng.uniform(0.3, 1.0, 110),
        ])
        self._bg_cache = self._build_backdrop()
        self._sphere_cache: Dict[Tuple[int, bool], Image.Image] = {}

    def _build_backdrop(self) -> Image.Image:
        yy, xx = np.ogrid[0 : self.h, 0 : self.w]
        nx = (xx - self.w * 0.5) / (self.w * 0.68)
        ny = (yy - self.h * 0.40) / (self.h * 0.68)
        radial = np.clip(1.0 - 0.78 * (nx * nx + ny * ny), 0.0, 1.0).astype(np.float32)

        arr = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        for c in range(3):
            vals = self.pal["bg_bot"][c] + (self.pal["bg_top"][c] - self.pal["bg_bot"][c]) * radial
            arr[:, :, c] = np.clip(vals, 0, 255).astype(np.uint8)

        img = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(img)

        step = max(28, int(48 * self.sc))
        grid_col = (17, 27, 50)
        for gx in range(0, self.w, step):
            draw.line([(gx, 0), (gx, self.h)], fill=grid_col, width=1)
        for gy in range(0, self.h, step):
            draw.line([(0, gy), (self.w, gy)], fill=grid_col, width=1)
        return img

    def _get_3d_shaded_sphere(self, radius: int, perturbed: bool) -> Image.Image:
        """Generates a 3D ray-shaded sphere with realistic sunlight diffuse + specular + limb glow."""
        key = (radius, perturbed)
        if key in self._sphere_cache:
            return self._sphere_cache[key]

        pad = int(radius * 0.28)
        size = (radius + pad) * 2
        y, x = np.ogrid[-radius - pad : radius + pad, -radius - pad : radius + pad]
        r2 = (x * x + y * y).astype(np.float32)
        rad_f = float(radius)
        dist = np.sqrt(r2) / rad_f

        rgba = np.zeros((size, size, 4), dtype=np.uint8)

        # Atmospheric outer halo
        halo_mask = (dist >= 0.96) & (dist <= 1.24)
        halo_alpha = np.clip(1.0 - (dist[halo_mask] - 0.96) / 0.28, 0.0, 1.0) ** 2.2
        halo_col = (244, 63, 94) if perturbed else (34, 211, 238)
        for c in range(3):
            rgba[halo_mask, c] = halo_col[c]
        rgba[halo_mask, 3] = (halo_alpha * 165).astype(np.uint8)

        # 3D Sphere surface normals (nx, ny, nz)
        in_mask = dist <= 1.0
        nx = x[in_mask] / rad_f
        ny = y[in_mask] / rad_f
        nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))

        # Directional sunlight from top-left (-0.55, -0.45, 0.70)
        lx, ly, lz = -0.55, -0.45, 0.70
        ndotl = np.clip(nx * lx + ny * ly + nz * lz, 0.04, 1.0)
        fresnel = (1.0 - nz) ** 2.5

        base_col = np.array([145, 38, 62] if perturbed else [16, 98, 178], dtype=np.float32)
        rim_col = np.array([251, 146, 60] if perturbed else [56, 225, 255], dtype=np.float32)

        for c in range(3):
            lit = base_col[c] * (ndotl ** 0.85) + rim_col[c] * fresnel * 0.55
            rgba[in_mask, c] = np.clip(lit, 0, 255).astype(np.uint8)
        rgba[in_mask, 3] = 255

        img = Image.fromarray(rgba, mode="RGBA")
        self._sphere_cache[key] = img
        return img

    def _draw_panel(self, draw: ImageDraw.Draw, x0: int, y0: int, x1: int, y1: int, border: Tuple[int, int, int], glow: Tuple[int, int, int]):
        draw.rounded_rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], radius=14, outline=glow, width=1)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=12, fill=self.pal["panel"], outline=border, width=2)
        draw.rounded_rectangle([x0 + 10, y0 + 3, x1 - 10, y0 + 5], radius=2, fill=glow)

    def _draw_planet_3d(self, frame: Image.Image, draw: ImageDraw.Draw, cx: float, cy: float, radius: float, phase: float, perturbed: bool, label: str):
        r_int = max(24, int(radius))
        sphere = self._get_3d_shaded_sphere(r_int, perturbed)
        sw, sh = sphere.size
        frame.paste(sphere, (int(cx - sw // 2), int(cy - sh // 2)), sphere)

        # Rotating continental / cloud bands clipped inside illuminated hemisphere
        band_col = (234, 88, 12) if perturbed else (34, 197, 148)
        for i in range(4):
            ang = phase * (0.25 if perturbed else 1.7) + i * 1.4
            lx = cx - radius * 0.12 + math.sin(ang) * radius * 0.36
            ly = cy - radius * 0.08 + math.cos(i * 1.2) * radius * 0.36
            rw = radius * 0.26 * abs(math.cos(ang * 0.65))
            rh = radius * 0.14
            if rw > 2:
                draw.ellipse([lx - rw, ly - rh, lx + rw, ly + rh], fill=band_col)

        # Axial tilt vector
        tilt = math.radians(36.0 * math.sin(phase * 2.0) if perturbed else 23.5)
        ax_len = radius * 1.32
        dx, dy = math.sin(tilt) * ax_len, -math.cos(tilt) * ax_len
        draw.line([(cx - dx, cy - dy), (cx + dx, cy + dy)], fill=self.pal["amber"], width=2)

        if label:
            f_lbl = get_font(max(11, int(12 * self.sc)), bold=True)
            bb = draw.textbbox((0, 0), label, font=f_lbl)
            col = self.pal["crimson"] if perturbed else self.pal["cyan"]
            draw.text((cx - (bb[2] - bb[0]) / 2, cy + radius + 12 * self.sc), label, font=f_lbl, fill=col)

    def _draw_visual(self, frame: Image.Image, draw: ImageDraw.Draw, scene: Dict[str, Any], prog: float, global_t: float, box: Tuple[int, int, int, int]):
        x0, y0, x1, y1 = box
        vw, vh = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        mode = scene["mode"]
        zoom = 1.0 + 0.07 * ease_in_out(prog)

        for sx, sy, sr, spd in self.stars:
            px = x0 + ((sx + global_t * 14.0 * spd) % vw)
            py = y0 + ((sy + global_t * 4.0 * spd) % vh)
            r_px = max(1, int(sr * self.sc))
            draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=(160, 195, 245))

        if mode == "dual_reality":
            mid_x = int(cx)
            draw.line([(mid_x, y0 + 12), (mid_x, y1 - 12)], fill=self.pal["violet"], width=2)
            rp = min(vw * 0.16, vh * 0.27) * zoom
            self._draw_planet_3d(frame, draw, (x0 + mid_x) * 0.5, cy, rp, global_t * 1.4, False, "NORMAL REALITY")
            self._draw_planet_3d(frame, draw, (mid_x + x1) * 0.5, cy, rp, global_t * 0.3, True, "ALTERED SCENARIO")
        elif mode == "consequence_map":
            rg = min(vw, vh) * 0.28 * zoom
            self._draw_planet_3d(frame, draw, cx, cy, rg, global_t * 0.8, True, "GLOBAL CASCADE TELEMETRY")
            pulse_r = rg * (0.35 + (prog * 1.1) % 1.0)
            draw.ellipse([cx - pulse_r, cy - pulse_r, cx + pulse_r, cy + pulse_r], outline=self.pal["amber"], width=2)
        elif mode in ("asteroid_trajectory", "asteroid_impact"):
            re_r = min(vw, vh) * 0.23 * zoom
            ex, ey = x1 - vw * 0.28, cy + vh * 0.05
            self._draw_planet_3d(frame, draw, ex, ey, re_r, global_t, mode == "asteroid_impact", "EARTH")
            ax = x0 + vw * (0.14 + 0.55 * ease_in_out(prog))
            ay = y0 + vh * (0.20 + 0.33 * ease_in_out(prog))
            draw.line([(x0 + vw * 0.12, y0 + vh * 0.19), (ex, ey)], fill=self.pal["crimson"], width=2)
            ar = max(7, int(13 * self.sc))
            draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=self.pal["amber"], outline=(255, 255, 255), width=2)
        elif mode == "solar_system":
            sun_r = min(vw, vh) * 0.10 * zoom
            draw.ellipse([cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r], fill=self.pal["amber"])
            for orb_i, mult in enumerate([0.23, 0.35, 0.47], start=1):
                rx, ry = vw * mult, vh * mult * 0.60
                draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(48, 70, 112), width=1)
                ang = global_t * (1.8 / orb_i) + orb_i
                px, py = cx + math.cos(ang) * rx, cy + math.sin(ang) * ry
                pr = max(4, int((8 - orb_i) * self.sc))
                draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=self.pal["cyan"] if orb_i == 2 else self.pal["violet"])
        else:
            rp = min(vw, vh) * 0.29 * zoom
            self._draw_planet_3d(
                frame, draw, cx, cy, rp,
                global_t * (0.15 if scene["hyp"] else 1.8),
                scene["hyp"],
                "HYPOTHETICAL SIMULATION" if scene["hyp"] else "VERIFIED PHYSICAL MODEL",
            )

    def render_frame(self, scene: Dict[str, Any], idx: int, total: int, sc_prog: float, global_t: float, ov_prog: float) -> bytes:
        frame = self._bg_cache.copy()
        draw = ImageDraw.Draw(frame)
        pad = int(20 * self.sc)

        # Top HUD + Reality Clock
        top_h = int(42 * self.sc)
        self._draw_panel(draw, pad, pad, self.w - pad, pad + top_h, self.pal["cyan"], (14, 116, 144))

        f_brand = get_font(max(11, int(13 * self.sc)), bold=True)
        f_clock = get_font(max(11, int(14 * self.sc)), bold=True)

        tag_txt = "HYPOTHETICAL SIMULATION" if scene["hyp"] else "VERIFIED SCIENTIFIC DATA"
        tag_col = self.pal["amber"] if scene["hyp"] else self.pal["emerald"]
        draw.text((pad + 12, pad + int(13 * self.sc)), f"NEXUS // {tag_txt}", font=f_brand, fill=tag_col)

        clock_txt = f"REALITY CLOCK: {scene['clock']}"
        cb = draw.textbbox((0, 0), clock_txt, font=f_clock)
        draw.text((self.w - pad - (cb[2] - cb[0]) - 12, pad + int(12 * self.sc)), clock_txt, font=f_clock, fill=self.pal["cyan"])

        # Viewport + Telemetry Sidebar
        vp_top = pad + top_h + int(10 * self.sc)
        bot_h = int(126 * self.sc) if not self.is_vertical else int(185 * self.sc)
        vp_bot = self.h - pad - bot_h - int(10 * self.sc)

        if self.is_vertical:
            vp_box = (pad, vp_top, self.w - pad, vp_bot)
            self._draw_panel(draw, *vp_box, self.pal["violet"], (88, 28, 135))
            self._draw_visual(frame, draw, scene, sc_prog, global_t, vp_box)
        else:
            side_w = int(self.w * 0.27)
            vp_box = (pad, vp_top, self.w - pad - side_w - int(10 * self.sc), vp_bot)
            side_box = (self.w - pad - side_w, vp_top, self.w - pad, vp_bot)

            self._draw_panel(draw, *vp_box, self.pal["violet"], (88, 28, 135))
            self._draw_visual(frame, draw, scene, sc_prog, global_t, vp_box)

            self._draw_panel(draw, *side_box, self.pal["cyan"], (14, 116, 144))
            f_sh = get_font(max(10, int(12 * self.sc)), bold=True)
            f_sv = get_font(max(10, int(11 * self.sc)), bold=False)
            sy = side_box[1] + int(12 * self.sc)
            draw.text((side_box[0] + 12, sy), f"STAGE {idx + 1}/{total} TELEMETRY", font=f_sh, fill=self.pal["cyan"])
            sy += int(22 * self.sc)

            for k_lbl, v_lbl in scene.get("telemetry", {}).items():
                draw.text((side_box[0] + 12, sy), f"{k_lbl}:", font=f_sv, fill=self.pal["muted"])
                sy += int(14 * self.sc)
                draw.text((side_box[0] + 12, sy), str(v_lbl), font=f_sh, fill=self.pal["text"])
                sy += int(21 * self.sc)

            if sc_prog >= 0.40 and sy + 48 < side_box[3]:
                sy += int(4 * self.sc)
                draw.line([(side_box[0] + 10, sy), (side_box[2] - 10, sy)], fill=self.pal["amber"], width=1)
                sy += int(8 * self.sc)
                draw.text((side_box[0] + 12, sy), "HIDDEN CONSEQUENCE:", font=f_sh, fill=self.pal["amber"])
                sy += int(17 * self.sc)
                for h_ln in wrap_text(draw, scene.get("hidden", ""), f_sv, side_w - 24)[:5]:
                    if sy + 14 < side_box[3]:
                        draw.text((side_box[0] + 12, sy), h_ln, font=f_sv, fill=self.pal["text"])
                        sy += int(14 * self.sc)

        # Bottom Narration Deck
        deck_top = vp_bot + int(10 * self.sc)
        deck_bot = self.h - pad
        self._draw_panel(draw, pad, deck_top, self.w - pad, deck_bot, self.pal["cyan"], (14, 116, 144))

        f_head = get_font(max(13, int(17 * self.sc)), bold=True)
        f_nar = get_font(max(11, int(13 * self.sc)), bold=False)

        hy = deck_top + int(11 * self.sc)
        draw.text((pad + 14, hy), f"{scene['stage']} — {scene['headline']}", font=f_head, fill=self.pal["cyan"])

        ny = hy + int(25 * self.sc)
        for n_ln in wrap_text(draw, scene["narration"], f_nar, (self.w - 2 * pad) - 28)[:4]:
            draw.text((pad + 14, ny), n_ln, font=f_nar, fill=self.pal["text"])
            ny += int(18 * self.sc)

        bar_y = deck_bot - int(12 * self.sc)
        bar_w = (self.w - 2 * pad) - 28
        draw.rounded_rectangle([pad + 14, bar_y, pad + 14 + bar_w, bar_y + int(5 * self.sc)], radius=3, fill=(25, 36, 64))
        fill_w = max(6, int(bar_w * ov_prog))
        draw.rounded_rectangle([pad + 14, bar_y, pad + 14 + fill_w, bar_y + int(5 * self.sc)], radius=3, fill=self.pal["violet"])

        return frame.tobytes()


# ==============================================================================
# 5. ULTRA-REALISTIC MALE NEURAL VOICE ENGINE (DEDICATED ASYNC LOOP FIX)
# ==============================================================================
class VoiceEngine:
    VOICES = {
        "en-us": "en-US-AndrewNeural",
        "en-gb": "en-GB-RyanNeural",
        "en-au": "en-AU-WilliamNeural",
    }

    @staticmethod
    def build_ambient_bed(total_dur: float, out_wav: str):
        rate = 22050
        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            chunk = 20.0
            cur = 0.0
            while cur < total_dur:
                dur = min(chunk, total_dur - cur)
                n = int(round(dur * rate))
                if n <= 0:
                    break
                t = np.linspace(cur, cur + dur, n, endpoint=False, dtype=np.float32)
                drone = 0.50 * np.sin(2.0 * np.pi * 55.0 * t) + 0.32 * np.sin(2.0 * np.pi * 82.41 * t)
                sig = drone * 0.07
                wf.writeframes((np.clip(sig, -0.95, 0.95) * 32767.0).astype(np.int16).tobytes())
                cur += dur
                gc.collect()

    @staticmethod
    def _probe_dur(path: str) -> float:
        if not HAS_FFPROBE or not os.path.exists(path):
            return 0.0
        try:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, check=True,
            )
            return float(res.stdout.strip())
        except Exception:
            return 0.0

    @classmethod
    def synthesize_male_neural(cls, scenes: List[Dict[str, Any]], work_dir: str, voice_key: str = "en-us") -> Tuple[str, List[Dict[str, Any]], str]:
        """Dedicated event-loop synthesis so edge-tts works reliably inside Gunicorn threads."""
        if not HAS_EDGE_TTS:
            raise RuntimeError("edge-tts package is not installed. Please upload custom ElevenLabs audio instead.")

        voice = cls.VOICES.get(voice_key, "en-US-AndrewNeural")

        async def _synthesize_all():
            for idx, sc in enumerate(scenes):
                spoken = clean_speech_text(sc["narration"])
                mp3_path = os.path.join(work_dir, f"voice_{idx}.mp3")
                communicate = edge_tts.Communicate(spoken, voice=voice)
                await communicate.save(mp3_path)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_synthesize_all())
        finally:
            loop.close()

        synced_scenes: List[Dict[str, Any]] = []
        concat_wavs: List[str] = []
        cursor = 0.0

        for idx, sc in enumerate(scenes):
            mp3_p = os.path.join(work_dir, f"voice_{idx}.mp3")
            wav_p = os.path.join(work_dir, f"voice_sync_{idx}.wav")
            dur_s = cls._probe_dur(mp3_p)
            if dur_s <= 0.4:
                raise RuntimeError(f"Neural TTS returned empty audio for scene {idx + 1}. Upload ElevenLabs audio instead.")

            sc_dur = round(max(float(sc["duration_sec"]), dur_s + 0.75), 2)
            subprocess.run(
                ["ffmpeg", "-y", "-i", mp3_p, "-af", f"apad=whole_dur={sc_dur:.3f}", "-t", f"{sc_dur:.3f}", "-ar", "22050", "-ac", "1", wav_p],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            concat_wavs.append(wav_p)
            sc_copy = dict(sc)
            sc_copy["start_sec"] = round(cursor, 2)
            sc_copy["duration_sec"] = sc_dur
            synced_scenes.append(sc_copy)
            cursor += sc_dur

        list_txt = os.path.join(work_dir, "voice_list.txt")
        with open(list_txt, "w", encoding="utf-8") as fh:
            for w in concat_wavs:
                fh.write(f"file '{w}'\n")

        full_wav = os.path.join(work_dir, "master_voice.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt, "-c", "copy", full_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        return full_wav, synced_scenes, f"Ultra-Realistic Male Voice ({voice})"


# ==============================================================================
# 6. RENDER PIPELINE (AUTO-CLEANUP + CINEMATIC FILTERS)
# ==============================================================================
def update_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def run_render_job(job_id: str, payload: Dict[str, Any]):
    work_dir = tempfile.mkdtemp(prefix=f"nx_{job_id}_", dir=TMP_ROOT)
    try:
        purge_old_files(300)
        if not HAS_FFMPEG:
            raise RuntimeError("FFmpeg not available on server.")

        topic, scenes = build_scenes_from_topic(payload.get("topic", ""), int(payload.get("duration_sec", 45)))
        aspect = payload.get("aspect_ratio", "16:9")
        out_w, out_h = (540, 960) if aspect == "9:16" else (960, 540)
        fps = 16
        voice_mode = payload.get("voice_mode", "neural_male")
        uploaded_id = payload.get("uploaded_audio_id", "")

        update_job(job_id, status="running", progress=8, current_scene="Generating Ultra-Realistic Male Voiceover...")

        voice_wav: Optional[str] = None
        if voice_mode == "uploaded" and is_safe_id(uploaded_id):
            for fn in os.listdir(UPLOAD_DIR):
                if fn.startswith(uploaded_id):
                    voice_wav = os.path.join(UPLOAD_DIR, fn)
                    break
            if not voice_wav:
                raise FileNotFoundError("Uploaded ElevenLabs/custom audio not found. Please upload again.")
            audio_label = "Custom ElevenLabs / Uploaded Voiceover"
        else:
            voice_wav, scenes, audio_label = VoiceEngine.synthesize_male_neural(
                scenes, work_dir, voice_key=payload.get("voice_variant", "en-us")
            )

        total_dur = sum(float(s["duration_sec"]) for s in scenes)
        ambient_wav = os.path.join(work_dir, "ambient.wav")
        VoiceEngine.build_ambient_bed(total_dur, ambient_wav)

        master_wav = os.path.join(work_dir, "master_audio.wav")
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", voice_wav, "-i", ambient_wav,
                "-filter_complex", "[0:a]acompressor=threshold=-18dB:ratio=3,volume=1.2[v];[1:a]volume=0.45[m];[v][m]amix=inputs=2:duration=first[out]",
                "-map", "[out]", "-ar", "22050", "-ac", "1", master_wav,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        renderer = RealityCinematicRenderer(out_w, out_h, style_key=payload.get("visual_style", "nexus_obsidian"))
        dt = 1.0 / float(fps)
        global_t = 0.0
        total_sc = len(scenes)
        chunks: List[str] = []
        cinema_vf = "eq=contrast=1.06:brightness=0.01:saturation=1.18,unsharp=3:3:0.5:3:3:0.0,vignette=PI/6"

        for idx, sc in enumerate(scenes):
            sc_dur = float(sc["duration_sec"])
            n_frames = max(1, int(round(sc_dur * fps)))
            chunk_p = os.path.join(work_dir, f"c_{idx:02d}.mp4")

            update_job(
                job_id,
                progress=15 + int((idx / float(total_sc)) * 75),
                current_scene=f"Rendering Scene {idx + 1}/{total_sc}: {sc['headline']}",
            )

            proc = subprocess.Popen(
                [
                    "ffmpeg", "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
                    "-vf", cinema_vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-pix_fmt", "yuv420p", chunk_p,
                ],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            for f_i in range(n_frames):
                raw = renderer.render_frame(
                    sc, idx, total_sc, f_i / float(max(1, n_frames - 1)), global_t, min(1.0, global_t / max(1.0, total_dur))
                )
                proc.stdin.write(raw)
                global_t += dt
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f"FFmpeg failed on Scene {idx + 1}.")
            chunks.append(chunk_p)
            gc.collect()

        update_job(job_id, progress=93, current_scene="Muxing Final Cinematic MP4...")
        concat_txt = os.path.join(work_dir, "concat.txt")
        with open(concat_txt, "w", encoding="utf-8") as cf:
            for c in chunks:
                cf.write(f"file '{c}'\n")

        joined = os.path.join(work_dir, "joined.mp4")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", joined], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        slug = re.sub(r"[^a-zA-Z0-9]+", "_", topic)[:32].strip("_") or "NEXUS_REALITY"
        out_name = f"NEXUS_{slug}_{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)

        subprocess.run(
            ["ffmpeg", "-y", "-i", joined, "-i", master_wav, "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest", out_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )

        size_mb = round(os.path.getsize(out_path) / (1024.0 * 1024.0), 2)
        update_job(
            job_id,
            status="done",
            progress=100,
            current_scene="Completed & Auto-Downloading!",
            file_name=out_name,
            size_mb=size_mb,
            audio_note=audio_label,
        )

    except Exception as exc:
        update_job(job_id, status="error", error=str(exc), current_scene="Failed")
    finally:
        with JOBS_LOCK:
            ACTIVE_JOB_IDS.discard(job_id)
        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()


# ==============================================================================
# 7. STREAMLINED REST API
# ==============================================================================
@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "ffmpeg": HAS_FFMPEG,
        "neural_tts": HAS_EDGE_TTS,
    })


@app.route("/api/audio/upload", methods=["POST"])
def api_audio_upload():
    f = request.files.get("audio")
    if not f or not f.filename:
        return jsonify({"error": "Select an audio file first."}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".wav", ".mp3", ".m4a", ".ogg"):
        return jsonify({"error": "Only WAV, MP3, M4A, or OGG supported."}), 400
    aud_id = f"aud_{uuid.uuid4().hex[:12]}"
    f.save(os.path.join(UPLOAD_DIR, f"{aud_id}{ext}"))
    return jsonify({"ok": True, "audio_id": aud_id, "file_name": f.filename})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()
    if len(topic) < 3:
        return jsonify({"error": "Enter a topic (minimum 3 characters)."}), 400

    with JOBS_LOCK:
        if len(ACTIVE_JOB_IDS) >= 2:
            return jsonify({"error": "Server busy rendering another video. Wait a moment."}), 429
        job_id = f"nx_{uuid.uuid4().hex[:12]}"
        ACTIVE_JOB_IDS.add(job_id)
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 2,
            "current_scene": "Initializing NEXUS Engine...",
            "error": None,
        }

    threading.Thread(target=run_render_job, args=(job_id, data), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job expired or server restarted."}), 404
    return jsonify(job)


@app.route("/api/video/<job_id>")
def api_video(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or not job.get("file_name"):
        return jsonify({"error": "Video not ready."}), 404
    fname = os.path.basename(job["file_name"])
    target = os.path.join(OUTPUT_DIR, fname)
    if not os.path.exists(target):
        return jsonify({"error": "File already cleaned up from temporary storage."}), 404
    return send_file(target, as_attachment=(request.args.get("dl") == "1"), download_name=fname, mimetype="video/mp4")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
