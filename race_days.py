"""Per-user race-day markers, set from a chat with Claude via the MCP
server's mark_race_day tool ("I have a race on Oct 11th"). Purely a flag
on the fitness (CTL/ATL) chart — deliberately separate from race_plan.py,
which is a full structured training plan toward one goal race. Someone
can flag a race day here without ever pushing a training plan for it, and
can have several races marked across a season, not just one.

Nothing infers a race day automatically (e.g. from activity type) — Garmin
has no reliable "this was a race" signal for casual/untagged activities,
so a day only shows up here if a person actually said it was a race.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def db_path() -> str:
    return str(Path(os.getenv("RACE_DAYS_DB", "./data/race_days.db")).expanduser())


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS race_days (
            username TEXT NOT NULL,
            date TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (username, date)
        )
        """
    )
    return conn


def mark_race_day(username: str, race_date: str, name: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO race_days (username, date, name, created_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(username, date) DO UPDATE SET name = excluded.name
            """,
            (username, race_date, name),
        )
        conn.commit()


def unmark_race_day(username: str, race_date: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM race_days WHERE username = ? AND date = ?", (username, race_date))
        conn.commit()


def get_race_days(username: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT date, name FROM race_days WHERE username = ? ORDER BY date",
            (username,),
        ).fetchall()
    return [{"date": row[0], "name": row[1]} for row in rows]
