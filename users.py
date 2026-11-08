"""Multi-user store: one dashboard account per Garmin Connect account.

Backed by a single JSON file (gitignored, like .env). There is no separate
dashboard password: you sign in with your Garmin credentials (see
garmin_login.py), and the Garmin email on file is what maps a sign-in to an
account. Records are keyed by an internal account id — the old dashboard
username for accounts that predate Garmin sign-in — which every per-user
table, token directory and MCP OAuth token is scoped by. Keeping that id
stable is why an email change is a field update (manage_users.py
set-email), not a re-keying of everyone's data.

Garmin passwords are NEVER persisted here. garminconnect only needs the
real password at the moment of an interactive login — day to day, the
cached tokenstore is sufficient, so the password is used once and
discarded by the caller instead of being written to disk. This matters
because anyone with filesystem access to the machine running this app (the
admin, a housemate, anyone who compromises the box) could otherwise read
every user's real Garmin password straight out of this file.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

USERS_PATH = os.getenv("USERS_STORE", "./data/users.json")


def store_path() -> Path:
    return Path(USERS_PATH).expanduser()


def load_users() -> dict[str, dict]:
    path = store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write(users: dict[str, dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(users, indent=2))


def get_user(username: str) -> Optional[dict]:
    return load_users().get(username)


def find_by_garmin_email(garmin_email: str) -> Optional[str]:
    """The account id signed in to with this Garmin email, if any. Garmin
    itself treats emails case-insensitively, so this does too."""
    wanted = (garmin_email or "").strip().lower()
    if not wanted:
        return None
    for username, record in load_users().items():
        if (record.get("garmin_email") or "").strip().lower() == wanted:
            return username
    return None


def _new_username(garmin_email: str, existing: dict[str, dict]) -> str:
    """Readable id for a new account, from the email's local part — it ends
    up as a directory name under data/, so it's restricted to characters
    that can't escape that directory."""
    base = re.sub(r"[^a-z0-9]+", "-", garmin_email.split("@")[0].lower()).strip("-") or "user"
    # Case-insensitive: older ids like "Anna" are mixed-case, and "anna"
    # beside it would be two data directories a case-folding copy merges.
    taken = {u.lower() for u in existing}
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def create_user(garmin_email: str) -> str:
    """Create an account for a Garmin email that just signed in successfully
    for the first time. Returns the new account id."""
    users = load_users()
    username = _new_username(garmin_email, users)
    users[username] = {
        "garmin_email": garmin_email.strip(),
        "tokenstore": f"./data/{username}/garmin_tokens",
    }
    _write(users)
    return username


def set_garmin_email(username: str, garmin_email: str) -> bool:
    """Point an existing account at a different Garmin email (the person
    changed theirs at Garmin). Returns whether the account exists."""
    users = load_users()
    if username not in users:
        return False
    users[username]["garmin_email"] = garmin_email.strip()
    _write(users)
    return True


def garmin_linked(username: str) -> bool:
    """Whether this account can actually talk to Garmin — needs a tokenstore
    on disk. Normally always true for a signed-in user (signing in writes
    one), but an admin may have removed it."""
    record = get_user(username)
    if not record or not record.get("garmin_email"):
        return False
    tokenstore = record.get("tokenstore")
    return bool(tokenstore) and Path(tokenstore).expanduser().exists()


def list_usernames() -> list[str]:
    return sorted(load_users().keys())


def delete_user(username: str) -> bool:
    """Remove an account record so the Garmin email is free to sign up again.

    Only touches users.json — the per-user data directory (Garmin
    tokenstore) is left for the caller to deal with, since removing it is
    irreversible and worth doing deliberately rather than as a side effect.
    Returns whether there was anything to delete."""
    users = load_users()
    if username not in users:
        return False
    del users[username]
    _write(users)
    return True


def migrate_to_garmin_login() -> dict[str, list[str]]:
    """One-off migration from dashboard-password accounts to Garmin sign-in:
    drops the now-unused `password_hash` (and any `garmin_password` left
    over from before Garmin passwords stopped being persisted).

    Accounts are otherwise kept exactly as they are — same id, same
    tokenstore, same data — since the Garmin email each was linked to
    becomes its sign-in. Reports problems instead of guessing at them:
    `unlinked` accounts have no Garmin email so nobody can sign in to them,
    and `duplicate_email` accounts share one with another account, so a
    sign-in can't tell which was meant. Safe to run repeatedly."""
    users = load_users()
    changed = [u for u, rec in users.items() if "password_hash" in rec or "garmin_password" in rec]
    for u in changed:
        users[u].pop("password_hash", None)
        users[u].pop("garmin_password", None)
    if changed:
        _write(users)

    by_email: dict[str, list[str]] = {}
    for u, rec in users.items():
        email = (rec.get("garmin_email") or "").strip().lower()
        if email:
            by_email.setdefault(email, []).append(u)
    return {
        "migrated": changed,
        "unlinked": [u for u, rec in users.items() if not (rec.get("garmin_email") or "").strip()],
        "duplicate_email": sorted(u for group in by_email.values() if len(group) > 1 for u in group),
    }
