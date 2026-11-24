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

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Optional

# Bump whenever a change below would alter cached results: the sync job
# reprocesses every activity whose stored algo_version is older.
ALGO_VERSION = 1

WARMUP_S = 600                 # dropped from the start of each activity (HR settling)
CHUNK_S = 300                  # one data point per 5 min of steady effort
MIN_CHUNKS_IN_RUN = 2          # steady segment = >= 2 consecutive valid chunks (>= 10 min)
STOP_MIN_S = 5                 # zero output for longer than this counts as stopped
SEGMENT_BREAK_S = 30           # a stop/pause this long splits a steady segment
FILL_MAX_GAP_S = 10            # samples further apart than this = timer paused, not smart recording
HR_BAND_LO = 0.65              # aerobic band, fraction of HRmax...
HR_BAND_HI = 0.85              # ...capped at LTHR when that's lower
CHUNK_HR_STD_MAX = 5.0         # bpm; higher = HR still moving (a transition)
CHUNK_VI_MAX = 1.05            # power: NP / avg power
CHUNK_CV_MAX = 0.05            # speed-based: CV of the 30 s-smoothed output
CHUNK_COVERAGE_MIN = 0.95      # share of seconds with both HR and output present
SMOOTH_S = 30                  # rolling window for NP / CV
GRADE_WINDOW_M = 25.0          # horizontal distance a gradient is measured over
GRADE_MIN_SPAN_M = 5.0         # below this the athlete is basically stationary: grade 0
GRADE_CLAMP = 0.45             # Minetti's polynomials are fitted on -0.45..+0.45
ALT_MEDIAN_S = 5
ALT_MEAN_S = 30
RUN_STOP_SPEED = 1.0           # m/s
WALK_STOP_SPEED = 0.3          # m/s — steep hiking can be slow but still moving
DURABILITY_MIN_MOVING_S = 90 * 60
DURABILITY_VI_MAX = 1.10       # whole-session variability limit (excludes interval sessions)
DURABILITY_COVERAGE_MIN = 0.90
WINDOW_DAYS = 28
MIN_POINTS_FIT = 8
MIN_ACTIVITIES_FIT = 2         # a slope from one session mostly measures that session's cardiac drift
MAX_EXTRAPOLATION = 0.25       # fitted output may sit at most 25% beyond the outputs actually seen
MIN_ELASTICITY = 0.10          # %HR change per % output change; flatter = HR isn't tracking output
MIN_OUTPUT_RANGE = 0.10        # (max - min) / median output needed for a meaningful slope
HEAT_DOWNWEIGHT_C = 25.0       # outdoor points above this count half
HEAT_CORRECTION_BASE_C = 20.0  # optional HR correction: -1 bpm per degree above this
HEAT_CORRECTION_BPM_PER_C = 1.0
REF_HR_FRACTION = 0.65         # HRref = rest + 0.65 * (max - rest)


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
    "walking", "casual_walking", "speed_walking", "hiking", "rucking", "mountaineering",
    "backcountry_skiing", "backcountry_skiing_snowboarding_ws",
}
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

def output_signal(grid: Grid, series: str) -> list[Optional[float]]:
    """Per-second output in the series' unit."""
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
        return [minetti_walk_cost(g) * s if s is not None else None for s, g in zip(grid.speed, grade)]
    raise ValueError(signal)


def _stop_condition(grid: Grid, series: str) -> list[bool]:
    signal = SERIES[series]["signal"]
    if signal == "power":
        return [not p for p in grid.power]
    floor = WALK_STOP_SPEED if SERIES[series]["sport"] == "walking" else RUN_STOP_SPEED
    return [s is None or s < floor for s in grid.speed]


def active_seconds(grid: Grid, series: str) -> tuple[list[int], list[bool]]:
    """Indices of seconds that count as moving, plus a parallel flag that's
    True where a stop/pause of >= SEGMENT_BREAK_S precedes that second.
    Zero output only counts as a stop after STOP_MIN_S consecutive
    seconds — a 3 s coast or a stride glitch stays in."""
    stop = _stop_condition(grid, series)
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

def hr_band(hr_max: float, lthr: Optional[float]) -> tuple[float, float]:
    lo, hi = HR_BAND_LO * hr_max, HR_BAND_HI * hr_max
    if lthr:
        hi = min(hi, float(lthr))
    return lo, hi


def _filled(values: list[Optional[float]]) -> list[float]:
    return [v for v in values if v is not None]


def evaluate_chunk(hr: list[Optional[float]], out: list[Optional[float]], signal: str,
                   band: tuple[float, float]) -> tuple[Optional[dict], str]:
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
    if statistics.pstdev(hrs) >= CHUNK_HR_STD_MAX:
        return None, "hr_unstable"
    if signal == "power":
        vi = variability_index(outs)
        if vi is None or vi >= CHUNK_VI_MAX:
            return None, "variable_output"
    else:
        cv = smoothed_cv(outs)
        if cv is None or cv >= CHUNK_CV_MAX:
            return None, "variable_output"
    return {"output": mean_out, "hr": mean_hr}, "ok"


def steady_chunks(grid: Grid, series: str, hr_max: float, lthr: Optional[float],
                  out: Optional[list[Optional[float]]] = None) -> tuple[list[dict], dict]:
    """Valid 5-min chunk points for one series, plus a count of why the
    other chunks were rejected (useful for the "why no data?" question)."""
    if out is None:
        out = output_signal(grid, series)
    idx, breaks = active_seconds(grid, series)
    idx, breaks = idx[WARMUP_S:], breaks[WARMUP_S:]
    band = hr_band(hr_max, lthr)
    signal = SERIES[series]["signal"]

    segments: list[list[int]] = []
    for p, i in enumerate(idx):
        if p == 0 or breaks[p]:
            segments.append([])
        segments[-1].append(i)

    points: list[dict] = []
    reasons: dict[str, int] = {}
    for seg in segments:
        run: list[dict] = []
        for c in range(len(seg) // CHUNK_S):
            sl = seg[c * CHUNK_S:(c + 1) * CHUNK_S]
            point, reason = evaluate_chunk([grid.hr[i] for i in sl], [out[i] for i in sl], signal, band)
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


def durability(grid: Grid, series: str, out: Optional[list[Optional[float]]] = None) -> tuple[Optional[dict], str]:
    """First-half vs second-half output per heartbeat for a long, steady-ish
    session. (result, "ok") or (None, reason)."""
    if out is None:
        out = output_signal(grid, series)
    idx, _ = active_seconds(grid, series)
    moving_s = len(idx)
    if moving_s < DURABILITY_MIN_MOVING_S:
        return None, "too_short"
    idx = idx[WARMUP_S:]
    both = [(grid.hr[i], out[i]) for i in idx if grid.hr[i] is not None and out[i] is not None]
    if len(both) < DURABILITY_COVERAGE_MIN * len(idx):
        return None, "missing_data"
    outs_all = [out[i] if out[i] is not None else 0.0 for i in idx]
    vi = variability_index(outs_all)
    if vi is None or vi >= DURABILITY_VI_MAX:
        return None, "variable_output"

    half = len(idx) // 2

    def ef(part: list[int]) -> tuple[Optional[float], Optional[float], Optional[float]]:
        pairs = [(grid.hr[i], out[i]) for i in part if grid.hr[i] is not None and out[i] is not None]
        if not pairs:
            return None, None, None
        mo = statistics.fmean(o for _, o in pairs)
        mh = statistics.fmean(h for h, _ in pairs)
        return (mo / mh if mh else None), mo, mh

    ef1, out1, hr1 = ef(idx[:half])
    ef2, out2, hr2 = ef(idx[half:])
    if not ef1 or ef2 is None:
        return None, "missing_data"
    pct = (ef1 - ef2) / ef1 * 100
    return {
        "moving_s": moving_s,
        "vi": round(vi, 3),
        "ef1": ef1, "ef2": ef2,
        "output1": out1, "output2": out2, "hr1": hr1, "hr2": hr2,
        "decoupling_pct": pct,
        "label": durability_label(pct),
    }, "ok"


# ------------------------------------------------------------- one activity --

def analyze_activity(details: dict, type_key: str, zones: dict[str, dict]) -> dict:
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
    result: dict[str, Any] = {"series": {}, "skip_reason": None}
    for key in series_keys:
        z = zones.get(ZONE_SPORT[SERIES[key]["sport"]]) or zones.get("DEFAULT") or {}
        if not z.get("hr_max"):
            result["series"][key] = {"points": [], "durability": None, "durability_reason": "no_hr_max", "reasons": {}}
            continue
        out = output_signal(grid, key)
        points, reasons = steady_chunks(grid, key, z["hr_max"], z.get("lthr"), out=out)
        dur, dur_reason = durability(grid, key, out=out)
        result["series"][key] = {"points": points, "durability": dur, "durability_reason": dur_reason, "reasons": reasons}
    return result


# --------------------------------------------------------------- the model --

def reference_hr(hr_rest: float, hr_max: float) -> int:
    return int(round(hr_rest + REF_HR_FRACTION * (hr_max - hr_rest)))


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


def fit_window(points: list[dict], heat_correction: bool = False) -> Optional[dict]:
    """Model one 28-day window of chunk points. Stored as-is; the headline
    value is derived at read time (window_headline) so a changed reference
    HR or pinned reference output never needs a refit."""
    pts = [p for p in points if p.get("output") and p.get("hr")]
    if not pts:
        return None
    xs = [p["output"] for p in pts]
    ys = [heat_corrected_hr(p["hr"], p.get("temp_c")) if heat_correction else p["hr"] for p in pts]
    ws = [heat_weight(p.get("temp_c")) for p in pts]
    median_out = statistics.median(xs)
    out_range = (max(xs) - min(xs)) / median_out if median_out else 0.0
    fit = huber_fit(xs, ys, ws) if len(pts) >= 2 else None
    n_activities = len({p.get("activity_id", i) for i, p in enumerate(pts)})
    # Elasticity: a 10% output change should move HR by >= 1%. A flatter
    # slope means HR in this window isn't tracking output (heat, fatigue,
    # sensor noise), and solving it for HRref would be wildly unstable.
    elasticity = fit[1] * median_out / statistics.median(ys) if fit else None
    fit_ok = (
        fit is not None and len(pts) >= MIN_POINTS_FIT and n_activities >= MIN_ACTIVITIES_FIT
        and out_range >= MIN_OUTPUT_RANGE and fit[1] > 0 and elasticity >= MIN_ELASTICITY
    )
    ratios = [x / y for x, y in zip(xs, ys) if y]
    temps = [p["temp_c"] for p in pts if p.get("temp_c") is not None]
    return {
        "n": len(pts),
        "n_activities": n_activities,
        "a": fit[0] if fit_ok else None,
        "b": fit[1] if fit_ok else None,
        "fit_ok": fit_ok,
        "ratio": statistics.median(ratios) if ratios else None,
        "hr_min": min(ys), "hr_max": max(ys),
        "out_min": min(xs), "out_max": max(xs),
        "median_out": median_out,
        "mean_temp_c": statistics.fmean(temps) if temps else None,
        "hot_points": sum(1 for t in temps if t > HEAT_DOWNWEIGHT_C),
    }


def window_headline(window: dict, hr_ref: float, ref_output: Optional[float]) -> dict:
    """Output@HRref and HR@ref-output from a stored window fit, with a
    confidence grade: "high" (fit, HRref inside the data's HR span),
    "medium" (fit, but HRref is outside that span), "low" (the median
    output/HR ratio, used when there's no trustworthy fit or the fitted
    value lands more than MAX_EXTRAPOLATION beyond the outputs seen)."""
    if not window or not window.get("n"):
        return {"output_at_hr_ref": None, "hr_at_ref_output": None, "confidence": None}
    if window.get("fit_ok"):
        a, b = window["a"], window["b"]
        value = (hr_ref - a) / b
        inside = window["hr_min"] - 5 <= hr_ref <= window["hr_max"] + 5
        plausible = (window["out_min"] * (1 - MAX_EXTRAPOLATION) <= value
                     <= window["out_max"] * (1 + MAX_EXTRAPOLATION))
        if plausible:
            return {
                "output_at_hr_ref": value,
                "hr_at_ref_output": (a + b * ref_output) if ref_output else None,
                "confidence": "high" if inside else "medium",
            }
    ratio = window.get("ratio")
    if not ratio:
        return {"output_at_hr_ref": None, "hr_at_ref_output": None, "confidence": None}
    return {
        "output_at_hr_ref": ratio * hr_ref,
        "hr_at_ref_output": (ref_output / ratio) if ref_output else None,
        "confidence": "low",
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


def weekly_windows(points: list[dict], today: date, heat_correction: bool = False) -> list[dict]:
    """Points need "date" (date) plus output/hr/temp_c. One fitted window
    per week end, covering the trailing WINDOW_DAYS days."""
    if not points:
        return []
    first = min(p["date"] for p in points)
    out = []
    for end in window_ends(first, today):
        start = end - timedelta(days=WINDOW_DAYS - 1)
        inside = [p for p in points if start <= p["date"] <= end]
        fitted = fit_window(inside, heat_correction=heat_correction)
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
