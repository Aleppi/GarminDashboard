"""On-disk cache of per-day CTL/ATL/TSB, so the fitness chart stops
refetching months of history from Garmin on every dashboard load.

Garmin has no bulk endpoint for training status — it's one HTTP request per
day (see build_ridge_chart_for_days). At a 90-day window that's 90 requests
per cold load, fanned across a thread pool, and Garmin starts answering a
chunk of them with 504s. safe_call turns each failure into None, which the
chart then draws as a gap with a straight line interpolated across it —
even though the data exists and returns fine when asked for calmly.

The fix this supports: a past day's CTL/ATL is immutable — it's a rolling
average over training that already happened and can't change retroactively.
So once fetched successfully it never needs fetching again. Only "today" is
still moving and is deliberately never cached here.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


def db_path() -> str:
    return str(Path(os.getenv("TREND_CACHE_DB", "./data/trend_cache.db")).expanduser())


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_days (
            username TEXT NOT NULL,
            date TEXT NOT NULL,
            ctl REAL,
            atl REAL,
            tsb REAL,
            PRIMARY KEY (username, date)
        )
        """
    )
    return conn


def get_days(username: str, days: list[str]) -> dict[str, dict]:
    """Cached {date: {"ctl","atl","tsb"}} for whichever of `days` we have."""
    if not username or not days:
        return {}
    out: dict[str, dict] = {}
    with _connect() as conn:
        # Chunked to stay under SQLite's variable limit on long ranges.
        for i in range(0, len(days), 400):
            chunk = days[i : i + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT date, ctl, atl, tsb FROM trend_days WHERE username = ? AND date IN ({placeholders})",
                (username, *chunk),
            ).fetchall()
            for d, ctl, atl, tsb in rows:
                out[d] = {"ctl": ctl, "atl": atl, "tsb": tsb}
    return out


def save_days(username: str, entries: dict[str, dict]) -> None:
    """Store fetched days. Callers must pass only days that are safely
    immutable (strictly before today) and that actually carried data —
    caching a null would permanently freeze a gap in place."""
    if not username or not entries:
        return
    rows = [
        (username, d, v.get("ctl"), v.get("atl"), v.get("tsb"))
        for d, v in entries.items()
        if v.get("ctl") is not None
    ]
    if not rows:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO trend_days (username, date, ctl, atl, tsb)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username, date) DO UPDATE SET
                ctl = excluded.ctl, atl = excluded.atl, tsb = excluded.tsb
            """,
            rows,
        )
        conn.commit()


def clear(username: Optional[str] = None) -> None:
    """Drop cached history — all users, or just one."""
    with _connect() as conn:
        if username is None:
            conn.execute("DELETE FROM trend_days")
        else:
            conn.execute("DELETE FROM trend_days WHERE username = ?", (username,))
        conn.commit()
