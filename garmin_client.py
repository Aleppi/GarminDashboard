"""Thin wrapper around garminconnect.Garmin.

Handles login with on-disk token caching so the app reuses one Garmin
Connect session instead of logging in fresh on every request (repeated
logins can get an account flagged). One client per dashboard account — each
account in users.json is one Garmin account with its own token cache, which
signing in (garmin_login.py) writes.
"""

from __future__ import annotations

import functools
import inspect
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

import users

logger = logging.getLogger(__name__)


class GarminAuthRequired(Exception):
    """Raised when a human needs to complete login/MFA interactively."""


class GarminUnavailable(Exception):
    """Raised when Garmin Connect could not be reached at all."""


def _mfa_unsupported() -> str:
    raise GarminAuthRequired(
        "Garmin is asking for a multi-factor authentication code, which "
        "can't be answered in the background. Sign out and sign in again — "
        "the sign-in page asks for the code — then reload the dashboard."
    )


_clients: dict[str, "_ReauthingClient"] = {}
_login_locks: dict[str, threading.Lock] = {}
_login_locks_guard = threading.Lock()


def _lock_for(username: str) -> threading.Lock:
    with _login_locks_guard:
        return _login_locks.setdefault(username, threading.Lock())


def _looks_like_expired_session(exc: Exception) -> bool:
    """Garmin surfaces an expired OAuth session either as its own
    authentication error or as a bare 401 from the HTTP layer, depending on
    which endpoint tripped it — treat both as "re-login and try again"."""
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    return "401" in str(exc) and "unauthor" in str(exc).lower() or "Authentication failed" in str(exc)


class _ReauthingClient:
    """Wraps a logged-in Garmin client so an expired session re-logs in and
    retries once, instead of failing every call until the process restarts.

    The in-memory client used to be cached for the life of the process with
    no revalidation, while reset_client() was only ever called by the manual
    /reauth flow. So when Garmin expired a session mid-process, that process
    was stuck returning 401s indefinitely — worst for the MCP server, which
    stays up for days, even though the on-disk tokenstore was usually still
    good and a plain re-login would have fixed it.
    """

    def __init__(self, username: str, garmin: Garmin):
        self._username = username
        self._garmin = garmin

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._garmin, name)
        # Only bound methods get the retry wrapper. Class/enum attributes
        # (e.g. client.ActivityDownloadFormat) are callable too but must be
        # handed back untouched or attribute access on them breaks.
        if not inspect.ismethod(attr):
            return attr

        @functools.wraps(attr)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - narrowed by the check below
                if not _looks_like_expired_session(exc):
                    raise
                logger.info(
                    "Garmin session for %s looks expired (%s) — re-logging in and retrying %s.",
                    self._username,
                    exc,
                    name,
                )
                fresh = _login(self._username)
                self._garmin = fresh
                return getattr(fresh, name)(*args, **kwargs)

        return wrapper


def get_client(username: str) -> "_ReauthingClient":
    """Return a logged-in Garmin client for this dashboard user, reusing the
    cached session if possible. The returned client re-authenticates itself
    if Garmin expires the session (see _ReauthingClient).

    Raises GarminAuthRequired or GarminUnavailable with a message suitable
    for showing directly on the dashboard.
    """
    cached = _clients.get(username)
    if cached is not None:
        return cached

    # Serialize concurrent first-logins for the same user — dashboard_data
    # fans Garmin calls across a thread pool, so several threads can miss
    # the cache at once and each start their own login.
    with _lock_for(username):
        cached = _clients.get(username)
        if cached is not None:
            return cached
        client = _ReauthingClient(username, _login(username))
        _clients[username] = client
        return client


def _login(username: str) -> Garmin:
    """Fresh login from the user's stored tokenstore."""
    record = users.get_user(username)
    if record is None:
        raise GarminAuthRequired(f"No user '{username}' in the user store.")

    tokenstore = str(Path(record["tokenstore"]).expanduser())

    if not Path(tokenstore).exists():
        raise GarminAuthRequired(
            f"No cached Garmin session for '{username}' at {tokenstore}. "
            "Sign out and sign in again to create one."
        )

    # No password is kept on file (see users.py) — the cached tokenstore
    # is expected to carry the session on its own. email/password are left
    # unset here on purpose; if the tokens are rejected below, the fix is
    # signing in again, not resurrecting a stored password.
    garmin = Garmin(email=None, password=None, prompt_mfa=_mfa_unsupported)

    try:
        garmin.login(tokenstore=tokenstore)
    except GarminAuthRequired:
        raise
    except GarminConnectTooManyRequestsError as exc:
        raise GarminUnavailable(
            f"Garmin Connect is rate-limiting this account right now: {exc}. "
            "Wait a few minutes before retrying."
        ) from exc
    except GarminConnectAuthenticationError as exc:
        raise GarminAuthRequired(
            f"Garmin Connect rejected the cached session for '{username}' "
            f"({exc}). Sign in again to refresh it."
        ) from exc
    except GarminConnectConnectionError as exc:
        raise GarminUnavailable(f"Could not reach Garmin Connect: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected too
        raise GarminUnavailable(f"Unexpected error logging in to Garmin: {exc}") from exc

    return garmin


def reset_client(username: str) -> None:
    """Drop the cached in-memory client so the next call re-logs in."""
    _clients.pop(username, None)


def safe_call(fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Call a Garmin API method, returning `default` and logging on failure.

    One flaky/unsupported endpoint shouldn't take down the whole dashboard
    payload, so every per-field fetch in dashboard_data.py goes through this.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        label = getattr(fn, "__name__", str(fn))
        logger.warning("Garmin call %s%r failed: %s", label, args, exc)
        return default
