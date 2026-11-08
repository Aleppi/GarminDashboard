"""Per-user training plan pushed from a chat with Claude via the MCP
server's push_training_plan tool. Claude sets a goal (race, target time,
race date) and a set of dated sessions across any sport — running, cycling,
swimming, strength, or anything else; the dashboard shows whichever of
those sessions fall in the current calendar week, marking each one
completed once a matching activity shows up in Garmin activity history
(see build_training_plan in dashboard_data.py for the matching logic).

Storage here is sport-agnostic: `sessions` is just a list of free-form
dicts (date/session/activity_type), so no schema change was needed to grow
this from running-only to every sport — see the module/table names below,
kept as-is since they're purely internal.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def db_path() -> str:
    return str(Path(os.getenv("RACE_PLAN_DB", "./data/race_plan.db")).expanduser())


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS race_plan (
            username TEXT PRIMARY KEY,
            race_name TEXT NOT NULL,
            goal_time TEXT NOT NULL,
            race_date TEXT NOT NULL,
            sessions_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def save_plan(username: str, race_name: str, goal_time: str, race_date: str, sessions: list[dict]) -> None:
    """`sessions` is a list of {"date": "YYYY-MM-DD", "session": "Tempo run —
    6km @ 4:15/km", "activity_type": "running"}. Replaces the whole plan —
    Claude re-pushes it (e.g. weekly, or after a session gets adjusted)
    rather than the dashboard patching it in place."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO race_plan (username, race_name, goal_time, race_date, sessions_json, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(username) DO UPDATE SET
                race_name = excluded.race_name,
                goal_time = excluded.goal_time,
                race_date = excluded.race_date,
                sessions_json = excluded.sessions_json,
                updated_at = excluded.updated_at
            """,
            (username, race_name, goal_time, race_date, json.dumps(sessions)),
        )
        conn.commit()


def get_plan(username: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT race_name, goal_time, race_date, sessions_json, updated_at FROM race_plan WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        return None
    return {
        "race_name": row[0],
        "goal_time": row[1],
        "race_date": row[2],
        "sessions": json.loads(row[3]),
        "updated_at": row[4],
    }


def clear_plan(username: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM race_plan WHERE username = ?", (username,))
        conn.commit()
