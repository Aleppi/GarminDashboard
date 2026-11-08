"""Sign-in with Garmin Connect credentials — the only way into the dashboard
(web app and MCP connector alike). There is no separate dashboard password.

A sign-in is a real Garmin login: the password is used once, in memory, to
log in and dump a fresh tokenstore, then discarded. It is never written to
users.json (see users.py for why that matters). A side effect is that every
sign-in also refreshes the cached Garmin session, so "my Garmin session
expired" is fixed by simply signing in again — no separate reconnect step.

Garmin's MFA flow needs a code from a second HTTP request, so this uses
return_on_mfa=True + resume_login() and holds the live Garmin object in an
in-memory dict between requests. Restarting the process mid-MFA loses that
half-finished sign-in (the person just starts again); nothing else is lost.

Two guards keep sign-in from hurting everyone else:
  - An email with no account here is turned away BEFORE anything is sent to
    Garmin (unless the caller has already checked an invite code — that's
    signup). Otherwise anyone could use this server as a free Garmin
    password oracle, and enough failed attempts gets the server's IP
    rate-limited by Garmin, which blocks sign-in for every user. The cost is
    that the error tells a visitor whether an email has an account here.
  - Once Garmin 429s, all sign-ins back off for a cooldown — retrying into
    the block only extends it. The garminconnect library tries several login
    transports per call, so one submission is ~5-6 requests at Garmin's edge.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Optional

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

import garmin_client
import users

PENDING_TTL_SECONDS = int(os.getenv("SIGNUP_MFA_TTL_SECONDS", "600"))
MAX_MFA_ATTEMPTS = 5
GARMIN_LOGIN_COOLDOWN_SECONDS = int(os.getenv("SIGNUP_GARMIN_COOLDOWN", "900"))  # 15 min

NO_ACCOUNT_MESSAGE = (
    "There's no dashboard account for that Garmin email. Check it's the email "
    "you use for Garmin Connect — or, if you have an invite code, create an account."
)

_garmin_blocked_until = 0.0
_pending: dict[str, dict] = {}


def invite_code_configured() -> bool:
    return bool(os.getenv("SIGNUP_INVITE_CODE"))


def check_invite_code(code: str) -> bool:
    expected = os.getenv("SIGNUP_INVITE_CODE", "")
    return bool(expected) and secrets.compare_digest(code, expected)


def garmin_cooldown_remaining() -> int:
    """Seconds left before it's worth touching Garmin's login again."""
    return max(0, int(_garmin_blocked_until - time.time()))


def _note_garmin_rate_limited() -> None:
    global _garmin_blocked_until
    _garmin_blocked_until = time.time() + GARMIN_LOGIN_COOLDOWN_SECONDS


def _rate_limited_message(minutes: int) -> str:
    return (
        "Garmin has temporarily blocked sign-ins from this server (too many login "
        "attempts). Nothing is wrong with your details — please wait about "
        f"{max(1, minutes)} more minute(s) and try once. Retrying sooner only "
        "extends the block."
    )


def _cleanup_expired() -> None:
    now = time.time()
    for key in [k for k, v in _pending.items() if now - v["created_at"] > PENDING_TTL_SECONDS]:
        _pending.pop(key, None)


def _finalize(garmin: Garmin, garmin_email: str, username: Optional[str]) -> str:
    """Garmin accepted the login: create the account if this is a signup,
    save the fresh session, and return the account id to sign in as."""
    if username is None:
        # Re-check rather than trust the lookup from before the Garmin round
        # trip — two signups for one email can race through MFA.
        username = users.find_by_garmin_email(garmin_email) or users.create_user(garmin_email)
    record = users.get_user(username) or {}
    tokenstore = str(Path(record.get("tokenstore") or f"./data/{username}/garmin_tokens").expanduser())
    Path(tokenstore).parent.mkdir(parents=True, exist_ok=True)
    garmin.client.dump(tokenstore)
    garmin_client.reset_client(username)  # drop any stale in-memory client
    return username


def start_login(garmin_email: str, garmin_password: str, *, allow_new_account: bool = False) -> dict:
    """Sign in with Garmin credentials. `allow_new_account` creates an account
    for an email that doesn't have one yet — callers must only pass it once
    an invite code has been checked.

    Returns one of:
    {'status': 'done', 'username': <account id>}
    {'status': 'mfa', 'token': <str>}
    {'status': 'error', 'message': <str>, 'suspicious': <bool>}
    `suspicious` tells the caller whether to count this against its
    rate limit — a wrong password or unknown email does; Garmin being
    unreachable or rate-limited is nobody's guess and doesn't.
    """
    _cleanup_expired()

    email = (garmin_email or "").strip()
    if not email or not garmin_password:
        return {"status": "error", "message": "Enter your Garmin email and password.", "suspicious": False}

    username = users.find_by_garmin_email(email)
    if username is None and not allow_new_account:
        return {"status": "error", "message": NO_ACCOUNT_MESSAGE, "suspicious": True}

    cooldown = garmin_cooldown_remaining()
    if cooldown:
        return {"status": "error", "message": _rate_limited_message(round(cooldown / 60)), "suspicious": False}

    garmin = Garmin(email=email, password=garmin_password, return_on_mfa=True)
    try:
        mfa_status, _ = garmin.login()
    except GarminConnectAuthenticationError:
        return {"status": "error", "message": "Garmin rejected that email and password.", "suspicious": True}
    except GarminConnectTooManyRequestsError:
        _note_garmin_rate_limited()
        return {
            "status": "error",
            "message": _rate_limited_message(round(GARMIN_LOGIN_COOLDOWN_SECONDS / 60)),
            "suspicious": False,
        }
    except GarminConnectConnectionError as exc:
        return {"status": "error", "message": f"Could not reach Garmin Connect: {exc}", "suspicious": False}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"Unexpected error signing in to Garmin: {exc}", "suspicious": False}

    if mfa_status is None:
        return {"status": "done", "username": _finalize(garmin, email, username)}

    token = secrets.token_urlsafe(24)
    _pending[token] = {
        "garmin": garmin,
        "garmin_email": email,
        "username": username,
        "created_at": time.time(),
        "attempts": 0,
    }
    return {"status": "mfa", "token": token}


def complete_mfa(token: str, mfa_code: str) -> dict:
    """Second half of a sign-in Garmin asked an MFA code for. Same return
    shapes as start_login; a wrong code keeps the token alive for a retry."""
    _cleanup_expired()
    entry = _pending.get(token)
    if entry is None:
        return {"status": "error", "message": "That sign-in expired. Start again.", "suspicious": False}

    entry["attempts"] += 1
    if entry["attempts"] > MAX_MFA_ATTEMPTS:
        _pending.pop(token, None)
        return {"status": "error", "message": "Too many wrong codes. Start again.", "suspicious": True}

    try:
        entry["garmin"].resume_login({}, mfa_code)
    except Exception as exc:  # noqa: BLE001
        return {"status": "mfa", "token": token, "message": f"Wrong code, try again: {exc}"}

    _pending.pop(token, None)
    return {"status": "done", "username": _finalize(entry["garmin"], entry["garmin_email"], entry["username"])}
