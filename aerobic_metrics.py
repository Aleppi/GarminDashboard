"""Aerobic Efficiency and Durability, computed from raw activity streams.

Pure functions only — no Garmin calls, no storage — so the whole method can
be unit-tested on synthetic streams (tests/test_aerobic_metrics.py) and is
documented in one place (docs/metrics.md). aerobic_sync.py does the fetching
and caching around it.

Pipeline per activity and series (see docs/metrics.md for the reasoning):
  Garmin details stream -> 1 Hz grid -> per-second output signal (power,
  grade-adjusted speed, or walking metabolic power) -> drop stops and the
  first 10 min -> 5-min chunks -> keep steady, aerobic chunks that come in
  runs of >= 2 -> one (output, HR) point per chunk.
Long, non-interval sessions also get a first-half vs second-half decoupling.
Weekly, a robust HR = a + b*output fit over the trailing 28 days turns the
points into "output at a reference HR".
"""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Optional

# Bump whenever a change below would alter cached results: the sync job
# reprocesses every activity whose stored algo_version is older.
ALGO_VERSION = 9  # 2: outdoor-bike VI 1.15, coasting counts as moving, interval sessions excluded
                  # 3: durability by per-half regression (NP-EF fallback), outdoor-bike VI 1.20
                  # 4: outdoor-bike chunk VI 1.10, per-half VI check on NP-EF, ski touring
                  #    (uphill only) and snowshoeing in walk_outdoor
                  # 5: chunk HR trend (|slope| <= 1 bpm/min) instead of HR spread; walking HR
                  #    floor 30% HRR
                  # 6: HR trend limit 2 bpm/min, walking output CV limit 15%
                  # 7: chunk output = NP for power series; post-effort chunk exclusion
                  # 8: no efficiency chunks from interval sessions (any length)
                  # 9: drop chunks after a > 10% output drop and in the last 5 min

WARMUP_S = 600                 # dropped from the start of each activity (HR settling)
CHUNK_S = 300                  # one data point per 5 min of steady effort
MIN_CHUNKS_IN_RUN = 2          # steady segment = >= 2 consecutive valid chunks (>= 10 min)
STOP_MIN_S = 5                 # zero output for longer than this counts as stopped
SEGMENT_BREAK_S = 30           # a stop/pause this long splits a steady segment
FILL_MAX_GAP_S = 10            # samples further apart than this = timer paused, not smart recording
HR_BAND_LO = 0.65              # aerobic band, fraction of HRmax...
HR_BAND_HI = 0.85              # ...capped at LTHR when that's lower
CHUNK_HR_TREND_MAX = 2.0       # bpm/min; |slope| of 30 s-smoothed HR across the chunk. Steeper = HR
                               # still settling or drifting (a transition). A trend, not a spread: on
                               # rolling terrain HR oscillates +-5-15 bpm around a flat level, which is
                               # steady for our purposes. 1.0 rejected most chunks after any effort
                               # change (HR needs 2-3 min to settle), indoors too: 2.0 keeps those
                               # while still rejecting a chunk that's mid-transition.
CHUNK_VI_MAX = 1.05            # power: NP / avg power (inclusive)
# Outdoor loops with small rollers move power around even at a steady
# effort; 1.05 left 0-4 chunks per half on every long outdoor ride.
CHUNK_VI_MAX_BY_SERIES = {"bike_outdoor": 1.10}
CHUNK_CV_MAX = 0.05            # speed-based: CV of the 30 s-smoothed output
# Trail walking changes pace constantly (steps, roots, gradient) even at a
# steady effort: 5% rejected every hike chunk; 15% keeps the steady stretches.
CHUNK_CV_MAX_BY_SPORT = {"walking": 0.15}
CHUNK_COVERAGE_MIN = 0.95      # share of seconds with both HR and output present
# Post-effort exclusion: HR stays raised for minutes after a hard effort,
# so a "steady" chunk right after one reads too much HR for its output
# (e.g. recoveries in an interval ride). A chunk is dropped when an effort
# falls in the POST_EFFORT_S before it starts, or inside it: 30 s rolling
# power > POST_EFFORT_FTP_FRAC x FTP (power + FTP known), or HR > LTHR.
POST_EFFORT_S = 300
POST_EFFORT_FTP_FRAC = 1.20
# HR lags a drop in output by minutes, so the chunk after one (typically a
# cool-down step) pairs the lower output with a still-raised HR. A chunk whose
# mean output is more than OUTPUT_DROP_MAX below the preceding chunk's (same
# activity) is dropped, as is any chunk overlapping the last END_EXCLUDE_S of
# moving time (the cool-down itself).
OUTPUT_DROP_MAX = 0.10
END_EXCLUDE_S = 300
SMOOTH_S = 30                  # rolling window for NP / CV
GRADE_WINDOW_M = 25.0          # horizontal distance a gradient is measured over
GRADE_MIN_SPAN_M = 5.0         # below this the athlete is basically stationary: grade 0
GRADE_CLAMP = 0.45             # Minetti's polynomials are fitted on -0.45..+0.45
ALT_MEDIAN_S = 5
ALT_MEAN_S = 30
RUN_STOP_SPEED = 1.0           # m/s
WALK_STOP_SPEED = 0.3          # m/s — steep hiking can be slow but still moving
DURABILITY_MIN_MOVING_S = 90 * 60
DURABILITY_VI_MAX = 1.10       # whole-session variability limit, inclusive (excludes interval sessions)
# Outdoor riding varies power with terrain even when steady: hilly but
# steady rides measured 1.10-1.15, against 1.00-1.02 for indoor ERG. The
# per-half regression below compares like with like, so outdoor rides can
# be let in up to 1.20.
DURABILITY_VI_MAX_BY_SERIES = {"bike_outdoor": 1.20}
DURABILITY_MIN_CHUNKS_PER_HALF = 4   # valid 5-min chunks needed in each half for the regression method
DURABILITY_OVERLAP_TOL = 0.02        # halves' chunk output ranges may miss each other by 2% of the median
DURABILITY_NP_MAX_DIFF = 0.10        # NP-EF fallback only when the halves' NP differ by <= 10%...
DURABILITY_HALF_VI_MAX_DIFF = 0.05   # ...and their VIs by <= 0.05: a surgier half inflates its NP
UPHILL_MIN_GRADE = 0.05              # ski touring: only seconds climbing steeper than +5% count
MOVING_SPEED = 1.0             # m/s; a coasting rider (zero power) is still moving
DURABILITY_COVERAGE_MIN = 0.90
WINDOW_DAYS = 28
# Shared-slope model: one HR-vs-output slope b per user and sport (cycling
# pools indoor + outdoor; running; walking) and, per series and 28-day
# window, only an intercept a_w = median(HR - b*output). A slope fitted per
# window rests on a handful of sessions and swung the outdoor value
# 155 <-> 210 W.
# The slope is a WITHIN-session estimate, first-difference form: Theil-Sen
# over pairs of chunks from the same activity that start <= 20 min apart.
# Comparing chunks across sessions mixes in heat, fatigue, day form and the
# indoor/outdoor offset (the pooled fit gave 0.235 bpm/W). Demeaning each
# activity removes those but not cardiac drift *within* a long session:
# 2.5 h at a constant 154 W with HR drifting 124 -> 137 then a cool-down
# gave that ride a slope of -0.11 and, with 30 chunks, dragged the
# estimate to 0.21. Over <= 20 min drift is ~1 bpm, so time-local pairs
# measure HR's response to output, not to time.
SLOPE_DAYS = 180
SLOPE_ACTIVITY_MIN_CHUNKS = 3   # an activity contributes only with >= 3 valid chunks...
SLOPE_ACTIVITY_MIN_RANGE = 0.15 # ...spanning >= 15% of its median output
SLOPE_PAIR_MIN_DOUT = 0.05     # pairs need |delta output| >= 5% of the activity's median output (else noise)
SLOPE_PAIR_MAX_GAP_S = 20 * 60 # pairs only between chunks starting <= 20 min apart (drift ~1 bpm)
SLOPE_MIN_POINTS = 8           # pairs; fewer, or fewer than SLOPE_MIN_ACTIVITIES contributing: population default
SLOPE_MIN_ACTIVITIES = 2
SLOPE_MIN_PAIRS = 8
SLOPE_FULL_POINTS = 40         # pairs; from here the fitted slope is used as-is, below shrunk toward the default
DEFAULT_SLOPE = {"W": 0.30, "m/min": 0.40, "W/kg": 6.0}
# Confidence from the window's own data: points and the median absolute
# residual (bpm) around the shared-slope line.
CONF_HIGH = (15, 3.0)          # >= 15 points and MAD <= 3 bpm
CONF_MEDIUM = (8, 5.0)         # >= 8 points and MAD <= 5 bpm; anything else is low
# Physiologically plausible HR-vs-output slopes (bpm per output unit). A
# fitted sport slope outside them is noise, not physiology, and is replaced
# by DEFAULT_SLOPE (b = 0.15 bpm/W would turn a 10 bpm error into 67 W). Bike is the
# anchor: 0.2-0.6 bpm/W. Converted through metabolic cost (~23% cycling
# efficiency, 55-85 kg riders), that's ~2.5-12 bpm per W/kg of metabolic
# power — the walking bound — and, at Minetti's 3.6 J/kg/m flat running
# cost (0.06 W/kg per m/min), ~0.15-0.75 bpm per m/min (2.5-12.5 bpm per
# km/h) for running speed / GAS.
SLOPE_BOUNDS = {"W": (0.2, 0.6), "m/min": (0.15, 0.75), "W/kg": (2.5, 12.0)}
HEAT_DOWNWEIGHT_C = 25.0       # outdoor points above this count half
HEAT_CORRECTION_BASE_C = 20.0  # optional HR correction: -1 bpm per degree above this
HEAT_CORRECTION_BPM_PER_C = 1.0
REF_HR_FRACTION = 0.65         # HRref = rest + 0.65 * (max - rest)
# Walking and hiking sit far below running/cycling HR: slow walking on
# gentle grades is ~80-125 bpm, under 65% of HRmax. For the walking sport
# the band's floor is 30% of HR reserve instead (above that, HR still
# tracks VO2 linearly), and the reference HR is 40% of HR reserve so it
# sits inside the HR range hikes actually produce.
WALK_HR_FLOOR_HRR = 0.30
WALK_HR_FLOOR_FALLBACK = 0.50  # fraction of HRmax when resting HR is unknown
REF_HR_FRACTION_BY_SPORT = {"walking": 0.40}
# Running: the reference HR is the athlete's own typical easy-run HR, not a
# % of reserve. Zones compress very differently: a fit runner's easy pace
# sits far below threshold, while an untrained one is in Z3 as soon as they
# run at all, so a fixed 65% of reserve can sit 10+ bpm below every run they
# do (and the headline becomes an extrapolation). Median of each run's
# median steady-chunk HR over EASY_REF_DAYS; needs EASY_REF_MIN_ACTIVITIES
# runs, else the % formula; only moves when it shifts >= EASY_REF_UPDATE_BPM
# (every week of history is re-read at the same reference, so a move only
# relabels the line, but it shouldn't flicker).
EASY_REF_SPORTS = {"running", "walking"}
EASY_REF_DAYS = 180
EASY_REF_MIN_ACTIVITIES = 3
EASY_REF_UPDATE_BPM = 3


# ---------------------------------------------------------------- Minetti --

def _clamp_grade(i: float) -> float:
    return max(-GRADE_CLAMP, min(GRADE_CLAMP, i))


def minetti_run_cost(i: float) -> float:
    """Energy cost of running, J/kg/m, at gradient i (fraction). Minetti et al. 2002."""
    i = _clamp_grade(i)
    return 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6


def minetti_walk_cost(i: float) -> float:
    """Energy cost of walking, J/kg/m, at gradient i (fraction). Minetti et al. 2002."""
    i = _clamp_grade(i)
    return 280.5 * i**5 - 58.7 * i**4 - 76.8 * i**3 + 51.9 * i**2 + 19.6 * i + 2.5


RUN_COST_FLAT = minetti_run_cost(0.0)


# ----------------------------------------------------------------- series --
# One series per sport x signal x indoor/outdoor, in display order (Run
# before Bike, matching the VO2 max tiles). Indoor and outdoor never
# mix: no wind/heat indoors, and treadmill/trainer speed is measured
# differently from GPS.

SERIES: dict[str, dict[str, Any]] = {
    "run_outdoor": {"sport": "running", "signal": "gas", "indoor": False, "unit": "m/min", "label": "Run"},
    "run_indoor": {"sport": "running", "signal": "speed", "indoor": True, "unit": "m/min", "label": "Treadmill"},
    "bike_outdoor": {"sport": "cycling", "signal": "power", "indoor": False, "unit": "W", "label": "Bike"},
    "bike_indoor": {"sport": "cycling", "signal": "power", "indoor": True, "unit": "W", "label": "Bike"},
    "walk_outdoor": {"sport": "walking", "signal": "walk_metab", "indoor": False, "unit": "W/kg", "label": "Hike/Walk"},
}

# Which of Garmin's per-sport HR-zone settings applies to each sport.
ZONE_SPORT = {"cycling": "CYCLING", "running": "RUNNING", "walking": "DEFAULT"}

CYCLING_OUTDOOR_TYPES = {
    "cycling", "road_biking", "mountain_biking", "gravel_cycling", "cyclocross",
    "track_cycling", "recumbent_cycling", "hand_cycling", "bmx", "downhill_biking",
}
CYCLING_INDOOR_TYPES = {"virtual_ride", "indoor_cycling"}
RUN_OUTDOOR_TYPES = {"running", "trail_running", "track_running", "street_running", "ultra_run", "obstacle_run"}
RUN_INDOOR_TYPES = {"treadmill_running", "indoor_running", "virtual_run"}
WALK_OUTDOOR_TYPES = {
    "walking", "casual_walking", "speed_walking", "hiking", "rucking", "mountaineering", "snow_shoe_ws",
    "backcountry_skiing", "backcountry_snowboarding", "backcountry_skiing_snowboarding_ws",
}
# Ski touring / splitboarding: the climbs are walking-like effort, the
# descents are gravity. Only uphill seconds (UPHILL_MIN_GRADE) count, and
# there's no Durability (half the moving time is descending).
UPHILL_ONLY_TYPES = {"backcountry_skiing", "backcountry_snowboarding", "backcountry_skiing_snowboarding_ws"}
# Walking metabolic power needs the terrain: without GPS distance and
# altitude it's just speed.
GPS_ALT_MIN_COVERAGE = 0.5
WALK_INDOOR_TYPES = {"treadmill_walking", "indoor_walking"}


def classify_activity(type_key: str, has_power: bool) -> tuple[list[str], Optional[str]]:
    """(series this activity feeds, reason if it feeds none)."""
    t = (type_key or "").lower()
    if t in CYCLING_OUTDOOR_TYPES or t in CYCLING_INDOOR_TYPES:
        if not has_power:
            # Bike speed is wind, drafting and terrain, not fitness — never used.
            return [], "bike_without_power"
        return (["bike_indoor"] if t in CYCLING_INDOOR_TYPES else ["bike_outdoor"]), None
    if t in RUN_OUTDOOR_TYPES:
        # Running power (Garmin's wrist estimate or a pod) is deliberately not
        # used: it isn't a measured output for most users, and pace already
        # covers runs.
        return ["run_outdoor"], None
    if t in RUN_INDOOR_TYPES:
        return ["run_indoor"], None
    if t in WALK_OUTDOOR_TYPES:
        return ["walk_outdoor"], None
    if t in WALK_INDOOR_TYPES:
        # Garmin doesn't store treadmill incline, and walking cost depends
        # on it far more than running does — no honest output signal.
        return [], "treadmill_walk_no_incline"
    return [], "unsupported_type"


# ----------------------------------------------------------------- stream --

@dataclass
class Grid:
    """1 Hz stream. `t` is real epoch seconds; seconds where the timer was
    paused simply aren't present, so consecutive entries can jump."""
    t: list[int] = field(default_factory=list)
    hr: list[Optional[float]] = field(default_factory=list)
    power: list[Optional[float]] = field(default_factory=list)
    speed: list[Optional[float]] = field(default_factory=list)
    dist: list[Optional[float]] = field(default_factory=list)
    alt: list[Optional[float]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t)


def samples_from_garmin_details(details: dict) -> list[dict]:
    """Garmin's /activity-service/activity/{id}/details response -> samples
    of {t (epoch s), hr, power, speed, dist, alt}."""
    descriptors = details.get("metricDescriptors") or []
    index = {m.get("key"): m.get("metricsIndex") for m in descriptors}
    keys = {
        "t": "directTimestamp", "hr": "directHeartRate", "power": "directPower",
        "speed": "directSpeed", "dist": "sumDistance", "alt": "directElevation",
    }
    if index.get("directTimestamp") is None:
        return []
    out = []
    for row in details.get("activityDetailMetrics") or []:
        metrics = row.get("metrics") or []
        sample = {}
        for name, key in keys.items():
            idx = index.get(key)
            sample[name] = metrics[idx] if idx is not None and idx < len(metrics) else None
        if sample["t"] is None:
            continue
        sample["t"] = sample["t"] / 1000.0
        out.append(sample)
    return out


def resample_1hz(samples: list[dict]) -> Grid:
    """Hold each sample's values until the next one (smart recording can
    space samples a few seconds apart). Gaps longer than FILL_MAX_GAP_S are
    the timer being paused and are left as gaps."""
    grid = Grid()
    samples = sorted(samples, key=lambda s: s["t"])
    for idx, s in enumerate(samples):
        t0 = int(round(s["t"]))
        if grid.t and t0 <= grid.t[-1]:
            continue
        if idx + 1 < len(samples):
            span = int(round(samples[idx + 1]["t"])) - t0
            span = 1 if span <= 0 or span > FILL_MAX_GAP_S else span
        else:
            span = 1
        for k in range(span):
            grid.t.append(t0 + k)
            grid.hr.append(_pos(s.get("hr")))
            grid.power.append(_nonneg(s.get("power")))
            grid.speed.append(_nonneg(s.get("speed")))
            grid.dist.append(s.get("dist"))
            grid.alt.append(s.get("alt"))
    return grid


def _pos(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def _nonneg(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and v >= 0 else None


def has_power_stream(grid: Grid) -> bool:
    """A real power meter: power present and non-zero on a decent share of
    the activity (an empty directPower column doesn't count)."""
    if not len(grid):
        return False
    with_power = sum(1 for p in grid.power if p)
    return with_power >= 0.5 * len(grid)


# --------------------------------------------------------------- gradient --

def _forward_back_fill(values: list[Optional[float]]) -> Optional[list[float]]:
    first = next((v for v in values if v is not None), None)
    if first is None:
        return None
    out, last = [], first
    for v in values:
        if v is not None:
            last = v
        out.append(last)
    return out


def rolling_median(values: list[float], window: int) -> list[float]:
    half = window // 2
    n = len(values)
    return [statistics.median(values[max(0, i - half): min(n, i + half + 1)]) for i in range(n)]


def rolling_mean_centered(values: list[float], window: int) -> list[float]:
    half = window // 2
    n = len(values)
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append((prefix[hi] - prefix[lo]) / (hi - lo))
    return out


def smooth_altitude(alt: list[Optional[float]]) -> Optional[list[float]]:
    filled = _forward_back_fill(alt)
    if filled is None:
        return None
    return rolling_mean_centered(rolling_median(filled, ALT_MEDIAN_S), ALT_MEAN_S)


def gradients(dist: list[Optional[float]], alt: list[Optional[float]]) -> list[float]:
    """Per-second gradient measured over >= GRADE_WINDOW_M of horizontal
    distance centred on each point (never point-to-point: GPS/baro altitude
    noise would dominate). 0 where there's no altitude or distance."""
    n = len(dist)
    d = _forward_back_fill(dist)
    a = smooth_altitude(alt)
    if d is None or a is None:
        return [0.0] * n
    half = GRADE_WINDOW_M / 2
    out = []
    j = k = 0
    for i in range(n):
        # j: last index at least `half` metres behind i; k: first index at least `half` ahead.
        while j + 1 <= i and d[i] - d[j + 1] >= half:
            j += 1
        if k < i:
            k = i
        while k < n - 1 and d[k] - d[i] < half:
            k += 1
        span = d[k] - d[j]
        out.append(_clamp_grade((a[k] - a[j]) / span) if span >= GRADE_MIN_SPAN_M else 0.0)
    return out


# ----------------------------------------------------------------- output --

def output_signal(grid: Grid, series: str, uphill_only: bool = False) -> list[Optional[float]]:
    """Per-second output in the series' unit. `uphill_only` (ski touring)
    blanks every second not climbing steeper than UPHILL_MIN_GRADE, so a
    chunk containing descent fails the coverage check."""
    signal = SERIES[series]["signal"]
    if signal == "power":
        return list(grid.power)
    if signal == "speed":  # treadmill: grade 0 (Garmin doesn't record incline)
        return [s * 60 if s is not None else None for s in grid.speed]
    grade = gradients(grid.dist, grid.alt)
    if signal == "gas":
        return [s * 60 * minetti_run_cost(g) / RUN_COST_FLAT if s is not None else None
                for s, g in zip(grid.speed, grade)]
    if signal == "walk_metab":
        return [minetti_walk_cost(g) * s if s is not None and (not uphill_only or g > UPHILL_MIN_GRADE) else None
                for s, g in zip(grid.speed, grade)]
    raise ValueError(signal)


def _stop_condition(grid: Grid, series: str, moving: bool = False) -> list[bool]:
    """Per second: is the athlete stopped? For power series that means "not
    pedalling" — unless `moving`, where coasting at speed still counts as
    moving (that's how the 90-min moving-time rule should see a descent)."""
    signal = SERIES[series]["signal"]
    if signal == "power":
        if moving:
            return [not p and (v is None or v < MOVING_SPEED) for p, v in zip(grid.power, grid.speed)]
        return [not p for p in grid.power]
    floor = WALK_STOP_SPEED if SERIES[series]["sport"] == "walking" else RUN_STOP_SPEED
    return [s is None or s < floor for s in grid.speed]


def active_seconds(grid: Grid, series: str, moving: bool = False) -> tuple[list[int], list[bool]]:
    """Indices of seconds that count as moving, plus a parallel flag that's
    True where a stop/pause of >= SEGMENT_BREAK_S precedes that second.
    Zero output only counts as a stop after STOP_MIN_S consecutive
    seconds — a 3 s coast or a stride glitch stays in."""
    stop = _stop_condition(grid, series, moving=moving)
    n = len(grid)
    stopped = [False] * n
    i = 0
    while i < n:
        if stop[i]:
            j = i
            while j < n and stop[j] and (j == i or grid.t[j] - grid.t[j - 1] == 1):
                j += 1
            if j - i > STOP_MIN_S:
                for k in range(i, j):
                    stopped[k] = True
            i = j
        else:
            i += 1
    idx = [i for i in range(n) if not stopped[i]]
    breaks = [False] * len(idx)
    for p in range(1, len(idx)):
        if grid.t[idx[p]] - grid.t[idx[p - 1]] > SEGMENT_BREAK_S:
            breaks[p] = True
    return idx, breaks


# --------------------------------------------------------- variability --

def rolling_mean_trailing(values: list[float], window: int) -> list[float]:
    if len(values) < window:
        return [statistics.fmean(values)] if values else []
    out, s = [], sum(values[:window])
    out.append(s / window)
    for i in range(window, len(values)):
        s += values[i] - values[i - window]
        out.append(s / window)
    return out


def variability_index(values: list[float]) -> Optional[float]:
    """NP-style VI: 4th-power mean of the 30 s rolling average over the
    plain average. Coggan's normalized power for power; the same formula on
    speed-based outputs gives the "equivalent variability limit" durability
    needs for runs and hikes."""
    if not values:
        return None
    avg = statistics.fmean(values)
    if avg <= 0:
        return None
    rolled = rolling_mean_trailing(values, SMOOTH_S)
    np_ = statistics.fmean(v**4 for v in rolled) ** 0.25
    return np_ / avg


def smoothed_cv(values: list[float]) -> Optional[float]:
    rolled = rolling_mean_trailing(values, SMOOTH_S)
    if len(rolled) < 2:
        return None
    mean = statistics.fmean(rolled)
    return statistics.pstdev(rolled) / mean if mean > 0 else None


# ---------------------------------------------------------------- chunks --

def hr_band(hr_max: float, lthr: Optional[float], hr_rest: Optional[float] = None,
            sport: Optional[str] = None) -> tuple[float, float]:
    lo, hi = HR_BAND_LO * hr_max, HR_BAND_HI * hr_max
    if sport == "walking":
        lo = (hr_rest + WALK_HR_FLOOR_HRR * (hr_max - hr_rest)) if hr_rest else WALK_HR_FLOOR_FALLBACK * hr_max
    if lthr:
        hi = min(hi, float(lthr))
    return lo, hi


def _filled(values: list[Optional[float]]) -> list[float]:
    return [v for v in values if v is not None]


def hr_trend_bpm_per_min(hrs: list[float]) -> Optional[float]:
    """Least-squares slope of the 30 s-smoothed HR over the chunk, in bpm/min
    (one sample per second)."""
    smooth = rolling_mean_trailing(hrs, SMOOTH_S)
    n = len(smooth)
    if n < 2:
        return None
    mx = (n - 1) / 2
    my = statistics.fmean(smooth)
    sxx = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (y - my) for i, y in enumerate(smooth)) / sxx * 60


def evaluate_chunk(hr: list[Optional[float]], out: list[Optional[float]], signal: str,
                   band: tuple[float, float], vi_max: float = CHUNK_VI_MAX,
                   cv_max: float = CHUNK_CV_MAX) -> tuple[Optional[dict], str]:
    """(point, "ok") for a steady aerobic chunk, else (None, reason)."""
    n = len(hr)
    both = [(h, o) for h, o in zip(hr, out) if h is not None and o is not None]
    if len(both) < CHUNK_COVERAGE_MIN * n:
        return None, "missing_data"
    hrs = [h for h, _ in both]
    outs = [o for _, o in both]
    mean_hr = statistics.fmean(hrs)
    mean_out = statistics.fmean(outs)
    if mean_out <= 0:
        return None, "no_output"
    if not band[0] <= mean_hr <= band[1]:
        return None, "hr_outside_band"
    trend = hr_trend_bpm_per_min(hrs)
    if trend is None or abs(trend) > CHUNK_HR_TREND_MAX:
        return None, "hr_drifting"
    if signal == "power":
        vi = variability_index(outs)
        if vi is None or vi > vi_max:
            return None, "variable_output"
        # Normalized power: at the same average, a chunk with surges costs
        # more than a smooth one (outdoor VI 1.03-1.10 vs ~1.00 on the
        # trainer), and NP tracks that cost. avg_output keeps the plain mean.
        return {"output": normalized_output(outs), "avg_output": mean_out, "hr": mean_hr}, "ok"
    cv = smoothed_cv(outs)
    if cv is None or cv >= cv_max:
        return None, "variable_output"
    return {"output": mean_out, "avg_output": mean_out, "hr": mean_hr}, "ok"


def effort_times(grid: Grid, series: str, lthr: Optional[float], ftp: Optional[float]) -> list[int]:
    """Timestamps (sorted) of hard-effort seconds: 30 s rolling power above
    POST_EFFORT_FTP_FRAC x FTP (power series with an FTP), or HR above LTHR."""
    times: set[int] = set()
    if ftp and SERIES[series]["signal"] == "power":
        p = [v if v is not None else 0.0 for v in grid.power]
        limit = POST_EFFORT_FTP_FRAC * ftp
        if len(p) >= SMOOTH_S:
            for k, v in enumerate(rolling_mean_trailing(p, SMOOTH_S)):
                if v > limit:
                    times.add(grid.t[k + SMOOTH_S - 1])
    if lthr:
        times.update(t for t, h in zip(grid.t, grid.hr) if h is not None and h > lthr)
    return sorted(times)


def _after_effort(efforts: list[int], t_start: int, t_end: int) -> bool:
    k = bisect.bisect_left(efforts, t_start - POST_EFFORT_S)
    return k < len(efforts) and efforts[k] <= t_end


def steady_chunks(grid: Grid, series: str, hr_max: float, lthr: Optional[float],
                  out: Optional[list[Optional[float]]] = None,
                  hr_rest: Optional[float] = None, ftp: Optional[float] = None) -> tuple[list[dict], dict]:
    """Valid 5-min chunk points for one series, plus a count of why the
    other chunks were rejected (useful for the "why no data?" question)."""
    if out is None:
        out = output_signal(grid, series)
    idx, breaks = active_seconds(grid, series)
    end_cut = grid.t[idx[-1]] - END_EXCLUDE_S if idx else None
    idx, breaks = idx[WARMUP_S:], breaks[WARMUP_S:]
    band = hr_band(hr_max, lthr, hr_rest=hr_rest, sport=SERIES[series]["sport"])
    signal = SERIES[series]["signal"]
    vi_max = CHUNK_VI_MAX_BY_SERIES.get(series, CHUNK_VI_MAX)
    cv_max = CHUNK_CV_MAX_BY_SPORT.get(SERIES[series]["sport"], CHUNK_CV_MAX)
    efforts = effort_times(grid, series, lthr, ftp)

    segments: list[list[int]] = []
    for p, i in enumerate(idx):
        if p == 0 or breaks[p]:
            segments.append([])
        segments[-1].append(i)

    points: list[dict] = []
    reasons: dict[str, int] = {}
    prev_out: Optional[float] = None  # mean output of the preceding chunk, valid or not
    for seg in segments:
        run: list[dict] = []
        for c in range(len(seg) // CHUNK_S):
            sl = seg[c * CHUNK_S:(c + 1) * CHUNK_S]
            outs = [out[i] for i in sl]
            point, reason = evaluate_chunk([grid.hr[i] for i in sl], outs, signal, band, vi_max, cv_max)
            if point is not None and _after_effort(efforts, grid.t[sl[0]], grid.t[sl[-1]]):
                point, reason = None, "post_effort"
            if point is not None and grid.t[sl[-1]] > end_cut:
                point, reason = None, "activity_end"
            if point is not None and prev_out and point["avg_output"] < (1 - OUTPUT_DROP_MAX) * prev_out:
                point, reason = None, "output_drop"
            present = [o for o in outs if o is not None]
            if present:
                prev_out = statistics.fmean(present)
            reasons[reason] = reasons.get(reason, 0) + 1
            if point is not None:
                point["t_start"] = grid.t[sl[0]]
                run.append(point)
                continue
            if len(run) >= MIN_CHUNKS_IN_RUN:
                points.extend(run)
            elif run:
                reasons["isolated_chunk"] = reasons.get("isolated_chunk", 0) + len(run)
                reasons["ok"] -= len(run)
            run = []
        if len(run) >= MIN_CHUNKS_IN_RUN:
            points.extend(run)
        elif run:
            reasons["isolated_chunk"] = reasons.get("isolated_chunk", 0) + len(run)
            reasons["ok"] -= len(run)
    return points, reasons


# ------------------------------------------------------------- durability --

def durability_label(decoupling_pct: Optional[float]) -> Optional[str]:
    if decoupling_pct is None:
        return None
    if decoupling_pct < 5:
        return "strong"
    if decoupling_pct <= 8:
        return "moderate"
    return "low"


def normalized_output(values: list[float]) -> Optional[float]:
    """Coggan-style NP: 4th-power mean of the 30 s rolling average."""
    if not values:
        return None
    rolled = rolling_mean_trailing(values, SMOOTH_S)
    return statistics.fmean(v**4 for v in rolled) ** 0.25


def _half_fit(pts: list[dict], unit: str) -> tuple[float, float]:
    """HR = a + b*output over one half's chunks. Within one session the
    output range can be tiny (ERG), which leaves the slope unidentified; it's
    clamped to [0, the plausible maximum] and the line re-centred on the
    half's mean, so evaluating it inside the data stays an interpolation."""
    xs = [p["output"] for p in pts]
    ys = [p["hr"] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    fit = huber_fit(xs, ys)
    if fit is None:
        return my, 0.0
    a, b = fit
    hi = SLOPE_BOUNDS.get(unit, (0.0, math.inf))[1]
    if not 0.0 <= b <= hi:
        b = min(max(b, 0.0), hi)
        a = my - b * mx
    return a, b


def durability(grid: Grid, series: str, out: Optional[list[Optional[float]]] = None,
               interval_session: bool = False, points: Optional[list[dict]] = None) -> tuple[Optional[dict], str]:
    """Aerobic decoupling of one long, steady-ish session.

    Eligibility: >= 90 min moving, not an interval session (the dashboard's
    own lap/pace detection: long steady reps like 2x20 min have a low VI, so
    VI alone doesn't catch them), HR/output present, VI within limit.

    After the first 10 min, moving time is split into two equal halves.
      "regression" (primary): fit HR = a + b*output on each half's valid
        5-min chunks (`points`, from steady_chunks: same filters as Aerobic
        Efficiency). Needs >= 4 chunks per half and overlapping output ranges.
        Both fits are read at a common reference output (middle of the
        overlap): decoupling = (HR2 - HR1) / HR2, i.e. the drop in
        output-per-heartbeat at the same output — a like-for-like Pa:HR.
      "np_ef" (fallback): EF = NP / mean HR per half (mean GAS / metabolic
        power for speed-based sports), only if the halves' NP differ by <= 10%
        and their VIs (NP / mean, per half) by <= 0.05.
      "excluded": neither is possible — the halves aren't comparable; stored
        with decoupling None.
    Returns (result, "ok"), (excluded result, "not_comparable"), or
    (None, reason) for an ineligible session."""
    if out is None:
        out = output_signal(grid, series)
    moving_s = len(active_seconds(grid, series, moving=True)[0])
    if moving_s < DURABILITY_MIN_MOVING_S:
        return None, "too_short"
    if interval_session:
        return None, "intervals"
    idx, _ = active_seconds(grid, series)
    idx = idx[WARMUP_S:]
    both = [(grid.hr[i], out[i]) for i in idx if grid.hr[i] is not None and out[i] is not None]
    if len(both) < DURABILITY_COVERAGE_MIN * len(idx):
        return None, "missing_data"
    outs_all = [out[i] if out[i] is not None else 0.0 for i in idx]
    vi = variability_index(outs_all)
    if vi is None or vi > DURABILITY_VI_MAX_BY_SERIES.get(series, DURABILITY_VI_MAX):
        return None, "variable_output"

    meta = SERIES[series]
    half = len(idx) // 2

    def means(lo: int, hi: int) -> tuple[Optional[float], Optional[float], Optional[float]]:
        part = idx[lo:hi]
        pairs = [(grid.hr[i], out[i]) for i in part if grid.hr[i] is not None and out[i] is not None]
        if not pairs:
            return None, None, None
        mo = statistics.fmean(o for _, o in pairs)
        mh = statistics.fmean(h for h, _ in pairs)
        # NP for power; the plain mean for GAS / metabolic power, which
        # already account for terrain.
        norm = normalized_output(outs_all[lo:hi]) if meta["signal"] == "power" else mo
        return mo, mh, norm

    out1, hr1, np1 = means(0, half)
    out2, hr2, np2 = means(half, len(idx))
    vi1 = variability_index(outs_all[:half])
    vi2 = variability_index(outs_all[half:])
    if not hr1 or not hr2 or not out1 or not out2:
        return None, "missing_data"

    result: dict[str, Any] = {
        "moving_s": moving_s, "vi": round(vi, 3),
        "vi1": round(vi1, 3) if vi1 else None, "vi2": round(vi2, 3) if vi2 else None, "exclude_reason": None,
        "output1": out1, "output2": out2, "hr1": hr1, "hr2": hr2, "np1": np1, "np2": np2,
        "n_chunks1": 0, "n_chunks2": 0, "ref_output": None, "hr1_at_ref": None, "hr2_at_ref": None,
        "ef1": None, "ef2": None, "decoupling_pct": None, "label": None, "method": "excluded",
    }

    pos = {grid.t[i]: p for p, i in enumerate(idx)}
    halves: tuple[list[dict], list[dict]] = ([], [])
    for pt in points or []:
        p = pos.get(pt.get("t_start"))
        if p is not None:
            halves[0 if p + CHUNK_S / 2 < half else 1].append(pt)
    c1, c2 = halves
    result["n_chunks1"], result["n_chunks2"] = len(c1), len(c2)

    if len(c1) >= DURABILITY_MIN_CHUNKS_PER_HALF and len(c2) >= DURABILITY_MIN_CHUNKS_PER_HALF:
        lo = max(min(p["output"] for p in c1), min(p["output"] for p in c2))
        hi = min(max(p["output"] for p in c1), max(p["output"] for p in c2))
        tol = DURABILITY_OVERLAP_TOL * statistics.median([p["output"] for p in c1 + c2])
        if lo <= hi + tol:
            ref = (lo + hi) / 2
            a1, b1 = _half_fit(c1, meta["unit"])
            a2, b2 = _half_fit(c2, meta["unit"])
            h1, h2 = a1 + b1 * ref, a2 + b2 * ref
            if h1 > 0 and h2 > 0:
                result.update(method="regression", ref_output=ref, hr1_at_ref=h1, hr2_at_ref=h2,
                              ef1=ref / h1, ef2=ref / h2)

    if result["method"] == "excluded":
        # NP rewards surges, so NP-EF is only like-for-like when both halves
        # were ridden at a similar level AND with similar variability.
        if not (np1 and np2) or abs(np2 - np1) / np1 > DURABILITY_NP_MAX_DIFF:
            result["exclude_reason"] = "np_differs"
        elif vi1 is None or vi2 is None or abs(vi2 - vi1) > DURABILITY_HALF_VI_MAX_DIFF:
            result["exclude_reason"] = "vi_differs"
        else:
            result.update(method="np_ef", ef1=np1 / hr1, ef2=np2 / hr2)

    if result["method"] == "excluded":
        return result, "not_comparable"
    pct = (result["ef1"] - result["ef2"]) / result["ef1"] * 100
    result.update(decoupling_pct=pct, label=durability_label(pct))
    return result, "ok"


# ------------------------------------------------------------- one activity --

def analyze_activity(details: dict, type_key: str, zones: dict[str, dict], interval_session: bool = False) -> dict:
    """Everything this module derives from one activity.

    `zones` maps a Garmin zone sport ("CYCLING", "RUNNING", "DEFAULT") to
    {"hr_max", "lthr"}. Returns {"series": {key: {"points", "durability",
    "durability_reason", "reasons"}}, "skip_reason"}."""
    grid = resample_1hz(samples_from_garmin_details(details or {}))
    if not len(grid):
        return {"series": {}, "skip_reason": "no_stream"}
    if sum(1 for h in grid.hr if h is not None) < 0.5 * len(grid):
        return {"series": {}, "skip_reason": "no_hr"}
    series_keys, reason = classify_activity(type_key, has_power_stream(grid))
    if not series_keys:
        return {"series": {}, "skip_reason": reason}
    uphill_only = (type_key or "").lower() in UPHILL_ONLY_TYPES
    if "walk_outdoor" in series_keys:
        n = len(grid)
        if (sum(1 for d in grid.dist if d is not None) < GPS_ALT_MIN_COVERAGE * n
                or sum(1 for a in grid.alt if a is not None) < GPS_ALT_MIN_COVERAGE * n):
            return {"series": {}, "skip_reason": "no_gps_altitude"}
    result: dict[str, Any] = {"series": {}, "skip_reason": None}
    for key in series_keys:
        z = zones.get(ZONE_SPORT[SERIES[key]["sport"]]) or zones.get("DEFAULT") or {}
        if not z.get("hr_max"):
            result["series"][key] = {"points": [], "durability": None, "durability_reason": "no_hr_max", "reasons": {}}
            continue
        out = output_signal(grid, key, uphill_only=uphill_only)
        points, reasons = steady_chunks(grid, key, z["hr_max"], z.get("lthr"), out=out, hr_rest=z.get("hr_rest"),
                                        ftp=z.get("ftp") if SERIES[key]["signal"] == "power" else None)
        if interval_session and points:
            # Detected interval session (laps / pace structure): its steady
            # stretches are recoveries between efforts, HR still raised
            # (e.g. 150 W at 140-144 bpm after 220 W reps, below the
            # post-effort triggers), so none of it counts for efficiency.
            reasons["interval_session"] = len(points)
            reasons["ok"] = reasons.get("ok", 0) - len(points)
            points = []
        if uphill_only:
            dur, dur_reason = None, "uphill_only"
        else:
            dur, dur_reason = durability(grid, key, out=out, interval_session=interval_session, points=points)
        result["series"][key] = {"points": points, "durability": dur, "durability_reason": dur_reason, "reasons": reasons}
    return result


# --------------------------------------------------------------- the model --

def reference_hr(hr_rest: float, hr_max: float, sport: Optional[str] = None) -> int:
    return int(round(hr_rest + REF_HR_FRACTION_BY_SPORT.get(sport, REF_HR_FRACTION) * (hr_max - hr_rest)))


def easy_reference_hr(points: list[dict], today: date) -> tuple[Optional[float], int]:
    """(typical easy-session HR, number of sessions) from steady chunks:
    the median of each activity's median chunk HR over EASY_REF_DAYS, so one
    long run can't dominate. None when fewer than EASY_REF_MIN_ACTIVITIES."""
    since = today - timedelta(days=EASY_REF_DAYS)
    per_act: dict[Any, list[float]] = {}
    for p in points:
        if p.get("hr") and p["date"] >= since:
            per_act.setdefault(p.get("activity_id"), []).append(p["hr"])
    if len(per_act) < EASY_REF_MIN_ACTIVITIES:
        return None, len(per_act)
    return statistics.median(statistics.median(v) for v in per_act.values()), len(per_act)


def heat_weight(temp_c: Optional[float]) -> float:
    return 0.5 if temp_c is not None and temp_c > HEAT_DOWNWEIGHT_C else 1.0


def heat_corrected_hr(hr: float, temp_c: Optional[float]) -> float:
    if temp_c is None or temp_c <= HEAT_CORRECTION_BASE_C:
        return hr
    return hr - HEAT_CORRECTION_BPM_PER_C * (temp_c - HEAT_CORRECTION_BASE_C)


def _wls(xs: list[float], ys: list[float], ws: list[float]) -> Optional[tuple[float, float]]:
    sw = sum(ws)
    if sw <= 0:
        return None
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    sxx = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    if sxx <= 0:
        return None
    b = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys)) / sxx
    return my - b * mx, b


def huber_fit(xs: list[float], ys: list[float], ws: Optional[list[float]] = None,
              k: float = 1.345, iterations: int = 30) -> Optional[tuple[float, float]]:
    """Robust y = a + b*x by iteratively reweighted least squares with Huber
    weights (scale from the MAD of residuals). Huber rather than Theil-Sen
    because it takes per-point weights, which the heat downweighting needs."""
    if len(xs) < 2:
        return None
    base = list(ws) if ws else [1.0] * len(xs)
    fit = _wls(xs, ys, base)
    if fit is None:
        return None
    for _ in range(iterations):
        a, b = fit
        resid = [y - (a + b * x) for x, y in zip(xs, ys)]
        med = statistics.median(resid)
        scale = 1.4826 * statistics.median(abs(r - med) for r in resid)
        if scale < 1e-9:
            break
        hw = [1.0 if abs(r / scale) <= k else k / abs(r / scale) for r in resid]
        new = _wls(xs, ys, [w * h for w, h in zip(base, hw)])
        if new is None:
            break
        converged = abs(new[0] - a) < 1e-6 and abs(new[1] - b) < 1e-9
        fit = new
        if converged:
            break
    return fit


def within_session_slopes(points: list[dict]) -> tuple[list[float], int]:
    """Pairwise slopes dHR/doutput between chunks of the same qualifying
    activity (>= 3 chunks spanning >= 15% of its median output) that start
    <= SLOPE_PAIR_MAX_GAP_S apart and differ in output by >= 5% of its
    median. An activity's weight grows with its chunks (more pairs).
    Returns (slopes, number of activities that contributed)."""
    by_act: dict[Any, list[dict]] = {}
    for p in points:
        by_act.setdefault(p.get("activity_id"), []).append(p)
    slopes: list[float] = []
    n_act = 0
    for pts in by_act.values():
        outs = [p["output"] for p in pts]
        med = statistics.median(outs)
        if len(pts) < SLOPE_ACTIVITY_MIN_CHUNKS or not med or (max(outs) - min(outs)) / med < SLOPE_ACTIVITY_MIN_RANGE:
            continue
        pts = sorted(pts, key=lambda p: p.get("t_start", 0))
        before = len(slopes)
        for i, p in enumerate(pts):
            for q in pts[i + 1:]:
                if q.get("t_start", 0) - p.get("t_start", 0) > SLOPE_PAIR_MAX_GAP_S:
                    break
                dx = q["output"] - p["output"]
                if abs(dx) >= SLOPE_PAIR_MIN_DOUT * med:
                    slopes.append((q["hr"] - p["hr"]) / dx)
        n_act += len(slopes) > before
    return slopes, n_act


def sport_slope(points: list[dict], unit: str, today: date) -> dict:
    """One HR-vs-output slope for a user's sport, from the within-session
    variation of its valid chunks (all its series) in the trailing
    SLOPE_DAYS. Points need date, output, hr, activity_id.

    source "fitted": within-session Theil-Sen, plausible, >= SLOPE_FULL_POINTS pairs.
    source "shrunk": plausible but fewer pairs: weighted toward the default
                     by n_pairs / SLOPE_FULL_POINTS.
    source "default": too little within-session data, or the fit is
                      implausible (SLOPE_BOUNDS) — the population default."""
    since = today - timedelta(days=SLOPE_DAYS)
    pts = [p for p in points if p.get("output") and p.get("hr") and p["date"] >= since]
    slopes, n_act = within_session_slopes(pts)
    n_pairs = len(slopes)
    fitted = statistics.median(slopes) if slopes else None
    n = n_pairs  # the evidence is the pairs; SLOPE_MIN_POINTS / SLOPE_FULL_POINTS count them
    default = DEFAULT_SLOPE[unit]
    lo, hi = SLOPE_BOUNDS[unit]
    out = {"b": default, "source": "default", "b_fitted": fitted, "n": n, "n_activities": n_act,
           "n_pairs": n_pairs, "default": default, "reason": None, "method": "within_session_first_difference_theil_sen"}
    if n < SLOPE_MIN_POINTS or n_act < SLOPE_MIN_ACTIVITIES or n_pairs < SLOPE_MIN_PAIRS or fitted is None:
        out["reason"] = "too_little_data"
    elif not lo <= fitted <= hi:
        out["reason"] = "fitted_slope_out_of_range"
    elif n >= SLOPE_FULL_POINTS:
        out.update(b=fitted, source="fitted")
    else:
        w = n / SLOPE_FULL_POINTS
        out.update(b=w * fitted + (1 - w) * default, source="shrunk")
    return out


def weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights))
    half = sum(weights) / 2
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def fit_window(points: list[dict], b: float, heat_correction: bool = False) -> Optional[dict]:
    """One series' 28-day window under the shared slope b: only the
    intercept a_w = (heat-weighted) median(HR - b*output) is estimated.
    Stored as-is; the headline is derived at read time (window_headline) so
    a changed reference HR or pinned reference output never needs a refit."""
    pts = [p for p in points if p.get("output") and p.get("hr")]
    if not pts or not b:
        return None
    xs = [p["output"] for p in pts]
    ys = [heat_corrected_hr(p["hr"], p.get("temp_c")) if heat_correction else p["hr"] for p in pts]
    ws = [heat_weight(p.get("temp_c")) for p in pts]
    a = weighted_median([y - b * x for x, y in zip(xs, ys)], ws)
    mad = statistics.median(abs(y - a - b * x) for x, y in zip(xs, ys))
    temps = [p["temp_c"] for p in pts if p.get("temp_c") is not None]
    return {
        "n": len(pts),
        "n_activities": len({p.get("activity_id", i) for i, p in enumerate(pts)}),
        "a": a, "b": b, "mad": mad,
        "fit_ok": True, "fit_issue": None, "ratio": None, "raw_b": None,
        "hr_min": min(ys), "hr_max": max(ys),
        "out_min": min(xs), "out_max": max(xs),
        "median_out": statistics.median(xs),
        "mean_temp_c": statistics.fmean(temps) if temps else None,
        "hot_points": sum(1 for t in temps if t > HEAT_DOWNWEIGHT_C),
    }


def window_confidence(n: int, mad: Optional[float]) -> str:
    if mad is None:
        return "low"
    if n >= CONF_HIGH[0] and mad <= CONF_HIGH[1]:
        return "high"
    if n >= CONF_MEDIUM[0] and mad <= CONF_MEDIUM[1]:
        return "medium"
    return "low"


def window_headline(window: dict, hr_ref: float, ref_output: Optional[float]) -> dict:
    """Output@HRref = (HRref - a_w) / b and HR@ref-output = a_w + b*ref from
    a stored window, with confidence from its points and residual MAD."""
    if not window or not window.get("n") or window.get("a") is None or not window.get("b"):
        return {"output_at_hr_ref": None, "hr_at_ref_output": None, "confidence": None}
    a, b = window["a"], window["b"]
    return {
        "output_at_hr_ref": (hr_ref - a) / b,
        "hr_at_ref_output": (a + b * ref_output) if ref_output else None,
        "confidence": window_confidence(window["n"], window.get("mad")),
        "method": "shared_slope",
    }


def window_ends(first_day: date, today: date) -> list[date]:
    """Weekly evaluation dates: every Sunday from the first data onwards,
    plus today for the week in progress."""
    end = first_day + timedelta(days=(6 - first_day.weekday()) % 7)
    out = []
    while end < today:
        out.append(end)
        end += timedelta(days=7)
    out.append(today)
    return out


def weekly_windows(points: list[dict], today: date, b: float, heat_correction: bool = False) -> list[dict]:
    """Points need "date" (date) plus output/hr/temp_c. One fitted window
    per week end, covering the trailing WINDOW_DAYS days."""
    if not points:
        return []
    first = min(p["date"] for p in points)
    out = []
    for end in window_ends(first, today):
        start = end - timedelta(days=WINDOW_DAYS - 1)
        inside = [p for p in points if start <= p["date"] <= end]
        fitted = fit_window(inside, b, heat_correction=heat_correction)
        if fitted:
            fitted["week_end"] = end.isoformat()
            out.append(fitted)
    return out


def default_ref_output(outputs: Iterable[float], unit: str) -> Optional[float]:
    """Median of recent outputs, rounded to something a person would say:
    5 W, 0.1 W/kg, or (for running speeds) a whole 5 s/km pace."""
    vals = [o for o in outputs if o]
    if not vals:
        return None
    med = statistics.median(vals)
    if unit == "W":
        return float(5 * round(med / 5))
    if unit == "W/kg":
        return round(med, 1)
    if unit == "m/min":
        pace_s = 5 * round((60000 / med) / 5)  # seconds per km
        return 60000 / pace_s
    return med


def pace_str(m_per_min: Optional[float]) -> Optional[str]:
    if not m_per_min:
        return None
    sec = 60000 / m_per_min
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}/km"


def format_output(value: Optional[float], unit: str) -> Optional[str]:
    if value is None:
        return None
    if unit == "m/min":
        return pace_str(value)
    if unit == "W/kg":
        return f"{value:.2f} W/kg"
    return f"{value:.0f} W"


def change_vs(weeks: list[dict], key: str, days_back: int = 42) -> Optional[dict]:
    """Latest value vs the week ending closest to `days_back` days earlier
    (within 4 days); None if there's no such week."""
    vals = [w for w in weeks if w.get(key) is not None]
    if len(vals) < 2:
        return None
    latest = vals[-1]
    target = date.fromisoformat(latest["week_end"]) - timedelta(days=days_back)
    best = min(vals[:-1], key=lambda w: abs((date.fromisoformat(w["week_end"]) - target).days))
    if abs((date.fromisoformat(best["week_end"]) - target).days) > 4:
        return None
    delta = latest[key] - best[key]
    return {"from_week_end": best["week_end"], "abs": delta,
            "pct": (delta / best[key] * 100) if best[key] else None}


def finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)
