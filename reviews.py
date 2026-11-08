"""SQLite-backed storage for per-activity review notes, scoped per user."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


def db_path() -> str:
    path = os.getenv("REVIEWS_DB", "./data/reviews.db")
    return str(Path(path).expanduser())


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
    if existing_cols and "username" not in existing_cols:
        # Pre-multi-user schema (activity_id was the whole primary key, no
        # username). Nothing from that single-user era needs preserving
        # across accounts, so just rebuild with the per-user schema.
        conn.execute("DROP TABLE reviews")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            username TEXT NOT NULL,
            activity_id TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (username, activity_id)
        )
        """
    )
    return conn


def get_review(username: str, activity_id: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT note FROM reviews WHERE username = ? AND activity_id = ?",
            (username, str(activity_id)),
        ).fetchone()
        return row[0] if row else None


def get_reviews(username: str, activity_ids: list) -> dict[str, str]:
    """Batch lookup for attaching notes to a page of activities at once —
    used by the dashboard/MCP payload, which may carry dozens of activities,
    rather than one query per row."""
    ids = [str(a) for a in activity_ids if a is not None]
    if not ids:
        return {}
    with _connect() as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT activity_id, note FROM reviews WHERE username = ? AND activity_id IN ({placeholders})",
            (username, *ids),
        ).fetchall()
    return {activity_id: note for activity_id, note in rows if note}


def save_review(username: str, activity_id: str, note: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO reviews (username, activity_id, note, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(username, activity_id) DO UPDATE SET
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (username, str(activity_id), note),
        )
        conn.commit()
