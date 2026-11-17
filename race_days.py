"""Per-user races and goals, set from a chat with Claude via the MCP
server's mark_race_day tool ("I have a race on Oct 11th"). Each entry
flags its day on the fitness (CTL/ATL) chart and, if it's still ahead,
shows up on the dashboard's Races tab with a countdown and whatever
details were given (distance, climb, goal, ...). Deliberately separate
from race_plan.py, which is a full structured training plan toward one
goal race — someone can mark a race here without ever pushing a plan for
it, and can have several across a season, not just one.

Nothing infers a race day automatically (e.g. from activity type) — Garmin
has no reliable "this was a race" signal for casual/untagged activities,
so a day only shows up here if a person actually said it was a race.

Every detail column is optional and was added after the table first
shipped, so _connect() migrates older databases in place.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

KINDS = ("race", "goal")  # goal = a dated target that isn't an event (e.g. "base block done")
STATUSES = ("confirmed", "tentative")

# Optional free-text details, in display order. Kept as text on purpose:
# "~30 km", "106 km", "1,500 m D+", "08:30" all read better verbatim than
# forced into numbers, and they come from a chat, not a form.
DETAIL_FIELDS = ("sport", "location", "distance", "elevation", "goal", "start_time", "notes", "url", "target_time")

_COLUMNS: dict[str, str] = {
    "kind": "TEXT NOT NULL DEFAULT 'race'",
    "status": "TEXT NOT NULL DEFAULT 'confirmed'",
    "date_approx": "INTEGER NOT NULL DEFAULT 0",  # 1 = organiser hasn't fixed the exact date yet
    **{f: "TEXT" for f in DETAIL_FIELDS},
}


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
    existing = {row[1] for row in conn.execute("PRAGMA table_info(race_days)")}
    for column, decl in _COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE race_days ADD COLUMN {column} {decl}")
    return conn


def mark_race_day(
    username: str,
    race_date: str,
    name: str = "",
    *,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    date_approx: Optional[bool] = None,
    **details: Optional[str],
) -> None:
    """Create or update the entry for that date. On update, only the fields
    actually passed change — e.g. push_training_plan re-marking its race
    date by name alone doesn't wipe the distance/goal/notes set earlier.
    Pass an empty string to clear a detail field."""
    unknown = set(details) - set(DETAIL_FIELDS)
    if unknown:
        raise ValueError(f"unknown race detail field(s): {', '.join(sorted(unknown))}")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")

    values: dict[str, Any] = {"kind": kind, "status": status,
                              "date_approx": None if date_approx is None else int(bool(date_approx))}
    values.update({f: details.get(f) for f in DETAIL_FIELDS})
    cols = list(values)
    with _connect() as conn:
        # Insert with column defaults for anything not given, then patch.
        conn.execute(
            "INSERT OR IGNORE INTO race_days (username, date, name) VALUES (?, ?, ?)",
            (username, race_date, name),
        )
        if name:
            conn.execute("UPDATE race_days SET name = ? WHERE username = ? AND date = ?", (name, username, race_date))
        given = [c for c in cols if values[c] is not None]
        if given:
            conn.execute(
                f"UPDATE race_days SET {', '.join(c + ' = ?' for c in given)} WHERE username = ? AND date = ?",
                [values[c] if values[c] != "" else None for c in given] + [username, race_date],
            )
        conn.commit()


def unmark_race_day(username: str, race_date: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM race_days WHERE username = ? AND date = ?", (username, race_date))
        conn.commit()


def get_race_days(username: str) -> list[dict]:
    cols = ["date", "name", *_COLUMNS]
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM race_days WHERE username = ? ORDER BY date",
            (username,),
        ).fetchall()
    out = []
    for row in rows:
        entry = dict(zip(cols, row))
        entry["date_approx"] = bool(entry["date_approx"])
        out.append(entry)
    return out


def race_dates(username: str) -> set[str]:
    """Dates of actual races (not goals) — what gets a flag on the fitness
    chart and counts as a race when classifying sessions."""
    return {r["date"] for r in get_race_days(username) if r["kind"] == "race"}


PAST_RACES_SHOWN = 6


def races_overview(username: str, today: str, include_profile: bool = True) -> dict:
    """What the Races tab shows: everything still ahead (races and goals,
    soonest first, each with days to go) plus the most recent past races.
    A race that the stored training plan targets is tagged with that plan's
    goal time, so the tab can say a plan is driving toward it."""
    import race_courses
    import race_plan  # local import: race_plan doesn't need this module back

    from datetime import date

    plan = race_plan.get_plan(username)
    today_d = date.fromisoformat(today)
    upcoming, past = [], []
    for entry in get_race_days(username):
        entry["days_to_go"] = (date.fromisoformat(entry["date"]) - today_d).days
        entry["has_plan"] = bool(plan and plan["race_date"] == entry["date"])
        if entry["has_plan"] and plan.get("goal_time"):
            entry["plan_goal"] = plan["goal_time"]
        if entry["days_to_go"] >= 0:
            course = race_courses.get_course(username, entry["date"])
            entry["course"] = course if include_profile else race_courses.summary(course)
            upcoming.append(entry)
        elif entry["kind"] == "race":
            past.append(entry)
    past.reverse()
    return {"today": today, "upcoming": upcoming, "past": past[:PAST_RACES_SHOWN]}
