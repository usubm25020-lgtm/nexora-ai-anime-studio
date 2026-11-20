import os
import re
import gc
import json
import math
import time
import uuid
import wave
import shutil
import tempfile
import subprocess
import threading
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, request, jsonify, render_template, send_file

# ==============================================================================
# 1. APPLICATION CONFIGURATION & PATH SECURITY
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
TMP_ROOT = os.path.join(BASE_DIR, "tmp_work")

for directory in (OUTPUT_DIR, UPLOAD_DIR, TMP_ROOT):
    os.makedirs(directory, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload cap

# In-memory job registry (structured for easy upgrade to Redis/Celery)
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
HAS_ESPEAK = ESPEAK_BIN is not None


# ==============================================================================
# 2. TYPOGRAPHY & MATH HELPERS
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
                "stage": "1. Initial Event",
                "clock": "T + 00:00:01",
                "visual_mode": "earth_rotation",
                "feature": "REALITY_CLOCK",
                "headline": "Angular Momentum Halts at 1,670 km/h",
                "narration": "Earth's solid crust locks abruptly, while the atmosphere and oceans retain their eastward rotational velocity of up to 465 meters per second at the equator.",
                "telemetry": {"Equatorial Crust": "0 km/h", "Air Shear": "1,670 km/h", "Max Impact": "Equatorial Belt"},
                "hidden_consequence": "Only observers standing within a few kilometers of the North and South Poles would initially survive the surface deceleration.",
                "is_hypothetical": True,
            },
            {
                "stage": "2. Immediate Consequences",
                "clock": "T + 00:04:00",
                "visual_mode": "atmosphere_shock",
                "feature": "DUAL_REALITY",
                "headline": "Supersonic Winds Scour the Continents",
                "narration": "Super-hurricane winds four times faster than the strongest recorded tornado strip landscapes, generating intense aerodynamic friction heat and global firestorms.",
                "telemetry": {"Wind Speed": "> 450 m/s", "Dynamic Pressure": "130 kPa", "Surface Shear": "Extreme"},
                "hidden_consequence": "Atmospheric friction would rapidly heat the lower troposphere and trigger continent-wide electrostatic lightning storms.",
                "is_hypothetical": True,
            },
            {
                "stage": "3. Short-Term Effects",
                "clock": "T + 12:00:00",
                "visual_mode": "ocean_surge",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Mega-Tsunamis & Polar Ocean Migration",
                "narration": "Without centrifugal force bulging water around the equator, global oceans surge toward both poles, flooding northern and southern landmasses while exposing an equatorial supercontinent.",
                "telemetry": {"Equatorial Bulge": "-21 km Drop", "Ocean Basins": "2 Polar Oceans", "Sea Level Shift": "Kilometers"},
                "hidden_consequence": "Earth's equatorial radius is 21 kilometers wider than its polar radius; without spin, gravity pulls water directly toward the poles.",
                "is_hypothetical": True,
            },
            {
                "stage": "4. Long-Term Effects",
                "clock": "T + 180 DAYS",
                "visual_mode": "two_worlds",
                "feature": "TWO_WORLDS",
                "headline": "Six Months of Daylight, Six Months of Night",
                "narration": "A single day-night cycle now takes one full year as Earth orbits the Sun. One hemisphere bakes under continuous sunlight while the dark side plunges into deep freeze.",
                "telemetry": {"Solar Day": "365 Earth Days", "Day Hemisphere": "> 60°C", "Night Hemisphere": "< -50°C"},
                "hidden_consequence": "Reduced liquid outer-core convection would progressively weaken Earth's protective magnetic field over geological timescales.",
                "is_hypothetical": True,
            },
            {
                "stage": "5. Human & Environmental Impact",
                "clock": "T + 05 YEARS",
                "visual_mode": "human_biosphere",
                "feature": "REALITY_REPLAY",
                "headline": "Biosphere Retreats to Polar Twilight Zones",
                "narration": "Photosynthesis ceases across the frozen night hemisphere, collapsing food webs. Surviving human settlements would be restricted to narrow twilight bands along polar coastlines.",
                "telemetry": {"Habitable Zone": "Polar Twilight", "Food Production": "Subsurface Only", "Cosmic Flux": "Elevated"},
                "hidden_consequence": "Replaying the first 60 seconds from orbital altitude shows the entire global cloud deck shearing off in a single eastward spiral.",
                "is_hypothetical": True,
            },
            {
                "stage": "6. Final Scientific Explanation",
                "clock": "SCIENTIFIC REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "Real Earth Slows by Just 1.8 ms Per Century",
                "narration": "In reality, no physical mechanism can stop Earth instantaneously. Tidal friction with the Moon lengthens our day by roughly 1.8 milliseconds per century—keeping our planet safely spinning.",
                "telemetry": {"Actual Slowdown": "1.8 ms / Century", "Energy Needed": "2.13 x 10^29 J", "Status": "Scientifically Safe"},
                "hidden_consequence": "Halting Earth's spin would require absorbing 2.13 x 10^29 joules—more energy than the Sun radiates in six full days.",
                "is_hypothetical": False,
            },
        ],
    },
    "moon_disappear": {
        "match": ["moon disappear", "without the moon", "no moon", "moon vanished", "moon gone"],
        "category": "Orbital & Tidal Science",
        "title": "What If the Moon Suddenly Disappeared?",
        "hook": "The Moon is far more than a night light—it is Earth's gravitational anchor. If it vanished tonight, our skies would darken within 1.3 seconds and Earth's climate would destabilize over millennia.",
        "scientific_basis": "Gravitational tidal mechanics, axial obliquity stabilization, and marine circadian biology.",
        "stages": [
            {
                "stage": "1. Initial Event",
                "clock": "T + 00:00:01.3",
                "visual_mode": "solar_system",
                "feature": "REALITY_CLOCK",
                "headline": "Gravitational Release & Sudden Night Darkness",
                "narration": "Because light takes 1.28 seconds to travel from the Moon to Earth, the night sky turns pitch black 1.3 seconds after disappearance as lunar gravity releases Earth's oceans.",
                "telemetry": {"Light Delay": "1.28 Seconds", "Night Sky Lux": "-95% Drop", "Tidal Driver": "Solar Only"},
                "hidden_consequence": "Gravitational waves travel at the speed of light, meaning Earth's tidal bulge relaxes at the exact moment the Moon vanishes from view.",
                "is_hypothetical": True,
            },
            {
                "stage": "2. Immediate & Short-Term Effects",
                "clock": "T + 24 HOURS",
                "visual_mode": "ocean_surge",
                "feature": "DUAL_REALITY",
                "headline": "Ocean Tides Drop by 75% Worldwide",
                "narration": "Without lunar gravity, ocean tides shrink to one-quarter of their normal amplitude, driven solely by the distant Sun. Coastal intertidal zones lose their daily nutrient exchange.",
                "telemetry": {"Tidal Height": "-75% Reduction", "Solar Tide Share": "25% Remaining", "Coastal Mixing": "Weakened"},
                "hidden_consequence": "Lunar tides drive deep-ocean vertical mixing; weaker tides reduce nutrient upwelling essential for global phytoplankton.",
                "is_hypothetical": True,
            },
            {
                "stage": "3. Environmental Impact",
                "clock": "T + 01 YEAR",
                "visual_mode": "consequence_map",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Marine Spawning & Nocturnal Ecology Disrupted",
                "narration": "Thousands of marine species—including reef corals, sea turtles, and crabs—rely on lunar cycles to synchronize reproduction. Global marine ecosystems enter sharp decline.",
                "telemetry": {"Coral Synchrony": "Disrupted", "Predator/Prey": "Imbalanced", "Solar Eclipses": "Impossible"},
                "hidden_consequence": "Total solar eclipses only occur because the Moon is 400 times smaller than the Sun yet 400 times closer to Earth.",
                "is_hypothetical": True,
            },
            {
                "stage": "4. Long-Term Effects",
                "clock": "T + 100,000 YEARS",
                "visual_mode": "two_worlds",
                "feature": "TWO_WORLDS",
                "headline": "Chaotic Axial Tilt & Extreme Climate Swings",
                "narration": "Without the Moon's stabilizing pull, Earth's 23.5-degree axial tilt wobbles chaotically between zero and forty-five degrees, triggering severe ice ages and extreme heat eras.",
                "telemetry": {"Normal Tilt": "23.5° Stable", "Unanchored Tilt": "0° to 45°+ Chaos", "Seasons": "Unpredictable"},
                "hidden_consequence": "Mars lacks a large stabilizing moon, which is why its axial tilt has swung wildly over millions of years.",
                "is_hypothetical": True,
            },
            {
                "stage": "5. Human Impact & Reality Replay",
                "clock": "T + 500,000 YEARS",
                "visual_mode": "human_biosphere",
                "feature": "REALITY_REPLAY",
                "headline": "Civilization Adapts to Shifting Climate Belts",
                "narration": "Replaying Earth's rotational pole from space shows climate zones migrating across latitudes, forcing agriculture and coastal infrastructure to relocate across centuries.",
                "telemetry": {"Ice Sheets": "Rapid Advance/Retreat", "Sea Level": "±50m Swings", "Astronomy": "Zero Lunar Glare"},
                "hidden_consequence": "Paradoxically, ground-based optical astronomy would improve significantly without moonlight scattering in the atmosphere.",
                "is_hypothetical": True,
            },
            {
                "stage": "6. Final Scientific Explanation",
                "clock": "SCIENTIFIC REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "The Moon Recedes Safely at 3.8 cm Per Year",
                "narration": "Apollo lunar laser reflectors prove the Moon drifts away by just 3.8 centimeters annually—a gradual orbital evolution that keeps Earth's climate stabilized for hundreds of millions of years.",
                "telemetry": {"Recession Rate": "3.8 cm / Year", "Mean Distance": "384,400 km", "Orbit Status": "Stabilized"},
                "hidden_consequence": "In roughly 600 million years, the Moon will appear too small in our sky to completely cover the Sun during an eclipse.",
                "is_hypothetical": False,
            },
        ],
    },
    "sun_disappear": {
        "match": ["sun disappear", "without the sun", "no sun", "sun vanished", "sun goes out"],
        "category": "Astrophysics & Thermodynamics",
        "title": "What If the Sun Suddenly Disappeared?",
        "hook": "The Sun provides 99.98% of the energy warming Earth's surface and holds our planet in orbit. If it vanished instantaneously, we wouldn't even know for eight minutes and twenty seconds.",
        "scientific_basis": "Speed of light and gravity (c = 299,792 km/s), radiative cooling laws, and geothermal heat flux.",
        "stages": [
            {
                "stage": "1. Initial Event",
                "clock": "T + 00:08:20",
                "visual_mode": "solar_system",
                "feature": "REALITY_CLOCK",
                "headline": "Darkness Arrives & Earth Flies Off on a Tangent",
                "narration": "At 8 minutes and 20 seconds, the last solar photons reach Earth. Simultaneously, the Sun's gravitational pull releases, sending Earth hurtling into interstellar space at 30 km/s.",
                "telemetry": {"Light & Gravity Lag": "8 min 20 sec", "Escape Velocity": "29.78 km/s", "Sky Illumination": "Starlight Only"},
                "hidden_consequence": "General relativity confirms gravitational changes propagate at the exact speed of light, not instantaneously.",
                "is_hypothetical": True,
            },
            {
                "stage": "2. Immediate & Short-Term Effects",
                "clock": "T + 07 DAYS",
                "visual_mode": "two_worlds",
                "feature": "DUAL_REALITY",
                "headline": "Photosynthesis Halts & Global Temps Drop Below -17°C",
                "narration": "Without solar radiation, small plants die within days and global average surface temperature plunges from 15°C to below -17°C within the first week.",
                "telemetry": {"Week 1 Avg Temp": "-17°C (1°F)", "Photosynthesis": "0% Active", "Moon Visibility": "Invisible"},
                "hidden_consequence": "The Moon would also vanish from sight because it only shines by reflecting sunlight.",
                "is_hypothetical": True,
            },
            {
                "stage": "3. Environmental Cascade",
                "clock": "T + 01 YEAR",
                "visual_mode": "consequence_map",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Surface Oceans Freeze While Deep Seas Stay Liquid",
                "narration": "Within a year, global surface temperatures fall below -70°C, encasing the oceans in thick ice—yet geothermal heat from Earth's core keeps deep abyssal waters liquid.",
                "telemetry": {"Year 1 Temp": "< -70°C", "Ice Crust": "Insulates Deep Sea", "Forests": "Frozen"},
                "hidden_consequence": "Ice is less dense than liquid water; floating sea ice acts as an insulating blanket that prevents oceans from freezing solid for billions of years.",
                "is_hypothetical": True,
            },
            {
                "stage": "4. Long-Term Planetary State",
                "clock": "T + 10 YEARS",
                "visual_mode": "atmosphere_shock",
                "feature": "TWO_WORLDS",
                "headline": "Atmosphere Liquefies & Snows Nitrogen onto the Ice",
                "narration": "As temperatures approach -200°C over a decade, atmospheric oxygen and nitrogen condense into liquid rain and eventually freeze into snow covering the planetary ice shell.",
                "telemetry": {"Surface Temp": "~ -200°C", "Atmosphere": "Condensed/Frozen", "Distance Traveled": "~63 AU"},
                "hidden_consequence": "After 10 years at 30 km/s, Earth would already have drifted past the orbit of Pluto into deep interstellar darkness.",
                "is_hypothetical": True,
            },
            {
                "stage": "5. Human Survival & Reality Replay",
                "clock": "T + 50 YEARS",
                "visual_mode": "human_biosphere",
                "feature": "REALITY_REPLAY",
                "headline": "Deep Geothermal & Hydrothermal Vent Refuges",
                "narration": "Replaying Earth's thermal profile reveals that deep-sea hydrothermal vent ecosystems survive completely unbothered, while humans could only endure in nuclear- or geothermal-powered underground habitats.",
                "telemetry": {"Vent Ecosystems": "100% Intact", "Human Power": "Geothermal/Nuclear", "Oxygen Source": "Electrolysis"},
                "hidden_consequence": "Life around deep ocean hydrothermal vents relies on chemosynthesis rather than sunlight, making it independent of the Sun.",
                "is_hypothetical": True,
            },
            {
                "stage": "6. Final Scientific Explanation",
                "clock": "SCIENTIFIC REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "The Sun Has 5 Billion Years of Hydrogen Fuel Left",
                "narration": "Conservation of mass-energy makes sudden disappearance impossible. Our Sun is a stable main-sequence star with roughly 5 billion years of core hydrogen fusion remaining.",
                "telemetry": {"Solar Age": "4.6 Billion Yrs", "Remaining Fuel": "~5 Billion Yrs", "Status": "Main-Sequence Stable"},
                "hidden_consequence": "Every second, the Sun fuses 600 million tons of hydrogen into helium, releasing steady energy that sustains Earth.",
                "is_hypothetical": False,
            },
        ],
    },
    "atmosphere_vanish": {
        "match": ["atmosphere vanish", "no atmosphere", "atmosphere disappear", "air disappear", "lose atmosphere"],
        "category": "Atmospheric Physics & Biology",
        "title": "What If Earth's Atmosphere Vanished Instantly?",
        "hook": "Earth's atmosphere provides the air we breathe, the pressure keeping liquid water from boiling, and our shield against solar radiation. Here is what happens second by second if it disappeared.",
        "scientific_basis": "Ideal gas law, vapor pressure of water (Armstrong limit), acoustic wave propagation, and radiative equilibrium.",
        "stages": [
            {
                "stage": "1. Initial Event",
                "clock": "T + 00:00:01",
                "visual_mode": "atmosphere_shock",
                "feature": "REALITY_CLOCK",
                "headline": "Instant Vacuum, Total Silence & Black Daytime Sky",
                "narration": "Surface pressure drops from 101.3 kPa to zero. Without air molecules to scatter blue light or carry sound waves, the daytime sky turns pitch black and complete silence falls.",
                "telemetry": {"Surface Pressure": "0.0 kPa (Vacuum)", "Sound Speed": "0 m/s (Silent)", "Daytime Sky": "Pitch Black"},
                "hidden_consequence": "Even the loudest explosion or jet engine would be completely inaudible in a vacuum.",
                "is_hypothetical": True,
            },
            {
                "stage": "2. Immediate Human & Biological Impact",
                "clock": "T + 00:00:15",
                "visual_mode": "human_biosphere",
                "feature": "DUAL_REALITY",
                "headline": "Rapid Ebullism & Loss of Consciousness in 15 Seconds",
                "narration": "Below the 6.3 kPa Armstrong limit, exposed moisture on the tongue and eyes begins to boil at body temperature. Unprotected humans lose consciousness within 15 seconds due to oxygen depletion.",
                "telemetry": {"Armstrong Limit": "6.3 kPa", "Consciousness": "~15 Seconds", "Birds & Planes": "Lose All Lift"},
                "hidden_consequence": "Human blood does not boil immediately inside intact blood vessels because arterial pressure keeps it above the Armstrong threshold.",
                "is_hypothetical": True,
            },
            {
                "stage": "3. Short-Term Ocean & Weather Effects",
                "clock": "T + 02:00:00",
                "visual_mode": "ocean_surge",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Surface Oceans Boil Rapidly Until Self-Freezing",
                "narration": "With zero atmospheric pressure holding water molecules down, the top layer of Earth's oceans boils violently, absorbing latent heat until the cooling water freezes into an ice crust.",
                "telemetry": {"Boiling Trigger": "Zero Pressure", "Latent Cooling": "Forms Ice Cap", "Weather/Wind": "Eliminated"},
                "hidden_consequence": "Evaporative cooling removes heat so quickly that a thick layer of ice forms over the oceans, trapping remaining liquid water underneath.",
                "is_hypothetical": True,
            },
            {
                "stage": "4. Long-Term Thermal & Radiation Shift",
                "clock": "T + 30 DAYS",
                "visual_mode": "two_worlds",
                "feature": "TWO_WORLDS",
                "headline": "Greenhouse Blanket Lost & Extreme Day-Night Swings",
                "narration": "Without greenhouse gases trapping heat or ozone blocking ultraviolet radiation, Earth's average temperature drops by 33°C while daytime surfaces suffer lethal UV bombardment.",
                "telemetry": {"Greenhouse Loss": "-33°C Drop", "Day-Night Swing": "> 150°C", "UV Shield": "0% Remaining"},
                "hidden_consequence": "Without atmospheric drag, millions of tiny micrometeorites strike Earth's surface every day at full orbital speed.",
                "is_hypothetical": True,
            },
            {
                "stage": "5. Reality Replay & Subsurface Refuges",
                "clock": "T + 01 YEAR",
                "visual_mode": "reality_replay",
                "feature": "REALITY_REPLAY",
                "headline": "Earth Resembles a Larger, Water-Covered Moon",
                "narration": "Replaying Earth from orbit shows a stark, airless world with sharp shadows, ice-capped oceans, and survival possible only inside pressurized, radiation-shielded habitats.",
                "telemetry": {"Surface Life": "Extinct", "Deep Ocean Life": "Temporarily Intact", "Habitat Need": "Pressurized Domes"},
                "hidden_consequence": "Over geological time, volcanic outgassing would slowly begin rebuilding a secondary atmosphere of carbon dioxide and water vapor.",
                "is_hypothetical": True,
            },
            {
                "stage": "6. Final Scientific Explanation",
                "clock": "SCIENTIFIC REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "Gravity & Earth's Magnetosphere Protect Our Air",
                "narration": "Earth's escape velocity of 11.2 km/s prevents nitrogen and oxygen from escaping to space, while our magnetic field deflects the solar wind that stripped Mars's ancient atmosphere.",
                "telemetry": {"Escape Velocity": "11.2 km/s", "Total Air Mass": "5.15 x 10^18 kg", "Protection": "Gravity + Magnetosphere"},
                "hidden_consequence": "Earth's entire atmosphere weighs roughly 5.15 quintillion kilograms—held firmly in place by gravity.",
                "is_hypothetical": False,
            },
        ],
    },
    "asteroid_impact": {
        "match": ["asteroid", "meteor", "chicxulub", "impact", "comet"],
        "category": "Planetary Defense & Astrophysics",
        "title": "What If a 10-Kilometer Asteroid Approached Earth?",
        "hook": "Sixty-six million years ago, a mountain-sized asteroid traveling at 20 kilometers per second reshaped life on Earth. Here is the exact physics timeline if a similar object were detected today.",
        "scientific_basis": "Kinetic energy equations (E = 1/2 mv²), hypervelocity impact cratering, and NASA DART planetary defense mechanics.",
        "stages": [
            {
                "stage": "1. Initial Detection & Approach",
                "clock": "T - 30 DAYS",
                "visual_mode": "asteroid_trajectory",
                "feature": "REALITY_CLOCK",
                "headline": "10-km Object Locked on Earth Intersection Corridor",
                "narration": "Deep-sky survey telescopes track a 10-kilometer carbonaceous asteroid accelerating under Earth's gravity toward a terminal entry velocity of 20 kilometers per second.",
                "telemetry": {"Diameter": "10.0 km", "Entry Velocity": "20 km/s", "Kinetic Energy": "~100 Million MT"},
                "hidden_consequence": "At 20 km/s, the asteroid crosses the entire thickness of Earth's breathable atmosphere in less than two seconds.",
                "is_hypothetical": True,
            },
            {
                "stage": "2. Immediate Impact Event",
                "clock": "T + 00:00:02",
                "visual_mode": "asteroid_impact",
                "feature": "DUAL_REALITY",
                "headline": "Hypervelocity Flash & 100-km Transient Crater",
                "narration": "Upon contact, kinetic energy converts instantaneously into thermal radiation and shockwaves, vaporizing rock and excavating a crater over 100 kilometers wide within seconds.",
                "telemetry": {"Peak Temp": "> 10,000 K", "Seismic Shock": "Magnitude 11+", "Ejecta Plume": "Sub-Orbital"},
                "hidden_consequence": "Superheated rock droplets launched into sub-orbital trajectories re-enter globally, heating the upper atmosphere intensely.",
                "is_hypothetical": True,
            },
            {
                "stage": "3. Short-Term Global Effects",
                "clock": "T + 48 HOURS",
                "visual_mode": "consequence_map",
                "feature": "CONSEQUENCE_MAP",
                "headline": "Megatsunamis & Continental Wildfire Fronts",
                "narration": "If impact occurs in an ocean basin, kilometer-high waves radiate outward while incandescent ejecta ignites wildfires across continental interiors.",
                "telemetry": {"Tsunami Height": "100m - 1,000m", "Soot Injection": "15,000 Tg", "Sky Brightness": "Twilight at Noon"},
                "hidden_consequence": "Sulfur aerosols vaporized from carbonate-rich crust amplify global cooling far more than dust alone.",
                "is_hypothetical": True,
            },
            {
                "stage": "4. Long-Term Impact Winter",
                "clock": "T + 02 YEARS",
                "visual_mode": "two_worlds",
                "feature": "TWO_WORLDS",
                "headline": "Global Temperature Plunges by 15°C to 20°C",
                "narration": "Stratospheric soot and sulfate aerosols block up to 90 percent of sunlight for years, halting photosynthesis and plunging continents into an extended impact winter.",
                "telemetry": {"Sunlight Drop": "-85% to -90%", "Global Cooling": "-18°C Average", "Agriculture": "Halted"},
                "hidden_consequence": "Acid rain from nitric and sulfuric oxides rapidly acidifies surface ocean waters, stressing marine shell-forming organisms.",
                "is_hypothetical": True,
            },
            {
                "stage": "5. Human Survival & Reality Replay",
                "clock": "T + 10 YEARS",
                "visual_mode": "human_biosphere",
                "feature": "REALITY_REPLAY",
                "headline": "Subterranean Refuges & Chemosynthetic Food Systems",
                "narration": "Replaying the atmospheric aerosol spread demonstrates why early detection decades in advance is critical: only hardened underground reserves and non-solar food systems could sustain survivors.",
                "telemetry": {"Recovery Span": "10+ Years", "Critical Factor": "Early Warning", "Deflection Window": "Decades Prior"},
                "hidden_consequence": "Small burrowing mammals survived the Cretaceous impact largely because soil insulated them from the initial thermal pulse.",
                "is_hypothetical": True,
            },
            {
                "stage": "6. Final Scientific Explanation",
                "clock": "SCIENTIFIC REALITY",
                "visual_mode": "timeline_science",
                "feature": "HIDDEN_DETECTOR",
                "headline": "NASA DART Proved We Can Deflect Asteroids",
                "narration": "In September 2022, NASA's DART spacecraft altered the orbit of asteroid Dimorphos by 33 minutes—proving humanity possesses real kinetic deflection capability.",
                "telemetry": {"DART Mission": "Sept 26, 2022", "Orbit Shift": "-33 Minutes", "10km Threats": "Zero Tracked"},
                "hidden_consequence": "Over 95% of near-Earth asteroids larger than 1 kilometer have already been mapped, and none pose an impact threat this century.",
                "is_hypothetical": False,
            },
        ],
    },
}


class IntelligenceEngine:
    """Converts any user topic into a structured, scientifically grounded multi-scene script."""

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
            source_label = "Built-in NEXUS First-Principles Reasoning Template (Add external references below if desired)"
            raw_stages = [
                {
                    "stage": "1. Initial Event",
                    "clock": "T + 00:00:00",
                    "visual_mode": "solar_system",
                    "feature": "REALITY_CLOCK",
                    "headline": f"Baseline Equilibrium: {clean_topic[:40]}",
                    "narration": f"Every natural system maintains balance until a boundary condition changes. To examine {clean_topic}, we first measure the baseline forces holding reality steady.",
                    "telemetry": {"System State": "Equilibrium", "Primary Driver": "Energy & Mass", "Observation": "Baseline"},
                    "hidden_consequence": "Small shifts in initial boundary conditions often amplify non-linearly across coupled physical systems.",
                    "is_hypothetical": True,
                },
                {
                    "stage": "2. Immediate Consequences",
                    "clock": "T + 00:05:00",
                    "visual_mode": "atmosphere_shock",
                    "feature": "DUAL_REALITY",
                    "headline": "Primary Physical Reaction & Energy Transfer",
                    "narration": "Within the opening moments, conservation of energy and momentum govern how pressure, heat, and motion redistribute across the immediate environment.",
                    "telemetry": {"Response Speed": "Instantaneous", "Energy State": "Disrupted", "Comparison": "Dual View"},
                    "hidden_consequence": "Secondary pressure waves frequently propagate faster through dense media than visible surface changes.",
                    "is_hypothetical": True,
                },
                {
                    "stage": "3. Short-Term Effects",
                    "clock": "T + 24 HOURS",
                    "visual_mode": "consequence_map",
                    "feature": "CONSEQUENCE_MAP",
                    "headline": "Regional & Global Cascade Propagation",
                    "narration": "Over the following hours, localized disruptions propagate through atmospheric circulation, ocean currents, and interconnected infrastructure networks.",
                    "telemetry": {"Propagation": "Global Scale", "Feedback Loop": "Active", "Stability": "Low"},
                    "hidden_consequence": "Interconnected power and communication grids experience cascading latency before physical waves arrive.",
                    "is_hypothetical": True,
                },
                {
                    "stage": "4. Long-Term Effects",
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
                    "stage": "5. Human & Environmental Impact",
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
                    "stage": "6. Final Scientific Explanation",
                    "clock": "SCIENTIFIC REALITY",
                    "visual_mode": "timeline_science",
                    "feature": "HIDDEN_DETECTOR",
                    "headline": "What Verified Science Confirms Today",
                    "narration": "Separating hypothetical modeling from established physical laws shows how finely balanced constants keep our environment habitable.",
                    "telemetry": {"Verification": "First Principles", "Speculation": "Clearly Labeled", "Status": "Verified"},
                    "hidden_consequence": "Controlled scientific simulations allow researchers to test extreme scenarios safely without real-world risk.",
                    "is_hypothetical": False,
                },
            ]

        duration_sec = max(30, min(180, int(duration_sec)))
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
                "duration_sec": max(4.0, sc_dur),
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
# 5. REALITY ENGINE & CINEMATIC ENGINE (MODULES B & D)
# ==============================================================================
class RealityCinematicRenderer:
    """Procedural NumPy + Pillow scientific visualization and NEXUS HUD engine."""

    PALETTES = {
        "nexus_obsidian": {
            "bg_top": (8, 15, 32),
            "bg_bot": (3, 5, 12),
            "cyan": (34, 211, 238),
            "violet": (168, 85, 247),
            "amber": (251, 191, 36),
            "crimson": (244, 63, 94),
            "emerald": (16, 185, 129),
            "panel": (11, 18, 36),
            "text": (241, 245, 249),
            "muted": (148, 163, 184),
        },
        "deep_cosmos": {
            "bg_top": (15, 10, 36),
            "bg_bot": (2, 3, 9),
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
            "bg_top": (24, 15, 30),
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
        self.sc = min(width, height) / 720.0
        self.pal = self.PALETTES.get(style_key, self.PALETTES["nexus_obsidian"])

        rng = np.random.default_rng(42)
        self.stars = np.column_stack([
            rng.uniform(0, width, 110),
            rng.uniform(0, height, 110),
            rng.uniform(0.6, 2.4, 110),
            rng.uniform(0.3, 1.0, 110),
        ])
        self._bg_cache = self._build_base_backdrop()

    def _build_base_backdrop(self) -> Image.Image:
        yy, xx = np.ogrid[0 : self.h, 0 : self.w]
        nx = (xx - self.w * 0.5) / (self.w * 0.72)
        ny = (yy - self.h * 0.42) / (self.h * 0.72)
        radial = np.clip(1.0 - 0.78 * (nx * nx + ny * ny), 0.0, 1.0).astype(np.float32)

        arr = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        for c in range(3):
            vals = self.pal["bg_bot"][c] + (self.pal["bg_top"][c] - self.pal["bg_bot"][c]) * radial
            arr[:, :, c] = np.clip(vals, 0, 255).astype(np.uint8)

        img = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(img)

        step = max(36, int(64 * self.sc))
        grid_col = (18, 28, 52)
        for gx in range(0, self.w, step):
            draw.line([(gx, 0), (gx, self.h)], fill=grid_col, width=1)
        for gy in range(0, self.h, step):
            draw.line([(0, gy), (self.w, gy)], fill=grid_col, width=1)
        return img

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
            rr = radius + ring * 5 * self.sc
            draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=halo_col, width=1)

        ocean_col = (48, 20, 44) if perturbed else (14, 62, 118)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=ocean_col, outline=halo_col, width=2)

        land_col = (185, 85, 12) if perturbed else (20, 148, 118)
        for i in range(5):
            ang = phase * (0.2 if perturbed else 1.8) + i * 1.25
            lx = cx + math.sin(ang) * radius * 0.48
            ly = cy + math.cos(i * 1.1) * radius * 0.44
            rw = radius * (0.34 - 0.03 * i) * abs(math.cos(ang * 0.7))
            rh = radius * 0.22
            if rw > 3:
                draw.ellipse([lx - rw, ly - rh, lx + rw, ly + rh], fill=land_col)

        draw.pieslice(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            start=285,
            end=75,
            fill=(5, 9, 20),
        )
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=halo_col, width=2)

        tilt = math.radians(38.0 * math.sin(phase * 2.0) if perturbed else 23.5)
        ax_len = radius * 1.38
        dx = math.sin(tilt) * ax_len
        dy = -math.cos(tilt) * ax_len
        draw.line([(cx - dx, cy - dy), (cx + dx, cy + dy)], fill=self.pal["amber"], width=2)

        if label:
            f_lbl = get_font(max(12, int(15 * self.sc)), bold=True)
            bb = draw.textbbox((0, 0), label, font=f_lbl)
            draw.text((cx - (bb[2] - bb[0]) / 2, cy + radius + 12 * self.sc), label, font=f_lbl, fill=halo_col)

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

        for sx, sy, sr, spd in self.stars:
            px = x0 + ((sx + global_t * 14.0 * spd) % vw)
            py = y0 + ((sy + global_t * 4.0 * spd) % vh)
            r_px = max(1, int(sr * self.sc))
            draw.ellipse([px - r_px, py - r_px, px + r_px, py + r_px], fill=(140, 175, 220))

        # DUAL REALITY & TWO WORLDS SIDE-BY-SIDE COMPARISON
        if feature in ("DUAL_REALITY", "TWO_WORLDS") or mode in ("atmosphere_shock", "two_worlds"):
            mid_x = int(cx)
            draw.line([(mid_x, y0 + 16), (mid_x, y1 - 16)], fill=self.pal["violet"], width=2)
            r_p = min(vw * 0.17, vh * 0.28)
            self._draw_planet(draw, (x0 + mid_x) * 0.5, cy, r_p, global_t * 1.4, perturbed=False, label="NORMAL REALITY")
            self._draw_planet(draw, (mid_x + x1) * 0.5, cy, r_p, global_t * 0.3, perturbed=True, label="ALTERED SCENARIO")

            for k in range(6):
                wy = cy - r_p + k * (2 * r_p / 5.0)
                wx_start = mid_x + 18 + ((global_t * 180 + k * 35) % max(40, int(vw * 0.38)))
                draw.line([(wx_start, wy), (wx_start + 38 * self.sc, wy)], fill=self.pal["crimson"], width=2)

        # CONSEQUENCE MAP
        elif feature == "CONSEQUENCE_MAP" or mode in ("ocean_surge", "consequence_map"):
            r_globe = min(vw, vh) * 0.31
            self._draw_planet(draw, cx, cy, r_globe, global_t * 0.8, perturbed=True, label="GLOBAL CASCADE TELEMETRY")
            pulse_r = r_globe * (0.3 + (prog * 1.1) % 1.0)
            draw.ellipse([cx - pulse_r, cy - pulse_r, cx + pulse_r, cy + pulse_r], outline=self.pal["amber"], width=2)
            nodes = [
                (-0.45, -0.35, "EQUATORIAL ZONE"),
                (0.45, -0.30, "NORTHERN BASIN"),
                (-0.42, 0.35, "COASTAL GRID"),
                (0.42, 0.35, "ATMOSPHERIC JET"),
            ]
            f_nd = get_font(max(11, int(13 * self.sc)), bold=True)
            for nx_f, ny_f, n_lbl in nodes:
                nx = cx + nx_f * vw * 0.68
                ny = cy + ny_f * vh * 0.62
                draw.line([(cx, cy), (nx, ny)], fill=self.pal["cyan"], width=1)
                draw.ellipse([nx - 5, ny - 5, nx + 5, ny + 5], fill=self.pal["amber"])
                draw.text((nx - 45 * self.sc, ny + 8), n_lbl, font=f_nd, fill=self.pal["text"])

        # ASTEROID TRAJECTORY & IMPACT
        elif mode in ("asteroid_trajectory", "asteroid_impact"):
            r_earth = min(vw, vh) * 0.24
            ex = x1 - vw * 0.28
            ey = cy + vh * 0.06
            self._draw_planet(draw, ex, ey, r_earth, global_t, perturbed=(mode == "asteroid_impact"), label="EARTH")

            ax = x0 + vw * (0.15 + 0.54 * ease_in_out(prog))
            ay = y0 + vh * (0.22 + 0.32 * ease_in_out(prog))
            draw.line([(x0 + vw * 0.12, y0 + vh * 0.20), (ex, ey)], fill=self.pal["crimson"], width=2)
            ar = max(8, int(16 * self.sc))
            draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=self.pal["amber"], outline=(255, 255, 255), width=2)
            if mode == "asteroid_impact" and prog > 0.55:
                blast = r_earth * (prog - 0.55) * 3.2
                draw.ellipse([ex - blast, ey - blast, ex + blast, ey + blast], outline=self.pal["amber"], width=3)

        # SOLAR SYSTEM & ORBITAL MECHANICS
        elif mode == "solar_system":
            sun_r = min(vw, vh) * 0.11
            draw.ellipse([cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r], fill=self.pal["amber"])
            for orb_i, mult in enumerate([0.24, 0.36, 0.48], start=1):
                rx = vw * mult
                ry = vh * mult * 0.62
                draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(45, 65, 105), width=1)
                ang = global_t * (1.8 / orb_i) + orb_i
                px = cx + math.cos(ang) * rx
                py = cy + math.sin(ang) * ry
                pr = max(5, int((9 - orb_i) * self.sc))
                col = self.pal["cyan"] if orb_i == 2 else self.pal["violet"]
                draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=col)

        # BIOSPHERE & REALITY REPLAY
        elif mode in ("human_biosphere", "reality_replay"):
            pts = []
            steps = 70
            for i in range(steps):
                t_norm = i / float(steps - 1)
                px = x0 + 24 + t_norm * (vw - 48)
                freq = 18.0 if scene["is_hypothetical"] else 9.0
                amp = vh * (0.22 * (1.0 - 0.4 * prog) if scene["is_hypothetical"] else 0.16)
                py = cy + math.sin(t_norm * freq + global_t * 6.0) * amp * math.sin(t_norm * math.pi)
                pts.append((px, py))
            draw.line(pts, fill=self.pal["emerald"] if not scene["is_hypothetical"] else self.pal["cyan"], width=3)
            ret_r = min(vw, vh) * 0.26
            draw.ellipse([cx - ret_r, cy - ret_r, cx + ret_r, cy + ret_r], outline=self.pal["violet"], width=2)

        # DEFAULT: EARTH ROTATION & SCIENTIFIC TIMELINE
        else:
            r_p = min(vw, vh) * 0.30
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
        pad = int(28 * self.sc)

        # 1. TOP HUD HEADER + REALITY CLOCK
        top_h = int(52 * self.sc)
        draw.rounded_rectangle(
            [pad, pad, self.w - pad, pad + top_h],
            radius=10,
            fill=self.pal["panel"],
            outline=self.pal["cyan"],
            width=2,
        )

        f_brand = get_font(max(13, int(17 * self.sc)), bold=True)
        f_clock = get_font(max(13, int(18 * self.sc)), bold=True)

        tag_txt = "HYPOTHETICAL SIMULATION" if scene["is_hypothetical"] else "VERIFIED SCIENTIFIC DATA"
        tag_col = self.pal["amber"] if scene["is_hypothetical"] else self.pal["emerald"]
        draw.text((pad + 14, pad + int(15 * self.sc)), f"NEXUS // {tag_txt}", font=f_brand, fill=tag_col)

        clock_txt = f"REALITY CLOCK: {scene['clock']}"
        cb = draw.textbbox((0, 0), clock_txt, font=f_clock)
        draw.text((self.w - pad - (cb[2] - cb[0]) - 14, pad + int(14 * self.sc)), clock_txt, font=f_clock, fill=self.pal["cyan"])

        # 2. MAIN SIMULATION VIEWPORT & TELEMETRY SIDEBAR
        vp_top = pad + top_h + int(14 * self.sc)
        bot_h = int(168 * self.sc) if not self.is_vertical else int(240 * self.sc)
        vp_bot = self.h - pad - bot_h - int(14 * self.sc)

        if self.is_vertical:
            vp_box = (pad, vp_top, self.w - pad, vp_bot)
            draw.rounded_rectangle(vp_box, radius=14, fill=(6, 10, 22), outline=self.pal["violet"], width=2)
            self._draw_reality_visual(draw, scene, scene_prog, global_t, vp_box)
        else:
            side_w = int(self.w * 0.27)
            vp_box = (pad, vp_top, self.w - pad - side_w - int(12 * self.sc), vp_bot)
            side_box = (self.w - pad - side_w, vp_top, self.w - pad, vp_bot)

            draw.rounded_rectangle(vp_box, radius=14, fill=(6, 10, 22), outline=self.pal["violet"], width=2)
            self._draw_reality_visual(draw, scene, scene_prog, global_t, vp_box)

            draw.rounded_rectangle(side_box, radius=14, fill=self.pal["panel"], outline=self.pal["cyan"], width=2)
            f_sh = get_font(max(12, int(15 * self.sc)), bold=True)
            f_sv = get_font(max(12, int(14 * self.sc)), bold=False)
            sy = side_box[1] + int(14 * self.sc)
            draw.text((side_box[0] + 14, sy), f"STAGE {scene_idx + 1}/{total_scenes} TELEMETRY", font=f_sh, fill=self.pal["cyan"])
            sy += int(28 * self.sc)

            for k_lbl, v_lbl in scene.get("telemetry", {}).items():
                draw.text((side_box[0] + 14, sy), f"{k_lbl}:", font=f_sv, fill=self.pal["muted"])
                sy += int(18 * self.sc)
                draw.text((side_box[0] + 14, sy), str(v_lbl), font=f_sh, fill=self.pal["text"])
                sy += int(26 * self.sc)

            # HIDDEN CONSEQUENCE DETECTOR
            if scene_prog >= 0.45 and sy + 60 < side_box[3]:
                sy += int(6 * self.sc)
                draw.line([(side_box[0] + 12, sy), (side_box[2] - 12, sy)], fill=self.pal["amber"], width=1)
                sy += int(10 * self.sc)
                draw.text((side_box[0] + 14, sy), "HIDDEN CONSEQUENCE:", font=f_sh, fill=self.pal["amber"])
                sy += int(22 * self.sc)
                hc_lines = wrap_text(draw, scene.get("hidden_consequence", ""), f_sv, side_w - 28)
                for h_ln in hc_lines[:5]:
                    if sy + 18 < side_box[3]:
                        draw.text((side_box[0] + 14, sy), h_ln, font=f_sv, fill=self.pal["text"])
                        sy += int(18 * self.sc)

        # 3. BOTTOM NARRATION DECK & TIMELINE PROGRESS BAR
        deck_top = vp_bot + int(14 * self.sc)
        deck_bot = self.h - pad
        draw.rounded_rectangle(
            [pad, deck_top, self.w - pad, deck_bot],
            radius=14,
            fill=self.pal["panel"],
            outline=self.pal["cyan"],
            width=2,
        )

        f_head = get_font(max(16, int(23 * self.sc)), bold=True)
        f_nar = get_font(max(13, int(17 * self.sc)), bold=False)

        hy = deck_top + int(14 * self.sc)
        draw.text((pad + 18, hy), f"{scene['stage']} — {scene['headline']}", font=f_head, fill=self.pal["cyan"])

        ny = hy + int(34 * self.sc)
        nar_lines = wrap_text(draw, scene["narration"], f_nar, (self.w - 2 * pad) - 36)
        for n_ln in nar_lines[:4]:
            draw.text((pad + 18, ny), n_ln, font=f_nar, fill=self.pal["text"])
            ny += int(24 * self.sc)

        bar_y = deck_bot - int(16 * self.sc)
        bar_w = (self.w - 2 * pad) - 36
        draw.rounded_rectangle([pad + 18, bar_y, pad + 18 + bar_w, bar_y + int(7 * self.sc)], radius=4, fill=(25, 36, 64))
        fill_w = max(8, int(bar_w * overall_prog))
        draw.rounded_rectangle([pad + 18, bar_y, pad + 18 + fill_w, bar_y + int(7 * self.sc)], radius=4, fill=self.pal["violet"])

        return frame.tobytes()


# ==============================================================================
# 6. VOICE & AUDIO ENGINE (MODULE E)
# ==============================================================================
class VoiceAudioEngine:
    @staticmethod
    def build_ambient_bed(total_dur: float, out_wav: str, volume: float = 0.25):
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
                    0.45 * np.sin(2.0 * np.pi * 55.0 * t)
                    + 0.30 * np.sin(2.0 * np.pi * 82.41 * t)
                    + 0.25 * np.sin(2.0 * np.pi * 110.0 * t + 0.4 * np.sin(2.0 * np.pi * 0.25 * t))
                )
                pulse = np.exp(-((t % 2.0) * 6.0)) * np.sin(2.0 * np.pi * 440.0 * t) * 0.14
                sig = (drone + pulse) * max(0.0, min(1.0, volume)) * 0.35
                pcm = (np.clip(sig, -0.95, 0.95) * 32767.0).astype(np.int16)
                wf.writeframes(pcm.tobytes())
                cursor += dur
                gc.collect()

    @staticmethod
    def synthesize_narration_track(
        scenes: List[Dict[str, Any]],
        total_dur: float,
        work_dir: str,
        speed_wpm: int = 155,
        voice_variant: str = "en-us",
    ) -> Tuple[Optional[str], str]:
        if not ESPEAK_BIN or not HAS_FFMPEG:
            return None, "Offline TTS engine not installed; using ambient telemetry audio."

        speed_wpm = max(110, min(210, int(speed_wpm)))
        voice = voice_variant if voice_variant in ("en-us", "en-gb", "en-au", "en") else "en-us"

        concat_parts: List[str] = []
        cursor = 0.0

        for idx, sc in enumerate(scenes):
            sc_start = float(sc["start_sec"])
            sc_dur = float(sc["duration_sec"])
            if sc_start > cursor:
                gap_wav = os.path.join(work_dir, f"gap_{idx}.wav")
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                        "-t", f"{sc_start - cursor:.3f}", gap_wav,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if os.path.exists(gap_wav):
                    concat_parts.append(gap_wav)
                cursor = sc_start

            raw_tts = os.path.join(work_dir, f"tts_raw_{idx}.wav")
            padded_tts = os.path.join(work_dir, f"tts_pad_{idx}.wav")
            text = f"{sc['headline']}. {sc['narration']}"

            subprocess.run(
                [ESPEAK_BIN, "-v", voice, "-s", str(speed_wpm), "-w", raw_tts, text],
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
                        "-ar", "22050", "-ac", "1", padded_tts,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if os.path.exists(padded_tts):
                    concat_parts.append(padded_tts)
                    cursor += sc_dur

        if not concat_parts:
            return None, "TTS synthesis returned no audio; using ambient audio."

        list_txt = os.path.join(work_dir, "tts_concat.txt")
        with open(list_txt, "w", encoding="utf-8") as fh:
            for p in concat_parts:
                fh.write(f"file '{p}'\n")

        full_tts = os.path.join(work_dir, "full_narration.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt, "-c", "copy", full_tts],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        if os.path.exists(full_tts) and os.path.getsize(full_tts) > 500:
            return full_tts, f"Offline English TTS synthesized ({voice}, {speed_wpm} WPM)."
        return None, "TTS concat failed; using ambient telemetry audio."


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
            return False, "QC Failed: Output MP4 file is corrupted or empty (<10 KB).", {}

        meta: Dict[str, Any] = {"size_mb": round(size_bytes / (1024.0 * 1024.0), 2)}
        if not HAS_FFPROBE:
            return True, "QC Passed (file size verified; ffprobe unavailable).", meta

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
                return False, "QC Failed: No video stream found in MP4.", meta
            vw = int(v_streams[0].get("width", 0))
            vh = int(v_streams[0].get("height", 0))
            if vw != expected_w or vh != expected_h:
                return False, f"QC Failed: Dimension mismatch ({vw}x{vh} != {expected_w}x{expected_h}).", meta
            if dur <= 1.0:
                return False, "QC Failed: Invalid video duration.", meta

            meta["duration_sec"] = round(dur, 1)
            meta["resolution"] = f"{vw}x{vh}"
            meta["has_audio"] = len(a_streams) > 0
            return True, f"QC Verified: {vw}x{vh}, {dur:.1f}s, {meta['size_mb']} MB", meta
        except Exception as exc:
            return False, f"QC Probe Error: {exc}", meta


# ==============================================================================
# 8. TURBO RENDER ENGINE (MODULE F)
# ==============================================================================
def get_dimensions(aspect_ratio: str, resolution: str) -> Tuple[int, int]:
    is_1080 = resolution == "1080p"
    if aspect_ratio == "9:16":
        return (1080, 1920) if is_1080 else (720, 1280)
    if aspect_ratio == "1:1":
        return (1080, 1080) if is_1080 else (720, 720)
    return (1920, 1080) if is_1080 else (1280, 720)


def update_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def run_render_pipeline(job_id: str, payload: Dict[str, Any]):
    work_dir = tempfile.mkdtemp(prefix=f"nexus_{job_id}_", dir=TMP_ROOT)
    try:
        if not HAS_FFMPEG:
            raise RuntimeError("FFmpeg is not installed on the host container.")

        script_data = payload["script"]
        scenes = script_data.get("scenes", [])
        if not scenes:
            raise ValueError("Cannot render: script contains zero scenes.")

        aspect_ratio = payload.get("aspect_ratio", "16:9")
        resolution = payload.get("resolution", "720p")
        fps = max(12, min(30, int(payload.get("fps", 20))))
        visual_style = payload.get("visual_style", "nexus_obsidian")
        voice_mode = payload.get("voice_mode", "offline_tts")
        voice_variant = payload.get("voice_variant", "en-us")
        speaking_speed = int(payload.get("speaking_speed", 155))
        music_volume = float(payload.get("music_volume", 0.25))
        uploaded_audio_id = payload.get("uploaded_audio_id", "")

        out_w, out_h = get_dimensions(aspect_ratio, resolution)
        total_dur = sum(float(s["duration_sec"]) for s in scenes)

        update_job(job_id, status="running", progress=5, current_scene="Audio & Voice Synthesis")

        ambient_wav = os.path.join(work_dir, "ambient.wav")
        VoiceAudioEngine.build_ambient_bed(total_dur, ambient_wav, volume=music_volume)

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
            narration_wav, audio_note = VoiceAudioEngine.synthesize_narration_track(
                scenes, total_dur, work_dir, speed_wpm=speaking_speed, voice_variant=voice_variant
            )

        master_wav = os.path.join(work_dir, "master_audio.wav")
        if narration_wav and os.path.exists(narration_wav):
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", narration_wav,
                    "-i", ambient_wav,
                    "-filter_complex", "[0:a]volume=1.0[v];[1:a]volume=0.45[m];[v][m]amix=inputs=2:duration=longest[out]",
                    "-map", "[out]", "-t", f"{total_dur:.3f}",
                    "-ar", "22050", "-ac", "1", master_wav,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        else:
            shutil.copyfile(ambient_wav, master_wav)

        renderer = RealityCinematicRenderer(out_w, out_h, style_key=visual_style)
        dt = 1.0 / float(fps)
        global_t = 0.0
        total_scenes = len(scenes)
        chunk_paths: List[str] = []

        for idx, sc in enumerate(scenes):
            sc_dur = float(sc["duration_sec"])
            n_frames = max(1, int(round(sc_dur * fps)))
            chunk_mp4 = os.path.join(work_dir, f"chunk_{idx:02d}.mp4")

            update_job(
                job_id,
                progress=12 + int((idx / float(total_scenes)) * 74),
                current_scene=f"Scene {idx + 1}/{total_scenes}: {sc['headline']}",
            )

            ffmpeg_chunk = [
                "ffmpeg", "-y",
                "-f", "rawvideo", "-vcodec", "rawvideo",
                "-s", f"{out_w}x{out_h}", "-pix_fmt", "rgb24",
                "-r", str(fps), "-i", "-",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
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

        update_job(job_id, progress=90, current_scene="Combining Chunks & Muxing Master Audio")
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
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "ok",
        "service": "NEXUS Worldwide Reality Video Generator",
        "ffmpeg": HAS_FFMPEG,
        "ffprobe": HAS_FFPROBE,
        "offline_tts": HAS_ESPEAK,
        "tts_engine": ESPEAK_BIN or "none",
        "active_jobs": len(ACTIVE_JOB_IDS),
        "queue_note": "In-memory job queue active; completed MP4 files persist in /app/output.",
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

    if duration_sec < 30 or duration_sec > 180:
        return jsonify({"error": "Duration must be between 30 and 180 seconds in v1.0."}), 400

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
            "current_scene": "Queued for TURBO Render Engine",
            "error": None,
        }

    payload = {
        "script": script,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "fps": data.get("fps", 20),
        "visual_style": data.get("visual_style", "nexus_obsidian"),
        "voice_mode": data.get("voice_mode", "offline_tts"),
        "voice_variant": data.get("voice_variant", "en-us"),
        "speaking_speed": data.get("speaking_speed", 155),
        "music_volume": data.get("music_volume", 0.25),
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
