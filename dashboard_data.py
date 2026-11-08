"""Turns raw Garmin Connect API responses into the JSON the dashboard renders.

Garmin's Connect API is not officially documented — field names below were
learned from the `garminconnect` package's own usage and community
knowledge, not a spec. Every read from a Garmin response goes through the
`dig()` / `first_value()` helpers and section builders are wrapped so a
missing or renamed field degrades to "no data" in that one spot instead of
crashing the whole payload.
"""

from __future__ import annotations

import logging
import os
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from garmin_client import safe_call

logger = logging.getLogger(__name__)

TREND_DAYS = int(os.getenv("DASHBOARD_TREND_DAYS", "42"))
TREND_FETCH_WORKERS = int(os.getenv("DASHBOARD_TREND_WORKERS", "8"))
ROUTE_LOG_DAYS = int(os.getenv("DASHBOARD_ROUTE_LOG_DAYS", "21"))
SLEEP_LOOKBACK_DAYS = 10
MAX_ACTIVITY_RANGE_DAYS = 400  # cap per-call span for arbitrary-range activity lookups (calendar view, MCP list_activities)


# ---------------------------------------------------------------- helpers --

def dig(obj: Any, *path: str) -> Any:
    """Walk a chain of dict keys, returning None as soon as one is missing."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def first_value(obj: Any) -> Any:
    """Garmin often keys per-device dicts by an opaque device id — grab the
    first (only, in practice) value rather than assuming the id."""
    if isinstance(obj, dict) and obj:
        return next(iter(obj.values()))
    return None


def first_of(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def humanize_key(key: str) -> str:
    """'hrvFactorPercent' -> 'Hrv'; 'sleepHistoryFactorPercent' -> 'Sleep History'."""
    stripped = re.sub(r"FactorPercent$", "", key)
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", stripped).split()
    label = " ".join(w if w.isupper() else w.capitalize() for w in words)
    return label.replace("Hrv", "HRV").replace("Acwr", "ACWR")


def to_title(value: Any) -> Optional[str]:
    """Garmin sometimes returns enum-like fields (level, classification,
    feedback phrases) as an int code instead of a string depending on the
    endpoint/account — normalize either into display text."""
    if isinstance(value, str):
        return value.replace("_", " ").title() or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_distance(meters: Optional[float], activity_type: str = "", is_leg: bool = False) -> str:
    if not meters:
        return "—"
    km = meters / 1000
    if "swim" in (activity_type or "").lower() and km < 2:
        return f"{meters:.0f} m"
    decimals = 2 if is_leg else 1
    return f"{km:.{decimals}f} km"

def fmt_pace_or_speed(distance: Optional[float], duration: Optional[float], type_key: str) -> str:
    """Sport-appropriate pace/speed string: running/hiking -> min:sec/km
    pace, cycling -> km/h average speed, swimming -> min:sec/100m pace.
    Anything else (strength, etc.) has no meaningful pace, so this is "—"."""
    if not distance or not duration:
        return "—"
    t = (type_key or "").lower()
    if RUN_TYPE_HINT in t or any(hint in t for hint in HIKE_TYPE_HINTS):
        m, s = divmod(int(round(duration / (distance / 1000))), 60)
        return f"{m}:{s:02d} /km"
    if any(hint in t for hint in CYCLING_TYPE_HINTS):
        return f"{(distance / 1000) / (duration / 3600):.1f} km/h"
    if SWIM_TYPE_HINT in t:
        m, s = divmod(int(round(duration / (distance / 100))), 60)
        return f"{m}:{s:02d} /100m"
    return "—"


def pace_label_for(type_key: str) -> str:
    """Column/row label matching what fmt_pace_or_speed actually returns for
    this sport — "Speed" for cycling (km/h), "Pace" for everything else that
    has one (running/hiking min:sec/km, swimming min:sec/100m)."""
    t = (type_key or "").lower()
    if any(hint in t for hint in CYCLING_TYPE_HINTS):
        return "Speed"
    return "Pace"


def fmt_elevation(meters: Optional[float]) -> str:
    if meters is None:
        return "—"
    return f"+{round(meters)} m"


def fmt_int(value: Any) -> Any:
    return round(value) if isinstance(value, (int, float)) else None


def fmt_decimal1(value: Any) -> Optional[str]:
    """One decimal place, as a string on purpose: Garmin reports VO2 max to
    a tenth (vo2MaxPreciseValue), and rounding it to a whole number hides
    real week-to-week movement. Returning a float wouldn't be enough — JSON
    56.0 arrives in JS as the number 56 and renders as "56", dropping the
    decimal again — so the formatting has to survive as text."""
    if not isinstance(value, (int, float)):
        return None
    return f"{value:.1f}"


def date_range(days: int, end: Optional[date] = None) -> list[str]:
    end = end or date.today()
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


# ------------------------------------------------------------- readiness --

def build_hero(readiness_entries: list[dict]) -> dict:
    if not readiness_entries:
        return {
            "readiness_now": None,
            "readiness_label": None,
            "context": "No training readiness data from Garmin today.",
            "wake_reading": None,
            "latest_reading": None,
        }

    def entry_time(e: dict) -> str:
        ts = e.get("timestampLocal") or e.get("timestamp") or ""
        return ts

    ordered = sorted(readiness_entries, key=entry_time)
    wake = next((e for e in ordered if e.get("inputContext") == "AFTER_WAKEUP_RESET"), ordered[0])
    latest = ordered[-1]

    def reading(e: dict) -> dict:
        ts = entry_time(e)
        time_label = None
        if ts:
            try:
                time_label = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%H:%M")
            except ValueError:
                time_label = None
        return {
            "score": e.get("score"),
            "label": to_title(e.get("level")),
            "time": time_label,
        }

    return {
        "readiness_now": latest.get("score"),
        "readiness_label": to_title(latest.get("level")),
        "context": build_readiness_context(latest),
        "wake_reading": reading(wake),
        "latest_reading": reading(latest),
    }


def build_readiness_context(latest: dict) -> str:
    """Garmin's feedbackLong/feedbackShort fields are raw internal enum keys
    (e.g. 'HIGH_RT_HIGHEST_SS_AVAILABLE') meant to be translated client-side
    by the Garmin app's own string table, which we don't have access to — so
    build a plain-language sentence from the structured score/factor data
    instead of showing that code."""
    score = latest.get("score")
    level = to_title(latest.get("level"))
    parts = []
    if level and score is not None:
        parts.append(f"{level} readiness ({score}/100)")
    elif level:
        parts.append(f"{level} readiness")

    factors = build_vitals_factors(latest)
    with_data = [f for f in factors if not f.get("no_data") and isinstance(f.get("pct"), (int, float))]
    if with_data:
        best = max(with_data, key=lambda f: f["pct"])
        parts.append(f"driven by {best['label']} ({round(best['pct'])}%)")
    missing = [f["label"] for f in factors if f.get("no_data")]
    if missing:
        parts.append("no " + ", ".join(missing) + " data today")

    return " · ".join(parts)


def build_vitals_factors(readiness_entry: Optional[dict]) -> list[dict]:
    if not readiness_entry:
        return []
    factors = []
    for key, value in readiness_entry.items():
        if not key.endswith("FactorPercent"):
            continue
        prefix = key[: -len("FactorPercent")]
        feedback = to_title(readiness_entry.get(f"{prefix}FactorFeedback"))
        has_data = isinstance(value, (int, float))
        factors.append(
            {
                "label": humanize_key(key),
                "pct": value if has_data else None,
                "status": "good" if (has_data and value >= 70) else "mod",
                "no_data": not has_data,
                "display": f"{round(value)}% · {feedback}" if (has_data and feedback) else (
                    f"{round(value)}%" if has_data else "No data"
                ),
            }
        )
    return factors


# ---------------------------------------------------------- training load --

MAX_TREND_RANGE_DAYS = 180  # cap per-call span for on-demand older-history chunks


def _extract_training_load(status: Optional[dict]) -> dict:
    """Garmin returns training status keyed by DEVICE, and an account with
    more than one device gets an entry per device on days both reported.
    Only the device actually tracking training load carries a populated
    acuteTrainingLoadDTO; the other's is null. So scan for the device that
    has the data instead of taking whichever key happens to come first —
    picking the first one silently produced null CTL/ATL on every
    multi-device day, which the chart then drew as a gap with a straight
    line interpolated across it, on dates that really did have data."""
    per_device = dig(status, "mostRecentTrainingStatus", "latestTrainingStatusData") or {}
    if not isinstance(per_device, dict):
        per_device = {}

    ctl = atl = None
    for device_data in per_device.values():
        acute = dig(device_data, "acuteTrainingLoadDTO") or {}
        candidate_ctl = acute.get("dailyTrainingLoadChronic")
        candidate_atl = acute.get("dailyTrainingLoadAcute")
        if candidate_ctl is not None or candidate_atl is not None:
            ctl, atl = candidate_ctl, candidate_atl
            break

    tsb = (ctl - atl) if (ctl is not None and atl is not None) else None
    return {"ctl": ctl, "atl": atl, "tsb": tsb}


TREND_RETRY_ROUNDS = 2  # serial passes over days the concurrent fan-out couldn't get


def build_ridge_chart_for_days(
    client, days: list[str], race_dates: set[str], username: Optional[str] = None
) -> dict:
    """CTL/ATL history has no bulk endpoint — Garmin's training-status API
    only takes one date at a time (confirmed against the live API; a range
    query on the same endpoint 404s and query params are silently ignored).
    Fetching a window of history sequentially was the slow part of a cold
    dashboard load, so fan the per-day requests out across a small thread
    pool instead of awaiting them one at a time.

    That fan-out is also why this needs a cache and a retry. At a 90-day
    window Garmin answers a large share of the burst with 504s — measured
    at 41 of 90 days failing — and safe_call turns each failure into None,
    which the chart draws as a gap with a line interpolated across it. The
    data is fine; the same days return normally when asked for one at a
    time. So: serve what's already cached, fetch only the rest, and give
    whatever still came back empty a couple of calmer serial retries.

    A past day's CTL/ATL can't change retroactively, so successful days
    before today are cached permanently (see trend_cache) and never
    refetched. Today is always refetched — its value is still moving.

    Every requested day gets a trend entry, even ones Garmin genuinely has
    no ctl/atl for — ctl/atl/tsb are just null. That keeps the entry list
    gapless/one-per-day, which is what lets a future race day's flag get
    positioned at all (see build_ridge_chart)."""
    import trend_cache  # local import: keeps this module Garmin-only otherwise

    today_str = date.today().isoformat()
    cached = trend_cache.get_days(username, days) if username else {}
    resolved: dict[str, dict] = {d: v for d, v in cached.items() if d != today_str}
    pending = [d for d in days if d not in resolved]

    if pending:
        with ThreadPoolExecutor(max_workers=TREND_FETCH_WORKERS) as pool:
            futures = {d: pool.submit(safe_call, client.get_training_status, d, default=None) for d in pending}
            for d, future in futures.items():
                resolved[d] = _extract_training_load(future.result())

        # Anything still empty is far more likely throttled than genuinely
        # absent, so re-ask serially before accepting a gap.
        for _ in range(TREND_RETRY_ROUNDS):
            retry_days = [d for d in pending if resolved.get(d, {}).get("ctl") is None]
            if not retry_days:
                break
            logger.info("Retrying %d trend day(s) Garmin didn't return on the first pass.", len(retry_days))
            for d in retry_days:
                resolved[d] = _extract_training_load(safe_call(client.get_training_status, d, default=None))

        if username:
            trend_cache.save_days(
                username, {d: v for d, v in resolved.items() if d < today_str and d in pending}
            )

    trend = [{"date": d, **resolved.get(d, {"ctl": None, "atl": None, "tsb": None})} for d in days]
    still_missing = sum(1 for t in trend if t["ctl"] is None and t["date"] <= today_str)
    if still_missing:
        logger.warning("Trend still missing %d/%d day(s) after retries.", still_missing, len(days))

    return {
        "trend": trend,
        "race_markers": sorted(race_dates & set(days)),
    }


def build_ridge_chart(client, race_dates: set[str], username: Optional[str] = None) -> dict:
    """Default rolling window shown on first load — build_trend_range covers
    fetching further back on demand, e.g. as the chart is scrolled/zoomed
    out past what this window already has.

    This is a fitness chart, not a general-purpose calendar — it has no
    data past today, so the window never extends into the future, full
    stop, even for a marked future race day: that day simply has no flag
    on this chart (the Race Plan panel already shows its countdown)."""
    return build_ridge_chart_for_days(client, date_range(TREND_DAYS), race_dates, username=username)


def build_trend_range(
    client, start_date: str, end_date: str, race_dates: set[str], username: Optional[str] = None
) -> dict:
    """Same shape as build_ridge_chart, but for an arbitrary caller-supplied
    range — powers the ridge chart's "load older history" as it's scrolled
    back, instead of fetching years of data no one asked to see yet."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    return build_ridge_chart_for_days(client, days, race_dates, username=username)


def fetch_latest_vo2max(client, today: str) -> tuple[Optional[float], Optional[float]]:
    """VO2 max isn't recomputed daily, so a single-day lookup is often empty —
    scan back over a window and take the most recent value for run ('generic')
    and bike ('cycling') independently, since they don't always update the
    same day."""
    start = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    entries = safe_call(client.get_max_metrics_range, start, today, default=[]) or []

    def entry_date(e: dict) -> str:
        return dig(e, "generic", "calendarDate") or dig(e, "cycling", "calendarDate") or ""

    vo2_run = vo2_bike = None
    for entry in sorted(entries, key=entry_date, reverse=True):
        if vo2_run is None:
            vo2_run = dig(entry, "generic", "vo2MaxPreciseValue")
            if vo2_run is None:
                vo2_run = dig(entry, "generic", "vo2MaxValue")
        if vo2_bike is None:
            vo2_bike = dig(entry, "cycling", "vo2MaxPreciseValue")
            if vo2_bike is None:
                vo2_bike = dig(entry, "cycling", "vo2MaxValue")
        if vo2_run is not None and vo2_bike is not None:
            break
    return vo2_run, vo2_bike


# Garmin's own published "VO2 Max Standard Ratings" table (Cooper
# Institute data), reproduced from their device-manual documentation —
# verified against the source rather than guessed, since getting fitness
# category cutoffs wrong is worse than not showing one. Tiers ordered
# highest-first; below the lowest listed threshold is "Poor".
VO2MAX_TABLE = {
    "MALE": {
        (20, 29): {"Superior": 55.4, "Excellent": 51.1, "Good": 45.4, "Fair": 41.7},
        (30, 39): {"Superior": 54.0, "Excellent": 48.3, "Good": 44.0, "Fair": 40.5},
        (40, 49): {"Superior": 52.5, "Excellent": 46.4, "Good": 42.4, "Fair": 38.5},
        (50, 59): {"Superior": 48.9, "Excellent": 43.4, "Good": 39.2, "Fair": 35.6},
        (60, 69): {"Superior": 45.7, "Excellent": 39.5, "Good": 35.5, "Fair": 32.3},
        (70, 79): {"Superior": 42.1, "Excellent": 36.7, "Good": 32.3, "Fair": 29.4},
    },
    "FEMALE": {
        (20, 29): {"Superior": 49.6, "Excellent": 43.9, "Good": 39.5, "Fair": 36.1},
        (30, 39): {"Superior": 47.4, "Excellent": 42.4, "Good": 37.8, "Fair": 34.4},
        (40, 49): {"Superior": 45.3, "Excellent": 39.7, "Good": 36.3, "Fair": 33.0},
        (50, 59): {"Superior": 41.1, "Excellent": 36.7, "Good": 33.0, "Fair": 30.1},
        (60, 69): {"Superior": 37.8, "Excellent": 33.0, "Good": 30.0, "Fair": 27.5},
        (70, 79): {"Superior": 36.7, "Excellent": 30.9, "Good": 28.1, "Fair": 25.9},
    },
}
VO2MAX_TIER_ORDER = ["Poor", "Fair", "Good", "Excellent", "Superior"]


def calc_age(birth_date_str: Optional[str], today: date) -> Optional[int]:
    if not birth_date_str:
        return None
    try:
        birth = date.fromisoformat(birth_date_str[:10])
    except ValueError:
        return None
    age = today.year - birth.year
    if (today.month, today.day) < (birth.month, birth.day):
        age -= 1
    return age


def classify_vo2max(value: Optional[float], age: Optional[int], gender: Optional[str]) -> dict:
    """Returns {'category', 'next_category', 'points_to_next'} — any of
    which may be None (no score, no age/gender on file, unrecognized
    gender, or already at the top published tier)."""
    empty = {"category": None, "next_category": None, "points_to_next": None}
    if value is None or age is None or gender not in VO2MAX_TABLE:
        return empty

    brackets = VO2MAX_TABLE[gender]
    bracket_key = next((b for b in brackets if b[0] <= age <= b[1]), None)
    if bracket_key is None:
        # Clamp to the nearest defined bracket rather than refusing to
        # classify someone outside the table's 20-79 span.
        bracket_key = min(brackets, key=lambda b: min(abs(age - b[0]), abs(age - b[1])))
    thresholds = brackets[bracket_key]  # {'Superior':..., 'Excellent':..., 'Good':..., 'Fair':...}

    tiers_desc = [("Superior", thresholds["Superior"]), ("Excellent", thresholds["Excellent"]),
                  ("Good", thresholds["Good"]), ("Fair", thresholds["Fair"])]
    current = "Poor"
    for name, threshold in tiers_desc:
        if value >= threshold:
            current = name
            break

    idx = VO2MAX_TIER_ORDER.index(current)
    if idx == len(VO2MAX_TIER_ORDER) - 1:
        return {"category": current, "next_category": None, "points_to_next": None}
    next_name = VO2MAX_TIER_ORDER[idx + 1]
    next_threshold = thresholds[next_name]
    return {"category": current, "next_category": next_name, "points_to_next": round(next_threshold - value, 1)}


def classify_endurance_score(score: Optional[float], data: Optional[dict]) -> dict:
    """Same idea as classify_vo2max, but the tier thresholds come straight
    from Garmin's own API response for this account rather than a
    hardcoded table — get_endurance_score already returns
    classificationLowerLimit* fields for every tier."""
    empty = {"category": None, "next_category": None, "points_to_next": None}
    if score is None or not isinstance(data, dict):
        return empty

    tiers = [
        ("Beginner", data.get("gaugeLowerLimit")),
        ("Intermediate", data.get("classificationLowerLimitIntermediate")),
        ("Trained", data.get("classificationLowerLimitTrained")),
        ("Well Trained", data.get("classificationLowerLimitWellTrained")),
        ("Expert", data.get("classificationLowerLimitExpert")),
        ("Superior", data.get("classificationLowerLimitSuperior")),
        ("Elite", data.get("classificationLowerLimitElite")),
    ]
    tiers = [(name, t) for name, t in tiers if isinstance(t, (int, float))]
    tiers.sort(key=lambda t: t[1])
    if not tiers:
        return empty

    current = None
    next_tier = None
    for name, threshold in tiers:
        if score >= threshold:
            current = name
        elif next_tier is None:
            next_tier = (name, threshold)

    if current is None:
        return empty
    if next_tier is None:
        return {"category": current, "next_category": None, "points_to_next": None}
    return {"category": current, "next_category": next_tier[0], "points_to_next": round(next_tier[1] - score)}


# ------------------------------------------------- metric history charts --

METRIC_HISTORY_DAYS = int(os.getenv("DASHBOARD_METRIC_HISTORY_DAYS", "180"))


def vo2max_tier_bounds(age: Optional[int], gender: Optional[str]) -> list[dict]:
    """Ascending [{name, min}] for this person's VO2 max tiers — the same
    Cooper Institute table classify_vo2max scores against, reshaped into
    bands a chart can paint. "Poor" starts at 0 since the table only
    publishes lower limits; the top tier is open-ended (no max), and the
    caller decides where to stop drawing."""
    if age is None or gender not in VO2MAX_TABLE:
        return []
    brackets = VO2MAX_TABLE[gender]
    bracket_key = next((b for b in brackets if b[0] <= age <= b[1]), None)
    if bracket_key is None:
        bracket_key = min(brackets, key=lambda b: min(abs(age - b[0]), abs(age - b[1])))
    t = brackets[bracket_key]
    return [
        {"name": "Poor", "min": 0.0},
        {"name": "Fair", "min": float(t["Fair"])},
        {"name": "Good", "min": float(t["Good"])},
        {"name": "Excellent", "min": float(t["Excellent"])},
        {"name": "Superior", "min": float(t["Superior"])},
    ]


def endurance_tier_bounds(data: Optional[dict]) -> list[dict]:
    """Ascending [{name, min}] for endurance-score tiers. Unlike VO2 max
    these aren't a fixed published table — Garmin returns this account's own
    thresholds on the endurance-score response, so they're read from there
    rather than hardcoded."""
    if not isinstance(data, dict):
        return []
    raw = [
        ("Beginner", data.get("gaugeLowerLimit")),
        ("Intermediate", data.get("classificationLowerLimitIntermediate")),
        ("Trained", data.get("classificationLowerLimitTrained")),
        ("Well Trained", data.get("classificationLowerLimitWellTrained")),
        ("Expert", data.get("classificationLowerLimitExpert")),
        ("Superior", data.get("classificationLowerLimitSuperior")),
        ("Elite", data.get("classificationLowerLimitElite")),
    ]
    tiers = [{"name": n, "min": float(v)} for n, v in raw if isinstance(v, (int, float))]
    tiers.sort(key=lambda t: t["min"])
    return tiers


def build_metric_history(client, metric: str, today: str) -> dict:
    """Time series + tier bands for one expandable stat tile.

    `metric` is "vo2_run", "vo2_bike" or "endurance". Returns points in
    ascending date order plus the tier bands to paint alongside them, so
    the chart can show not just the trend but how far the next tier is.
    """
    start = (date.fromisoformat(today) - timedelta(days=METRIC_HISTORY_DAYS)).isoformat()

    if metric in ("vo2_run", "vo2_bike"):
        key = "generic" if metric == "vo2_run" else "cycling"
        entries = safe_call(client.get_max_metrics_range, start, today, default=[]) or []
        points = []
        for entry in entries:
            section = entry.get(key) if isinstance(entry, dict) else None
            if not isinstance(section, dict):
                continue
            value = section.get("vo2MaxPreciseValue")
            if value is None:
                value = section.get("vo2MaxValue")
            day = section.get("calendarDate")
            if value is not None and day:
                points.append({"date": day, "value": round(float(value), 1)})
        points.sort(key=lambda p: p["date"])

        profile = safe_call(client.get_user_profile, default=None) or {}
        age = calc_age(dig(profile, "userData", "birthDate"), date.fromisoformat(today))
        gender = dig(profile, "userData", "gender")
        return {
            "metric": metric,
            "label": "VO2 Max — Run" if metric == "vo2_run" else "VO2 Max — Bike",
            "unit": "ml/kg/min",
            "decimals": 1,
            "points": points,
            "tiers": vo2max_tier_bounds(age, gender),
        }

    if metric == "endurance":
        data = safe_call(client.get_endurance_score, start, today, default=None) or {}
        dto = data.get("enduranceScoreDTO") if isinstance(data, dict) else None
        # groupMap is week-bucketed (key = week start). groupMax is the best
        # score reached that week, which is what the headline number on the
        # dashboard represents — groupAverage would read lower than the
        # score the tile shows and look like a mismatch.
        points = []
        for day, group in (data.get("groupMap") or {}).items():
            value = (group or {}).get("groupMax")
            if value is not None:
                points.append({"date": day, "value": round(float(value))})
        points.sort(key=lambda p: p["date"])
        return {
            "metric": metric,
            "label": "Endurance Score",
            "unit": "",
            "decimals": 0,
            "points": points,
            "tiers": endurance_tier_bounds(dto),
        }

    return {"error": f"unknown metric '{metric}'"}


def build_training_load(client, today: str) -> dict:
    status = safe_call(client.get_training_status, today, default=None)
    balance_dto = first_value(dig(status, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap")) or {}
    status_dto = first_value(dig(status, "mostRecentTrainingStatus", "latestTrainingStatusData")) or {}
    acute = dig(status_dto, "acuteTrainingLoadDTO") or {}

    ctl = acute.get("dailyTrainingLoadChronic")
    atl = acute.get("dailyTrainingLoadAcute")
    tsb = (ctl - atl) if (ctl is not None and atl is not None) else None
    acwr = acute.get("dailyAcuteChronicWorkloadRatio")

    def balance_row(label: str, key: str) -> Optional[dict]:
        actual = balance_dto.get(f"monthlyLoad{key}")
        tmin = balance_dto.get(f"monthlyLoad{key}TargetMin")
        tmax = balance_dto.get(f"monthlyLoad{key}TargetMax")
        if actual is None:
            return None
        if tmin is not None and actual < tmin:
            state = "below"
        elif tmax is not None and actual > tmax:
            state = "above"
        else:
            state = "within"
        return {"label": label, "actual": actual, "target_min": tmin, "target_max": tmax, "status": state}

    balance = [
        row
        for row in (
            balance_row("Aerobic Low", "AerobicLow"),
            balance_row("Aerobic High", "AerobicHigh"),
            balance_row("Anaerobic", "Anaerobic"),
        )
        if row is not None
    ]

    vo2_run, vo2_bike = fetch_latest_vo2max(client, today)

    profile = safe_call(client.get_user_profile, default=None)
    user_data = dig(profile, "userData") or {}
    age = calc_age(user_data.get("birthDate"), date.today())
    gender = user_data.get("gender")
    vo2_run_class = classify_vo2max(vo2_run, age, gender)
    vo2_bike_class = classify_vo2max(vo2_bike, age, gender)

    endurance = safe_call(client.get_endurance_score, today, default=None)
    endurance_score = dig(endurance, "overallScore")
    endurance_class = classify_endurance_score(endurance_score, endurance)
    hill = safe_call(client.get_hill_score, today, default=None)
    predictions = safe_call(client.get_race_predictions, default=None)

    def race_time(*keys: str) -> Optional[str]:
        for k in keys:
            v = predictions.get(k) if isinstance(predictions, dict) else None
            if v:
                return fmt_duration(v)
        return None

    return {
        "ctl": fmt_int(ctl),
        "atl": fmt_int(atl),
        "tsb": fmt_int(tsb),
        "acwr": round(acwr, 2) if isinstance(acwr, (int, float)) else None,
        "status_phrase": to_title(dig(status_dto, "trainingStatusFeedbackPhrase")),
        "gender": gender,
        "balance": balance,
        "vo2max_run": fmt_decimal1(vo2_run),
        "vo2max_run_category": vo2_run_class["category"],
        "vo2max_run_next_category": vo2_run_class["next_category"],
        "vo2max_run_points_to_next": vo2_run_class["points_to_next"],
        "vo2max_bike": fmt_decimal1(vo2_bike),
        "vo2max_bike_category": vo2_bike_class["category"],
        "vo2max_bike_next_category": vo2_bike_class["next_category"],
        "vo2max_bike_points_to_next": vo2_bike_class["points_to_next"],
        "endurance_score": fmt_int(endurance_score),
        "endurance_category": endurance_class["category"],
        "endurance_next_category": endurance_class["next_category"],
        "endurance_points_to_next": endurance_class["points_to_next"],
        "hill_score": fmt_int(dig(hill, "overallScore")),
        "hill_strength": fmt_int(dig(hill, "strengthScore")),
        "hill_endurance": fmt_int(dig(hill, "enduranceScore")),
        "race_predictions": {
            "5k": race_time("time5K", "raceTime5K"),
            "10k": race_time("time10K", "raceTime10K"),
            "half_marathon": race_time("timeHalfMarathon", "raceTimeHalfMarathon"),
            "marathon": race_time("timeMarathon", "raceTimeMarathon"),
        },
    }


# --------------------------------------------------------------- vitals --

def classify_body_battery(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if value >= 75:
        return "High"
    if value >= 25:
        return "Moderate"
    if value >= 5:
        return "Low"
    return "Very Low"


def build_body_battery(client, today: str) -> Optional[dict]:
    data = safe_call(client.get_body_battery, today, default=None)
    entry = data[0] if isinstance(data, list) and data else None
    series = dig(entry, "bodyBatteryValuesArray") or []
    if not series:
        return None
    last_point = series[-1]
    current = last_point[1] if isinstance(last_point, list) and len(last_point) > 1 else None
    values = [p[1] for p in series if isinstance(p, list) and len(p) > 1 and isinstance(p[1], (int, float))]
    return {
        "current": fmt_int(current),
        "label": classify_body_battery(current),
        "high": fmt_int(max(values)) if values else None,
        "low": fmt_int(min(values)) if values else None,
    }


def build_last_sleep(client) -> Optional[dict]:
    # date_range() is oldest-first; walk it newest-first so we return the
    # most recent night with data instead of stopping at the oldest one.
    for day in reversed(date_range(SLEEP_LOOKBACK_DAYS)):
        data = safe_call(client.get_sleep_data, day, default=None)
        dto = dig(data, "dailySleepDTO") or {}
        total = dto.get("sleepTimeSeconds")
        if not total:
            continue
        overall = dig(data, "dailySleepDTO", "sleepScores", "overall") or {}
        deep = dto.get("deepSleepSeconds") or 0
        light = dto.get("lightSleepSeconds") or 0
        rem = dto.get("remSleepSeconds") or 0
        awake = dto.get("awakeSleepSeconds") or 0
        parts_total = deep + light + rem
        in_bed = parts_total + awake
        stages = [
            {"key": key, "label": label, "seconds": secs,
             "pct": round(100 * secs / in_bed) if in_bed else None}
            for key, label, secs in (
                ("awake", "Awake", awake),
                ("rem", "REM", rem),
                ("light", "Light", light),
                ("deep", "Deep", deep),
            )
        ]
        return {
            "date": day,
            "duration": fmt_duration(total),
            "score": overall.get("value"),
            "quality": to_title(overall.get("qualifierKey")),
            "deep_pct": round(100 * deep / parts_total) if parts_total else None,
            "light_pct": round(100 * light / parts_total) if parts_total else None,
            "rem_pct": round(100 * rem / parts_total) if parts_total else None,
            "stages": stages if in_bed else [],
        }
    return None


def build_resting_hr(client, today: str) -> Optional[dict]:
    day_data = safe_call(client.get_rhr_day, today, default=None)
    fresh = dig(day_data, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE")
    fresh_val = fresh[0].get("value") if isinstance(fresh, list) and fresh else None

    week_start = (date.today() - timedelta(days=6)).isoformat()
    week_data = safe_call(client.get_rhr_daily, week_start, today, default=None) or []
    values = []
    for entry in week_data if isinstance(week_data, list) else []:
        v = entry.get("value") if isinstance(entry, dict) else None
        if v is None:
            v = dig(entry, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE")
            v = v[0].get("value") if isinstance(v, list) and v else None
        if isinstance(v, (int, float)):
            values.append(v)

    if fresh_val is None and not values:
        return None
    return {
        "fresh": fmt_int(fresh_val),
        "week_avg": fmt_int(sum(values) / len(values)) if values else None,
    }


def build_fitness_age(client, today: str) -> Optional[dict]:
    data = safe_call(client.get_fitnessage_data, today, default=None)
    fitness_age = dig(data, "fitnessAge")
    if fitness_age is None:
        return None
    achievable = dig(data, "achievableFitnessAge")
    return {
        "fitness_age": round(fitness_age, 1),
        "chronological_age": dig(data, "chronologicalAge"),
        "achievable_age": round(achievable, 1) if isinstance(achievable, (int, float)) else achievable,
    }


def build_vitals(client, today: str, readiness_entries: list[dict]) -> dict:
    latest = None
    if readiness_entries:
        latest = sorted(readiness_entries, key=lambda e: e.get("timestampLocal") or "")[-1]
    factors = build_vitals_factors(latest)

    body_battery = build_body_battery(client, today)
    last_sleep = build_last_sleep(client)
    resting_hr = build_resting_hr(client, today)
    fitness_age = build_fitness_age(client, today)

    missing = [f["label"] for f in factors if f.get("no_data")]
    gap_callout = None
    if missing:
        gap_callout = (
            "Wearable gap: " + ", ".join(missing) + " not reporting today. "
            "Numbers above reflect whatever Garmin does have recorded."
        )

    return {
        "factors": factors,
        "body_battery": body_battery,
        "last_sleep": last_sleep,
        "resting_hr": resting_hr,
        "fitness_age": fitness_age,
        "gap_callout": gap_callout,
    }


# ---------------------------------------------------------- activities --

LEG_TYPE_LABELS = {
    "transition": "Transition",
    "open_water_swimming": "Swim",
    "lap_swimming": "Swim",
    "cycling": "Bike",
    "road_biking": "Bike",
    "mountain_biking": "Bike",
    "running": "Run",
    "trail_running": "Run",
}


def leg_label(activity: dict) -> str:
    type_key = dig(activity, "activityType", "typeKey") or ""
    if type_key in LEG_TYPE_LABELS:
        return LEG_TYPE_LABELS[type_key]
    name = activity.get("activityName") or type_key.replace("_", " ").title()
    return name


def summarize_activity(activity: dict, is_leg: bool = False) -> dict:
    type_key = dig(activity, "activityType", "typeKey") or ""
    distance = activity.get("distance")
    duration = activity.get("duration") or activity.get("movingDuration")
    avg_hr = activity.get("averageHR")
    max_hr = activity.get("maxHR")
    return {
        "activity_id": activity.get("activityId"),
        "name": leg_label(activity) if is_leg else (activity.get("activityName") or type_key.replace("_", " ").title()),
        "type_key": type_key,
        "distance": fmt_distance(distance, type_key, is_leg=is_leg),
        "duration": fmt_duration(duration),
        "pace": fmt_pace_or_speed(distance, duration, type_key),
        "pace_label": pace_label_for(type_key),
        "avg_hr": fmt_int(avg_hr) if avg_hr else (f"max {fmt_int(max_hr)}" if max_hr else "—"),
        "elevation_gain": fmt_elevation(activity.get("elevationGain")),
        "calories": fmt_int(activity.get("calories")) or "—",
        "avg_power": fmt_int(activity.get("avgPower")),
        "date": (activity.get("startTimeLocal") or "")[:10],
        "training_load": activity.get("activityTrainingLoad"),
    }


def fetch_multisport_legs(client, activity_id: Any) -> list[dict]:
    """Best-effort fetch of the child legs (swim/T1/bike/T2/run) of a
    multisport parent activity. Garmin's own web client resolves these via
    an undocumented endpoint keyed by child activity ids; if that shape
    doesn't hold for this account, we just skip the expand-into-legs UI for
    this row instead of failing the whole dashboard."""
    try:
        detail = client.connectapi(f"/activity-service/activity/{activity_id}")
        child_ids = (
            dig(detail, "metadataDTO", "childIds")
            or detail.get("childIds")
            or []
        )
        legs = []
        for child_id in child_ids:
            child = client.get_activity(str(child_id))
            if child:
                # get_activity() (single-activity detail) has a different
                # shape than the list endpoint everything else here uses:
                # metrics live under summaryDTO and the type key is under
                # activityTypeDTO, not top-level distance/duration/activityType.
                flat = dict(child)
                flat.update(child.get("summaryDTO") or {})
                flat["activityType"] = child.get("activityTypeDTO") or child.get("activityType")
                legs.append(summarize_activity(flat, is_leg=True))
        return legs
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not fetch multisport legs for %s: %s", activity_id, exc)
        return []


def build_route_log(client, activities: list[dict]) -> list[dict]:
    rows = []
    for activity in activities:
        row = summarize_activity(activity)
        type_key = dig(activity, "activityType", "typeKey") or ""
        if type_key == "multi_sport":
            legs = fetch_multisport_legs(client, activity.get("activityId"))
            if legs:
                row["legs"] = legs
        rows.append(row)
    return rows


MAX_PACE_GAP_CALENDAR_RANGE_DAYS = 40  # calendar month-view sized; beyond this (e.g. MCP's list_activities over months) skip the extra per-activity fetch rather than fire off a slow burst of heavy Garmin calls


def build_activity_calendar(
    client, start_date: str, end_date: str, race_dates: frozenset[str] | set[str] = frozenset()
) -> dict[str, list[dict]]:
    """Sparse per-day activity summaries for an arbitrary date range, keyed
    by ISO date — powers the dashboard's calendar view (and the MCP's
    list_activities tool, for looking further back than the dashboard's
    default rolling window). Days with no activity are simply absent from
    the result rather than listed empty.

    `race_dates` are the user's flagged race days (the same ones that put a
    flag on the fitness chart) — see _race_activity_ids for how the race
    itself is picked out of that day's activities."""
    activities = safe_call(client.get_activities_by_date, start_date, end_date, default=[]) or []
    race_ids = _race_activity_ids(activities, race_dates)

    # Session-type classification compares an activity's distance against
    # its peers to judge "long" — grouped by sport so a ride's km isn't
    # compared against a run's.
    running = [a for a in activities if _is_run(a)]
    cycling = [a for a in activities if _is_cycling(a)]

    # Same pace/HR structure check build_training_plan uses (see
    # _has_interval_structure) — it's the only thing that earns an
    # "interval" badge. Only for a range small enough to be an actual
    # calendar view; a long MCP list_activities lookup skips fetching full
    # per-second streams for potentially hundreds of activities, so nothing
    # in it is tagged "interval" (no evidence either way).
    structured_ids: set = set()
    try:
        range_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    except ValueError:
        range_days = MAX_PACE_GAP_CALENDAR_RANGE_DAYS + 1  # malformed dates: safest to skip
    classifiable = [(a, "running") for a in running] + [(a, "cycling") for a in cycling]
    if range_days <= MAX_PACE_GAP_CALENDAR_RANGE_DAYS and classifiable:
        with ThreadPoolExecutor(max_workers=TREND_FETCH_WORKERS) as pool:
            futures = {
                a["activityId"]: pool.submit(_has_interval_structure, client, a, kind)
                for a, kind in classifiable
                if a.get("activityId") is not None
            }
            structured_ids = {aid for aid, f in futures.items() if f.result()}

    # Keyed on the full timestamp (not just the date) so each day's list can
    # be sorted earliest-first below — desktop shows that top to bottom,
    # the mobile "rotated" card layout shows it left to right.
    by_date: dict[str, list[tuple[str, dict]]] = {}
    for activity in activities:
        start = activity.get("startTimeLocal") or ""
        d = start[:10]
        if not d:
            continue
        row = summarize_activity(activity)
        type_key = dig(activity, "activityType", "typeKey") or ""
        if type_key == "multi_sport":
            legs = fetch_multisport_legs(client, activity.get("activityId"))
            if legs:
                row["legs"] = legs
        if activity.get("activityId") in race_ids:
            row["session_type"] = "race"
        elif _is_run(activity):
            row["session_type"] = _classify_session_type(
                activity, running, has_interval_structure=activity.get("activityId") in structured_ids
            )
        elif _is_cycling(activity):
            row["session_type"] = _classify_session_type(
                activity, cycling, has_interval_structure=activity.get("activityId") in structured_ids
            )
        by_date.setdefault(d, []).append((start, row))

    return {d: [row for _, row in sorted(entries, key=lambda e: e[0])] for d, entries in by_date.items()}


_ZONE_BUCKET_CUTOFFS = {"aerobic_low_max": 2, "aerobic_high_max": 4}  # zone <=2 low, <=4 high, else anaerobic


def _bucket_zone_time(zones: list[dict]) -> dict[str, float]:
    buckets = {"aerobic_low": 0.0, "aerobic_high": 0.0, "anaerobic": 0.0}
    for z in zones:
        num = z.get("zoneNumber")
        secs = z.get("secsInZone") or 0
        if num is None:
            continue
        if num <= _ZONE_BUCKET_CUTOFFS["aerobic_low_max"]:
            buckets["aerobic_low"] += secs
        elif num <= _ZONE_BUCKET_CUTOFFS["aerobic_high_max"]:
            buckets["aerobic_high"] += secs
        else:
            buckets["anaerobic"] += secs
    return buckets


def build_activity_training_load(client, activity_id: Any, prefer_power: bool, total_load: Optional[float]) -> dict:
    """Per-activity aerobic-low / aerobic-high / anaerobic LOAD contribution —
    the same three categories the Training Load Balance panel tracks as a
    monthly aggregate (monthlyLoadAerobicLow/High/Anaerobic), broken down
    here for just this one activity so it's clear which sessions are
    actually driving that monthly number.

    Garmin doesn't expose a per-activity 3-way load split directly — only
    one combined `activityTrainingLoad` figure per activity. This splits
    that figure across the three buckets in proportion to how the
    activity's own zone TIME was distributed (e.g. an activity that spent
    60% of its time in aerobic-low zones is credited with ~60% of its load
    there) — a derived estimate, not a number Garmin reports directly, but
    it's built from the same zone data and should track the monthly
    aggregate reasonably closely over a full month of activities.

    Prefers power zones (the standard way cyclists gauge effort) when the
    activity actually has power data; falls back to heart-rate zones
    otherwise, which is the only signal available for running (or for a
    ride with no power meter)."""
    zones: list[dict] = []
    source = None

    if prefer_power:
        power_zones = safe_call(client.get_activity_power_in_timezones, str(activity_id), default=None) or []
        if any((z.get("secsInZone") or 0) > 0 for z in power_zones):
            zones = power_zones
            source = "power"

    if not zones:
        hr_zones = safe_call(client.get_activity_hr_in_timezones, str(activity_id), default=None) or []
        if hr_zones:
            zones = hr_zones
            source = "heart_rate"

    if not zones or not total_load:
        return {"source": None, "aerobic_low": None, "aerobic_high": None, "anaerobic": None}

    buckets = _bucket_zone_time(zones)
    total_seconds = sum(buckets.values())
    if not total_seconds:
        return {"source": None, "aerobic_low": None, "aerobic_high": None, "anaerobic": None}

    return {
        "source": source,
        "aerobic_low": round(total_load * buckets["aerobic_low"] / total_seconds),
        "aerobic_high": round(total_load * buckets["aerobic_high"] / total_seconds),
        "anaerobic": round(total_load * buckets["anaerobic"] / total_seconds),
    }


def coach_note_is_stale(coach_note: Optional[dict], last_activity: Optional[dict]) -> bool:
    """A coach note is advice for the athlete's very next session — once any
    activity has happened since it was given, it's been acted on (whether
    or not it matched what the note said) and shouldn't keep overriding the
    dashboard's own next-session suggestion (Training Plan match, or the
    auto-suggestion) for up to FRESHNESS_HOURS more.

    Compared at calendar-date granularity, not exact timestamps: the note's
    created_at is UTC (see coach_notes.py) while an activity's date is the
    athlete's local wall-clock, and this app has no per-user timezone to
    reconcile the two exactly — close enough given coach notes are already
    freshness-windowed in tens of hours."""
    if not coach_note or not last_activity:
        return False
    created_date = (coach_note.get("created_at") or "")[:10]
    activity_date = (last_activity.get("date") or "")[:10]
    return bool(created_date and activity_date and activity_date >= created_date)


def build_last_activity(client, activities: list[dict]) -> Optional[dict]:
    if not activities:
        return None
    activity = activities[0]
    summary = summarize_activity(activity)
    activity_id = activity.get("activityId")

    zones = safe_call(client.get_activity_hr_in_timezones, str(activity_id), default=None) or []
    zone_lines = []
    for z in zones:
        secs = z.get("secsInZone")
        zone_num = z.get("zoneNumber")
        low = z.get("zoneLowBoundary")
        if secs:
            zone_lines.append(f"{fmt_duration(secs)} in Z{zone_num} ({low}+)" if low else f"{fmt_duration(secs)} in Z{zone_num}")

    summary["activity_id"] = activity_id
    summary["description"] = activity.get("description") or ""
    summary["zone_breakdown"] = zone_lines
    return summary


# ------------------------------------------------------------- top level --

STRENGTH_TYPES = {"strength_training"}


def days_since_activity_type(activities: list[dict], type_keys: set[str]) -> Optional[int]:
    dates = []
    for a in activities:
        if (dig(a, "activityType", "typeKey") or "") in type_keys:
            d = (a.get("startTimeLocal") or "")[:10]
            if d:
                try:
                    dates.append(date.fromisoformat(d))
                except ValueError:
                    pass
    return (date.today() - max(dates)).days if dates else None


def build_next_waypoint(training_load: dict, readiness_entries: list[dict], activities: list[dict]) -> dict:
    """Turn training-load balance + readiness + recent activity history into
    concrete session suggestions ('4x4 intervals on the bike/run') instead of
    just restating raw numbers, plus a running set of coaching-style notes."""
    latest = sorted(readiness_entries, key=lambda e: e.get("timestampLocal") or "")[-1] if readiness_entries else {}
    # Garmin reports recoveryTime in MINUTES, not hours — treating it as
    # hours directly (the old bug here) turned e.g. a real ~19h recovery
    # into a nonsensical "1164h". REACHED_ZERO means fully recovered even
    # if Garmin left a stale nonzero minutes value behind.
    recovery_minutes = latest.get("recoveryTime")
    if latest.get("recoveryTimeChangePhrase") == "REACHED_ZERO":
        recovery_minutes = 0
    recovery_hours = (recovery_minutes / 60) if recovery_minutes else None
    acwr = training_load.get("acwr")
    balance = {row["label"]: row for row in training_load.get("balance", [])}

    def deficit_ratio(label: str) -> Optional[float]:
        row = balance.get(label)
        if not row or row["status"] != "below" or not row.get("target_min"):
            return None
        return row["actual"] / row["target_min"]

    deficits = [(label, deficit_ratio(label)) for label in ("Anaerobic", "Aerobic High")]
    deficits = [d for d in deficits if d[1] is not None]
    deficits.sort(key=lambda t: t[1])

    # Recovery time is a countdown, not a verdict on right now — the athlete
    # isn't asking "am I clear this instant", they're asking what to do for
    # their NEXT session, which in practice is tomorrow, not later today.
    # So judge readiness against whether recovery will have cleared by
    # roughly a day out rather than against the current moment.
    recovery_clears_by_next_session = recovery_hours is None or recovery_hours <= 30
    ready_for_intensity = recovery_clears_by_next_session and (acwr is None or acwr <= 1.2)
    suggest_for_tomorrow = bool(recovery_hours) and ready_for_intensity

    def modality(kind: str) -> str:
        if kind == "interval":
            return "4×4 intervals on the bike or run (4 min hard / 3 min easy, ×4)"
        if kind == "tempo":
            return "Tempo run or tempo bike — steady zone 3 effort, 30–40 min"
        if kind == "easy":
            return "Easy aerobic run or bike — load balance is on track, keep it steady"
        if kind == "recover":
            return "Easy aerobic spin, swim, or jog — let recovery time / ACWR settle before adding intensity"
        raise ValueError(kind)

    primary_sessions: list[str] = []
    if not ready_for_intensity:
        primary_sessions.append(modality("recover"))
    elif deficits:
        top_label = deficits[0][0]
        primary_sessions.append(modality("interval" if top_label == "Anaerobic" else "tempo"))
        if len(deficits) > 1:
            other = deficits[1][0]
            primary_sessions.append(modality("interval" if other == "Anaerobic" else "tempo"))
    else:
        primary_sessions.append(modality("easy"))

    if suggest_for_tomorrow:
        primary_sessions = ["Tomorrow: " + s for s in primary_sessions]

    sessions: list[str] = list(primary_sessions)

    days_strength = days_since_activity_type(activities, STRENGTH_TYPES)
    if days_strength is None or days_strength >= 4:
        tail = f" ({days_strength}d since last)" if days_strength is not None else ""
        sessions.append("Lower body or full body session at the gym" + tail)

    notes: list[str] = []
    if isinstance(acwr, (int, float)):
        if acwr < 0.8:
            notes.append(
                f"ACWR is low ({acwr}) — you're currently training lighter than your fitness can handle. "
                "Good window to insert a longer or more intense session."
            )
        elif acwr > 1.3:
            notes.append(f"ACWR is elevated ({acwr}) — prioritize recovery before adding more intensity.")

    for label, ratio in deficits:
        row = balance[label]
        kind = "interval" if label == "Anaerobic" else "tempo"
        notes.append(
            f"{label} is under target ({row['actual']:.0f} vs {row['target_min']:.0f}–{row['target_max']:.0f}) "
            f"— more {kind} work would help close the gap."
        )

    if recovery_hours:
        cleared = datetime.now() + timedelta(hours=recovery_hours)
        notes.append(f"Garmin recovery time: {round(recovery_hours)}h — clears around {cleared.strftime('%a %H:%M')}")

    status_phrase = training_load.get("status_phrase")
    if status_phrase:
        notes.append(f"Garmin training status: {status_phrase}")

    return {
        "title": "Suggested next session",
        "sessions": sessions,
        "notes": notes,
        "source": "auto",
    }


RUN_TYPE_HINT = "run"
CYCLING_TYPE_HINTS = ("cycl", "bik")  # covers road_biking, mountain_biking, e_bike_mountain, cycling, ...
SWIM_TYPE_HINT = "swim"
STRENGTH_TYPE_HINT = "strength"
HIKE_TYPE_HINTS = ("hik", "mountaineering")  # "hik" is safe against "biking" -- no h in it

# Kinds whose completed sessions get matched against a plan by effort TYPE
# (easy/tempo/interval/long, via _classify_session_type) — sports where that
# distinction is meaningful and Garmin's Training Effect data supports it.
# Everything else (strength, and anything unrecognized) matches on date
# alone: there's no "tempo" strength session to tell apart from an "easy"
# one, so a planned strength session just claims the nearest completed
# strength activity this week.
TYPE_CLASSIFIABLE_KINDS = {"running", "cycling", "swimming"}

# Per-kind pace-based interval-detection threshold — not the broader type-
# classification above. Each kind uses a DIFFERENT underlying signal (see
# _has_interval_structure): running trusts the raw per-second speed stream
# (activity_sustained_pace_gap), since it correctly caught a real interval
# run recorded under plain auto-lap, with no manual lap presses at all.
# Cycling can't use that signal — ordinary terrain/traffic alone (a real
# hill's climb+descent, city stop-and-go) swings sustained speed as much as
# genuine intervals do, with no reliable way to tell them apart from speed
# alone. Cycling instead trusts the athlete's own manually-pressed laps
# (activity_manual_lap_pace_gap) — sidesteps the terrain/traffic problem
# entirely (nobody manually laps at every hilltop or red light), at the
# cost of missing a genuinely-interval ride that was only auto-lapped.
PACE_INTERVAL_THRESHOLDS = {"running": 0.25, "cycling": 0.35}

# A pace gap alone can't tell intervals from hills: every trail run checked
# (Arosa 0.69, Küssnacht 0.74, Hauenstein 0.65, Reigoldswil 0.80) out-scored
# the two real interval runs (0.58, 0.68). Heart rate tells them apart — in
# intervals HR rises on the fast stretches and falls on the deliberately
# easy ones, while a climb is slow with HR still high. Measured as the HR
# gap between the fastest and slowest fifth of a run (see
# activity_hr_follows_pace_gap): the interval runs scored +29/+32 bpm, the
# trail runs +1 to +14, steady road runs and races mostly under +15. Same
# idea as the cycling check's _MANUAL_LAP_HR_DROP_BPM, on the stream rather
# than on laps.
_HR_FOLLOWS_PACE_MIN_BPM = 20


def _has_interval_structure(client, activity: dict, kind: str) -> bool:
    """Whether an activity's own pace and heart-rate data show work/recovery
    structure — the only thing that earns an "interval" tag (see
    _classify_session_type). Running: a large sustained pace gap that HR
    follows (pace alone if there's no HR stream). Cycling: a large gap
    across manually-pressed laps, which activity_manual_lap_pace_gap
    already requires HR to confirm, or laps alternating work/rest power
    (activity_lap_power_structure — catches hill repeats, where speed is
    backwards). No usable data means no evidence, so False."""
    if kind == "running":
        gap = activity_sustained_pace_gap(client, activity)
        if gap is None or gap < PACE_INTERVAL_THRESHOLDS["running"]:
            return False
        hr_gap = activity_hr_follows_pace_gap(client, activity)
        return hr_gap is None or hr_gap >= _HR_FOLLOWS_PACE_MIN_BPM
    if kind == "cycling":
        gap = activity_manual_lap_pace_gap(client, activity)
        if gap is not None and gap >= PACE_INTERVAL_THRESHOLDS["cycling"]:
            return True
        return activity_lap_power_structure(client, activity)
    return False

_PLAN_KIND_ALIASES = {
    "run": "running", "running": "running", "jog": "running", "jogging": "running",
    "bike": "cycling", "biking": "cycling", "cycle": "cycling", "cycling": "cycling", "ride": "cycling",
    "swim": "swimming", "swimming": "swimming",
    "strength": "strength", "gym": "strength", "weights": "strength", "lifting": "strength",
    "strength_training": "strength", "strength training": "strength",
}


def _normalize_plan_kind(raw: Optional[str]) -> str:
    """A plan session with no activity_type at all defaults to "running" —
    every plan pushed before this field existed is a pure running plan, so
    this keeps those matching exactly as before. An explicit but unrecognized
    value (e.g. "yoga") falls back to "other": date-only matching, same as
    strength, rather than erroring on a sport we don't have a specific
    classifier for."""
    if not raw:
        return "running"
    return _PLAN_KIND_ALIASES.get(raw.strip().lower(), "other")


def _is_run(activity: dict) -> bool:
    type_key = (dig(activity, "activityType", "typeKey") or "").lower()
    return RUN_TYPE_HINT in type_key


def _is_cycling(activity: dict) -> bool:
    type_key = (dig(activity, "activityType", "typeKey") or "").lower()
    return any(hint in type_key for hint in CYCLING_TYPE_HINTS)


def _activity_kind(activity: dict) -> str:
    type_key = (dig(activity, "activityType", "typeKey") or "").lower()
    if RUN_TYPE_HINT in type_key:
        return "running"
    if any(hint in type_key for hint in CYCLING_TYPE_HINTS):
        return "cycling"
    if SWIM_TYPE_HINT in type_key:
        return "swimming"
    if STRENGTH_TYPE_HINT in type_key:
        return "strength"
    return "other"


# Markers for a planned entry that describes what a given DAY is, rather
# than a workout owed. Deliberately specific phrases, not a bare "rest" —
# interval sessions routinely say things like "400m jog rest" and must not
# be caught by this.
_DATE_PINNED_PATTERNS = (
    "travel day",
    "rest day",
    "day off",
    "off day",
    "full rest",
    "or rest",
    "easy/rest",
    "no structured training",
    "no training",
    "depart",
    "rehab reps only",
)


def _is_date_pinned(entry: dict, kind: str) -> bool:
    """True for a planned entry tied to its own calendar date — a travel
    day, a rest day, "no structured training" — as opposed to a session the
    athlete still owes.

    These must never slide forward: a travel day that has passed wasn't
    missed, it happened. Rescheduling one onto today (which is what the
    "slide unmatched sessions to the next free day" rule did) claims the
    athlete still has to travel, on a day they already travelled.

    "other" counts as pinned on its own: it's the type used for entries
    with no sport to match against, so there's nothing to reschedule
    meaningfully even when it isn't one of the phrases above."""
    if kind == "other":
        return True
    text = (entry.get("session") or "").lower()
    return any(pattern in text for pattern in _DATE_PINNED_PATTERNS)


def _classify_plan_text(text: str) -> str:
    """Best-effort session-type tag from a planned session's free text
    (e.g. "6x100m strides/sprints..." -> interval). "other" means the text
    didn't say anything type-specific — those fall back to any-type
    matching rather than being stranded."""
    t = (text or "").lower()
    if any(k in t for k in ("interval", "sprint", "stride", "vo2", " rep", "×", "hill rep")):
        return "interval"
    if any(k in t for k in ("tempo", "threshold")):
        return "tempo"
    if "long" in t:
        return "long"
    if any(k in t for k in ("easy", "recovery", "aerobic", "z1", "z2", "jog")):
        return "easy"
    return "other"


# What each Garmin label says about effort. "interval" here only means "hard"
# — _classify_session_type won't call a session an interval from its label
# alone; that takes measured pace/HR structure.
_TE_LABEL_TYPES = {
    "RECOVERY": "easy",
    "BASE": "easy",
    "AEROBIC_BASE": "easy",
    "MAINTAINING": "easy",
    "TEMPO": "tempo",
    "THRESHOLD": "tempo",
    "LACTATE_THRESHOLD": "tempo",
    "VO2MAX": "interval",
    "ANAEROBIC_CAPACITY": "interval",
    "SPRINT": "interval",
}


# Lap/split and raw-detail data for a completed activity never change, so a
# per-process cache with no TTL is safe — shared across whatever needs them
# (pace-variability check, MCP lap-lookup tool) so a given activity's data
# is fetched from Garmin at most once per process lifetime, not once per
# caller.
_activity_splits_cache: dict[str, Optional[dict]] = {}
_activity_details_cache: dict[str, Optional[dict]] = {}


def fetch_activity_splits(client, activity_id: Any) -> Optional[dict]:
    key = str(activity_id)
    if key not in _activity_splits_cache:
        _activity_splits_cache[key] = safe_call(client.get_activity_splits, key, default=None)
    return _activity_splits_cache[key]


def fetch_activity_details(client, activity_id: Any) -> Optional[dict]:
    key = str(activity_id)
    if key not in _activity_details_cache:
        _activity_details_cache[key] = safe_call(client.get_activity_details, key, default=None)
    return _activity_details_cache[key]


# Per-kind threshold values live in PACE_INTERVAL_THRESHOLDS, above
# TYPE_CLASSIFIABLE_KINDS — running's 0.25 comes from Garmin's raw per-
# second speed stream (not lap markers): a known interval session scored
# ~0.59-0.60 regardless of smoothing window size (5/10/15s buckets all
# agreed); steady easy/tempo/threshold road runs sat at 0.04-0.20. Trail
# runs with big climbs score as high as intervals (0.65-0.80), which is why
# _has_interval_structure also requires HR to follow the pace swings.
_SUSTAINED_PACE_BUCKET_SECONDS = 15  # real-time smoothing window, not a sample count -- robust to whatever the device's recording interval was
_SUSTAINED_PACE_MIN_SPEED = 1.0  # m/s -- below this treated as stopped (traffic light, water break), not a "slow sustained pace"
_SUSTAINED_PACE_MIN_BUCKETS = 10


def activity_sustained_pace_gap(client, activity: dict) -> Optional[float]:
    """How much faster an activity's sustained hard efforts are than its
    sustained easier ones, from Garmin's raw per-second pace stream rather
    than lap markers — so it still catches real interval structure even
    when auto-lap is set coarser than the actual work/rest cycle (e.g. a 1km
    auto-lap covering an 800m rep + 200m jog as one blended lap), which
    lap-based detection is blind to.

    Bucketed into real-TIME windows (not sample count, since recording
    interval varies) to smooth GPS jitter without blending away a short
    rep. Near-stopped buckets (a traffic light, a water break) are dropped
    entirely rather than counted as "slow" — a stop isn't a sustained
    recovery pace, it's not moving at all, and this exclusion is what
    makes the metric robust to an isolated stop the way a stdev-based
    variance measure isn't (one stop inflates variance broadly; it barely
    moves a percentile).

    Compares the fast ~20th-percentile bucket against the slow ~80th-
    percentile (still-moving) bucket. None if there's no usable stream
    (fetch failure, or too short/too few moving buckets to say anything)
    — callers should treat that as "no signal", not "definitely steady"."""
    activity_id = activity.get("activityId")
    if activity_id is None:
        return None
    details = fetch_activity_details(client, activity_id)
    if not details:
        return None
    mdesc = details.get("metricDescriptors") or []
    speed_idx = next((m.get("metricsIndex") for m in mdesc if m.get("key") == "directSpeed"), None)
    ts_idx = next((m.get("metricsIndex") for m in mdesc if m.get("key") == "directTimestamp"), None)
    if speed_idx is None or ts_idx is None:
        return None

    buckets: dict[int, list[float]] = {}
    for sample in details.get("activityDetailMetrics") or []:
        metrics = sample.get("metrics") or []
        if len(metrics) <= max(speed_idx, ts_idx):
            continue
        speed, ts_ms = metrics[speed_idx], metrics[ts_idx]
        if speed is None or ts_ms is None:
            continue
        bucket_key = int(ts_ms // 1000) // _SUSTAINED_PACE_BUCKET_SECONDS
        buckets.setdefault(bucket_key, []).append(speed)

    bucket_speeds = [statistics.mean(v) for v in buckets.values() if v]
    moving = [s for s in bucket_speeds if s >= _SUSTAINED_PACE_MIN_SPEED]
    if len(moving) < _SUSTAINED_PACE_MIN_BUCKETS:
        return None

    paces = sorted(1000 / s for s in moving)  # sec/km, ascending: fastest first
    n = len(paces)
    fast_pace = paces[int(n * 0.20)]
    slow_pace = paces[int(n * 0.80)]
    mean_pace = statistics.mean(paces)
    if not mean_pace:
        return None
    return (slow_pace - fast_pace) / mean_pace


# HR trails a change of pace by tens of seconds, so each speed bucket is
# paired with the HR of the buckets 30-75s after it (2-4 buckets of 15s).
_HR_LAG_BUCKETS = (2, 3, 4)


def activity_hr_follows_pace_gap(client, activity: dict) -> Optional[float]:
    """How much higher heart rate runs on an activity's fastest stretches
    than on its slowest moving ones, in bpm — from the same per-second
    stream and 15s buckets as activity_sustained_pace_gap.

    Large and positive means the slow stretches were genuine recoveries
    (interval work/rest). Near zero means the pace swings weren't changes
    of effort — on a hilly run the slow stretches are climbs, where HR
    stays high. Compares the mean lagged HR of the fastest fifth of moving
    buckets against the slowest fifth. None when there's no HR stream or
    too little data, which callers treat as "no HR evidence either way"."""
    activity_id = activity.get("activityId")
    if activity_id is None:
        return None
    details = fetch_activity_details(client, activity_id)
    if not details:
        return None
    mdesc = details.get("metricDescriptors") or []
    index = {m.get("key"): m.get("metricsIndex") for m in mdesc}
    speed_idx, hr_idx, ts_idx = index.get("directSpeed"), index.get("directHeartRate"), index.get("directTimestamp")
    if speed_idx is None or hr_idx is None or ts_idx is None:
        return None

    speeds: dict[int, list[float]] = {}
    hrs: dict[int, list[float]] = {}
    for sample in details.get("activityDetailMetrics") or []:
        metrics = sample.get("metrics") or []
        if len(metrics) <= max(speed_idx, hr_idx, ts_idx) or metrics[ts_idx] is None:
            continue
        bucket_key = int(metrics[ts_idx] // 1000) // _SUSTAINED_PACE_BUCKET_SECONDS
        if metrics[speed_idx] is not None:
            speeds.setdefault(bucket_key, []).append(metrics[speed_idx])
        if metrics[hr_idx] is not None:
            hrs.setdefault(bucket_key, []).append(metrics[hr_idx])

    hr_by_bucket = {k: statistics.mean(v) for k, v in hrs.items() if v}

    def lagged_hr(k: int) -> Optional[float]:
        later = [hr_by_bucket[k + lag] for lag in _HR_LAG_BUCKETS if k + lag in hr_by_bucket]
        return statistics.mean(later) if later else None

    moving = sorted(
        (statistics.mean(v), k) for k, v in speeds.items() if v and statistics.mean(v) >= _SUSTAINED_PACE_MIN_SPEED
    )
    if len(moving) < _SUSTAINED_PACE_MIN_BUCKETS:
        return None
    fifth = max(1, len(moving) // 5)
    slow_hr = [h for h in (lagged_hr(k) for _, k in moving[:fifth]) if h is not None]
    fast_hr = [h for h in (lagged_hr(k) for _, k in moving[-fifth:]) if h is not None]
    if not slow_hr or not fast_hr:
        return None
    return statistics.mean(fast_hr) - statistics.mean(slow_hr)


# Auto-lap-by-distance/time produces near-identical lap lengths (see real
# examples below); a lap distance coefficient of variation this high means
# the athlete pressed lap themselves at points they chose, not points a
# fixed interval produced. Calibrated against two real rides: an auto-
# lapped-every-5km ride scored 0.277 (8 identical 5000m laps + one trailing
# partial), a manually-lapped interval ride (long warmup, 5 alternating
# work/rest laps, long cooldown) scored 1.033. 0.5 sits with real margin on
# both sides.
_MANUAL_LAP_DISTANCE_CV_THRESHOLD = 0.5
_MIN_LAP_DISTANCE_M = 100  # drop near-zero blips (GPS pause artifacts) when pulling lap paces
_MIN_LAPS_FOR_MANUAL_GAP = 3
# Real interval recovery laps in the validated example dropped ~30-35bpm
# vs the work laps (avgHR ~155-161 work, ~120-127 recovery) -- 10bpm is a
# conservative floor with real margin under that, not a tight fit to one
# example.
_MANUAL_LAP_HR_DROP_BPM = 10


def _laps_look_manually_pressed(laps: list[dict]) -> bool:
    distances = [lap.get("distance") or 0 for lap in laps if (lap.get("distance") or 0) > 0]
    if len(distances) < 3:
        return False
    mean_distance = statistics.mean(distances)
    if not mean_distance:
        return False
    return (statistics.pstdev(distances) / mean_distance) >= _MANUAL_LAP_DISTANCE_CV_THRESHOLD


def activity_manual_lap_pace_gap(client, activity: dict) -> Optional[float]:
    """Sustained fast-vs-slow pace gap computed across an activity's OWN
    laps — but only when those laps look like the athlete pressed lap
    themselves (see _laps_look_manually_pressed), not Garmin's auto-lap.
    This is the right signal for cycling specifically: activity_sustained_
    pace_gap (raw per-second stream) produces false positives there from
    ordinary terrain/traffic (a real hill's climb+descent, or city traffic
    stop-and-go, can swing sustained speed as much as genuine intervals
    do) — trusting the athlete's own explicit lap markers instead sidesteps
    that entirely, since nobody manually laps at every traffic light or
    hilltop. The tradeoff: an interval session recorded under plain auto-
    lap (no manual lap presses) won't be caught by this at all — returns
    None, same as "no signal", rather than guessing.

    Manually-pressed laps aren't proof of intervals by themselves, though —
    someone lapping a hilly ride at each climb/descent boundary produces the
    same irregular-lap-distance signature as someone lapping work/rest reps.
    Pace alone can't tell those apart (a climb is genuinely slow too). HR
    can: real interval recovery drops HR meaningfully because the athlete
    is deliberately backing off, while climbing at low speed is still hard
    effort against gravity and typically keeps HR up. So when HR data is
    available, the "slow" side of the gap must also show a real HR drop
    (see _MANUAL_LAP_HR_DROP_BPM) — otherwise this returns None rather than
    treating a slow-but-still-working lap as recovery. Without HR data this
    check is simply skipped (pace alone still applies)."""
    activity_id = activity.get("activityId")
    if activity_id is None:
        return None
    splits = fetch_activity_splits(client, activity_id)
    if not splits:
        return None
    laps = splits.get("lapDTOs") or []
    if not _laps_look_manually_pressed(laps):
        return None
    paced_laps = []
    for lap in laps:
        lap_distance = lap.get("distance") or 0
        lap_duration = lap.get("duration") or 0
        if lap_distance >= _MIN_LAP_DISTANCE_M and lap_duration > 0:
            paced_laps.append((lap_duration / (lap_distance / 1000), lap))
    if len(paced_laps) < _MIN_LAPS_FOR_MANUAL_GAP:
        return None
    paced_laps.sort(key=lambda pl: pl[0])
    n = len(paced_laps)
    fast_pace, fast_lap = paced_laps[int(n * 0.20)]
    slow_pace, slow_lap = paced_laps[int(n * 0.80)]
    mean_pace = statistics.mean(p for p, _ in paced_laps)
    if not mean_pace:
        return None

    fast_hr, slow_hr = fast_lap.get("averageHR"), slow_lap.get("averageHR")
    if fast_hr is not None and slow_hr is not None and (fast_hr - slow_hr) < _MANUAL_LAP_HR_DROP_BPM:
        return None

    return (slow_pace - fast_pace) / mean_pace


# Power-based lap check for rides (activity_lap_power_structure). Speed is
# the wrong effort signal for hill repeats: the work is the slow climb and
# the recovery is the fast descent, so activity_manual_lap_pace_gap's HR
# check sees HR *fall* on the fastest laps and rejects it — and climb and
# descent laps on the same road are near-identical in distance, so
# _laps_look_manually_pressed misses them too (the 2026-11-08 4x hill
# repeat: CV 0.425). Power doesn't care about terrain: work laps there sat
# at 239-253W, descents at 8-9W.
#
# Work laps are within _LAP_POWER_WORK_FRACTION of the 80th-percentile lap
# power, rest laps at or under _LAP_POWER_REST_FRACTION of it; laps in
# between are ignored. Calibrated on ~6 months of one athlete's rides with
# power: the lapped interval sessions (flat 5x3min 288-332W / 10-17W rest,
# 3x30min ~235W / 78-125W rest, 2x20min ~215W / 132W rest, the hill
# repeat) all show >= 2 work blocks split by rest laps with HR dropping
# 17-35bpm; steady/hilly rides under 5km auto-lap are excluded outright
# (_laps_look_auto_lapped) since an auto-lap's power is just whatever the
# terrain did in that 5km.
_LAP_POWER_WORK_FRACTION = 0.8
_LAP_POWER_REST_FRACTION = 0.65
_MIN_LAP_POWER_WORK_BLOCKS = 2
_MIN_LAP_POWER_DURATION_S = 30  # drop pause/blip laps before judging power
# Garmin auto-lap distances are exact (5000m, 5000m, ...) apart from the
# trailing partial lap; manually-pressed laps practically never coincide
# this closely more than a couple of times.
_AUTO_LAP_DISTANCE_TOLERANCE = 0.01


def _laps_look_auto_lapped(laps: list[dict]) -> bool:
    """Whether at least half the laps share one distance (within
    _AUTO_LAP_DISTANCE_TOLERANCE) — the auto-lap-by-distance signature."""
    distances = [lap.get("distance") or 0 for lap in laps if (lap.get("distance") or 0) > 0]
    if len(distances) < 2:
        return False
    most_alike = max(
        sum(1 for other in distances if abs(other - d) <= d * _AUTO_LAP_DISTANCE_TOLERANCE)
        for d in distances
    )
    return most_alike >= 2 and most_alike * 2 >= len(distances)


def activity_lap_power_structure(client, activity: dict) -> bool:
    """Whether a ride's own laps alternate between work and recovery power
    — at least _MIN_LAP_POWER_WORK_BLOCKS separate runs of work laps, each
    split by a rest lap, with HR on those rest laps at least
    _MANUAL_LAP_HR_DROP_BPM below the work laps when HR is recorded. Unlike
    activity_manual_lap_pace_gap this catches hill repeats (see the comment
    above _LAP_POWER_WORK_FRACTION). False when the ride has no lap power
    or was auto-lapped."""
    activity_id = activity.get("activityId")
    if activity_id is None:
        return False
    splits = fetch_activity_splits(client, activity_id)
    if not splits:
        return False
    laps = splits.get("lapDTOs") or []
    if _laps_look_auto_lapped(laps):
        return False
    powered = [
        lap
        for lap in laps
        if lap.get("averagePower") is not None and (lap.get("duration") or 0) >= _MIN_LAP_POWER_DURATION_S
    ]
    if len(powered) < _MIN_LAP_POWER_WORK_BLOCKS * 2 - 1:
        return False
    powers = sorted(lap["averagePower"] for lap in powered)
    reference = powers[int(len(powers) * 0.80)]
    if not reference:
        return False

    # Collapse into alternating work/rest blocks, skipping in-between laps.
    blocks: list[tuple[str, list[dict]]] = []
    for lap in powered:
        power = lap["averagePower"]
        if power >= reference * _LAP_POWER_WORK_FRACTION:
            kind = "work"
        elif power <= reference * _LAP_POWER_REST_FRACTION:
            kind = "rest"
        else:
            continue
        if blocks and blocks[-1][0] == kind:
            blocks[-1][1].append(lap)
        else:
            blocks.append((kind, [lap]))
    # Rest before the first or after the last work block is warm-up/cool-
    # down, not recovery between reps.
    while blocks and blocks[0][0] == "rest":
        blocks.pop(0)
    while blocks and blocks[-1][0] == "rest":
        blocks.pop()
    work_laps = [lap for kind, group in blocks if kind == "work" for lap in group]
    rest_laps = [lap for kind, group in blocks if kind == "rest" for lap in group]
    if sum(1 for kind, _ in blocks if kind == "work") < _MIN_LAP_POWER_WORK_BLOCKS:
        return False

    work_hr = [lap["averageHR"] for lap in work_laps if lap.get("averageHR")]
    rest_hr = [lap["averageHR"] for lap in rest_laps if lap.get("averageHR")]
    if work_hr and rest_hr and statistics.mean(work_hr) - statistics.mean(rest_hr) < _MANUAL_LAP_HR_DROP_BPM:
        return False
    return True


def get_activity_laps(client, activity_id: Any) -> list[dict]:
    """Lap-by-lap breakdown for one activity — Garmin's own auto-laps if the
    athlete didn't lap manually, so this shows real internal structure
    (e.g. interval reps + recoveries) that summarize_activity's totals
    alone can't: just distance/duration/avg HR for the whole session. Used
    by the MCP list_activities/get_training_snapshot tools so Claude can
    inspect a specific session in that kind of detail on request."""
    activity = safe_call(client.get_activity, str(activity_id), default=None) or {}
    type_key = dig(activity, "activityTypeDTO", "typeKey") or dig(activity, "activityType", "typeKey") or ""

    splits = fetch_activity_splits(client, activity_id)
    if not splits:
        return []

    laps = []
    for i, lap in enumerate(splits.get("lapDTOs") or [], start=1):
        distance = lap.get("distance")
        duration = lap.get("duration")
        avg_hr = lap.get("averageHR")
        max_hr = lap.get("maxHR")
        laps.append(
            {
                "lap": i,
                "distance": fmt_distance(distance, type_key, is_leg=True),
                "duration": fmt_duration(duration),
                "pace": fmt_pace_or_speed(distance, duration, type_key),
                "avg_hr": fmt_int(avg_hr) if avg_hr else (f"max {fmt_int(max_hr)}" if max_hr else "—"),
                "avg_power": fmt_int(lap.get("averagePower")),
                "elevation_gain": fmt_elevation(lap.get("elevationGain")),
            }
        )
    return laps


def _race_activity_ids(activities: list[dict], race_dates: frozenset[str] | set[str]) -> set:
    """Which activities were races, so they get a "race" badge instead of
    whatever effort type _classify_session_type would infer — a race is a
    hard sustained effort, which Garmin routinely labels VO2MAX (a ~2h half
    marathon at race HR earns that label from aerobic TE alone), and that
    label maps to "interval".

    Garmin's own eventType "race" counts wherever it's set, but most people
    never set it, so a flagged race day counts too. On a race day only the
    longest activity is the race: a warm-up or cool-down jog recorded
    separately the same day shouldn't be badged as one."""
    ids = {
        a.get("activityId")
        for a in activities
        if dig(a, "eventType", "typeKey") == "race" and a.get("activityId") is not None
    }
    longest_by_date: dict[str, dict] = {}
    for a in activities:
        d = (a.get("startTimeLocal") or "")[:10]
        if d not in race_dates or a.get("activityId") is None:
            continue
        best = longest_by_date.get(d)
        if best is None or (a.get("distance") or 0) > (best.get("distance") or 0):
            longest_by_date[d] = a
    ids.update(a["activityId"] for a in longest_by_date.values())
    return ids


def _classify_session_type(
    activity: dict, peer_activities: list[dict], has_interval_structure: bool = False
) -> str:
    """Session-type guess for a completed run or ride.

    "interval" comes ONLY from the activity's own measured structure —
    `has_interval_structure`, from _has_interval_structure: pace that swings
    between sustained fast and slow stretches, with heart rate following.
    Garmin's Training Effect label and anaerobic TE are not enough on their
    own. A race or hard steady run at high HR routinely earns a VO2MAX label
    from aerobic load alone (the Locarno half marathon: VO2MAX, pace gap
    0.06), and a hilly ride posts anaerobic TE 2.5+ with no intervals in it.
    When those labels fire without structure, the session was hard but
    steady, so it reads as tempo (or long, below).

    `peer_activities` should be other activities of the SAME sport (so a
    ride's distance isn't compared against a run's) — used only to judge
    "long" relative to the rest of that batch.

    Hard-effort labels describe the dominant physiological stimulus, not
    literally "this was run at tempo pace": aerobic TE is a function of
    duration x intensity, so a long run at moderate pace can max it out and
    earn TEMPO/LACTATE_THRESHOLD (or VO2MAX) purely from time-on-feet. So a
    strong distance outlier overrides a hard-effort label: a run
    dramatically longer than everything else that week is a long run,
    whatever Garmin tagged it.

    Garmin's own label is trusted over the raw TE numbers when it's present
    — its algorithm is already sport-calibrated, and the numbers alone
    aren't comparable across sports (a cycling ride routinely posts
    anaerobic TE 2.5+ on a ride Garmin itself calls AEROBIC_BASE). The
    raw-number thresholds below only fire when Garmin didn't attach a
    recognized label."""
    if has_interval_structure:
        return "interval"

    anaerobic_te = activity.get("anaerobicTrainingEffect")
    aerobic_te = activity.get("aerobicTrainingEffect")
    label = (activity.get("trainingEffectLabel") or "").upper()

    distance = activity.get("distance") or 0
    # peer_activities includes the activity being classified itself (callers
    # pass the whole same-kind batch for the period) — median must exclude
    # it, or a small batch (very often just 2 runs in a week) self-dilutes
    # the comparison: for a 2-run week {a, b} with a > b, median = (a+b)/2,
    # so a/median = 2a/(a+b), which can never exceed ~2x no matter how much
    # longer a is than b (a 13.8km run against a 6.9km peer — genuinely 2x —
    # only registered as a 1.33x "outlier" before this exclusion, comfortably
    # under any reasonable threshold).
    self_id = activity.get("activityId")
    peers = [a for a in peer_activities if a is not activity and a.get("activityId") != self_id]
    distances = [a.get("distance") for a in peers if a.get("distance")]
    median_distance = statistics.median(distances) if distances else distance
    # A higher bar than the bare distance-only fallback below (1.3x) — this
    # one overrides a decisive Garmin signal (a tempo/threshold label), so
    # it should only fire when the distance gap is hard to explain any
    # other way.
    is_long_outlier = bool(distance and median_distance and distance >= median_distance * 1.5)

    if label in _TE_LABEL_TYPES:
        # An interval-mapped label without measured structure (returned
        # above if present) is a hard steady effort — same as tempo.
        if _TE_LABEL_TYPES[label] in ("tempo", "interval"):
            return "long" if is_long_outlier else "tempo"
        # mapped == "easy" — still worth checking distance for "long" below
    elif (anaerobic_te is not None and anaerobic_te >= 2.0) or (aerobic_te is not None and aerobic_te >= 3.5):
        return "long" if is_long_outlier else "tempo"

    if is_long_outlier or (distance and median_distance and distance >= median_distance * 1.3):
        return "long"

    return "easy"


_PLAN_TARGET_REP_PREFIX_RE = re.compile(r"\d+(?:\.\d+)?\s*[x×]\s*$", re.IGNORECASE)
_PLAN_TARGET_KM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?km", re.IGNORECASE)
_PLAN_TARGET_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?\s*(h|hr|hrs|hour|hours|min|mins|minute|minutes)\b",
    re.IGNORECASE,
)
_PLAN_TARGET_PACE_RE = re.compile(r"@\s*(\d{1,2}):(\d{2})\s*/\s*km", re.IGNORECASE)
# Any "Nx..." rep count anywhere in the text (e.g. "6x800m", "2x20min") means
# a stated "@ pace" belongs to one rep, not the session's overall average —
# an interval session's average pace (including jog recoveries) is nowhere
# near its rep pace, so pace is skipped entirely for such sessions rather
# than risk comparing a whole activity's pace against a rep target.
_PLAN_TARGET_HAS_REPS_RE = re.compile(r"\d+\s*[x×]\s*\d")


def _parse_plan_target(text: str) -> dict[str, Optional[float]]:
    """Best-effort targets parsed from a planned session's own free text —
    e.g. "Long run — 14km @ 6:15/km" -> {distance_m: 14000, pace_s_per_km:
    375, duration_s: None}, "Easy run, 40min Z1-Z2" -> {distance_m: None,
    pace_s_per_km: None, duration_s: 2400}, "Z2 endurance ride, 2-2.5h" ->
    {..., duration_s: 8100} (range midpoint). Skips a number immediately
    preceded by a rep multiplier ("2x20min", "6x800m") — that's a REP's
    distance/duration, not the session's overall total, and matching
    against it would compare a whole activity to one interval rep. Pace is
    skipped entirely (not just the rep-adjacent mention) whenever the text
    has ANY rep structure, for the same reason — an interval session's
    average pace bears no relation to its rep pace."""
    t = text or ""

    distance_m = None
    for m in _PLAN_TARGET_KM_RE.finditer(t):
        if _PLAN_TARGET_REP_PREFIX_RE.search(t[: m.start()]):
            continue
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        distance_m = (lo + hi) / 2 * 1000
        break

    duration_s = None
    for m in _PLAN_TARGET_DURATION_RE.finditer(t):
        if _PLAN_TARGET_REP_PREFIX_RE.search(t[: m.start()]):
            continue
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        unit = m.group(3).lower()
        mid = (lo + hi) / 2
        duration_s = mid * 3600 if unit.startswith("h") else mid * 60
        break

    pace_s_per_km = None
    if not _PLAN_TARGET_HAS_REPS_RE.search(t):
        m = _PLAN_TARGET_PACE_RE.search(t)
        if m:
            pace_s_per_km = int(m.group(1)) * 60 + int(m.group(2))

    return {"distance_m": distance_m, "duration_s": duration_s, "pace_s_per_km": pace_s_per_km}


# Distance/duration tolerance is generous (day-to-day route/terrain
# variance); pace is tighter since it's the more sensitive, effort-specific
# signal — a real pace mismatch (easy vs tempo) should fail this even when
# the distance happens to line up.
_PLAN_TARGET_MATCH_TOLERANCE = 0.35
_PLAN_TARGET_PACE_TOLERANCE = 0.15


def _greedy_match_by_closeness(
    entry_ids: set,
    act_dates: set,
    session_targets: dict[int, dict[str, Optional[float]]],
    act_metrics: dict[str, dict[str, Optional[float]]],
    plan_types: Optional[dict[int, str]] = None,
    interval_like_dates: frozenset = frozenset(),
) -> dict[int, str]:
    """Pair plan entries to activity dates by how close the activity's real
    distance/pace/duration is to the session's OWN stated target — the most
    literal, precise signal available when the plan gives numbers, ahead of
    inferring intent from Garmin's effort classification (which is HR-driven
    and can be pulled around by heat, fatigue, etc. without the athlete's
    actual pace target changing at all — a 13.8km run at 6:24/km is clearly
    aiming at a 14km @ 6:15/km target regardless of what HR-based label
    Garmin attached to it).

    When a plan entry states both distance and pace, a candidate activity
    must land within tolerance on EVERY target it can be checked against
    (not just on average) to count as a match at all — distance and pace
    corroborating each other is what makes a match confident; one being
    close doesn't excuse the other being clearly off. Duration only comes
    into it when no distance was stated (nothing to compute a pace from
    either, then). Entries with no parseable target, or activities missing
    the relevant metric(s), simply don't participate here and fall through
    to the type/date-based passes.

    `interval_like_dates` (activities with measured interval structure —
    see _has_interval_structure) never satisfy a non-interval
    entry here either, even on a close distance/pace match: a botched-lap
    interval session's BLENDED average is exactly the case that can coincidentally
    land close to an easy/tempo/long target's numbers despite being
    structurally nothing like a steady effort at that pace."""
    pairs = []
    for entry_id in entry_ids:
        target = session_targets.get(entry_id)
        if not target:
            continue
        target_distance = target.get("distance_m")
        target_pace = target.get("pace_s_per_km")
        target_duration = target.get("duration_s") if target_distance is None else None

        for act_date_str in act_dates:
            if act_date_str in interval_like_dates and plan_types and plan_types.get(entry_id) not in ("interval", "other"):
                continue
            metrics = act_metrics.get(act_date_str, {})
            rel_errs = []
            ok = True

            if target_distance:
                actual = metrics.get("distance")
                if actual is None:
                    ok = False
                else:
                    rel_err = abs(actual - target_distance) / target_distance
                    if rel_err > _PLAN_TARGET_MATCH_TOLERANCE:
                        ok = False
                    rel_errs.append(rel_err)

            if ok and target_pace:
                actual_pace = metrics.get("pace_s_per_km")
                if actual_pace is not None:
                    rel_err = abs(actual_pace - target_pace) / target_pace
                    if rel_err > _PLAN_TARGET_PACE_TOLERANCE:
                        ok = False
                    rel_errs.append(rel_err)
                # actual_pace missing (e.g. no duration on the activity) —
                # just skip pace as a signal rather than reject the pair.

            if ok and target_duration:
                actual = metrics.get("duration")
                if actual is None:
                    ok = False
                else:
                    rel_err = abs(actual - target_duration) / target_duration
                    if rel_err > _PLAN_TARGET_MATCH_TOLERANCE:
                        ok = False
                    rel_errs.append(rel_err)

            if ok and rel_errs:
                pairs.append((sum(rel_errs) / len(rel_errs), entry_id, act_date_str))

    pairs.sort(key=lambda p: p[0])
    matched: dict[int, str] = {}
    claimed: set = set()
    for _, entry_id, act_date_str in pairs:
        if entry_id in matched or act_date_str in claimed:
            continue
        matched[entry_id] = act_date_str
        claimed.add(act_date_str)
    return matched


def _greedy_match_by_date(
    entry_ids: set,
    act_dates: set,
    same_type_only: bool,
    session_date_by_id: dict[int, date],
    plan_types: dict[int, str],
    act_types: dict[str, str],
    interval_like_dates: frozenset = frozenset(),
) -> dict[int, str]:
    """Pair plan entries to completed-activity dates by nearest date, cheapest
    pairing first — optionally restricted to matching type (easy/tempo/
    interval/long) on both sides. Both `entry_ids` and `act_dates` are keys
    into the caller's own lookups, not raw dates — plan entries are keyed by
    a stable index (a single kind can have more than one session on the same
    date, e.g. two strength sessions this week) while activities stay keyed
    by date (already deduped to one activity per kind+date upstream).

    `interval_like_dates` never satisfies a non-interval entry, even when
    same_type_only is False (the any-type fallback pass) — an activity with
    wildly variable lap-to-lap pace is essentially never a steady easy/
    tempo/long effort regardless of what act_types otherwise says about it."""
    pairs = []
    for entry_id in entry_ids:
        session_date = session_date_by_id[entry_id]
        for act_date_str in act_dates:
            if same_type_only and plan_types[entry_id] != act_types[act_date_str]:
                continue
            if act_date_str in interval_like_dates and plan_types.get(entry_id) not in ("interval", "other"):
                continue
            act_date = date.fromisoformat(act_date_str)
            pairs.append((abs((act_date - session_date).days), entry_id, act_date_str))
    pairs.sort(key=lambda p: p[0])
    matched: dict[int, str] = {}
    claimed: set = set()
    for _, entry_id, act_date_str in pairs:
        if entry_id in matched or act_date_str in claimed:
            continue
        matched[entry_id] = act_date_str
        claimed.add(act_date_str)
    return matched


def build_training_plan(plan: Optional[dict], activities: list[dict], client=None) -> Optional[dict]:
    """Cross-reference a pushed training plan's dated sessions against this
    week's actual activities, so completed sessions show what was really
    done instead of just the plan. Only this calendar week's sessions are
    surfaced — the plan itself may span the whole training block, but the
    dashboard is a weekly view that refreshes as the week rolls over, not a
    full training-plan browser.

    Each session carries an activity_type (running/cycling/swimming/
    strength/other; defaults to "running" for plans pushed before this field
    existed). Matching is scoped to sessions and activities of the SAME
    kind — a completed strength session never claims a planned run's slot.
    Within a kind:
      - running/cycling/swimming match by session TYPE (easy/tempo/interval/
        long, from Garmin's own Training Effect data) before falling back to
        nearest-date — the plan cares that you did an easy run and a long
        run this week, not which specific day each landed on.
      - strength (and anything else without a meaningful "type") matches by
        nearest date alone: if the plan says two strength sessions this
        week, the two nearest completed strength activities claim them.
    A planned session that hasn't found a match yet doesn't just sit on its
    original date getting more "missed" — it slides to the next day this
    week still free FOR THAT KIND, so e.g. a day that already has a matched
    run can still take a still-unmatched strength session.

    `client`, if given, is used to fetch lap/split data for this week's
    running/cycling/swimming activities, to catch a real interval session
    that never gets tagged as one because its whole-activity average pace/
    TE blends fast reps with slow recoveries into something that can
    resemble a completely different, steady-effort session (see
    _has_interval_structure). Without a client, that check is simply
    skipped — matching still works, just without that extra signal."""
    if plan is None:
        return None

    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday
    is_current_week = True

    all_sessions = plan.get("sessions", [])
    dated_sessions = []
    for entry in all_sessions:
        try:
            dated_sessions.append((date.fromisoformat(entry.get("date", "")), entry))
        except ValueError:
            continue

    if not any(week_start <= d <= week_end for d, _ in dated_sessions):
        # Nothing due this calendar week — rather than showing an empty,
        # explanation-free panel (e.g. right after pushing a block that
        # starts next week, or once this week's block is fully done and the
        # next one hasn't been pushed yet), jump to whichever week actually
        # HAS planned sessions, closest to today, preferring the future on
        # a tie (a block that just ended is less useful to land on than the
        # next upcoming one).
        if dated_sessions:
            nearest_date = min((d for d, _ in dated_sessions), key=lambda d: (abs((d - today).days), d < today))
            week_start = nearest_date - timedelta(days=nearest_date.weekday())
            week_end = week_start + timedelta(days=6)
            is_current_week = False

    # Completed activities this week, bucketed by kind then date — first
    # (most recent, since Garmin returns activities newest-first) activity
    # wins if there were two of the same kind on the same day. That dedup is
    # a constraint of the MATCHING pass only (it pairs at most one activity
    # per kind+day against a planned session); week_activities below keeps
    # every activity, so a second ride on a day that already has one still
    # gets surfaced as unplanned rather than silently disappearing.
    activities_by_kind: dict[str, dict[str, dict]] = {}
    week_activities: list[tuple[str, dict]] = []
    for activity in activities:
        start = activity.get("startTimeLocal") or ""
        d = start[:10]
        if not d:
            continue
        try:
            activity_date = date.fromisoformat(d)
        except ValueError:
            continue
        if not (week_start <= activity_date <= week_end):
            continue
        kind = _activity_kind(activity)
        bucket = activities_by_kind.setdefault(kind, {})
        if d not in bucket:
            bucket[d] = activity
        week_activities.append((start, activity))

    # This week's planned sessions, index-keyed rather than date-keyed —
    # unlike the running-only plan this replaces, a single day can now
    # legitimately carry more than one planned session (e.g. an easy run
    # and a strength session), so date alone is no longer a unique key.
    week_sessions_raw = []  # (session_date, entry, kind)
    for entry in plan.get("sessions", []):
        try:
            session_date = date.fromisoformat(entry.get("date", ""))
        except ValueError:
            continue
        if week_start <= session_date <= week_end:
            kind = _normalize_plan_kind(entry.get("activity_type"))
            week_sessions_raw.append((session_date, entry, kind))
    week_sessions_raw.sort(key=lambda t: t[0])  # earliest-owed session gets first pick of a free day
    week_sessions = [(idx, d, e, k) for idx, (d, e, k) in enumerate(week_sessions_raw)]

    matched_date_for_entry: dict[int, str] = {}
    occupied_by_kind: dict[str, set] = {}

    kinds_in_plan = {kind for _, _, _, kind in week_sessions}
    for kind in kinds_in_plan:
        kind_activities = activities_by_kind.get(kind, {})  # date -> activity
        kind_entries = [(idx, d, e) for idx, d, e, k in week_sessions if k == kind]
        entry_ids = {idx for idx, _, _ in kind_entries}
        session_date_by_id = {idx: d for idx, d, _ in kind_entries}
        act_dates = set(kind_activities)

        plan_types: dict[int, str] = {}
        interval_like_dates: frozenset = frozenset()
        act_types: dict[str, str] = {}
        if kind in TYPE_CLASSIFIABLE_KINDS:
            plan_types = {idx: _classify_plan_text(e.get("session", "")) for idx, _, e in kind_entries}

            if client is not None and kind_activities and kind in PACE_INTERVAL_THRESHOLDS:
                with ThreadPoolExecutor(max_workers=TREND_FETCH_WORKERS) as pool:
                    futures = {
                        d: pool.submit(_has_interval_structure, client, a, kind) for d, a in kind_activities.items()
                    }
                    interval_like_dates = frozenset(d for d, f in futures.items() if f.result())

            kind_activity_list = list(kind_activities.values())
            act_types = {
                d: _classify_session_type(
                    a,
                    kind_activity_list,
                    has_interval_structure=d in interval_like_dates,
                )
                for d, a in kind_activities.items()
            }

        # Pass 0: closeness to the session's own stated distance/pace/
        # duration target, when the plan text has one — the most literal
        # signal available, ahead of inferring intent from effort
        # classification.
        session_targets = {idx: _parse_plan_target(e.get("session", "")) for idx, _, e in kind_entries}
        act_metrics: dict[str, dict[str, Optional[float]]] = {}
        for d, a in kind_activities.items():
            a_distance = a.get("distance")
            a_duration = a.get("duration") or a.get("movingDuration")
            pace_s_per_km = (a_duration / (a_distance / 1000)) if a_distance and a_duration else None
            act_metrics[d] = {"distance": a_distance, "duration": a_duration, "pace_s_per_km": pace_s_per_km}
        matched = _greedy_match_by_closeness(
            entry_ids, act_dates, session_targets, act_metrics, plan_types, interval_like_dates
        )
        claimed0 = set(matched.values())
        remaining_entries = entry_ids - set(matched)
        remaining_acts = act_dates - claimed0

        if kind in TYPE_CLASSIFIABLE_KINDS:
            # Pass 1: match same-type only (an easy run only claims an easy slot).
            matched.update(
                _greedy_match_by_date(
                    remaining_entries, remaining_acts, True, session_date_by_id, plan_types, act_types, interval_like_dates
                )
            )
            claimed1 = set(matched.values()) - claimed0
            # Pass 2: fallback any-type match for whatever's left — keeps
            # plans written in free text without clear type keywords working.
            remaining_entries = remaining_entries - set(matched)
            remaining_acts = remaining_acts - claimed1
            matched.update(
                _greedy_match_by_date(
                    remaining_entries, remaining_acts, False, session_date_by_id, plan_types, act_types, interval_like_dates
                )
            )
        else:
            matched.update(_greedy_match_by_date(remaining_entries, remaining_acts, False, session_date_by_id, {}, {}))

        matched_date_for_entry.update(matched)
        occupied_by_kind[kind] = {date.fromisoformat(d) for d in matched.values()}

    # A session whose own day hasn't arrived yet needs no sliding at all —
    # it just isn't due yet. Sliding is only for a day that's ALREADY
    # passed without a match. Reserve still-future unmatched sessions'
    # dates up front (alongside matched-completed dates) so a past-due
    # session sliding forward can't double-book a day some other, still-
    # scheduled session of the same kind already legitimately owns.
    for idx, session_date, entry, kind in week_sessions:
        if idx not in matched_date_for_entry and session_date >= today:
            occupied_by_kind.setdefault(kind, set()).add(session_date)

    # Free days for reassigning a still-unmatched, already-past-due session
    # are scoped per kind — a day already holding a matched run can still
    # be "free" for a still-unmatched strength session, since both can
    # coexist that day.
    free_days_by_kind: dict[str, list[date]] = {}

    def free_days_for(kind: str) -> list[date]:
        if kind not in free_days_by_kind:
            occupied = occupied_by_kind.get(kind, set())
            free_days_by_kind[kind] = sorted(
                week_start + timedelta(days=i)
                for i in range((week_end - week_start).days + 1)
                if (week_start + timedelta(days=i)) >= today and (week_start + timedelta(days=i)) not in occupied
            )
        return free_days_by_kind[kind]

    sessions = []
    for idx, session_date, entry, kind in week_sessions:
        matched_date_str = matched_date_for_entry.get(idx)
        if matched_date_str is not None:
            sessions.append(
                {
                    "date": matched_date_str,
                    "weekday": date.fromisoformat(matched_date_str).strftime("%a"),
                    "planned_session": entry.get("session", ""),
                    "activity_type": kind,
                    "status": "completed",
                    "completed_activity": summarize_activity(activities_by_kind[kind][matched_date_str]),
                }
            )
        elif _is_date_pinned(entry, kind):
            # Belongs to its own day whether or not it happened — show it
            # there and never reschedule it. Past ones aren't "missed"
            # (nothing was owed), so they get a neutral status rather than
            # the red one.
            sessions.append(
                {
                    "date": entry["date"],
                    "weekday": session_date.strftime("%a"),
                    "planned_session": entry.get("session", ""),
                    "activity_type": kind,
                    "status": "upcoming" if session_date >= today else "rest",
                    "completed_activity": None,
                }
            )
        elif is_current_week and session_date >= today:
            # Not due yet — show it on its own planned date, nothing to slide.
            sessions.append(
                {
                    "date": entry["date"],
                    "weekday": session_date.strftime("%a"),
                    "planned_session": entry.get("session", ""),
                    "activity_type": kind,
                    "status": "upcoming",
                    "completed_activity": None,
                }
            )
        elif is_current_week:
            fdays = free_days_for(kind)
            if fdays:
                # Still owed this week — slide it to the next open day
                # rather than mark it missed just because its original day
                # passed.
                reassigned = fdays.pop(0)
                sessions.append(
                    {
                        "date": reassigned.isoformat(),
                        "weekday": reassigned.strftime("%a"),
                        "planned_session": entry.get("session", ""),
                        "activity_type": kind,
                        "status": "upcoming",
                        "completed_activity": None,
                    }
                )
            else:
                # No days left this week to fit it in — genuinely missed.
                sessions.append(
                    {
                        "date": entry["date"],
                        "weekday": session_date.strftime("%a"),
                        "planned_session": entry.get("session", ""),
                        "activity_type": kind,
                        "status": "missed",
                        "completed_activity": None,
                    }
                )
        else:
            # Previewing a week other than the current one (see
            # is_current_week above) — there's no "in progress, redistribute
            # the remaining days" concept for a week that hasn't started yet
            # (or one that's already fully over), so each session just
            # keeps its own originally-planned date instead of sliding.
            sessions.append(
                {
                    "date": entry["date"],
                    "weekday": session_date.strftime("%a"),
                    "planned_session": entry.get("session", ""),
                    "activity_type": kind,
                    "status": "upcoming" if session_date >= today else "missed",
                    "completed_activity": None,
                }
            )

    # Anything actually done this week that no planned session claimed —
    # an extra ride beyond the two the plan asked for, a second session on
    # a day already accounted for, or a sport the plan never mentioned.
    # Without these the panel silently under-reports the week: it would
    # show the plan's two rides as done and simply lose the third.
    claimed_activity_ids = set()
    for idx, _, _, kind in week_sessions:
        matched_date_str = matched_date_for_entry.get(idx)
        if matched_date_str is None:
            continue
        claimed = activities_by_kind.get(kind, {}).get(matched_date_str)
        if claimed is not None and claimed.get("activityId") is not None:
            claimed_activity_ids.add(claimed["activityId"])

    for start, activity in sorted(week_activities, key=lambda pair: pair[0]):
        activity_id = activity.get("activityId")
        if activity_id is not None and activity_id in claimed_activity_ids:
            continue
        activity_date = start[:10]
        sessions.append(
            {
                "date": activity_date,
                "weekday": date.fromisoformat(activity_date).strftime("%a"),
                "planned_session": "",  # nothing planned it — that's the point
                "activity_type": _activity_kind(activity),
                "status": "unplanned",
                "completed_activity": summarize_activity(activity),
            }
        )

    sessions.sort(key=lambda s: s["date"])

    race_date_str = plan.get("race_date")
    days_to_race = None
    if race_date_str:
        try:
            days_to_race = (date.fromisoformat(race_date_str) - today).days
        except ValueError:
            pass

    return {
        "race_name": plan.get("race_name"),
        "goal_time": plan.get("goal_time"),
        "race_date": race_date_str,
        "days_to_race": days_to_race,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "is_current_week": is_current_week,
        "sessions": sessions,
    }


def build_next_waypoint_from_training_plan(training_plan_payload: Optional[dict], stored_plan: Optional[dict]) -> Optional[dict]:
    """When a training plan is active, "what's next" should just be the next
    planned session, not the load-balance auto-suggestion — the plan already
    says what to do, no need to guess at it. Only used when a training plan
    exists; build_next_waypoint (the auto-suggestion) is the fallback for
    everyone else."""
    if not training_plan_payload:
        return None

    next_entry = next((s for s in training_plan_payload["sessions"] if s["status"] == "upcoming"), None)

    if next_entry is None and stored_plan:
        # This week's planned sessions are all done/missed — look past the
        # dashboard's weekly window into the rest of the stored plan.
        future = sorted(
            (e for e in stored_plan.get("sessions", []) if e.get("date", "") > training_plan_payload["week_end"]),
            key=lambda e: e["date"],
        )
        if future:
            entry = future[0]
            try:
                weekday = date.fromisoformat(entry["date"]).strftime("%a")
            except ValueError:
                weekday = ""
            next_entry = {
                "date": entry["date"],
                "weekday": weekday,
                "planned_session": entry.get("session", ""),
                "activity_type": _normalize_plan_kind(entry.get("activity_type")),
            }

    if next_entry is None:
        return None

    try:
        entry_date = date.fromisoformat(next_entry["date"])
        when = "Today" if entry_date == date.today() else f"{next_entry['weekday']} {entry_date.strftime('%b %-d')}"
    except ValueError:
        when = next_entry.get("weekday", "")

    return {
        "title": "Next planned session",
        "sessions": [next_entry["planned_session"]],
        "notes": [],
        "source": "training_plan",
        "when": when,
    }


def get_dashboard_payload(client, username: Optional[str] = None) -> dict:
    errors: list[str] = []
    today = date.today().isoformat()

    activities = safe_call(client.get_activities_by_date, date_range(ROUTE_LOG_DAYS)[0], today, default=[]) or []
    if not activities:
        errors.append("No recent activities returned by Garmin.")

    race_dates: set[str] = set()
    if username is not None:
        import race_days as race_days_module  # local import: keeps this module Garmin-only otherwise

        race_dates = {r["date"] for r in race_days_module.get_race_days(username)}

    readiness_entries = safe_call(client.get_training_readiness, today, default=[]) or []
    if isinstance(readiness_entries, dict):
        readiness_entries = [readiness_entries]

    hero = build_hero(readiness_entries)
    ridge_chart = build_ridge_chart(client, race_dates, username=username)
    training_load = build_training_load(client, today)
    vitals = build_vitals(client, today, readiness_entries)
    last_activity = build_last_activity(client, activities)
    route_log = build_route_log(client, activities)

    training_plan_payload = None
    next_waypoint = None
    if username is not None:
        import race_plan as race_plan_module  # local import: keeps this module Garmin-only otherwise

        stored_plan = race_plan_module.get_plan(username)
        training_plan_payload = build_training_plan(stored_plan, activities, client=client)
        next_waypoint = build_next_waypoint_from_training_plan(training_plan_payload, stored_plan)

    if next_waypoint is None:
        # No race plan (or nothing left to point at in one) — fall back to
        # the load-balance/recovery auto-suggestion.
        next_waypoint = build_next_waypoint(training_load, readiness_entries, activities)

    if username is not None:
        # "How did it feel?" notes carry the injury/fatigue context Garmin's
        # own numbers can't — attach them wherever an activity shows up so
        # the coach (human reading the dashboard, or Claude via the MCP)
        # actually sees them instead of them only living behind the
        # dashboard's own review textarea.
        import reviews as reviews_module  # local import: keeps this module Garmin-only otherwise

        candidates = [last_activity, *route_log]
        if training_plan_payload:
            candidates += [s.get("completed_activity") for s in training_plan_payload["sessions"]]
        activity_ids = [c.get("activity_id") for c in candidates if c]
        notes_by_id = reviews_module.get_reviews(username, activity_ids)
        for c in candidates:
            if c and c.get("activity_id") is not None:
                note = notes_by_id.get(str(c["activity_id"]))
                if note:
                    c["review_note"] = note

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_label": date.today().strftime("%a %-d %b %Y") if os.name != "nt" else date.today().strftime("%a %d %b %Y"),
        "errors": errors,
        "hero": hero,
        "ridge_chart": ridge_chart,
        "training_load": training_load,
        "vitals": vitals,
        "last_activity": last_activity,
        "next_waypoint": next_waypoint,
        "route_log": route_log,
        "training_plan": training_plan_payload,
    }
