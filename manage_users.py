"""Admin commands for dashboard accounts, run from a terminal on the server.

Accounts are Garmin Connect accounts — there is no dashboard password —
so most people never need this: they sign up at /signup with the invite
code and sign in with their Garmin email and password. This covers the
rest:

    python manage_users.py init                   # create SESSION_SECRET in .env if missing
    python manage_users.py list                   # accounts and their Garmin emails
    python manage_users.py add <garmin_email>     # add someone without an invite code
    python manage_users.py relogin <account|email>  # refresh a Garmin session from here
    python manage_users.py set-email <account> <new_garmin_email>
    python manage_users.py remove <account>       # delete the account record (keeps its data dir)
    python manage_users.py migrate                # convert pre-Garmin-sign-in users.json

`add` and `relogin` prompt for that person's Garmin password (and an MFA
code if Garmin asks). The password is used once, here, to sign in and cache
a session — it is never written anywhere (see users.py).
"""

from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

import garmin_login
import users

ENV_PATH = Path(__file__).parent / ".env"


def _ensure_session_secret() -> None:
    if not ENV_PATH.exists():
        print(f"{ENV_PATH} doesn't exist yet. Copy .env.example to .env first.")
        raise SystemExit(1)
    # Only generate one if unset, so a routine run doesn't silently log out
    # every existing session.
    if not dotenv_values(ENV_PATH).get("SESSION_SECRET"):
        set_key(str(ENV_PATH), "SESSION_SECRET", secrets.token_hex(32))
        print("Generated SESSION_SECRET in .env.")


def _sign_in(garmin_email: str, allow_new_account: bool) -> int:
    password = getpass.getpass(f"Garmin Connect password for {garmin_email}: ")
    result = garmin_login.start_login(garmin_email, password, allow_new_account=allow_new_account)
    while result["status"] == "mfa":
        if result.get("message"):
            print(result["message"])
        code = input("Enter the verification code Garmin sent: ").strip()
        result = garmin_login.complete_mfa(result["token"], code)
    if result["status"] != "done":
        print(result["message"])
        return 1
    print(f"Signed in to Garmin as {garmin_email} — account '{result['username']}', session cached.")
    return 0


def _resolve(account_or_email: str) -> str | None:
    if users.get_user(account_or_email) is not None:
        return account_or_email
    return users.find_by_garmin_email(account_or_email)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]

    if cmd == "init" and not args:
        _ensure_session_secret()
        return 0

    if cmd == "list" and not args:
        for username in users.list_usernames():
            record = users.get_user(username) or {}
            linked = "" if users.garmin_linked(username) else "  (no Garmin session cached)"
            print(f"{username:20} {record.get('garmin_email') or '-'}{linked}")
        return 0

    if cmd == "add" and len(args) == 1:
        _ensure_session_secret()
        existing = users.find_by_garmin_email(args[0])
        if existing:
            print(f"{args[0]} already has an account ('{existing}') — refreshing its Garmin session instead.")
        return _sign_in(args[0], allow_new_account=True)

    if cmd == "relogin" and len(args) == 1:
        username = _resolve(args[0])
        if username is None:
            print(f"No account or Garmin email '{args[0]}'. Try `python manage_users.py list`.")
            return 1
        return _sign_in(users.get_user(username)["garmin_email"], allow_new_account=False)

    if cmd == "set-email" and len(args) == 2:
        username, new_email = args
        other = users.find_by_garmin_email(new_email)
        if other and other != username:
            print(f"{new_email} is already the sign-in for account '{other}'.")
            return 1
        if not users.set_garmin_email(username, new_email):
            print(f"No account '{username}'.")
            return 1
        print(f"'{username}' now signs in as {new_email}.")
        return 0

    if cmd == "remove" and len(args) == 1:
        if not users.delete_user(args[0]):
            print(f"No account '{args[0]}'.")
            return 1
        print(f"Removed '{args[0]}' from {users.store_path()}. Its data under ./data/{args[0]} was left in place.")
        return 0

    if cmd == "migrate" and not args:
        report = users.migrate_to_garmin_login()
        print(f"Dropped dashboard passwords from: {', '.join(report['migrated']) or 'none (already migrated)'}")
        for username in users.list_usernames():
            print(f"  {username:20} signs in as {(users.get_user(username) or {}).get('garmin_email') or '-'}")
        problems = 0
        if report["unlinked"]:
            problems = 1
            print(f"No Garmin email, so nobody can sign in: {', '.join(report['unlinked'])} — "
                  "fix with `set-email`, or `remove`.")
        if report["duplicate_email"]:
            problems = 1
            print(f"Share a Garmin email, so sign-in picks one arbitrarily: {', '.join(report['duplicate_email'])} — "
                  "`remove` the extra account or `set-email` it.")
        return problems

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
