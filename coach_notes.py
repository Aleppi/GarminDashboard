"""Per-user coaching notes pushed from a chat with Claude via the MCP
server's push_coach_note tool. The dashboard prefers showing the latest
one (while it's still fresh) over the rule-based Next Waypoint suggestion.

Stored as two parts, not one blob of text: `session` is the short, concrete
recommendation (shown bold, so it's grabbable at a glance), `reasoning` is
the supporting explanation (shown below in lighter body text for whoever
wants to read why).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

FRESHNESS_HOURS = int(os.getenv("COACH_NOTE_FRESHNESS_HOURS", "48"))


def db_path() -> str:
    return str(Path(os.getenv("COACH_NOTES_DB", "./data/coach_notes.db")).expanduser())


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(coach_notes)")}
    if existing_cols and "session_text" not in existing_cols:
        # Pre-split-fields schema (a single `note` blob). Coach notes are
        # inherently short-lived (FRESHNESS_HOURS), so there's nothing worth
        # migrating — just rebuild with the new schema.
        conn.execute("DROP TABLE coach_notes")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coach_notes (
            username TEXT PRIMARY KEY,
            session_text TEXT NOT NULL,
            reasoning_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def save_note(username: str, session_text: str, reasoning_text: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO coach_notes (username, session_text, reasoning_text, created_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(username) DO UPDATE SET
                session_text = excluded.session_text,
                reasoning_text = excluded.reasoning_text,
                created_at = excluded.created_at
            """,
            (username, session_text, reasoning_text),
        )
        conn.commit()


def get_latest(username: str) -> Optional[dict]:
    """Returns None once the note is older than FRESHNESS_HOURS — a
    stale plan from days ago shouldn't keep overriding the dashboard's
    own auto-generated suggestion forever."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT session_text, reasoning_text, created_at FROM coach_notes WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        return None
    created_at = datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created_at
    if age > timedelta(hours=FRESHNESS_HOURS):
        return None
    return {
        "session": row[0],
        "reasoning": row[1],
        "created_at": row[2],
        "age_hours": round(age.total_seconds() / 3600, 1),
    }
