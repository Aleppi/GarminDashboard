"""SQLite storage backing the MCP server's OAuth authorization server.

Plain tables for DCR-registered clients, authorization codes, access
tokens, and refresh tokens — every code/token row carries a `subject`
(the dashboard username it was issued for), which is how tool calls know
whose Garmin data to serve.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


def db_path() -> str:
    return str(Path(os.getenv("OAUTH_DB", "./data/oauth.db")).expanduser())


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_codes (
            code TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            redirect_uri_provided_explicitly INTEGER NOT NULL,
            scopes TEXT NOT NULL,
            resource TEXT,
            subject TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_tokens (
            token TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            scopes TEXT NOT NULL,
            resource TEXT,
            subject TEXT NOT NULL,
            expires_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            scopes TEXT NOT NULL,
            subject TEXT NOT NULL,
            expires_at INTEGER
        )
        """
    )
    return conn


# ------------------------------------------------------------- clients --

def save_client(client_id: str, data: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO oauth_clients (client_id, data) VALUES (?, ?) "
            "ON CONFLICT(client_id) DO UPDATE SET data = excluded.data",
            (client_id, json.dumps(data)),
        )
        conn.commit()


def get_client(client_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT data FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return json.loads(row[0]) if row else None


# --------------------------------------------------------- auth codes --

def save_auth_code(
    code: str, client_id: str, code_challenge: str, redirect_uri: str,
    redirect_uri_provided_explicitly: bool, scopes: list[str], resource: Optional[str],
    subject: str, expires_at: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO auth_codes
                (code, client_id, code_challenge, redirect_uri, redirect_uri_provided_explicitly,
                 scopes, resource, subject, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (code, client_id, code_challenge, redirect_uri, int(redirect_uri_provided_explicitly),
             json.dumps(scopes), resource, subject, expires_at),
        )
        conn.commit()


def get_auth_code(code: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT client_id, code_challenge, redirect_uri, redirect_uri_provided_explicitly, "
            "scopes, resource, subject, expires_at FROM auth_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if not row:
            return None
        return {
            "client_id": row[0], "code_challenge": row[1], "redirect_uri": row[2],
            "redirect_uri_provided_explicitly": bool(row[3]), "scopes": json.loads(row[4]),
            "resource": row[5], "subject": row[6], "expires_at": row[7],
        }


def delete_auth_code(code: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM auth_codes WHERE code = ?", (code,))
        conn.commit()


# ------------------------------------------------------------ tokens --

def save_access_token(token: str, client_id: str, scopes: list[str], resource: Optional[str],
                       subject: str, expires_at: Optional[int]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO access_tokens (token, client_id, scopes, resource, subject, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, client_id, json.dumps(scopes), resource, subject, expires_at),
        )
        conn.commit()


def get_access_token(token: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT client_id, scopes, resource, subject, expires_at FROM access_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        return {"client_id": row[0], "scopes": json.loads(row[1]), "resource": row[2],
                "subject": row[3], "expires_at": row[4]}


def delete_access_token(token: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM access_tokens WHERE token = ?", (token,))
        conn.commit()


def save_refresh_token(token: str, client_id: str, scopes: list[str], subject: str,
                        expires_at: Optional[int]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO refresh_tokens (token, client_id, scopes, subject, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token, client_id, json.dumps(scopes), subject, expires_at),
        )
        conn.commit()


def get_refresh_token(token: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT client_id, scopes, subject, expires_at FROM refresh_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        return {"client_id": row[0], "scopes": json.loads(row[1]), "subject": row[2], "expires_at": row[3]}


def delete_refresh_token(token: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
        conn.commit()


def delete_tokens_for_client_subject(client_id: str, subject: str) -> None:
    """Used when rotating: invalidate every prior token for this (client, user) pair."""
    with _connect() as conn:
        conn.execute("DELETE FROM access_tokens WHERE client_id = ? AND subject = ?", (client_id, subject))
        conn.execute("DELETE FROM refresh_tokens WHERE client_id = ? AND subject = ?", (client_id, subject))
        conn.commit()


# ------------------------------------------------------- pending auth --
# Bridges the MCP SDK's /authorize call to our own first-party /mcp/login
# form: authorize() can't show HTML itself (it only returns a redirect
# URL), so it stashes the validated AuthorizationParams here under a
# short-lived continuation id and redirects to /mcp/login?continuation=...,
# which reads them back once the user has logged in.
_pending_auth: dict[str, dict[str, Any]] = {}
PENDING_AUTH_TTL_SECONDS = 600


def save_pending_authorization(continuation_id: str, params: dict[str, Any]) -> None:
    now = time.time()
    for key in [k for k, v in _pending_auth.items() if now - v["_created_at"] > PENDING_AUTH_TTL_SECONDS]:
        _pending_auth.pop(key, None)
    params["_created_at"] = now
    _pending_auth[continuation_id] = params


def get_pending_authorization(continuation_id: str) -> Optional[dict[str, Any]]:
    return _pending_auth.get(continuation_id)


def pop_pending_authorization(continuation_id: str) -> Optional[dict[str, Any]]:
    return _pending_auth.pop(continuation_id, None)
