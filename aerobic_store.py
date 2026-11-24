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

import json
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
CREATE TABLE IF NOT EXISTS slope (
    username TEXT NOT NULL, sport TEXT NOT NULL, unit TEXT, b REAL NOT NULL, source TEXT NOT NULL,
    b_fitted REAL, default_b REAL, n INTEGER, n_activities INTEGER, n_pairs INTEGER, reason TEXT, updated_at REAL,
    PRIMARY KEY (username, sport)
);
CREATE TABLE IF NOT EXISTS sync_state (
    username TEXT PRIMARY KEY, last_sync REAL, backfilled INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
"""


# Columns added after the first release; CREATE TABLE IF NOT EXISTS won't
# add them to an existing database, so they're added here on connect.
_ADDED_COLUMNS = {
    "sessions": {"method": "TEXT", "n_chunks1": "INTEGER", "n_chunks2": "INTEGER", "ref_output": "REAL",
                 "hr1_at_ref": "REAL", "hr2_at_ref": "REAL", "np1": "REAL", "np2": "REAL",
                 "vi1": "REAL", "vi2": "REAL", "exclude_reason": "TEXT"},
    "weekly": {"fit_issue": "TEXT", "raw_b": "REAL", "mad": "REAL"},
    "user_ref": {"hr_ref_source": "TEXT", "easy_hr": "REAL", "easy_activities": "INTEGER", "easy_set_on": "TEXT",
                 "ftp": "REAL"},
    "chunks": {"avg_output": "REAL"},
    "processed": {"chunk_reasons": "TEXT"},
}
_migrated: set[str] = set()


def _migrate(conn: sqlite3.Connection, path: str) -> None:
    if path in _migrated:
        return
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    _migrated.add(path)


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn, path)
    return conn


def _rows(sql: str, args: Iterable[Any] = ()) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


# --------------------------------------------------------------- activities --

def processed_ids(username: str, algo_version: int) -> set[str]:
    return {r["activity_id"] for r in _rows(
        "SELECT activity_id FROM processed WHERE username = ? AND algo_version >= ?", (username, algo_version))}


def has_outdated(username: str, algo_version: int) -> bool:
    return bool(_rows("SELECT 1 FROM processed WHERE username = ? AND algo_version < ? LIMIT 1",
                      (username, algo_version)))


def save_activity(username: str, activity_id: str, day: str, type_key: str, name: str,
                  analysis: dict, temp_c: Optional[float], algo_version: int) -> None:
    """Replace everything stored for one activity with a fresh analysis."""
    aid = str(activity_id)
    series = analysis.get("series") or {}
    n_points = sum(len(s["points"]) for s in series.values())
    n_sessions = sum(1 for s in series.values() if (s.get("durability") or {}).get("decoupling_pct") is not None)
    status = "skipped" if analysis.get("skip_reason") else ("used" if n_points or n_sessions else "no_steady_data")
    with _connect() as conn:
        conn.execute("DELETE FROM chunks WHERE username = ? AND activity_id = ?", (username, aid))
        conn.execute("DELETE FROM sessions WHERE username = ? AND activity_id = ?", (username, aid))
        for key, s in series.items():
            temp = None if key.endswith("_indoor") else temp_c
            conn.executemany(
                "INSERT INTO chunks (username, activity_id, series, t_start, day, output, hr, temp_c, avg_output) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(username, aid, key, p["t_start"], day, p["output"], p["hr"], temp, p.get("avg_output"))
                 for p in s["points"]],
            )
            d = s.get("durability")
            if d:
                conn.execute(
                    """INSERT INTO sessions (username, activity_id, series, day, name, moving_s, vi, ef1, ef2,
                       output1, output2, hr1, hr2, decoupling_pct, label, temp_c,
                       method, n_chunks1, n_chunks2, ref_output, hr1_at_ref, hr2_at_ref, np1, np2,
                       vi1, vi2, exclude_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (username, aid, key, day, name, d["moving_s"], d["vi"], d.get("ef1"), d.get("ef2"), d["output1"],
                     d["output2"], d["hr1"], d["hr2"], d.get("decoupling_pct"), d.get("label"), temp,
                     d.get("method", "np_ef"), d.get("n_chunks1"), d.get("n_chunks2"), d.get("ref_output"),
                     d.get("hr1_at_ref"), d.get("hr2_at_ref"), d.get("np1"), d.get("np2"),
                     d.get("vi1"), d.get("vi2"), d.get("exclude_reason")),
                )
        reason = analysis.get("skip_reason")
        if not reason and status == "no_steady_data":
            reason = "; ".join(f"{k}: {s.get('durability_reason')}" for k, s in series.items())
        conn.execute(
            """INSERT INTO processed (username, activity_id, day, type_key, status, reason, algo_version, processed_at,
                                      chunk_reasons)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(username, activity_id) DO UPDATE SET day = excluded.day, type_key = excluded.type_key,
               status = excluded.status, reason = excluded.reason, algo_version = excluded.algo_version,
               processed_at = excluded.processed_at, chunk_reasons = excluded.chunk_reasons""",
            (username, aid, day, type_key, status, reason, algo_version, time.time(),
             json.dumps({k: s.get("reasons") or {} for k, s in series.items()}) if series else None),
        )
        conn.commit()


def mark_skipped(username: str, activity_id: str, day: str, type_key: str, reason: str, algo_version: int) -> None:
    save_activity(username, activity_id, day, type_key, "", {"series": {}, "skip_reason": reason}, None, algo_version)


def chunk_points(username: str, series: Optional[str] = None, since_day: Optional[str] = None) -> list[dict]:
    sql = "SELECT activity_id, series, t_start, day, output, hr, temp_c, avg_output FROM chunks WHERE username = ?"
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


def durability_sessions(username: str, series: Optional[str] = None, limit: Optional[int] = None,
                        include_excluded: bool = False) -> list[dict]:
    """Scored sessions, newest first. include_excluded=True also returns the
    long sessions whose halves weren't comparable (method "excluded",
    decoupling_pct NULL)."""
    sql = "SELECT * FROM sessions WHERE username = ?"
    if not include_excluded:
        sql += " AND decoupling_pct IS NOT NULL"
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
             hr_ref: int, source: str, hr_ref_source: str = "hr_reserve") -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO user_ref (username, zone_sport, hr_max, hr_rest, lthr, hr_ref, source, updated_at, hr_ref_source)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(username, zone_sport) DO UPDATE SET hr_max = excluded.hr_max, hr_rest = excluded.hr_rest,
               lthr = excluded.lthr, hr_ref = excluded.hr_ref, source = excluded.source, updated_at = excluded.updated_at,
               hr_ref_source = excluded.hr_ref_source""",
            (username, zone_sport, hr_max, hr_rest, lthr, hr_ref, source, time.time(), hr_ref_source),
        )
        conn.commit()


def freeze_easy_ref(username: str, zone_sport: str, hr_ref: int, easy_hr: float, n_activities: int,
                    on_day: str) -> None:
    """Store a measured easy-session reference HR; it stays until HRmax /
    resting HR change or the user resets it (reset_easy_ref)."""
    with _connect() as conn:
        conn.execute("""UPDATE user_ref SET hr_ref = ?, hr_ref_source = 'easy_sessions', easy_hr = ?,
                        easy_activities = ?, easy_set_on = ?, updated_at = ? WHERE username = ? AND zone_sport = ?""",
                     (hr_ref, easy_hr, n_activities, on_day, time.time(), username, zone_sport))
        conn.commit()


def reset_easy_ref(username: str, zone_sport: str, formula_hr_ref: int) -> None:
    """Back to the HR-reserve formula; the next rebuild measures (and freezes) it again."""
    with _connect() as conn:
        conn.execute("""UPDATE user_ref SET hr_ref = ?, hr_ref_source = 'hr_reserve', easy_hr = NULL,
                        easy_activities = NULL, easy_set_on = NULL, updated_at = ? WHERE username = ? AND zone_sport = ?""",
                     (formula_hr_ref, time.time(), username, zone_sport))
        conn.commit()


def set_ftp(username: str, ftp: Optional[float]) -> None:
    with _connect() as conn:
        conn.execute("UPDATE user_ref SET ftp = ? WHERE username = ? AND zone_sport = 'CYCLING'", (ftp, username))
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
                "median_out", "mean_temp_c", "hot_points", "fit_issue", "raw_b", "mad")


def replace_weekly(username: str, series: str, weeks: list[dict]) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM weekly WHERE username = ? AND series = ?", (username, series))
        conn.executemany(
            f"INSERT INTO weekly (username, series, week_end, {', '.join(_WEEKLY_COLS)}) "
            f"VALUES (?,?,?,{','.join('?' * len(_WEEKLY_COLS))})",
            [(username, series, w["week_end"], *[int(w[c]) if c == "fit_ok" else w.get(c) for c in _WEEKLY_COLS])
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


# ----------------------------------------------------------------- slope --

def save_slope(username: str, sport: str, unit: str, fit: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO slope (username, sport, unit, b, source, b_fitted, default_b, n, n_activities, n_pairs,
               reason, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(username, sport) DO UPDATE SET unit = excluded.unit, b = excluded.b,
               source = excluded.source, b_fitted = excluded.b_fitted, default_b = excluded.default_b,
               n = excluded.n, n_activities = excluded.n_activities, n_pairs = excluded.n_pairs,
               reason = excluded.reason, updated_at = excluded.updated_at""",
            (username, sport, unit, fit["b"], fit["source"], fit.get("b_fitted"), fit.get("default"), fit.get("n"),
             fit.get("n_activities"), fit.get("n_pairs"), fit.get("reason"), time.time()),
        )
        conn.commit()


def get_slopes(username: str) -> dict[str, dict]:
    return {r["sport"]: r for r in _rows("SELECT * FROM slope WHERE username = ?", (username,))}


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
        for table in ("processed", "chunks", "sessions", "weekly", "slope", "sync_state"):
            if username is None:
                conn.execute(f"DELETE FROM {table}")
            else:
                conn.execute(f"DELETE FROM {table} WHERE username = ?", (username,))
        conn.commit()
