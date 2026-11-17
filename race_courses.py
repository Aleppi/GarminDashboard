"""Race courses for the Races tab: an elevation profile, totals and the
sections the course splits into (at checkpoints, or every few km), attached
to one of a user's races (race_days.py) — from a GPX upload or one of the
user's saved Garmin Connect courses.

Stored processed, not raw: one JSON file per race at
data/courses/<username>/<race date>.json holding a downsampled profile,
checkpoints and sections. Raw GPX can be tens of MB; what the page needs
is a few hundred points.
"""

from __future__ import annotations

import json
import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

PROFILE_POINTS = 400
ELEVATION_HYSTERESIS_M = 4.0  # ignore GPS/DEM wiggles smaller than this when summing climb
CHECKPOINT_MAX_OFF_COURSE_M = 400.0  # a GPX waypoint further than this from the track isn't on the course
SECTION_STEPS_KM = (1, 5, 10, 20, 25, 50)
MAX_AUTO_SECTIONS = 12


def _root() -> Path:
    return Path(os.getenv("RACE_COURSES_DIR", "./data/courses")).expanduser()


def _path(username: str, race_date: str) -> Path:
    date.fromisoformat(race_date)  # validates, and rules out path tricks
    if not re.fullmatch(r"[A-Za-z0-9._@-]+", username or "") or username.startswith("."):
        raise ValueError("bad username")
    return _root() / username / f"{race_date}.json"


def get_course(username: str, race_date: str) -> Optional[dict]:
    try:
        path = _path(username, race_date)
    except ValueError:
        return None
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_course(username: str, race_date: str, course: dict) -> None:
    path = _path(username, race_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(course))
    os.replace(tmp, path)


def delete_course(username: str, race_date: str) -> None:
    try:
        _path(username, race_date).unlink(missing_ok=True)
    except ValueError:
        pass


def move_course(username: str, old_date: str, new_date: str) -> None:
    """Keep a race's course when the race moves to another date."""
    src = _path(username, old_date)
    if src.exists():
        os.replace(src, _path(username, new_date))


# ------------------------------------------------------------ building --

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _climb(elevations: Iterable[float]) -> tuple[float, float]:
    """(gain, loss) with hysteresis, so noise on flat ground doesn't add up
    to hundreds of phantom metres."""
    gain = loss = 0.0
    ref = None
    for e in elevations:
        if ref is None:
            ref = e
            continue
        if e - ref >= ELEVATION_HYSTERESIS_M:
            gain += e - ref
            ref = e
        elif ref - e >= ELEVATION_HYSTERESIS_M:
            loss += ref - e
            ref = e
    return gain, loss


def build_course(
    points: list[dict],
    checkpoints: Optional[list[dict]] = None,
    *,
    name: str = "",
    source: str = "",
    total_gain: Optional[float] = None,
    total_loss: Optional[float] = None,
) -> dict:
    """`points`: [{lat, lon, ele?, dist?}] in course order; dist (metres
    from the start) is computed if absent. `checkpoints`: [{name, lat, lon}]
    or [{name, dist}] — projected onto the track by nearest point."""
    pts = [p for p in points if isinstance(p.get("lat"), (int, float)) and isinstance(p.get("lon"), (int, float))]
    if len(pts) < 2:
        raise ValueError("the course needs at least two track points")
    if any(not isinstance(p.get("dist"), (int, float)) for p in pts):
        total = 0.0
        pts[0]["dist"] = 0.0
        for a, b in zip(pts, pts[1:]):
            total += _haversine(a["lat"], a["lon"], b["lat"], b["lon"])
            b["dist"] = total
    length = float(pts[-1]["dist"])
    if length < 100:
        raise ValueError("the course is shorter than 100 m — is this the right file?")

    has_ele = sum(1 for p in pts if isinstance(p.get("ele"), (int, float))) >= len(pts) * 0.8
    if has_ele:
        # Fill the odd missing elevation from the previous point.
        last = next(p["ele"] for p in pts if isinstance(p.get("ele"), (int, float)))
        for p in pts:
            if isinstance(p.get("ele"), (int, float)):
                last = p["ele"]
            else:
                p["ele"] = last
        gain, loss = _climb(p["ele"] for p in pts)
    else:
        gain = loss = 0.0
    if isinstance(total_gain, (int, float)):
        gain = total_gain
    if isinstance(total_loss, (int, float)):
        loss = total_loss

    # Downsample to evenly spaced distances for the profile chart.
    profile = []
    if has_ele:
        step = length / (PROFILE_POINTS - 1)
        j = 0
        for k in range(PROFILE_POINTS):
            target = min(length, k * step)
            while j < len(pts) - 2 and pts[j + 1]["dist"] < target:
                j += 1
            a, b = pts[j], pts[j + 1]
            span = (b["dist"] - a["dist"]) or 1.0
            t = max(0.0, min(1.0, (target - a["dist"]) / span))
            profile.append([round(target), round(a["ele"] + (b["ele"] - a["ele"]) * t, 1)])

    def ele_at(d: float) -> Optional[float]:
        if not profile:
            return None
        k = min(len(profile) - 1, max(0, round(d / length * (len(profile) - 1))))
        return profile[k][1]

    # Checkpoints onto the track.
    cps = []
    for cp in checkpoints or []:
        d = cp.get("dist")
        if not isinstance(d, (int, float)) and isinstance(cp.get("lat"), (int, float)):
            best, best_d = None, float("inf")
            for p in pts:
                off = _haversine(cp["lat"], cp["lon"], p["lat"], p["lon"])
                if off < best_d:
                    best_d, best = off, p
            if best is None or best_d > CHECKPOINT_MAX_OFF_COURSE_M:
                continue
            d = best["dist"]
        if not isinstance(d, (int, float)) or d < 200 or d > length - 200:
            continue  # start/finish markers aren't sections
        cps.append({"name": (cp.get("name") or "Checkpoint").strip()[:60], "dist": round(d), "ele": ele_at(d)})
    cps.sort(key=lambda c: c["dist"])
    deduped = []
    for c in cps:
        if not deduped or c["dist"] - deduped[-1]["dist"] > 300:
            deduped.append(c)
    cps = deduped

    # Sections: checkpoint to checkpoint, else every N km.
    if cps:
        cuts = [(c["dist"], c["name"]) for c in cps]
    else:
        km = length / 1000
        step_km = next((s for s in SECTION_STEPS_KM if km / s <= MAX_AUTO_SECTIONS), SECTION_STEPS_KM[-1])
        cuts = [(k * step_km * 1000.0, f"km {k * step_km:g}") for k in range(1, int(km / step_km) + 1)
                if k * step_km * 1000.0 < length - 200]
    cuts.append((length, "Finish"))

    sections = []
    start = 0.0
    for end, label in cuts:
        seg = [e for d, e in profile if start <= d <= end] if profile else []
        g, l = _climb(seg) if seg else (0.0, 0.0)
        km = (end - start) / 1000
        sections.append({
            "to": label,
            "from_m": round(start),
            "to_m": round(end),
            "gain": round(g),
            "loss": round(l),
            # ITRA effort: 1 km per 100 m of climb on top of the distance.
            "effort_km": round(km + g / 100, 3),
        })
        start = end

    # Scale section climb so it sums to the stated total (Garmin's figure
    # comes from a DEM and is more trustworthy than a raw GPX's GPS track).
    raw_gain = sum(s["gain"] for s in sections)
    if raw_gain and gain and abs(raw_gain - gain) > 1:
        f = gain / raw_gain
        for s in sections:
            s["gain"] = round(s["gain"] * f)
            s["effort_km"] = round((s["to_m"] - s["from_m"]) / 1000 + s["gain"] / 100, 3)

    eles = [e for _, e in profile]
    return {
        "name": name[:120],
        "source": source,
        "distance_m": round(length),
        "gain_m": round(gain),
        "loss_m": round(loss),
        "max_ele": max(eles) if eles else None,
        "min_ele": min(eles) if eles else None,
        "profile": profile,
        "checkpoints": cps,
        "sections": sections,
        "effort_km": round(sum(s["effort_km"] for s in sections), 2),
    }


def summary(course: Optional[dict]) -> Optional[dict]:
    """Course without the profile points — for the MCP, where the shape of
    the course matters, not 400 chart points."""
    if not course:
        return None
    return {k: v for k, v in course.items() if k != "profile"}


# ------------------------------------------------------------- sources --

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_gpx(data: bytes, filename: str = "") -> dict:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"not a readable GPX file ({exc})") from None
    if _local(root.tag) != "gpx":
        raise ValueError("not a GPX file")

    def pt(el) -> dict:
        ele = next((c.text for c in el if _local(c.tag) == "ele"), None)
        try:
            ele_v = float(ele) if ele is not None else None
        except ValueError:
            ele_v = None
        return {"lat": float(el.get("lat")), "lon": float(el.get("lon")), "ele": ele_v}

    track = [pt(el) for el in root.iter() if _local(el.tag) == "trkpt"]
    if not track:
        track = [pt(el) for el in root.iter() if _local(el.tag) == "rtept"]
    waypoints = []
    for el in root.iter():
        if _local(el.tag) == "wpt":
            p = pt(el)
            p["name"] = next((c.text for c in el if _local(c.tag) == "name"), None) or "Checkpoint"
            waypoints.append(p)
    name = next((c.text for el in root.iter() if _local(el.tag) in ("trk", "metadata", "rte")
                 for c in el if _local(c.tag) == "name" and c.text), None)
    return build_course(track, waypoints, name=name or Path(filename).stem, source="gpx")


def from_garmin_course(detail: dict) -> dict:
    geo = detail.get("geoPoints") or []
    points = [{"lat": g.get("latitude"), "lon": g.get("longitude"), "ele": g.get("elevation"), "dist": g.get("distance")}
              for g in geo]
    cps = []
    for cp in detail.get("coursePoints") or []:
        cps.append({"name": cp.get("name") or cp.get("coursePointType") or "Checkpoint",
                    "lat": cp.get("lat") or cp.get("latitude"), "lon": cp.get("lon") or cp.get("longitude"),
                    "dist": cp.get("distance")})
    return build_course(
        points, cps, name=detail.get("courseName") or "", source=f"garmin:{detail.get('courseId')}",
        total_gain=detail.get("elevationGainMeter"), total_loss=detail.get("elevationLossMeter"),
    )
