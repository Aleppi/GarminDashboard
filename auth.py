"""Session-based login gate for exposing the dashboard outside localhost.

Multi-user: each account in users.json is one Garmin Connect account, with
its own cache and own review notes. Signing in is done with the Garmin
credentials themselves (see garmin_login.py) — this module only holds the
session check and the per-IP failed-attempt limiter both front ends share.
"""

from __future__ import annotations

import os
import time
from functools import wraps
from typing import Optional

from flask import jsonify, redirect, request, session, url_for

import users

RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("LOGIN_RATE_LIMIT_ATTEMPTS", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_LIMIT_WINDOW", "900"))  # 15 min

# In-memory per-IP failed-attempt tracking. Resets on restart — fine for a
# single-process personal server; not meant to survive a real distributed attack.
_failed_attempts: dict[str, list[float]] = {}


def client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def is_locked_out(ip: Optional[str] = None) -> bool:
    ip = ip or client_ip()
    now = time.time()
    attempts = [t for t in _failed_attempts.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
    _failed_attempts[ip] = attempts
    return len(attempts) >= RATE_LIMIT_MAX_ATTEMPTS


def record_failed_attempt(ip: Optional[str] = None) -> None:
    ip = ip or client_ip()
    _failed_attempts.setdefault(ip, []).append(time.time())


def clear_failed_attempts(ip: Optional[str] = None) -> None:
    ip = ip or client_ip()
    _failed_attempts.pop(ip, None)


def current_username() -> Optional[str]:
    return session.get("username")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        username = session.get("username")
        # Sessions last SESSION_LIFETIME_DAYS — re-check the account still
        # exists so a removed one doesn't keep working until its cookie expires.
        if username and users.get_user(username) is not None:
            return view(*args, **kwargs)
        session.pop("username", None)
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not logged in.", "error_type": "unauthenticated"}), 401
        return redirect(url_for("login", next=request.path))

    return wrapped
