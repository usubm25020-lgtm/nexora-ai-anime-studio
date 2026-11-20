import os
import re
import gc
import json
import math
import time
import uuid
import wave
import shutil
import asyncio
import tempfile
import subprocess
import threading
from datetime import datetime, timezone
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
# 1. APPLICATION CONFIGURATION & PATH SECURITY
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
TMP_ROOT = os.path.join(BASE_DIR, "tmp_work")

for directory in (OUTPUT_DIR, UPLOAD_DIR, TMP_ROOT):
    os.makedirs(directory, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_JOB_IDS = set()

SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,40}$")


def is_safe_id(val: str) -> bool:
    return bool(val and SAFE_ID_RE.match(val))


def check_binary(name: str) -> bool:
    return shutil.which(name) is not None


HAS_FFMPEG = check_binary("ffmpeg")
HAS_FFPROBE = check_binary("ffprobe")
ESPEAK_BIN = (
    "espeak-ng"
    if check_binary("espeak-ng")
    else ("espeak" if check_binary("espeak") else None)
)


# ==============================================================================
# 2. TYPOGRAPHY, SPEECH NORMALIZER & MATH HELPERS
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


def wrap_text(
    draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> List[str]:
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


def clean_text_for_speech(text: str) -> str:
    """Converts symbols and scientific units into natural spoken English."""
    s = str(text or "")
    replacements = [
        (r"\bkm/h\b", "kilometers per hour"),
        (r"\bkm/s\b", "kilometers per second"),
        (r"\bm/s\b", "meters per second"),
        (r"\bcm/year\b", "centimeters per year"),
        (r"°C\b", " degrees Celsius"),
        (r"°F\b", " degrees Fahrenheit"),
        (r"°\b", " degrees"),
        (r"\bkPa\b", "kilopascals"),
        (r"10\^29", "ten to the twenty-ninth power"),
        (r"10\^18", "ten to the eighteenth power"),
        (r"—", ", "),
        (r"&", "and"),
    ]
    for pat, rep in replacements:
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)
    return s.strip()


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


# ==============================================================================
# 3. INTELLIGENCE ENGINE & CONSEQUENCE ENGINE (MODULES A & C)
# ==============================================================================
KNOWLEDGE_SCENARIOS: Dict[str, Dict[str, Any]] = {
    "earth_stop": {
        "match": ["stop rotating", "stopped rotating", "earth stops", "rotation stop", "spin stop"],
        "category": "Planetary Physics",
        "title": "What If Earth Suddenly Stopped Rotating?",
        "hook": "At the equator, Earth's surface spins at roughly 1,670 kilometers per hour. If that motion halted in a single second, inertia would turn the atmosphere and oceans into a planetary shockwave.",
        "scientific_basis": "Newton's First Law of Motion (inertia), conservation of angular momentum, and geophysical fluid dynamics.",
        "stages": [
            {
                "stage": "01 // INITIAL EVENT",
                "clock": "T + 00:00:01",
                "visual_mode": "earth_rotation",
                "feature": "REALITY_CLOCK",
                "headline": "Angular Momentum Halts at 1,670 km/h",
                "narration": "Earth's solid crust locks abruptly, while the atmosphere and oceans keep moving eastward at up to 465 meters per second at the equator.",
                "telemetry": {"Equatorial Crust": "0 km/h", "Air Shear": "1,670 km/h", "Max Impact": "Equatorial Belt"},
                "hidden_consequence": "Only observers standing within a few kilometers of the North and South Poles would initially survive the surface deceleration.",
                "is_hypothetical": True,
            },
            {
                "stage": "02 // IMMEDIATE SHOCKWAVE",
                "clock": "T + 00:04:00",
                "visual_mode": "atmosphere_shock",
                "feature": "DUAL_REALITY",
                "headline": "Supersonic Winds Scour the Continents",
                "narration": "Super-hurricane winds four times faster than the strongest tornado strip landscapes, generating intense aerodynamic friction heat and global firestorms.",
                "telemetry": {"Wind Speed": "> 450 m/s", "Dynamic Pressure": "130 kPa", "Surface Shear": "Extreme"},
                "hidden_consequence": "Atmospheric friction rapidly heats the lower troposphere and triggers continent-wide electrostatic lightning storms.",
                "is_hypothetical": True,
            },
            {
                "stage": "03 // SHORT-TERM CASCADE",
                "clock": "T + 12:00:00",
                "visual_mode": "ocean_surge",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Mega-Tsunamis & Polar Ocean Migration",
                "narration": "Without centrifugal force bulging water around the equator, global oceans surge toward both poles, flooding northern and southern landmasses.",
                "telemetry": {"Equatorial Bulge": "-21 km Drop", "Ocean Basins": "2 Polar Oceans", "Sea Level Shift": "Kilometers"},
                "hidden_consequence": "Earth's equatorial radius is 21 kilometers wider than its polar radius; without spin, gravity pulls water straight to the poles.",
                "is_hypothetical": True,
            },
            {
                "stage": "04 // LONG-TERM SHIFT",
                "clock": "T + 180 DAYS",
                "visual_mode": "two_worlds",
                "feature": "TWO_WORLDS",
                "headline": "Six Months of Daylight, Six Months of Night",
                "narration": "A single day-night cycle now takes one full year. One hemisphere bakes under relentless sunlight while the dark side plunges into deep freeze.",
                "telemetry": {"Solar Day": "365 Earth Days", "Day Hemisphere": "> 60°C", "Night Hemisphere": "< -50°C"},
                "hidden_consequence": "Reduced liquid core convection progressively weakens Earth's protective magnetic shield over geological timescales.",
                "is_hypothetical": True,
            },
            {
                "stage": "05 // BIOSPHERE & REPLAY",
                "clock": "T + 05 YEARS",
                "visual_mode": "human_biosphere",
                "feature": "REALITY_REPLAY",
                "headline": "Life Retreats to Polar Twilight Zones",
                "narration": "Photosynthesis ceases across the frozen night hemisphere, collapsing food webs. Surviving human outposts cling to narrow twilight coastlines.",
                "telemetry": {"Habitable Zone": "Polar Twilight", "Agriculture": "Subsurface Only", "Radiation": "Elevated"},
                "hidden_consequence": "Replaying the first 60 seconds from orbital altitude shows the entire global cloud deck shearing off in a single eastward spiral.",
                "is_hypothetical": True,
            },
            {
                "stage": "06 // SCIENTIFIC REALITY",
                "clock": "VERIFIED REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "Real Earth Slows by Just 1.8 ms Per Century",
                "narration": "In reality, no force can stop Earth instantaneously. Lunar tidal friction lengthens our day by just 1.8 milliseconds per century—keeping our world safely spinning.",
                "telemetry": {"Actual Slowdown": "1.8 ms / Century", "Energy Needed": "2.13 x 10^29 J", "Status": "Scientifically Safe"},
                "hidden_consequence": "Halting Earth's spin would require absorbing more energy than the Sun radiates in six full days.",
                "is_hypothetical": False,
            },
        ],
    },
    "moon_disappear": {
        "match": ["moon disappear", "without the moon", "no moon", "moon vanished", "moon gone"],
        "category": "Orbital & Tidal Science",
        "title": "What If the Moon Suddenly Disappeared?",
        "hook": "The Moon is far more than a night light—it is Earth's gravitational anchor. If it vanished tonight, our skies would darken in 1.3 seconds and Earth's climate would destabilize.",
        "scientific_basis": "Gravitational tidal mechanics, axial obliquity stabilization, and marine circadian biology.",
        "stages": [
            {
                "stage": "01 // INITIAL EVENT",
                "clock": "T + 00:00:01.3",
                "visual_mode": "solar_system",
                "feature": "REALITY_CLOCK",
                "headline": "Gravitational Release & Sudden Darkness",
                "narration": "Light takes 1.28 seconds to reach Earth from the Moon. After 1.3 seconds, the night sky turns pitch black as lunar gravity releases Earth's oceans.",
                "telemetry": {"Light Delay": "1.28 Seconds", "Night Sky Lux": "-95% Drop", "Tidal Driver": "Solar Only"},
                "hidden_consequence": "Gravitational waves travel at light speed, meaning Earth's tidal bulge relaxes the exact instant the Moon vanishes from sight.",
                "is_hypothetical": True,
            },
            {
                "stage": "02 // TIDAL COLLAPSE",
                "clock": "T + 24 HOURS",
                "visual_mode": "ocean_surge",
                "feature": "DUAL_REALITY",
                "headline": "Ocean Tides Drop by 75% Worldwide",
                "narration": "Without lunar gravity, ocean tides shrink to one-quarter of their normal height, driven solely by the distant Sun. Coastal ecosystems lose their daily nutrient cycle.",
                "telemetry": {"Tidal Height": "-75% Reduction", "Solar Tide Share": "25% Remaining", "Coastal Mixing": "Weakened"},
                "hidden_consequence": "Lunar tides drive deep-ocean vertical mixing; weaker tides reduce nutrient upwelling for global phytoplankton.",
                "is_hypothetical": True,
            },
            {
                "stage": "03 // ECOLOGICAL IMPACT",
                "clock": "T + 01 YEAR",
                "visual_mode": "consequence_map",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Marine Spawning & Nocturnal Life Disrupted",
                "narration": "Thousands of marine species—including reef corals, sea turtles, and crabs—rely on lunar cycles to synchronize reproduction, triggering severe ecological decline.",
                "telemetry": {"Coral Synchrony": "Disrupted", "Predator/Prey": "Imbalanced", "Solar Eclipses": "Impossible"},
                "hidden_consequence": "Total solar eclipses only exist because the Moon is 400 times smaller than the Sun yet 400 times closer to Earth.",
                "is_hypothetical": True,
            },
            {
                "stage": "04 // AXIAL CHAOS",
                "clock": "T + 100,000 YEARS",
                "visual_mode": "two_worlds",
                "feature": "TWO_WORLDS",
                "headline": "Chaotic Axial Tilt & Extreme Ice Ages",
                "narration": "Without the Moon's stabilizing pull, Earth's 23.5-degree tilt wobbles chaotically up to 45 degrees, triggering extreme climate swings and massive ice ages.",
                "telemetry": {"Normal Tilt": "23.5° Stable", "Unanchored Tilt": "0° to 45°+ Chaos", "Seasons": "Unpredictable"},
                "hidden_consequence": "Mars has no large stabilizing moon, which is why its axial tilt has swung wildly over millions of years.",
                "is_hypothetical": True,
            },
            {
                "stage": "05 // CIVILIZATION REPLAY",
                "clock": "T + 500,000 YEARS",
                "visual_mode": "human_biosphere",
                "feature": "REALITY_REPLAY",
                "headline": "Humanity Adapts to Shifting Climate Belts",
                "narration": "Replaying Earth's rotational pole from orbit shows climate zones migrating across latitudes, forcing agriculture and coastal cities to relocate repeatedly.",
                "telemetry": {"Ice Sheets": "Rapid Swings", "Sea Level": "±50m Shift", "Astronomy": "Crystal Clear"},
                "hidden_consequence": "Paradoxically, ground-based optical astronomy would improve dramatically without lunar glare in the night sky.",
                "is_hypothetical": True,
            },
            {
                "stage": "06 // SCIENTIFIC REALITY",
                "clock": "VERIFIED REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "The Moon Recedes Safely at 3.8 cm Per Year",
                "narration": "Apollo laser reflectors prove the Moon drifts away by just 3.8 centimeters annually—a gradual dance that keeps Earth's climate stabilized for eons.",
                "telemetry": {"Recession Rate": "3.8 cm / Year", "Mean Distance": "384,400 km", "Orbit Status": "Stabilized"},
                "hidden_consequence": "In roughly 600 million years, the Moon will appear too small to completely cover the Sun during a total eclipse.",
                "is_hypothetical": False,
            },
        ],
    },
    "sun_disappear": {
        "match": ["sun disappear", "without the sun", "no sun", "sun vanished", "sun goes out"],
        "category": "Astrophysics & Thermodynamics",
        "title": "What If the Sun Suddenly Disappeared?",
        "hook": "The Sun provides 99.98% of the energy warming Earth's surface and holds our planet in orbit. If it vanished instantaneously, we wouldn't even know for eight minutes and twenty seconds.",
        "scientific_basis": "Speed of light and gravity, radiative cooling laws, and geothermal heat flux.",
        "stages": [
            {
                "stage": "01 // INITIAL EVENT",
                "clock": "T + 00:08:20",
                "visual_mode": "solar_system",
                "feature": "REALITY_CLOCK",
                "headline": "Darkness Arrives & Earth Flies Into Deep Space",
                "narration": "At 8 minutes and 20 seconds, the last solar photons arrive and solar gravity releases—sending Earth hurtling straight into interstellar space at 30 kilometers per second.",
                "telemetry": {"Light & Gravity Lag": "8 min 20 sec", "Escape Speed": "29.78 km/s", "Sky Light": "Starlight Only"},
                "hidden_consequence": "General relativity confirms gravitational changes travel at the exact speed of light, not instantaneously.",
                "is_hypothetical": True,
            },
            {
                "stage": "02 // RAPID FREEZE",
                "clock": "T + 07 DAYS",
                "visual_mode": "two_worlds",
                "feature": "DUAL_REALITY",
                "headline": "Photosynthesis Halts & Temps Plunge to -17°C",
                "narration": "Without sunlight, small plants perish within days and Earth's average surface temperature drops from 15°C to below minus 17 degrees Celsius within one week.",
                "telemetry": {"Week 1 Temp": "-17°C (1°F)", "Photosynthesis": "0% Active", "Moon Visibility": "Invisible"},
                "hidden_consequence": "The Moon would also disappear from view because it shines only by reflecting sunlight.",
                "is_hypothetical": True,
            },
            {
                "stage": "03 // OCEAN ICE SHELL",
                "clock": "T + 01 YEAR",
                "visual_mode": "consequence_map",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Surface Oceans Freeze While Deep Seas Stay Liquid",
                "narration": "Within a year, surface temperatures fall below minus 70 degrees Celsius, encasing oceans in thick ice—while Earth's core keeps deep abyssal waters liquid.",
                "telemetry": {"Year 1 Temp": "< -70°C", "Ice Crust": "Insulates Deep Sea", "Forests": "Frozen"},
                "hidden_consequence": "Floating sea ice acts as an insulating blanket that keeps the deep ocean liquid for billions of years.",
                "is_hypothetical": True,
            },
            {
                "stage": "04 // ATMOSPHERIC SNOW",
                "clock": "T + 10 YEARS",
                "visual_mode": "atmosphere_shock",
                "feature": "TWO_WORLDS",
                "headline": "Atmosphere Liquefies & Snows Nitrogen",
                "narration": "Approaching minus 200 degrees Celsius over a decade, atmospheric oxygen and nitrogen condense into rain and freeze into snow across the planetary ice shell.",
                "telemetry": {"Surface Temp": "~ -200°C", "Atmosphere": "Frozen Snow", "Drift Distance": "~63 AU"},
                "hidden_consequence": "After 10 years at 30 kilometers per second, Earth would already have drifted past the orbit of Pluto.",
                "is_hypothetical": True,
            },
            {
                "stage": "05 // SUBSURFACE SURVIVAL",
                "clock": "T + 50 YEARS",
                "visual_mode": "human_biosphere",
                "feature": "REALITY_REPLAY",
                "headline": "Geothermal Refuges & Hydrothermal Vent Life",
                "narration": "Replaying Earth's thermal profile shows deep-sea hydrothermal vent ecosystems thriving unbothered, while humans could survive only in geothermal underground habitats.",
                "telemetry": {"Vent Ecosystems": "100% Intact", "Human Power": "Geothermal/Nuclear", "Oxygen": "Electrolysis"},
                "hidden_consequence": "Deep-ocean vent organisms rely on chemosynthesis rather than sunlight, making them completely independent of the Sun.",
                "is_hypothetical": True,
            },
            {
                "stage": "06 // SCIENTIFIC REALITY",
                "clock": "VERIFIED REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "The Sun Has 5 Billion Years of Hydrogen Fuel Left",
                "narration": "Conservation of mass-energy makes sudden disappearance impossible. Our Sun is a stable main-sequence star with roughly 5 billion years of hydrogen fusion remaining.",
                "telemetry": {"Solar Age": "4.6 Billion Yrs", "Remaining Fuel": "~5 Billion Yrs", "Status": "Main-Sequence Stable"},
                "hidden_consequence": "Every second, the Sun fuses 600 million tons of hydrogen into helium, delivering steady warmth to Earth.",
                "is_hypothetical": False,
            },
        ],
    },
    "asteroid_impact": {
        "match": ["asteroid", "meteor", "chicxulub", "impact", "comet"],
        "category": "Planetary Defense & Astrophysics",
        "title": "What If a 10-Kilometer Asteroid Approached Earth?",
        "hook": "Sixty-six million years ago, a mountain-sized asteroid traveling at 20 kilometers per second reshaped life on Earth. Here is the exact physics timeline if a similar object were detected today.",
        "scientific_basis": "Kinetic energy mechanics, hypervelocity cratering, and NASA DART planetary defense.",
        "stages": [
            {
                "stage": "01 // DETECTION & APPROACH",
                "clock": "T - 30 DAYS",
                "visual_mode": "asteroid_trajectory",
                "feature": "REALITY_CLOCK",
                "headline": "10-km Object Locked on Earth Intersection Corridor",
                "narration": "Deep-sky survey telescopes track a 10-kilometer asteroid accelerating under Earth's gravity toward an entry velocity of 20 kilometers per second.",
                "telemetry": {"Diameter": "10.0 km", "Entry Velocity": "20 km/s", "Kinetic Energy": "~100 Million MT"},
                "hidden_consequence": "At 20 kilometers per second, the asteroid crosses Earth's entire breathable atmosphere in less than two seconds.",
                "is_hypothetical": True,
            },
            {
                "stage": "02 // HYPERVELOCITY IMPACT",
                "clock": "T + 00:00:02",
                "visual_mode": "asteroid_impact",
                "feature": "DUAL_REALITY",
                "headline": "Thermal Flash & 100-km Transient Crater",
                "narration": "Upon impact, kinetic energy converts instantaneously into intense heat and shockwaves, vaporizing rock and excavating a crater over 100 kilometers wide.",
                "telemetry": {"Peak Temp": "> 10,000 K", "Seismic Shock": "Magnitude 11+", "Ejecta Plume": "Sub-Orbital"},
                "hidden_consequence": "Superheated rock droplets launched into sub-orbital arcs re-enter globally, heating the upper atmosphere intensely.",
                "is_hypothetical": True,
            },
            {
                "stage": "03 // GLOBAL CASCADE",
                "clock": "T + 48 HOURS",
                "visual_mode": "consequence_map",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Megatsunamis & Continental Wildfire Fronts",
                "narration": "An ocean impact radiates kilometer-high waves outward while incandescent ejecta ignites wildfires across continental interiors within hours.",
                "telemetry": {"Tsunami Height": "100m - 1,000m", "Soot Injection": "15,000 Tg", "Sky Light": "Twilight at Noon"},
                "hidden_consequence": "Sulfur aerosols vaporized from carbonate-rich crust amplify global cooling far more than dust alone.",
                "is_hypothetical": True,
            },
            {
                "stage": "04 // IMPACT WINTER",
                "clock": "T + 02 YEARS",
                "visual_mode": "two_worlds",
                "feature": "TWO_WORLDS",
                "headline": "Global Temperature Plunges by 15°C to 20°C",
                "narration": "Stratospheric soot and sulfate aerosols block up to 90 percent of sunlight for years, halting photosynthesis and triggering a prolonged impact winter.",
                "telemetry": {"Sunlight Drop": "-85% to -90%", "Global Cooling": "-18°C Average", "Agriculture": "Halted"},
                "hidden_consequence": "Nitric and sulfuric acid rain rapidly acidifies surface ocean waters, stressing marine shell-forming organisms.",
                "is_hypothetical": True,
            },
            {
                "stage": "05 // SURVIVAL & REPLAY",
                "clock": "T + 10 YEARS",
                "visual_mode": "human_biosphere",
                "feature": "REALITY_REPLAY",
                "headline": "Subterranean Refuges & Early Warning Value",
                "narration": "Replaying the aerosol spread shows why early detection decades ahead is vital: hardened underground reserves and non-solar food systems would be essential.",
                "telemetry": {"Recovery Span": "10+ Years", "Critical Factor": "Early Warning", "Deflection": "Decades Prior"},
                "hidden_consequence": "Small burrowing mammals survived the Cretaceous impact largely because soil insulated them from the initial heat pulse.",
                "is_hypothetical": True,
            },
            {
                "stage": "06 // SCIENTIFIC REALITY",
                "clock": "VERIFIED REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "NASA DART Proved We Can Deflect Asteroids",
                "narration": "In September 2022, NASA's DART spacecraft altered the orbit of asteroid Dimorphos by 33 minutes—proving humanity possesses real kinetic deflection capability.",
                "telemetry": {"DART Mission": "Sept 26, 2022", "Orbit Shift": "-33 Minutes", "10km Threats": "Zero Tracked"},
                "hidden_consequence": "Over 95% of near-Earth asteroids larger than 1 kilometer are already mapped, and none pose a threat this century.",
                "is_hypothetical": False,
            },
        ],
    },
}


class IntelligenceEngine:
    @staticmethod
    def generate_script(
        topic: str,
        category: str = "Auto-Detect",
        duration_sec: int = 60,
        user_sources: str = "",
    ) -> Dict[str, Any]:
        clean_topic = (topic or "").strip() or "What If Earth Suddenly Stopped Rotating?"
        low = clean_topic.lower()

        matched_key: Optional[str] = None
        for key, data in KNOWLEDGE_SCENARIOS.items():
            if any(kw in low for kw in data["match"]):
                matched_key = key
                break

        if matched_key:
            bp = KNOWLEDGE_SCENARIOS[matched_key]
            resolved_cat = bp["category"] if category == "Auto-Detect" else category
            raw_stages = bp["stages"]
            hook_text = bp["hook"]
            sci_basis = bp["scientific_basis"]
            source_label = "Built-in NEXUS Physics & Earth Science Knowledge Base"
        else:
            resolved_cat = "Reality & Systems Science" if category == "Auto-Detect" else category
            hook_text = (
                f"What actually happens when we analyze '{clean_topic}' using conservation laws, "
                f"systems dynamics, and measurable physical cause and effect?"
            )
            sci_basis = "First-principles physics, thermodynamics, and comparative systems modeling."
            source_label = "Built-in NEXUS First-Principles Reasoning Template"
            raw_stages = [
                {
                    "stage": "01 // INITIAL EVENT",
                    "clock": "T + 00:00:00",
                    "visual_mode": "solar_system",
                    "feature": "REALITY_CLOCK",
                    "headline": f"Baseline Equilibrium: {clean_topic[:38]}",
                    "narration": f"Every natural system maintains balance until a boundary condition shifts. To examine {clean_topic}, we first measure the baseline forces holding reality steady.",
                    "telemetry": {"System State": "Equilibrium", "Primary Driver": "Energy & Mass", "Observation": "Baseline"},
                    "hidden_consequence": "Small shifts in initial boundary conditions often amplify non-linearly across coupled physical systems.",
                    "is_hypothetical": True,
                },
                {
                    "stage": "02 // IMMEDIATE REACTION",
                    "clock": "T + 00:05:00",
                    "visual_mode": "atmosphere_shock",
                    "feature": "DUAL_REALITY",
                    "headline": "Primary Physical Reaction & Energy Transfer",
                    "narration": "Within the opening moments, conservation of energy and momentum govern how pressure, heat, and motion redistribute across the environment.",
                    "telemetry": {"Response Speed": "Instantaneous", "Energy State": "Disrupted", "Comparison": "Dual View"},
                    "hidden_consequence": "Secondary pressure waves frequently propagate faster through dense media than visible surface changes.",
                    "is_hypothetical": True,
                },
                {
                    "stage": "03 // SHORT-TERM CASCADE",
                    "clock": "T + 24 HOURS",
                    "visual_mode": "consequence_map",
                    "feature": "CONSEQUENCE_MAP",
                    "headline": "Regional & Global Cascade Propagation",
                    "narration": "Over the following hours, localized disruptions propagate through atmospheric circulation, ocean currents, and global infrastructure networks.",
                    "telemetry": {"Propagation": "Global Scale", "Feedback Loop": "Active", "Stability": "Low"},
                    "hidden_consequence": "Interconnected power and communication grids experience cascading latency before physical waves arrive.",
                    "is_hypothetical": True,
                },
                {
                    "stage": "04 // LONG-TERM SHIFT",
                    "clock": "T + 10 YEARS",
                    "visual_mode": "two_worlds",
                    "feature": "TWO_WORLDS",
                    "headline": "Altered Equilibrium vs. Normal Earth",
                    "narration": "Comparing normal reality side by side with the altered scenario reveals how planetary cycles settle into a dramatically different long-term state.",
                    "telemetry": {"Normal World": "Balanced", "Altered World": "Reconfigured", "Timescale": "Decadal"},
                    "hidden_consequence": "Long-term stability depends on whether chemical and thermal cycles can re-establish closed feedback loops.",
                    "is_hypothetical": True,
                },
                {
                    "stage": "05 // BIOSPHERE & REPLAY",
                    "clock": "T + 50 YEARS",
                    "visual_mode": "human_biosphere",
                    "feature": "REALITY_REPLAY",
                    "headline": "Biosphere Resilience & Engineering Adaptation",
                    "narration": "Replaying the critical transition reveals how biological organisms and human engineering must adapt to survive resource and climate shifts.",
                    "telemetry": {"Biosphere": "High Stress", "Human Tech": "Adaptation Mode", "Perspective": "Orbital Replay"},
                    "hidden_consequence": "Microbial and subterranean ecosystems consistently demonstrate the highest resilience to surface disruptions.",
                    "is_hypothetical": True,
                },
                {
                    "stage": "06 // SCIENTIFIC REALITY",
                    "clock": "VERIFIED REALITY",
                    "visual_mode": "timeline_science",
                    "feature": "HIDDEN_DETECTOR",
                    "headline": "What Verified Science Confirms Today",
                    "narration": "Separating hypothetical modeling from established physical laws shows how finely balanced constants keep our environment habitable.",
                    "telemetry": {"Verification": "First Principles", "Speculation": "Clearly Labeled", "Status": "Verified"},
                    "hidden_consequence": "Controlled scientific simulations allow researchers to test extreme scenarios safely without real-world risk.",
                    "is_hypothetical": False,
                },
            ]

        duration_sec = max(30, min(600, int(duration_sec)))
        n_scenes = len(raw_stages)
        base_dur = round(duration_sec / float(n_scenes), 2)

        scenes: List[Dict[str, Any]] = []
        cursor = 0.0
        for idx, st in enumerate(raw_stages):
            sc_dur = round(duration_sec - cursor, 2) if idx == n_scenes - 1 else base_dur
            scenes.append({
                "index": idx + 1,
                "stage": st["stage"],
                "clock": st["clock"],
                "visual_mode": st["visual_mode"],
                "feature": st["feature"],
                "headline": st["headline"],
                "narration": st["narration"],
                "telemetry": st["telemetry"],
                "hidden_consequence": st["hidden_consequence"],
                "is_hypothetical": st["is_hypothetical"],
                "start_sec": round(cursor, 2),
                "duration_sec": max(5.0, sc_dur),
            })
            cursor += sc_dur

        sources = [source_label]
        if user_sources and user_sources.strip():
            for line in user_sources.strip().splitlines():
                if line.strip():
                    sources.append(f"User Reference: {line.strip()}")
        else:
            sources.append("No external live URLs fetched; add source URLs or reference text in Script Studio.")

        growth = YouTubeGrowthEngine.generate_metadata(clean_topic, resolved_cat, hook_text, scenes)

        return {
            "topic": clean_topic,
            "category": resolved_cat,
            "hook": hook_text,
            "scientific_basis": sci_basis,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "disclaimer": "Hypothetical visualizations are explicitly tagged [HYPOTHETICAL SIMULATION] and separated from verified physical measurements.",
            "sources": sources,
            "scenes": scenes,
            "growth": growth,
        }


# ==============================================================================
# 4. YOUTUBE GROWTH ENGINE (MODULE H)
# ==============================================================================
class YouTubeGrowthEngine:
    @staticmethod
    def generate_metadata(
        topic: str, category: str, hook: str, scenes: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        clean = topic.rstrip("?.!")
        titles = [
            f"{clean}? Here Is What Physics Actually Says",
            f"The Real Science Behind: {clean}",
            f"{clean} — Second-by-Second Reality Simulation",
            f"What Nobody Tells You About: {clean}",
            f"{clean}: Normal Earth vs. Altered Reality",
        ]

        chapters = []
        for sc in scenes:
            m = int(sc["start_sec"] // 60)
            s = int(sc["start_sec"] % 60)
            chapters.append(f"{m:02d}:{s:02d} — {sc['stage']}: {sc['headline']}")

        description = (
            f"{hook}\n\n"
            f"In this NEXUS Reality Explainer ({category}), we break down the physical timeline, "
            f"environmental cascades, and verified science behind: {topic}.\n\n"
            f"CHAPTERS:\n" + "\n".join(chapters) + "\n\n"
            f"SCIENTIFIC NOTE:\n"
            f"Hypothetical segments illustrate theoretical physics models; final conclusions reflect established scientific measurements."
        )

        keywords = [
            clean.lower(),
            f"{clean.lower()} explained",
            "what if science",
            "reality simulation",
            "astrophysics explainer",
            "earth science documentary",
            "physics visualization",
            "nexus reality",
        ]
        hashtags = ["#ScienceExplained", "#WhatIf", "#Physics", "#SpaceScience", "#NEXUSReality"]

        return {
            "titles": titles,
            "hook": hook,
            "description": description,
            "keywords": keywords,
            "hashtags": hashtags,
            "chapters": chapters,
            "thumbnail_concept": (
                f"High-contrast orbital split-screen: Left hemisphere shows sunlit Earth ('NORMAL REALITY'), "
                f"right hemisphere shows physical simulation for '{clean}' with glowing cyan Reality Clock."
            ),
            "thumbnail_texts": [
                "WHAT HAPPENS NEXT?",
                "REALITY vs SIMULATION",
                "SECOND BY SECOND",
            ],
        }


# ==============================================================================
# 5. MODERN REALITY & CINEMATIC RENDERER (MODULES B & D)
# ==============================================================================
class RealityCinematicRenderer:
    """High-speed, ultra-modern glassmorphism & procedural scientific simulation engine."""

    PALETTES = {
        "nexus_obsidian": {
            "bg_top": (10, 19, 42),
            "bg_bot": (3, 5, 13),
            "cyan": (34, 211, 238),
            "violet": (168, 85, 247),
            "amber": (251, 191, 36),
            "crimson": (244, 63, 94),
            "emerald": (16, 185, 129),
            "panel": (11, 18, 36),
            "text": (248, 250, 252),
            "muted": (148, 163, 184),
        },
        "deep_cosmos": {
            "bg_top": (18, 11, 44),
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
        "solar_telemetry": {
            "bg_top": (28, 16, 34),
            "bg_bot": (4, 6, 14),
            "cyan": (34, 211, 238),
            "violet": (139, 92, 246),
            "amber": (245, 158, 11),
            "crimson": (239, 68, 68),
            "emerald": (16, 185, 129),
            "panel": (18, 16, 32),
            "text": (248, 250, 252),
            "muted": (156, 163, 175),
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
            rng.uniform(0, width, 95),
            rng.uniform(0, height, 95),
            rng.uniform(0.7, 2.2, 95),
            rng.uniform(0.4, 1.0, 95),
        ])
        self._bg_cache = self._build_base_backdrop()

    def _build_base_backdrop(self) -> Image.Image:
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
        grid_col = (18, 28, 52)
        for gx in range(0, self.w, step):
            draw.line([(gx, 0), (gx, self.h)], fill=grid_col, width=1)
        for gy in range(0, self.h, step):
            draw.line([(0, gy), (self.w, gy)], fill=grid_col, width=1)

        # Sleek cinema edge accent bars
        draw.line([(4, int(self.h * 0.12)), (4, int(self.h * 0.88))], fill=self.pal["cyan"], width=2)
        draw.line([(self.w - 4, int(self.h * 0.12)), (self.w - 4, int(self.h * 0.88))], fill=self.pal["violet"], width=2)
        return img

    def _draw_modern_panel(
        self,
        draw: ImageDraw.Draw,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        border_col: Tuple[int, int, int],
        glow_col: Tuple[int, int, int],
    ):
        # Outer subtle aura + glass panel + top specular highlight + tech corner brackets
        draw.rounded_rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], radius=14, outline=glow_col, width=1)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=12, fill=self.pal["panel"], outline=border_col, width=2)
        draw.rounded_rectangle([x0 + 10, y0 + 3, x1 - 10, y0 + 6], radius=2, fill=glow_col)

        bl = max(8, int(12 * self.sc))
        for cx, cy, dx, dy in [
            (x0 - 3, y0 - 3, 1, 1),
            (x1 + 3, y0 - 3, -1, 1),
            (x0 - 3, y1 + 3, 1, -1),
            (x1 + 3, y1 + 3, -1, -1),
        ]:
            draw.line([(cx, cy), (cx + dx * bl, cy)], fill=(255, 255, 255), width=2)
            draw.line([(cx, cy), (cx, cy + dy * bl)], fill=(255, 255, 255), width=2)

    def _draw_planet(
        self,
        draw: ImageDraw.Draw,
        cx: float,
        cy: float,
        radius: float,
        phase: float,
        perturbed: bool = False,
        label: str = "",
    ):
        halo_col = self.pal["crimson"] if perturbed else self.pal["cyan"]
        for ring in range(3, 0, -1):
            rr = radius + ring * 4 * self.sc
            draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=halo_col, width=1)

        ocean_col = (52, 20, 46) if perturbed else (14, 68, 128)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=ocean_col, outline=halo_col, width=2)

        # Specular sunlit core highlight
        hi_r = radius * 0.65
        hi_col = (85, 32, 68) if perturbed else (26, 98, 168)
        draw.ellipse(
            [cx - radius * 0.72, cy - radius * 0.72, cx - radius * 0.72 + hi_r * 1.3, cy - radius * 0.72 + hi_r * 1.3],
            fill=hi_col,
        )

        land_col = (195, 88, 14) if perturbed else (20, 158, 122)
        for i in range(5):
            ang = phase * (0.2 if perturbed else 1.8) + i * 1.25
            lx = cx + math.sin(ang) * radius * 0.46
            ly = cy + math.cos(i * 1.1) * radius * 0.42
            rw = radius * (0.32 - 0.03 * i) * abs(math.cos(ang * 0.7))
            rh = radius * 0.20
            if rw > 2:
                draw.ellipse([lx - rw, ly - rh, lx + rw, ly + rh], fill=land_col)

        draw.pieslice(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            start=285,
            end=75,
            fill=(5, 9, 20),
        )
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=halo_col, width=2)

        tilt = math.radians(38.0 * math.sin(phase * 2.0) if perturbed else 23.5)
        ax_len = radius * 1.35
        dx = math.sin(tilt) * ax_len
        dy = -math.cos(tilt) * ax_len
        draw.line([(cx - dx, cy - dy), (cx + dx, cy + dy)], fill=self.pal["amber"], width=2)

        if label:
            f_lbl = get_font(max(11, int(12 * self.sc)), bold=True)
            bb = draw.textbbox((0, 0), label, font=f_lbl)
            draw.text((cx - (bb[2] - bb[0]) / 2, cy + radius + 9 * self.sc), label, font=f_lbl, fill=halo_col)

    def _draw_reality_visual(
        self,
        draw: ImageDraw.Draw,
        scene: Dict[str, Any],
        prog: float,
        global_t: float,
        box: Tuple[int, int, int, int],
    ):
        x0, y0, x1, y1 = box
        vw = x1 - x0
        vh = y1 - y0
        cx = (x0 + x1) * 0.5
        cy = (y0 + y1) * 0.5
        mode = scene["visual_mode"]
        feature = scene["feature"]

        # Smooth camera zoom factor across scene duration
        cam_zoom = 1.0 + 0.08 * ease_in_out(prog)

        for sx, sy, sr, spd in self.stars:
            px = x0 + ((sx + global_t * 14.0 * spd) % vw)
            py = y0 + ((sy + global_t * 4.0 * spd) % vh)
            r_px = max(1, int(sr * self.sc))
            draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=(145, 185, 235))

        if feature in ("DUAL_REALITY", "TWO_WORLDS") or mode in ("atmosphere_shock", "two_worlds"):
            mid_x = int(cx)
            draw.line([(mid_x, y0 + 12), (mid_x, y1 - 12)], fill=self.pal["violet"], width=2)
            r_p = min(vw * 0.16, vh * 0.27) * cam_zoom
            self._draw_planet(draw, (x0 + mid_x) * 0.5, cy, r_p, global_t * 1.4, perturbed=False, label="NORMAL REALITY")
            self._draw_planet(draw, (mid_x + x1) * 0.5, cy, r_p, global_t * 0.3, perturbed=True, label="ALTERED SCENARIO")

            for k in range(6):
                wy = cy - r_p + k * (2 * r_p / 5.0)
                wx_start = mid_x + 14 + ((global_t * 160 + k * 32) % max(30, int(vw * 0.36)))
                draw.line([(wx_start, wy), (wx_start + 30 * self.sc, wy)], fill=self.pal["crimson"], width=2)

        elif feature == "CONSEQUENCE_MAP" or mode in ("ocean_surge", "consequence_map"):
            r_globe = min(vw, vh) * 0.29 * cam_zoom
            self._draw_planet(draw, cx, cy, r_globe, global_t * 0.8, perturbed=True, label="GLOBAL CASCADE TELEMETRY")
            pulse_r = r_globe * (0.3 + (prog * 1.1) % 1.0)
            draw.ellipse([cx - pulse_r, cy - pulse_r, cx + pulse_r, cy + pulse_r], outline=self.pal["amber"], width=2)
            nodes = [
                (-0.45, -0.35, "EQUATORIAL ZONE"),
                (0.45, -0.30, "NORTHERN BASIN"),
                (-0.42, 0.35, "COASTAL GRID"),
                (0.42, 0.35, "ATMOSPHERIC JET"),
            ]
            f_nd = get_font(max(10, int(11 * self.sc)), bold=True)
            for nx_f, ny_f, n_lbl in nodes:
                nx = cx + nx_f * vw * 0.66
                ny = cy + ny_f * vh * 0.60
                draw.line([(cx, cy), (nx, ny)], fill=self.pal["cyan"], width=1)
                draw.ellipse([nx - 4, ny - 4, nx + 4, ny + 4], fill=self.pal["amber"])
                draw.text((nx - 36 * self.sc, ny + 6), n_lbl, font=f_nd, fill=self.pal["text"])

        elif mode in ("asteroid_trajectory", "asteroid_impact"):
            r_earth = min(vw, vh) * 0.23 * cam_zoom
            ex = x1 - vw * 0.28
            ey = cy + vh * 0.05
            self._draw_planet(draw, ex, ey, r_earth, global_t, perturbed=(mode == "asteroid_impact"), label="EARTH")

            ax = x0 + vw * (0.14 + 0.55 * ease_in_out(prog))
            ay = y0 + vh * (0.20 + 0.33 * ease_in_out(prog))
            draw.line([(x0 + vw * 0.12, y0 + vh * 0.19), (ex, ey)], fill=self.pal["crimson"], width=2)
            ar = max(7, int(13 * self.sc))
            draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=self.pal["amber"], outline=(255, 255, 255), width=2)
            if mode == "asteroid_impact" and prog > 0.55:
                blast = r_earth * (prog - 0.55) * 3.0
                draw.ellipse([ex - blast, ey - blast, ex + blast, ey + blast], outline=self.pal["amber"], width=3)

        elif mode == "solar_system":
            sun_r = min(vw, vh) * 0.10 * cam_zoom
            draw.ellipse([cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r], fill=self.pal["amber"])
            for orb_i, mult in enumerate([0.23, 0.35, 0.47], start=1):
                rx = vw * mult
                ry = vh * mult * 0.60
                draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(48, 70, 112), width=1)
                ang = global_t * (1.8 / orb_i) + orb_i
                px = cx + math.cos(ang) * rx
                py = cy + math.sin(ang) * ry
                pr = max(4, int((8 - orb_i) * self.sc))
                col = self.pal["cyan"] if orb_i == 2 else self.pal["violet"]
                draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=col)

        elif mode in ("human_biosphere", "reality_replay"):
            pts = []
            steps = 64
            for i in range(steps):
                t_norm = i / float(steps - 1)
                px = x0 + 20 + t_norm * (vw - 40)
                freq = 18.0 if scene["is_hypothetical"] else 9.0
                amp = vh * (0.21 * (1.0 - 0.4 * prog) if scene["is_hypothetical"] else 0.15)
                py = cy + math.sin(t_norm * freq + global_t * 6.0) * amp * math.sin(t_norm * math.pi)
                pts.append((px, py))
            draw.line(pts, fill=self.pal["emerald"] if not scene["is_hypothetical"] else self.pal["cyan"], width=3)
            ret_r = min(vw, vh) * 0.25 * cam_zoom
            draw.ellipse([cx - ret_r, cy - ret_r, cx + ret_r, cy + ret_r], outline=self.pal["violet"], width=2)

        else:
            r_p = min(vw, vh) * 0.29 * cam_zoom
            self._draw_planet(
                draw,
                cx,
                cy,
                r_p,
                global_t * (0.15 if scene["is_hypothetical"] else 1.8),
                perturbed=scene["is_hypothetical"],
                label="HYPOTHETICAL MODEL" if scene["is_hypothetical"] else "VERIFIED PHYSICAL MODEL",
            )

    def render_frame(
        self,
        topic: str,
        scene: Dict[str, Any],
        scene_idx: int,
        total_scenes: int,
        scene_prog: float,
        global_t: float,
        overall_prog: float,
    ) -> bytes:
        frame = self._bg_cache.copy()
        draw = ImageDraw.Draw(frame)
        pad = int(20 * self.sc)

        # 1. TOP MODERN HUD HEADER + REALITY CLOCK
        top_h = int(42 * self.sc)
        self._draw_modern_panel(draw, pad, pad, self.w - pad, pad + top_h, self.pal["cyan"], (14, 116, 144))

        f_brand = get_font(max(11, int(13 * self.sc)), bold=True)
        f_clock = get_font(max(11, int(14 * self.sc)), bold=True)

        tag_txt = "HYPOTHETICAL SIMULATION" if scene["is_hypothetical"] else "VERIFIED SCIENTIFIC DATA"
        tag_col = self.pal["amber"] if scene["is_hypothetical"] else self.pal["emerald"]
        draw.text((pad + 12, pad + int(13 * self.sc)), f"NEXUS // {tag_txt}", font=f_brand, fill=tag_col)

        clock_txt = f"REALITY CLOCK: {scene['clock']}"
        cb = draw.textbbox((0, 0), clock_txt, font=f_clock)
        draw.text((self.w - pad - (cb[2] - cb[0]) - 12, pad + int(12 * self.sc)), clock_txt, font=f_clock, fill=self.pal["cyan"])

        # 2. MAIN SIMULATION VIEWPORT & TELEMETRY SIDEBAR
        vp_top = pad + top_h + int(10 * self.sc)
        bot_h = int(128 * self.sc) if not self.is_vertical else int(185 * self.sc)
        vp_bot = self.h - pad - bot_h - int(10 * self.sc)

        if self.is_vertical:
            vp_box = (pad, vp_top, self.w - pad, vp_bot)
            self._draw_modern_panel(draw, *vp_box, self.pal["violet"], (88, 28, 135))
            self._draw_reality_visual(draw, scene, scene_prog, global_t, vp_box)
        else:
            side_w = int(self.w * 0.27)
            vp_box = (pad, vp_top, self.w - pad - side_w - int(10 * self.sc), vp_bot)
            side_box = (self.w - pad - side_w, vp_top, self.w - pad, vp_bot)

            self._draw_modern_panel(draw, *vp_box, self.pal["violet"], (88, 28, 135))
            self._draw_reality_visual(draw, scene, scene_prog, global_t, vp_box)

            self._draw_modern_panel(draw, *side_box, self.pal["cyan"], (14, 116, 144))
            f_sh = get_font(max(10, int(12 * self.sc)), bold=True)
            f_sv = get_font(max(10, int(11 * self.sc)), bold=False)
            sy = side_box[1] + int(12 * self.sc)
            draw.text((side_box[0] + 12, sy), f"STAGE {scene_idx + 1}/{total_scenes} TELEMETRY", font=f_sh, fill=self.pal["cyan"])
            sy += int(22 * self.sc)

            for k_lbl, v_lbl in scene.get("telemetry", {}).items():
                draw.text((side_box[0] + 12, sy), f"{k_lbl}:", font=f_sv, fill=self.pal["muted"])
                sy += int(14 * self.sc)
                draw.text((side_box[0] + 12, sy), str(v_lbl), font=f_sh, fill=self.pal["text"])
                sy += int(21 * self.sc)

            if scene_prog >= 0.42 and sy + 48 < side_box[3]:
                sy += int(4 * self.sc)
                draw.line([(side_box[0] + 10, sy), (side_box[2] - 10, sy)], fill=self.pal["amber"], width=1)
                sy += int(8 * self.sc)
                draw.text((side_box[0] + 12, sy), "HIDDEN CONSEQUENCE:", font=f_sh, fill=self.pal["amber"])
                sy += int(17 * self.sc)
                hc_lines = wrap_text(draw, scene.get("hidden_consequence", ""), f_sv, side_w - 24)
                for h_ln in hc_lines[:5]:
                    if sy + 14 < side_box[3]:
                        draw.text((side_box[0] + 12, sy), h_ln, font=f_sv, fill=self.pal["text"])
                        sy += int(14 * self.sc)

        # 3. BOTTOM NARRATION DECK & PROGRESS BAR
        deck_top = vp_bot + int(10 * self.sc)
        deck_bot = self.h - pad
        self._draw_modern_panel(draw, pad, deck_top, self.w - pad, deck_bot, self.pal["cyan"], (14, 116, 144))

        f_head = get_font(max(13, int(17 * self.sc)), bold=True)
        f_nar = get_font(max(11, int(13 * self.sc)), bold=False)

        hy = deck_top + int(11 * self.sc)
        draw.text((pad + 14, hy), f"{scene['stage']} — {scene['headline']}", font=f_head, fill=self.pal["cyan"])

        ny = hy + int(25 * self.sc)
        nar_lines = wrap_text(draw, scene["narration"], f_nar, (self.w - 2 * pad) - 28)
        for n_ln in nar_lines[:4]:
            draw.text((pad + 14, ny), n_ln, font=f_nar, fill=self.pal["text"])
            ny += int(18 * self.sc)

        bar_y = deck_bot - int(12 * self.sc)
        bar_w = (self.w - 2 * pad) - 28
        draw.rounded_rectangle([pad + 14, bar_y, pad + 14 + bar_w, bar_y + int(5 * self.sc)], radius=3, fill=(25, 36, 64))
        fill_w = max(6, int(bar_w * overall_prog))
        draw.rounded_rectangle([pad + 14, bar_y, pad + 14 + fill_w, bar_y + int(5 * self.sc)], radius=3, fill=self.pal["violet"])

        return frame.tobytes()


# ==============================================================================
# 6. ULTRA-REALISTIC MALE NEURAL VOICE & AUDIO ENGINE (MODULE E)
# ==============================================================================
class VoiceAudioEngine:
    NEURAL_MALE_VOICES = {
        "en-us": "en-US-AndrewNeural",
        "en-gb": "en-GB-RyanNeural",
        "en-au": "en-AU-WilliamNeural",
        "en": "en-US-ChristopherNeural",
    }

    @staticmethod
    def build_ambient_bed(total_dur: float, out_wav: str, volume: float = 0.22):
        rate = 22050
        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)

            chunk_sec = 20.0
            cursor = 0.0
            while cursor < total_dur:
                dur = min(chunk_sec, total_dur - cursor)
                n = int(round(dur * rate))
                if n <= 0:
                    break
                t = np.linspace(cursor, cursor + dur, n, endpoint=False, dtype=np.float32)
                drone = (
                    0.48 * np.sin(2.0 * np.pi * 55.0 * t)
                    + 0.30 * np.sin(2.0 * np.pi * 82.41 * t)
                    + 0.22 * np.sin(2.0 * np.pi * 110.0 * t + 0.35 * np.sin(2.0 * np.pi * 0.22 * t))
                )
                pulse = np.exp(-((t % 2.0) * 6.5)) * np.sin(2.0 * np.pi * 440.0 * t) * 0.12
                sig = (drone + pulse) * max(0.0, min(1.0, volume)) * 0.32
                pcm = (np.clip(sig, -0.95, 0.95) * 32767.0).astype(np.int16)
                wf.writeframes(pcm.tobytes())
                cursor += dur
                gc.collect()

    @staticmethod
    def _probe_audio_dur(path: str) -> float:
        if not HAS_FFPROBE or not os.path.exists(path):
            return 0.0
        try:
            res = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", path,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return float(res.stdout.strip())
        except Exception:
            return 0.0

    @classmethod
    def synthesize_neural_male_narration(
        cls,
        scenes: List[Dict[str, Any]],
        work_dir: str,
        speed_wpm: int = 155,
        voice_variant: str = "en-us",
    ) -> Tuple[Optional[str], List[Dict[str, Any]], str]:
        """Generates ultra-realistic male documentary narration via edge-tts and auto-syncs scene durations."""
        if not HAS_FFMPEG:
            return None, scenes, "FFmpeg missing; fallback to ambient score."

        voice_name = cls.NEURAL_MALE_VOICES.get(voice_variant, "en-US-AndrewNeural")
        rate_pct = int(round(((speed_wpm - 155) / 155.0) * 100))
        rate_str = f"{rate_pct:+d}%"

        if HAS_EDGE_TTS:
            try:
                async def _gen_all():
                    for idx, sc in enumerate(scenes):
                        spoken = clean_text_for_speech(sc["narration"])
                        mp3_out = os.path.join(work_dir, f"neural_{idx}.mp3")
                        comm = edge_tts.Communicate(spoken, voice=voice_name, rate=rate_str, pitch="-4Hz")
                        await comm.save(mp3_out)

                asyncio.run(_gen_all())

                synced_scenes: List[Dict[str, Any]] = []
                concat_parts: List[str] = []
                cursor = 0.0

                for idx, sc in enumerate(scenes):
                    mp3_p = os.path.join(work_dir, f"neural_{idx}.mp3")
                    wav_p = os.path.join(work_dir, f"neural_sync_{idx}.wav")
                    speech_dur = cls._probe_audio_dur(mp3_p)
                    if speech_dur <= 0.3:
                        raise RuntimeError("Empty neural TTS segment.")

                    # Add 0.8s breathing room after speech so narration never gets cut off!
                    final_sc_dur = round(max(float(sc["duration_sec"]), speech_dur + 0.85), 2)

                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-i", mp3_p,
                            "-af", f"apad=whole_dur={final_sc_dur:.3f}",
                            "-t", f"{final_sc_dur:.3f}",
                            "-ar", "22050", "-ac", "1", wav_p,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True,
                    )
                    concat_parts.append(wav_p)

                    sc_copy = dict(sc)
                    sc_copy["start_sec"] = round(cursor, 2)
                    sc_copy["duration_sec"] = final_sc_dur
                    synced_scenes.append(sc_copy)
                    cursor += final_sc_dur

                list_txt = os.path.join(work_dir, "neural_concat.txt")
                with open(list_txt, "w", encoding="utf-8") as fh:
                    for p in concat_parts:
                        fh.write(f"file '{p}'\n")

                full_wav = os.path.join(work_dir, "full_neural_male.wav")
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt, "-c", "copy", full_wav],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )

                if os.path.exists(full_wav) and os.path.getsize(full_wav) > 1000:
                    return full_wav, synced_scenes, f"Ultra-Realistic Male Neural Voice ({voice_name}, Auto-Synced)."
            except Exception:
                pass

        # Fallback to espeak-ng only if edge-tts network call is blocked
        if ESPEAK_BIN:
            concat_parts = []
            for idx, sc in enumerate(scenes):
                sc_dur = float(sc["duration_sec"])
                raw_tts = os.path.join(work_dir, f"esp_{idx}.wav")
                pad_tts = os.path.join(work_dir, f"esp_pad_{idx}.wav")
                spoken = clean_text_for_speech(sc["narration"])
                subprocess.run(
                    [ESPEAK_BIN, "-v", "en-us+m3", "-s", str(speed_wpm), "-w", raw_tts, spoken],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if os.path.exists(raw_tts) and os.path.getsize(raw_tts) > 100:
                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-i", raw_tts,
                            "-af", f"apad=whole_dur={sc_dur:.3f}",
                            "-t", f"{sc_dur:.3f}",
                            "-ar", "22050", "-ac", "1", pad_tts,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    if os.path.exists(pad_tts):
                        concat_parts.append(pad_tts)

            if concat_parts:
                list_txt = os.path.join(work_dir, "esp_concat.txt")
                with open(list_txt, "w", encoding="utf-8") as fh:
                    for p in concat_parts:
                        fh.write(f"file '{p}'\n")
                full_wav = os.path.join(work_dir, "full_esp.wav")
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt, "-c", "copy", full_wav],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if os.path.exists(full_wav):
                    return full_wav, scenes, "Fallback Offline TTS Voice Active."

        return None, scenes, "Ambient telemetry audio only."


# ==============================================================================
# 7. QUALITY CONTROL ENGINE (MODULE G)
# ==============================================================================
class QualityControlEngine:
    @staticmethod
    def validate_output(
        mp4_path: str, expected_w: int, expected_h: int
    ) -> Tuple[bool, str, Dict[str, Any]]:
        if not os.path.exists(mp4_path):
            return False, "QC Failed: Output MP4 file was not created.", {}
        size_bytes = os.path.getsize(mp4_path)
        if size_bytes < 10_000:
            return False, "QC Failed: Output MP4 file is corrupted (<10 KB).", {}

        meta: Dict[str, Any] = {"size_mb": round(size_bytes / (1024.0 * 1024.0), 2)}
        if not HAS_FFPROBE:
            return True, "QC Passed.", meta

        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=codec_type,width,height",
                "-of", "json", mp4_path,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            info = json.loads(res.stdout)
            streams = info.get("streams", [])
            v_streams = [s for s in streams if s.get("codec_type") == "video"]
            a_streams = [s for s in streams if s.get("codec_type") == "audio"]
            dur = float(info.get("format", {}).get("duration", 0.0))

            if not v_streams:
                return False, "QC Failed: No video stream found.", meta
            vw = int(v_streams[0].get("width", 0))
            vh = int(v_streams[0].get("height", 0))
            if vw != expected_w or vh != expected_h:
                return False, f"QC Failed: Dimension mismatch ({vw}x{vh} != {expected_w}x{expected_h}).", meta
            if dur <= 1.0:
                return False, "QC Failed: Invalid duration.", meta

            meta["duration_sec"] = round(dur, 1)
            meta["resolution"] = f"{vw}x{vh}"
            meta["has_audio"] = len(a_streams) > 0
            return True, f"QC Verified: {vw}x{vh}, {dur:.1f}s, {meta['size_mb']} MB", meta
        except Exception as exc:
            return False, f"QC Probe Error: {exc}", meta


# ==============================================================================
# 8. 5X TURBO RENDER ENGINE + CINEMATIC FILTER CHAIN (MODULE F)
# ==============================================================================
def get_dimensions(aspect_ratio: str, resolution: str) -> Tuple[int, int]:
    """Optimized crisp dimensions for 5X faster cloud rendering with sharp cinematic upscale."""
    is_1080 = resolution == "1080p"
    if aspect_ratio == "9:16":
        return (720, 1280) if is_1080 else (540, 960)
    if aspect_ratio == "1:1":
        return (720, 720) if is_1080 else (540, 540)
    return (1280, 720) if is_1080 else (960, 540)


def update_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def run_render_pipeline(job_id: str, payload: Dict[str, Any]):
    work_dir = tempfile.mkdtemp(prefix=f"nexus_{job_id}_", dir=TMP_ROOT)
    try:
        if not HAS_FFMPEG:
            raise RuntimeError("FFmpeg is not installed on the server.")

        script_data = payload["script"]
        scenes = script_data.get("scenes", [])
        if not scenes:
            raise ValueError("Script contains zero scenes.")

        aspect_ratio = payload.get("aspect_ratio", "16:9")
        resolution = payload.get("resolution", "720p")
        fps = max(12, min(24, int(payload.get("fps", 16))))
        visual_style = payload.get("visual_style", "nexus_obsidian")
        voice_mode = payload.get("voice_mode", "offline_tts")
        voice_variant = payload.get("voice_variant", "en-us")
        speaking_speed = int(payload.get("speaking_speed", 155))
        music_volume = float(payload.get("music_volume", 0.22))
        uploaded_audio_id = payload.get("uploaded_audio_id", "")

        out_w, out_h = get_dimensions(aspect_ratio, resolution)

        update_job(job_id, status="running", progress=6, current_scene="Synthesizing Ultra-Realistic Male Voiceover")

        # 1. Voice Narration Synthesis (auto-syncs scene durations so voice never cuts off!)
        narration_wav: Optional[str] = None
        audio_note = "Ambient telemetry score only."

        if voice_mode == "uploaded" and is_safe_id(uploaded_audio_id):
            for fname in os.listdir(UPLOAD_DIR):
                if fname.startswith(uploaded_audio_id):
                    narration_wav = os.path.join(UPLOAD_DIR, fname)
                    audio_note = f"Custom uploaded voiceover ({fname})."
                    break
            if not narration_wav:
                raise FileNotFoundError("Uploaded narration audio file was not found.")
        elif voice_mode == "offline_tts":
            narration_wav, scenes, audio_note = VoiceAudioEngine.synthesize_neural_male_narration(
                scenes, work_dir, speed_wpm=speaking_speed, voice_variant=voice_variant
            )

        total_dur = sum(float(s["duration_sec"]) for s in scenes)

        # 2. Build ambient bed matching synced total duration
        ambient_wav = os.path.join(work_dir, "ambient.wav")
        VoiceAudioEngine.build_ambient_bed(total_dur, ambient_wav, volume=music_volume)

        # 3. Mix master audio with studio compression & loudness normalization
        master_wav = os.path.join(work_dir, "master_audio.wav")
        if narration_wav and os.path.exists(narration_wav):
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", narration_wav,
                    "-i", ambient_wav,
                    "-filter_complex",
                    "[0:a]acompressor=threshold=-18dB:ratio=3:attack=10:release=120,volume=1.15[v];"
                    "[1:a]volume=0.38[m];"
                    "[v][m]amix=inputs=2:duration=longest[out]",
                    "-map", "[out]", "-t", f"{total_dur:.3f}",
                    "-ar", "22050", "-ac", "1", master_wav,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        else:
            shutil.copyfile(ambient_wav, master_wav)

        # 4. 5X Fast Chunked Video Rendering + Cinematic Color & Sharpen Filter
        renderer = RealityCinematicRenderer(out_w, out_h, style_key=visual_style)
        dt = 1.0 / float(fps)
        global_t = 0.0
        total_scenes = len(scenes)
        chunk_paths: List[str] = []

        # Cinematic post-processing filter (contrast boost, subtle saturation, crisp unsharp, subtle vignette)
        cinema_vf = "eq=contrast=1.06:brightness=0.01:saturation=1.18,unsharp=3:3:0.5:3:3:0.0,vignette=PI/6"

        for idx, sc in enumerate(scenes):
            sc_dur = float(sc["duration_sec"])
            n_frames = max(1, int(round(sc_dur * fps)))
            chunk_mp4 = os.path.join(work_dir, f"chunk_{idx:02d}.mp4")

            update_job(
                job_id,
                progress=14 + int((idx / float(total_scenes)) * 74),
                current_scene=f"Rendering Scene {idx + 1}/{total_scenes}: {sc['headline']}",
            )

            ffmpeg_chunk = [
                "ffmpeg", "-y",
                "-f", "rawvideo", "-vcodec", "rawvideo",
                "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
                "-r", str(fps), "-i", "-",
                "-vf", cinema_vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-pix_fmt", "yuv420p", chunk_mp4,
            ]
            proc = subprocess.Popen(ffmpeg_chunk, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            for f_i in range(n_frames):
                sc_prog = f_i / float(max(1, n_frames - 1))
                ov_prog = min(1.0, global_t / max(1.0, total_dur))
                raw_frame = renderer.render_frame(
                    script_data["topic"], sc, idx, total_scenes, sc_prog, global_t, ov_prog
                )
                proc.stdin.write(raw_frame)
                global_t += dt

            proc.stdin.close()
            rc = proc.wait()
            if rc != 0 or not os.path.exists(chunk_mp4):
                raise RuntimeError(f"FFmpeg chunk encoding failed on Scene {idx + 1}.")
            chunk_paths.append(chunk_mp4)
            gc.collect()

        # 5. Concatenate chunks & mux master audio
        update_job(job_id, progress=91, current_scene="Finalizing Cinematic Master MP4")
        concat_file = os.path.join(work_dir, "chunks.txt")
        with open(concat_file, "w", encoding="utf-8") as cf:
            for cp in chunk_paths:
                cf.write(f"file '{cp}'\n")

        silent_joined = os.path.join(work_dir, "joined_video.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", silent_joined],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

        safe_slug = re.sub(r"[^a-zA-Z0-9]+", "_", script_data["topic"])[:36].strip("_") or "NEXUS_REALITY"
        final_filename = f"NEXUS_{safe_slug}_{aspect_ratio.replace(':', 'x')}_{resolution}_{job_id}.mp4"
        final_path = os.path.join(OUTPUT_DIR, final_filename)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", silent_joined,
                "-i", master_wav,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-shortest", final_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

        update_job(job_id, progress=96, current_scene="Running Quality Control Verification")
        ok, qc_msg, qc_meta = QualityControlEngine.validate_output(final_path, out_w, out_h)
        if not ok:
            raise RuntimeError(qc_msg)

        meta_record = {
            "job_id": job_id,
            "topic": script_data["topic"],
            "category": script_data["category"],
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "fps": fps,
            "file_name": final_filename,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "audio_note": audio_note,
            "qc": qc_meta,
            "growth": script_data["growth"],
        }
        with open(os.path.join(OUTPUT_DIR, f"{job_id}.json"), "w", encoding="utf-8") as mf:
            json.dump(meta_record, mf, indent=2)

        update_job(
            job_id,
            status="done",
            progress=100,
            current_scene="Completed",
            file_name=final_filename,
            audio_note=audio_note,
            qc_message=qc_msg,
            qc_meta=qc_meta,
            growth=script_data["growth"],
        )

    except Exception as exc:
        update_job(job_id, status="error", error=str(exc), current_scene="Failed")
    finally:
        with JOBS_LOCK:
            ACTIVE_JOB_IDS.discard(job_id)
        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()


# ==============================================================================
# 9. FLASK REST API ENDPOINTS
# ==============================================================================
@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "ok",
        "service": "NEXUS Worldwide Reality Video Generator",
        "ffmpeg": HAS_FFMPEG,
        "ffprobe": HAS_FFPROBE,
        "offline_tts": HAS_EDGE_TTS or bool(ESPEAK_BIN),
        "tts_engine": "edge-tts (Male Neural)" if HAS_EDGE_TTS else (ESPEAK_BIN or "none"),
        "active_jobs": len(ACTIVE_JOB_IDS),
    })


@app.route("/api/script/generate", methods=["POST"])
def api_script_generate():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()
    if len(topic) < 3:
        return jsonify({"error": "Please enter a valid topic (at least 3 characters)."}), 400
    if len(topic) > 220:
        return jsonify({"error": "Topic is too long (maximum 220 characters)."}), 400

    category = str(data.get("category", "Auto-Detect")).strip()
    try:
        duration_sec = int(data.get("duration_sec", 60))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid duration value."}), 400

    if duration_sec < 30 or duration_sec > 600:
        return jsonify({"error": "Duration must be between 30 and 600 seconds."}), 400

    user_sources = str(data.get("user_sources", "")).strip()
    script_payload = IntelligenceEngine.generate_script(
        topic=topic,
        category=category,
        duration_sec=duration_sec,
        user_sources=user_sources,
    )
    return jsonify({"ok": True, "script": script_payload})


@app.route("/api/audio/upload", methods=["POST"])
def api_audio_upload():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file field provided."}), 400
    f = request.files["audio"]
    if not f or not f.filename:
        return jsonify({"error": "Empty audio filename."}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".wav", ".mp3", ".m4a", ".ogg"):
        return jsonify({"error": "Unsupported audio format. Use WAV, MP3, M4A, or OGG."}), 400

    audio_id = f"aud_{uuid.uuid4().hex[:12]}"
    dest = os.path.join(UPLOAD_DIR, f"{audio_id}{ext}")
    f.save(dest)
    return jsonify({"ok": True, "audio_id": audio_id, "file_name": f.filename})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    script = data.get("script")
    if not isinstance(script, dict) or not script.get("scenes"):
        topic = str(data.get("topic", "")).strip()
        if len(topic) < 3:
            return jsonify({"error": "Provide a valid topic or pre-generated script."}), 400
        script = IntelligenceEngine.generate_script(
            topic=topic,
            category=str(data.get("category", "Auto-Detect")),
            duration_sec=int(data.get("duration_sec", 60)),
            user_sources=str(data.get("user_sources", "")),
        )

    aspect_ratio = str(data.get("aspect_ratio", "16:9"))
    if aspect_ratio not in ("16:9", "9:16", "1:1"):
        return jsonify({"error": "Invalid aspect ratio. Choose 16:9, 9:16, or 1:1."}), 400

    resolution = str(data.get("resolution", "720p"))
    if resolution not in ("720p", "1080p"):
        return jsonify({"error": "Invalid resolution. Choose 720p or 1080p."}), 400

    with JOBS_LOCK:
        if len(ACTIVE_JOB_IDS) >= 2:
            return jsonify({"error": "Server is currently rendering 2 active jobs. Please wait for one to finish."}), 429
        job_id = f"nx_{uuid.uuid4().hex[:12]}"
        ACTIVE_JOB_IDS.add(job_id)
        JOBS[job_id] = {
            "job_id": job_id,
            "topic": script.get("topic", "NEXUS Reality Video"),
            "status": "queued",
            "progress": 1,
            "current_scene": "Queued for 5X TURBO Render Engine",
            "error": None,
        }

    payload = {
        "script": script,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "fps": data.get("fps", 16),
        "visual_style": data.get("visual_style", "nexus_obsidian"),
        "voice_mode": data.get("voice_mode", "offline_tts"),
        "voice_variant": data.get("voice_variant", "en-us"),
        "speaking_speed": data.get("speaking_speed", 155),
        "music_volume": data.get("music_volume", 0.22),
        "uploaded_audio_id": data.get("uploaded_audio_id", ""),
    }

    threading.Thread(target=run_render_pipeline, args=(job_id, payload), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID format."}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        meta_file = os.path.join(OUTPUT_DIR, f"{job_id}.json")
        if os.path.exists(meta_file):
            with open(meta_file, "r", encoding="utf-8") as mf:
                saved = json.load(mf)
            return jsonify({
                "job_id": job_id,
                "status": "done",
                "progress": 100,
                "current_scene": "Completed",
                "file_name": saved.get("file_name"),
                "qc_meta": saved.get("qc", {}),
                "growth": saved.get("growth", {}),
            })
        return jsonify({"error": "Job not found (server may have restarted while rendering)."}), 404
    return jsonify(job)


@app.route("/api/video/<job_id>", methods=["GET"])
def api_video(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID."}), 400

    meta_file = os.path.join(OUTPUT_DIR, f"{job_id}.json")
    if not os.path.exists(meta_file):
        return jsonify({"error": "Rendered video metadata not found."}), 404

    with open(meta_file, "r", encoding="utf-8") as mf:
        saved = json.load(mf)
    fname = os.path.basename(saved.get("file_name", ""))
    target = os.path.join(OUTPUT_DIR, fname)
    if not fname or not os.path.exists(target):
        return jsonify({"error": "MP4 file missing on disk."}), 404

    as_dl = request.args.get("dl") == "1"
    return send_file(target, as_attachment=as_dl, download_name=fname, mimetype="video/mp4")


@app.route("/api/library", methods=["GET"])
def api_library():
    items: List[Dict[str, Any]] = []
    for fname in sorted(os.listdir(OUTPUT_DIR), reverse=True):
        if fname.endswith(".json"):
            fpath = os.path.join(OUTPUT_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as mf:
                    rec = json.load(mf)
                mp4_name = os.path.basename(rec.get("file_name", ""))
                if mp4_name and os.path.exists(os.path.join(OUTPUT_DIR, mp4_name)):
                    items.append(rec)
            except Exception:
                pass
    return jsonify({"ok": True, "videos": items})


@app.route("/api/job/<job_id>", methods=["DELETE"])
def api_delete_job(job_id: str):
    if not is_safe_id(job_id):
        return jsonify({"error": "Invalid job ID."}), 400
    with JOBS_LOCK:
        if job_id in ACTIVE_JOB_IDS:
            return jsonify({"error": "Cannot delete an actively rendering job."}), 409
        JOBS.pop(job_id, None)

    meta_file = os.path.join(OUTPUT_DIR, f"{job_id}.json")
    if os.path.exists(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8") as mf:
                rec = json.load(mf)
            mp4_name = os.path.basename(rec.get("file_name", ""))
            if mp4_name:
                mp4_path = os.path.join(OUTPUT_DIR, mp4_name)
                if os.path.exists(mp4_path):
                    os.remove(mp4_path)
            os.remove(meta_file)
        except Exception as exc:
            return jsonify({"error": f"Failed to delete files: {exc}"}), 500

    return jsonify({"ok": True, "deleted": job_id})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
