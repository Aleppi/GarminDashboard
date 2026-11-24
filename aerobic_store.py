"""SQLite cache behind Aerobic Efficiency and Durability.

Streams are the expensive part (one Garmin request plus ~0.1-1 s of Python
per activity on the Pi), so each activity is analysed once and only its
results are kept: one row per steady 5-min chunk and one per long session.
The weekly model over those rows is cheap and is rebuilt on every sync.
`processed` remembers every activity already looked at — including ones
that produced nothing (no HR, bike without power, ...) — so nothing is
fetched twice unless aerobic_metrics.ALGO_VERSION is bumped.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional


def db_path() -> str:
    return str(Path(os.getenv("AEROBIC_DB", "./data/aerobic.db")).expanduser())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    username TEXT NOT NULL, activity_id TEXT NOT NULL, day TEXT, type_key TEXT,
    status TEXT NOT NULL, reason TEXT, algo_version INTEGER NOT NULL, processed_at REAL,
    PRIMARY KEY (username, activity_id)
);
CREATE TABLE IF NOT EXISTS chunks (
    username TEXT NOT NULL, activity_id TEXT NOT NULL, series TEXT NOT NULL, t_start INTEGER NOT NULL,
    day TEXT NOT NULL, output REAL NOT NULL, hr REAL NOT NULL, temp_c REAL,
    PRIMARY KEY (username, activity_id, series, t_start)
);
CREATE INDEX IF NOT EXISTS chunks_by_series ON chunks (username, series, day);
CREATE TABLE IF NOT EXISTS sessions (
    username TEXT NOT NULL, activity_id TEXT NOT NULL, series TEXT NOT NULL, day TEXT NOT NULL,
    name TEXT, moving_s REAL, vi REAL, ef1 REAL, ef2 REAL, output1 REAL, output2 REAL,
    hr1 REAL, hr2 REAL, decoupling_pct REAL, label TEXT, temp_c REAL,
    PRIMARY KEY (username, activity_id, series)
);
CREATE TABLE IF NOT EXISTS user_ref (
    username TEXT NOT NULL, zone_sport TEXT NOT NULL, hr_max REAL, hr_rest REAL, lthr REAL,
    hr_ref INTEGER, source TEXT, updated_at REAL,
    PRIMARY KEY (username, zone_sport)
);
CREATE TABLE IF NOT EXISTS ref_output (
    username TEXT NOT NULL, series TEXT NOT NULL, value REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (username, series)
);
CREATE TABLE IF NOT EXISTS weekly (
    username TEXT NOT NULL, series TEXT NOT NULL, week_end TEXT NOT NULL,
    n INTEGER, n_activities INTEGER, a REAL, b REAL, fit_ok INTEGER, ratio REAL, hr_min REAL, hr_max REAL,
    out_min REAL, out_max REAL, median_out REAL, mean_temp_c REAL, hot_points INTEGER,
    PRIMARY KEY (username, series, week_end)
);
CREATE TABLE IF NOT EXISTS sync_state (
    username TEXT PRIMARY KEY, last_sync REAL, backfilled INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
"""


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _rows(sql: str, args: Iterable[Any] = ()) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


# --------------------------------------------------------------- activities --

def processed_ids(username: str, algo_version: int) -> set[str]:
    return {r["activity_id"] for r in _rows(
        "SELECT activity_id FROM processed WHERE username = ? AND algo_version >= ?", (username, algo_version))}


def save_activity(username: str, activity_id: str, day: str, type_key: str, name: str,
                  analysis: dict, temp_c: Optional[float], algo_version: int) -> None:
    """Replace everything stored for one activity with a fresh analysis."""
    aid = str(activity_id)
    series = analysis.get("series") or {}
    n_points = sum(len(s["points"]) for s in series.values())
    n_sessions = sum(1 for s in series.values() if s.get("durability"))
    status = "skipped" if analysis.get("skip_reason") else ("used" if n_points or n_sessions else "no_steady_data")
    with _connect() as conn:
        conn.execute("DELETE FROM chunks WHERE username = ? AND activity_id = ?", (username, aid))
        conn.execute("DELETE FROM sessions WHERE username = ? AND activity_id = ?", (username, aid))
        for key, s in series.items():
            temp = None if key.endswith("_indoor") else temp_c
            conn.executemany(
                "INSERT INTO chunks (username, activity_id, series, t_start, day, output, hr, temp_c) VALUES (?,?,?,?,?,?,?,?)",
                [(username, aid, key, p["t_start"], day, p["output"], p["hr"], temp) for p in s["points"]],
            )
            d = s.get("durability")
            if d:
                conn.execute(
                    """INSERT INTO sessions (username, activity_id, series, day, name, moving_s, vi, ef1, ef2,
                       output1, output2, hr1, hr2, decoupling_pct, label, temp_c)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (username, aid, key, day, name, d["moving_s"], d["vi"], d["ef1"], d["ef2"], d["output1"],
                     d["output2"], d["hr1"], d["hr2"], d["decoupling_pct"], d["label"], temp),
                )
        reason = analysis.get("skip_reason")
        if not reason and status == "no_steady_data":
            reason = "; ".join(f"{k}: {s.get('durability_reason')}" for k, s in series.items())
        conn.execute(
            """INSERT INTO processed (username, activity_id, day, type_key, status, reason, algo_version, processed_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(username, activity_id) DO UPDATE SET day = excluded.day, type_key = excluded.type_key,
               status = excluded.status, reason = excluded.reason, algo_version = excluded.algo_version,
               processed_at = excluded.processed_at""",
            (username, aid, day, type_key, status, reason, algo_version, time.time()),
        )
        conn.commit()


def mark_skipped(username: str, activity_id: str, day: str, type_key: str, reason: str, algo_version: int) -> None:
    save_activity(username, activity_id, day, type_key, "", {"series": {}, "skip_reason": reason}, None, algo_version)


def chunk_points(username: str, series: Optional[str] = None, since_day: Optional[str] = None) -> list[dict]:
    sql = "SELECT activity_id, series, t_start, day, output, hr, temp_c FROM chunks WHERE username = ?"
    args: list[Any] = [username]
    if series:
        sql += " AND series = ?"
        args.append(series)
    if since_day:
        sql += " AND day >= ?"
        args.append(since_day)
    return _rows(sql + " ORDER BY t_start", args)


def series_with_points(username: str) -> list[str]:
    return [r["series"] for r in _rows("SELECT DISTINCT series FROM chunks WHERE username = ?", (username,))]


def durability_sessions(username: str, series: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
    sql = "SELECT * FROM sessions WHERE username = ?"
    args: list[Any] = [username]
    if series:
        sql += " AND series = ?"
        args.append(series)
    sql += " ORDER BY day DESC, activity_id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _rows(sql, args)


def processed_summary(username: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in _rows("SELECT status, reason, COUNT(*) AS n FROM processed WHERE username = ? GROUP BY status, reason",
                   (username,)):
        key = r["status"] if r["status"] != "skipped" else f"skipped:{r['reason']}"
        out[key] = out.get(key, 0) + r["n"]
    return out


# ------------------------------------------------------------------ refs --

def get_refs(username: str) -> dict[str, dict]:
    return {r["zone_sport"]: r for r in _rows("SELECT * FROM user_ref WHERE username = ?", (username,))}


def save_ref(username: str, zone_sport: str, hr_max: float, hr_rest: float, lthr: Optional[float],
             hr_ref: int, source: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO user_ref (username, zone_sport, hr_max, hr_rest, lthr, hr_ref, source, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(username, zone_sport) DO UPDATE SET hr_max = excluded.hr_max, hr_rest = excluded.hr_rest,
               lthr = excluded.lthr, hr_ref = excluded.hr_ref, source = excluded.source, updated_at = excluded.updated_at""",
            (username, zone_sport, hr_max, hr_rest, lthr, hr_ref, source, time.time()),
        )
        conn.commit()


def get_ref_outputs(username: str) -> dict[str, dict]:
    return {r["series"]: r for r in _rows("SELECT * FROM ref_output WHERE username = ?", (username,))}


def set_ref_output(username: str, series: str, value: Optional[float], pinned: bool) -> None:
    """value=None with pinned=False drops the pin; the next sync picks a default again."""
    with _connect() as conn:
        if value is None:
            conn.execute("DELETE FROM ref_output WHERE username = ? AND series = ?", (username, series))
        else:
            conn.execute(
                """INSERT INTO ref_output (username, series, value, pinned) VALUES (?,?,?,?)
                   ON CONFLICT(username, series) DO UPDATE SET value = excluded.value, pinned = excluded.pinned""",
                (username, series, value, 1 if pinned else 0),
            )
        conn.commit()


# ---------------------------------------------------------------- weekly --

_WEEKLY_COLS = ("n", "n_activities", "a", "b", "fit_ok", "ratio", "hr_min", "hr_max", "out_min", "out_max",
                "median_out", "mean_temp_c", "hot_points")


def replace_weekly(username: str, series: str, weeks: list[dict]) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM weekly WHERE username = ? AND series = ?", (username, series))
        conn.executemany(
            f"INSERT INTO weekly (username, series, week_end, {', '.join(_WEEKLY_COLS)}) "
            f"VALUES (?,?,?,{','.join('?' * len(_WEEKLY_COLS))})",
            [(username, series, w["week_end"], *[int(w[c]) if c == "fit_ok" else w[c] for c in _WEEKLY_COLS])
             for w in weeks],
        )
        conn.commit()


def weekly(username: str, series: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM weekly WHERE username = ?"
    args: list[Any] = [username]
    if series:
        sql += " AND series = ?"
        args.append(series)
    rows = _rows(sql + " ORDER BY series, week_end", args)
    for r in rows:
        r["fit_ok"] = bool(r["fit_ok"])
    return rows


# ------------------------------------------------------------- sync state --

def get_sync_state(username: str) -> Optional[dict]:
    rows = _rows("SELECT * FROM sync_state WHERE username = ?", (username,))
    return rows[0] if rows else None


def save_sync_state(username: str, backfilled: Optional[bool] = None, error: Optional[str] = None) -> None:
    prev = get_sync_state(username) or {}
    done = int(backfilled if backfilled is not None else bool(prev.get("backfilled")))
    with _connect() as conn:
        conn.execute(
            """INSERT INTO sync_state (username, last_sync, backfilled, last_error) VALUES (?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET last_sync = excluded.last_sync,
               backfilled = excluded.backfilled, last_error = excluded.last_error""",
            (username, time.time(), done, error),
        )
        conn.commit()


def clear(username: Optional[str] = None) -> None:
    with _connect() as conn:
        for table in ("processed", "chunks", "sessions", "weekly", "sync_state"):
            if username is None:
                conn.execute(f"DELETE FROM {table}")
            else:
                conn.execute(f"DELETE FROM {table} WHERE username = ?", (username,))
        conn.commit()
