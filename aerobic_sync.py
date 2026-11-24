"""Fetch new activities and turn them into Aerobic Efficiency / Durability data.

Run by trainstate-aerobic.timer every 30 min (see deploy/systemd), and once
by hand for the backfill:

    .venv/bin/python aerobic_sync.py                 # every user, new activities only
    .venv/bin/python aerobic_sync.py --user aleppi --days 120
    .venv/bin/python aerobic_sync.py --user aleppi --reprocess   # ignore the cache

The first run for a user automatically looks back BACKFILL_DAYS. Each
activity's stream is fetched and analysed once (aerobic_metrics) and only the
results are cached (aerobic_store); the dashboard and MCP read the cache and
never trigger Garmin calls for this.
"""

from __future__ import annotations

import argparse
import logging
import os
import statistics
import sys
import time
from datetime import date, timedelta
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

import aerobic_metrics as am  # noqa: E402
import aerobic_store as store  # noqa: E402
import garmin_client  # noqa: E402
import users  # noqa: E402
from garmin_client import GarminAuthRequired, GarminUnavailable, safe_call  # noqa: E402

logger = logging.getLogger("trainstate.aerobic")

BACKFILL_DAYS = int(os.getenv("AEROBIC_BACKFILL_DAYS", "120"))
INCREMENTAL_DAYS = 10            # re-list this far back each run, in case Garmin was late to sync
REQUEST_PAUSE_S = float(os.getenv("AEROBIC_REQUEST_PAUSE_S", "1.0"))  # be gentle with Garmin
DETAILS_MAX_CHART = 50000        # high enough that Garmin returns every 1 Hz sample (13.9 h)
RESTING_HR_DAYS = 28
DEFAULT_REF_OUTPUT_DAYS = 56


def heat_correction_enabled() -> bool:
    return os.getenv("AEROBIC_HEAT_CORRECTION", "0").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------- user refs --

def _resting_hr_median(client) -> Optional[float]:
    end = date.today()
    start = end - timedelta(days=RESTING_HR_DAYS - 1)
    data = safe_call(client.get_rhr_daily, start.isoformat(), end.isoformat(), default=None) or []
    values = []
    for entry in data if isinstance(data, list) else []:
        v = entry.get("value") if isinstance(entry, dict) else None
        if v is None and isinstance(entry, dict):
            m = (((entry.get("allMetrics") or {}).get("metricsMap") or {}).get("WELLNESS_RESTING_HEART_RATE"))
            v = m[0].get("value") if isinstance(m, list) and m else None
        if isinstance(v, (int, float)) and v > 0:
            values.append(v)
    return statistics.median(values) if values else None


def _observed_max_hr(activities: list[dict], sport: str) -> Optional[float]:
    """Fallback HRmax: the highest max HR in this sport's recent activities."""
    def sport_of(a: dict) -> Optional[str]:
        series, _ = am.classify_activity((a.get("activityType") or {}).get("typeKey", ""), True)
        return am.SERIES[series[0]]["sport"] if series else None
    vals = [a.get("maxHR") for a in activities if isinstance(a.get("maxHR"), (int, float)) and sport_of(a) == sport]
    return max(vals) if vals else None


def refresh_refs(username: str, client, activities: list[dict]) -> dict[str, dict]:
    """Per-sport HRmax / LTHR / resting HR / HRref.

    HRmax and LTHR come from Garmin's per-sport HR-zone settings (CYCLING,
    RUNNING, DEFAULT), with DEFAULT standing in for a sport the user hasn't
    configured and the highest recent HR as the last resort. Resting HR is
    Garmin's configured value if set, else the 28-day median of daily resting
    HR — measured once and then frozen, so HRref (and with it the whole
    history) stays comparable. HRref only moves when HRmax or a configured
    resting HR changes."""
    stored = store.get_refs(username)
    zones_raw = safe_call(client.connectapi, "/biometric-service/heartRateZones", default=None) or []
    by_sport = {z.get("sport"): z for z in zones_raw if isinstance(z, dict)}
    measured_rest: Optional[float] = None

    for sport, zone_sport in am.ZONE_SPORT.items():
        z = by_sport.get(zone_sport) or by_sport.get("DEFAULT") or {}
        prev = stored.get(zone_sport) or {}
        hr_max = z.get("maxHeartRateUsed")
        source = "garmin_zones" if hr_max else None
        if not hr_max:
            hr_max = prev.get("hr_max") if prev.get("source") == "garmin_zones" else None
            hr_max = hr_max or _observed_max_hr(activities, sport) or prev.get("hr_max")
            source = source or "observed_max"
        if not hr_max:
            continue
        lthr = z.get("lactateThresholdHeartRateUsed") or prev.get("lthr")
        hr_rest = z.get("restingHeartRateUsed")
        if not hr_rest:
            hr_rest = prev.get("hr_rest")
            if not hr_rest:
                if measured_rest is None:
                    measured_rest = _resting_hr_median(client)
                hr_rest = measured_rest
        if not hr_rest:
            continue
        # Deterministic from the (frozen) HRmax / resting HR and the sport's
        # fraction, so recomputing it every run never moves it by itself.
        hr_ref = am.reference_hr(hr_rest, hr_max, sport)
        if (prev.get("hr_max"), prev.get("hr_rest"), prev.get("lthr"), prev.get("hr_ref")) != (hr_max, hr_rest, lthr, hr_ref):
            store.save_ref(username, zone_sport, hr_max, hr_rest, lthr, hr_ref, source)
    return store.get_refs(username)


def zones_for_analysis(refs: dict[str, dict]) -> dict[str, dict]:
    return {zs: {"hr_max": r["hr_max"], "lthr": r.get("lthr"), "hr_rest": r.get("hr_rest")} for zs, r in refs.items()}


# --------------------------------------------------------------- activities --

def _weather_temp_c(client, activity_id: Any) -> Optional[float]:
    """Garmin's activity weather reports temperature in °F (a 21 °C Málaga
    morning comes back as temp=70), whatever the account's unit settings."""
    w = safe_call(client.get_activity_weather, str(activity_id), default=None)
    temp_f = w.get("temp") if isinstance(w, dict) else None
    return round((temp_f - 32) * 5 / 9, 1) if isinstance(temp_f, (int, float)) else None


def _is_interval_session(client, activity: dict) -> bool:
    """The dashboard's own interval detection (lap power for rides with power,
    pace + HR structure for runs) — the same check that labels sessions
    "interval" elsewhere. Only asked for sessions long enough for Durability,
    since it can cost an extra Garmin request (laps)."""
    if (activity.get("movingDuration") or activity.get("duration") or 0) < am.DURABILITY_MIN_MOVING_S:
        return False
    import dashboard_data  # local import: heavy module, only needed here

    type_key = (activity.get("activityType") or {}).get("typeKey", "")
    series, _ = am.classify_activity(type_key, True)
    sport = am.SERIES[series[0]]["sport"] if series else None
    if sport not in ("cycling", "running"):
        return False
    try:
        return bool(dashboard_data._has_interval_structure(client, activity, sport))
    except Exception as exc:  # noqa: BLE001 - no signal means "not shown to be intervals"
        logger.warning("aerobic: interval check failed for %s: %s", activity.get("activityId"), exc)
        return False


def process_activity(username: str, client, activity: dict, zones: dict[str, dict]) -> str:
    aid = str(activity.get("activityId"))
    type_key = (activity.get("activityType") or {}).get("typeKey", "")
    day = (activity.get("startTimeLocal") or "")[:10]
    name = activity.get("activityName") or ""

    # Decide from the summary when we can, so a swim or a strength session
    # never costs a stream download.
    series, reason = am.classify_activity(type_key, True)
    if not series:
        store.mark_skipped(username, aid, day, type_key, reason or "unsupported_type", am.ALGO_VERSION)
        return "skipped"
    if not activity.get("averageHR"):
        store.mark_skipped(username, aid, day, type_key, "no_hr", am.ALGO_VERSION)
        return "skipped"
    if am.SERIES[series[0]]["sport"] == "cycling" and not activity.get("avgPower"):
        store.mark_skipped(username, aid, day, type_key, "bike_without_power", am.ALGO_VERSION)
        return "skipped"

    details = client.get_activity_details(aid, maxchart=DETAILS_MAX_CHART)
    if not details:
        raise GarminUnavailable(f"no details for activity {aid}")  # leave unprocessed; retried next run
    analysis = am.analyze_activity(details, type_key, zones, interval_session=_is_interval_session(client, activity))
    temp_c = None
    if any(not am.SERIES[k]["indoor"] for k in analysis.get("series", {})):
        time.sleep(REQUEST_PAUSE_S)
        temp_c = _weather_temp_c(client, aid)
    store.save_activity(username, aid, day, type_key, name, analysis, temp_c, am.ALGO_VERSION)
    return "used" if analysis.get("series") else "skipped"


def rebuild_weekly(username: str, today: Optional[date] = None) -> None:
    """Refit every weekly window from cached chunks. Milliseconds per series
    — the expensive stream work never repeats — so a full rebuild is simpler
    and safer than tracking which windows a new activity touched."""
    today = today or date.today()
    heat = heat_correction_enabled()
    existing = store.get_ref_outputs(username)
    by_series: dict[str, list[dict]] = {}
    for series in store.series_with_points(username):
        rows = store.chunk_points(username, series)
        for r in rows:
            r["date"] = date.fromisoformat(r["day"])
        by_series[series] = rows
    # One slope per sport (cycling pools indoor + outdoor), shared by every
    # series and week of that sport; the windows only fit intercepts.
    slopes: dict[str, dict] = {}
    for sport in {am.SERIES[s]["sport"] for s in by_series}:
        unit = next(m["unit"] for m in am.SERIES.values() if m["sport"] == sport)
        pooled = [r for s, rows in by_series.items() if am.SERIES[s]["sport"] == sport for r in rows]
        slopes[sport] = am.sport_slope(pooled, unit, today)
        store.save_slope(username, sport, unit, slopes[sport])
    for series, rows in by_series.items():
        b = slopes[am.SERIES[series]["sport"]]["b"]
        store.replace_weekly(username, series, am.weekly_windows(rows, today, b, heat_correction=heat))
        if series not in existing:
            since = (today - timedelta(days=DEFAULT_REF_OUTPUT_DAYS)).isoformat()
            recent = [r["output"] for r in rows if r["day"] >= since] or [r["output"] for r in rows]
            ref = am.default_ref_output(recent, am.SERIES[series]["unit"])
            if ref:
                store.set_ref_output(username, series, ref, pinned=False)


def sync_user(username: str, days: Optional[int] = None, reprocess: bool = False) -> dict:
    state = store.get_sync_state(username) or {}
    # A bumped ALGO_VERSION means the whole cached history needs redoing,
    # not just the last few days.
    full = not state.get("backfilled") or store.has_outdated(username, am.ALGO_VERSION)
    lookback = days or (BACKFILL_DAYS if full else INCREMENTAL_DAYS)
    counts = {"used": 0, "skipped": 0, "failed": 0, "cached": 0}
    try:
        client = garmin_client.get_client(username)
        end = date.today()
        start = end - timedelta(days=lookback)
        activities = client.get_activities_by_date(start.isoformat(), end.isoformat()) or []
        refs = refresh_refs(username, client, activities)
        zones = zones_for_analysis(refs)
        done = set() if reprocess else store.processed_ids(username, am.ALGO_VERSION)
        for activity in sorted(activities, key=lambda a: a.get("startTimeGMT") or ""):
            if str(activity.get("activityId")) in done:
                counts["cached"] += 1
                continue
            try:
                outcome = process_activity(username, client, activity, zones)
                counts[outcome] += 1
                if outcome == "used":
                    time.sleep(REQUEST_PAUSE_S)
            except (GarminAuthRequired, GarminUnavailable):
                raise
            except Exception as exc:  # noqa: BLE001 - one bad activity mustn't stop the rest
                counts["failed"] += 1
                logger.warning("aerobic: %s activity %s failed: %s", username, activity.get("activityId"), exc)
        rebuild_weekly(username)
        store.save_sync_state(username, backfilled=bool(state.get("backfilled")) or lookback >= BACKFILL_DAYS)
    except (GarminAuthRequired, GarminUnavailable) as exc:
        store.save_sync_state(username, error=str(exc))
        logger.warning("aerobic: %s sync stopped: %s", username, exc)
        counts["error"] = str(exc)
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", action="append", help="account id (repeatable); default: every account")
    parser.add_argument("--days", type=int, help="look back this many days (default: backfill once, then incremental)")
    parser.add_argument("--reprocess", action="store_true", help="re-analyse activities already cached")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    for username in args.user or users.list_usernames():
        if not users.garmin_linked(username):
            continue
        t = time.time()
        counts = sync_user(username, days=args.days, reprocess=args.reprocess)
        logger.info("aerobic: %s %s in %.0fs", username, counts, time.time() - t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
