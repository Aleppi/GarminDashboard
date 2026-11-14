from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, render_template, request, session, url_for

load_dotenv()

import auth
import coach_notes
import garmin_client
import garmin_login
import race_days
import reviews
import users
from dashboard_data import (
    CYCLING_TYPE_HINTS,
    MAX_ACTIVITY_RANGE_DAYS,
    MAX_TREND_RANGE_DAYS,
    build_activity_calendar,
    build_activity_training_load,
    build_metric_history,
    build_trend_range,
    coach_note_is_stale,
    get_dashboard_payload,
)
from garmin_client import GarminAuthRequired, GarminUnavailable

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET") or os.urandom(32)
if not os.getenv("SESSION_SECRET"):
    logger.warning(
        "SESSION_SECRET not set in .env — using a random key for this run, "
        "which will log everyone out on every restart. Run "
        "`python manage_users.py init` to generate one and save it permanently."
    )
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=int(os.getenv("SESSION_LIFETIME_DAYS", "30")))


def asset_version(rel_path: str) -> int:
    """Static assets are served with a Cache-Control that only asks browsers
    to revalidate, not to skip caching outright — some still show a stale
    copy anyway (bfcache, aggressive disk cache). Stamping the URL with the
    file's own mtime forces a fresh fetch on every edit instead of relying
    on that revalidation happening."""
    try:
        return int(os.path.getmtime(os.path.join(app.static_folder, rel_path)))
    except OSError:
        return 0


app.jinja_env.globals["asset_version"] = asset_version

CACHE_TTL = int(os.getenv("DASHBOARD_CACHE_TTL", "1200"))
_cache_lock = threading.Lock()
# One cache entry per dashboard user, so each person's data refreshes on its
# own schedule and nobody's fetch waits on or overwrites anybody else's.
_caches: dict[str, dict] = {}


def _user_cache(username: str) -> dict:
    return _caches.setdefault(username, {"payload": None, "fetched_at": 0.0})


# Small cache shared by the calendar and trend-history endpoints, both of
# which fetch an arbitrary caller-supplied date range — keyed by a tuple
# starting with a tag ("activities"/"trend") plus (username, start, end).
# The frontend already avoids re-requesting a range it has loaded
# client-side; this just protects Garmin from repeat hits across page
# reloads/tabs within the same TTL.
_range_caches: dict[tuple, dict] = {}


def _fetch_dashboard(username: str, force: bool) -> tuple[dict, int, bool]:
    with _cache_lock:
        cache = _user_cache(username)
        fresh_enough = cache["payload"] is not None and (time.time() - cache["fetched_at"]) < CACHE_TTL
        if fresh_enough and not force:
            return cache["payload"], 200, True

    try:
        client = garmin_client.get_client(username)
        payload = get_dashboard_payload(client, username=username)
    except GarminAuthRequired as exc:
        return {"error": str(exc), "error_type": "auth"}, 200, False
    except GarminUnavailable as exc:
        return {"error": str(exc), "error_type": "unavailable"}, 200, False
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error building dashboard payload")
        return {"error": f"Unexpected server error: {exc}", "error_type": "unknown"}, 200, False

    with _cache_lock:
        cache = _user_cache(username)
        cache["payload"] = payload
        cache["fetched_at"] = time.time()
    return payload, 200, False


def _safe_next(path: str) -> str:
    # Only same-site paths — "//evil.example" is protocol-relative, not local.
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return url_for("index")


def _finish_login(result: dict, rl_key: str):
    """Shared tail of /login, /signup and /login/mfa: turn a garmin_login
    result into a signed-in redirect, an MFA redirect, or an error string
    (None when the result was handled with a redirect)."""
    if result["status"] == "done":
        auth.clear_failed_attempts(rl_key)
        session.permanent = True
        session["username"] = result["username"]
        return redirect(_safe_next(session.pop("login_next", "")))
    if result["status"] == "mfa":
        auth.clear_failed_attempts(rl_key)
        return redirect(url_for("login_mfa", token=result["token"]))
    if result.get("suspicious", True):
        auth.record_failed_attempt(rl_key)
    return result["message"]


@app.route("/login", methods=["GET", "POST"])
def login():
    """Sign in with Garmin Connect credentials — there is no separate
    dashboard password (see garmin_login.py)."""
    error = None
    garmin_email = ""
    if request.method == "POST":
        garmin_email = request.form.get("garmin_email", "").strip()
        rl_key = auth.client_ip()
        if auth.is_locked_out(rl_key):
            error = "Too many failed attempts. Try again in a few minutes."
        else:
            # Kept in the session rather than threaded through the MFA form,
            # so it survives that extra step without becoming a URL param.
            session["login_next"] = request.form.get("next", "")
            outcome = _finish_login(
                garmin_login.start_login(garmin_email, request.form.get("garmin_password", "")), rl_key
            )
            if not isinstance(outcome, str):
                return outcome
            error = outcome

    return render_template(
        "login.html",
        error=error,
        garmin_email=garmin_email,
        next=request.values.get("next", ""),
        expired=request.args.get("expired") == "1",
        signup_enabled=garmin_login.invite_code_configured(),
    )


@app.route("/login/mfa", methods=["GET", "POST"])
def login_mfa():
    token = request.values.get("token", "")
    error = None
    if request.method == "POST":
        rl_key = auth.client_ip()
        if auth.is_locked_out(rl_key):
            error = "Too many attempts. Try again in a few minutes."
        else:
            result = garmin_login.complete_mfa(token, request.form.get("mfa_code", "").strip())
            if result["status"] == "mfa":
                auth.record_failed_attempt(rl_key)
                error = result.get("message", "Wrong code, try again.")
            else:
                outcome = _finish_login(result, rl_key)
                if not isinstance(outcome, str):
                    return outcome
                error = outcome

    return render_template("login_mfa.html", error=error, token=token)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Invite-code-gated first sign-in for a Garmin account that has no
    dashboard account yet. Signing in with an email that already has one
    just signs in — nothing is duplicated."""
    if not garmin_login.invite_code_configured():
        return (
            "Signups aren't open. Ask whoever runs this dashboard to set "
            "SIGNUP_INVITE_CODE in .env, or to add you with "
            "`python manage_users.py add <your Garmin email>`.",
            503,
        )

    error = None
    garmin_email = ""
    if request.method == "POST":
        garmin_email = request.form.get("garmin_email", "").strip()
        rl_key = auth.client_ip() + ":signup"
        if auth.is_locked_out(rl_key):
            error = "Too many attempts. Try again in a few minutes."
        elif not garmin_login.check_invite_code(request.form.get("invite_code", "")):
            auth.record_failed_attempt(rl_key)
            error = "Wrong invite code."
        else:
            session.pop("login_next", None)
            outcome = _finish_login(
                garmin_login.start_login(
                    garmin_email, request.form.get("garmin_password", ""), allow_new_account=True
                ),
                rl_key,
            )
            if not isinstance(outcome, str):
                return outcome
            error = outcome

    return render_template("signup.html", error=error, garmin_email=garmin_email)


@app.route("/reauth")
def reauth():
    """The dashboard's "Garmin session expired" banner links here. Signing in
    again is what refreshes the session now, so this just starts that."""
    session.pop("username", None)
    return redirect(url_for("login", expired="1"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@auth.login_required
def index():
    # Signing in always writes a Garmin session, so this only trips if an
    # admin removed the tokenstore — signing in again recreates it.
    if not users.garmin_linked(auth.current_username()):
        return redirect(url_for("reauth"))
    # The page shell has no cache-busting query param of its own (unlike the
    # JS/CSS assets it links, which are mtime-stamped), so without an
    # explicit no-store a browser or intermediary (e.g. the Cloudflare
    # tunnel) can keep serving an old shell — missing newly-added elements —
    # even after it happily re-fetches the newer, cache-busted JS/CSS.
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/dashboard")
@auth.login_required
def api_dashboard():
    username = auth.current_username()
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    payload, status, served_from_cache = _fetch_dashboard(username, force)
    payload = dict(payload)
    payload["cached"] = served_from_cache
    fetched_at = _user_cache(username)["fetched_at"]
    payload["cache_age_seconds"] = int(time.time() - fetched_at) if fetched_at else None
    coach_note = coach_notes.get_latest(username)
    if coach_note_is_stale(coach_note, payload.get("last_activity")):
        coach_note = None
    payload["coach_note"] = coach_note
    return jsonify(payload), status


INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET")


@app.route("/internal/invalidate-cache", methods=["POST"])
def internal_invalidate_cache():
    """Called by mcp_server.py (a separate process — no shared memory) right
    after push_training_plan/push_coach_note/mark_race_day/unmark_race_day
    save, so the change is visible on the next dashboard load instead of
    sitting behind CACHE_TTL for up to 20 minutes. Guarded by a shared
    secret, not a session cookie — this is a machine-to-machine call, and a
    plain "request came from 127.0.0.1" check isn't enough on its own since
    the Cloudflare tunnel also terminates at 127.0.0.1, making external and
    internal callers look identical to Flask without the secret."""
    if not INTERNAL_API_SECRET or request.headers.get("X-Internal-Secret") != INTERNAL_API_SECRET:
        return jsonify({"error": "forbidden"}), 403
    username = (request.get_json(silent=True) or {}).get("username")
    if not username:
        return jsonify({"error": "username required"}), 400
    with _cache_lock:
        _caches.pop(username, None)
        # Range caches too — a newly marked race day changes both the
        # calendar's session badges and the trend chart's flags.
        for key in [k for k in _range_caches if k[1] == username]:
            _range_caches.pop(key, None)
    return jsonify({"invalidated": True})


@app.route("/api/activities")
@auth.login_required
def api_activities():
    username = auth.current_username()
    start_str = request.args.get("start", "")
    end_str = request.args.get("end", "")
    try:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
    except ValueError:
        return jsonify({"error": "start and end query params must be YYYY-MM-DD"}), 400
    if end < start:
        return jsonify({"error": "end must not be before start"}), 400
    if (end - start).days > MAX_ACTIVITY_RANGE_DAYS:
        return jsonify({"error": f"range too wide — max {MAX_ACTIVITY_RANGE_DAYS} days per request"}), 400

    cache_key = ("activities", username, start_str, end_str)
    with _cache_lock:
        cached = _range_caches.get(cache_key)
        if cached and (time.time() - cached["fetched_at"]) < CACHE_TTL:
            return jsonify({"days": cached["days"]})

    try:
        client = garmin_client.get_client(username)
    except GarminAuthRequired as exc:
        return jsonify({"error": str(exc), "error_type": "auth"}), 200
    except GarminUnavailable as exc:
        return jsonify({"error": str(exc), "error_type": "unavailable"}), 200

    race_dates = {r["date"] for r in race_days.get_race_days(username)}
    days = build_activity_calendar(client, start_str, end_str, race_dates)
    with _cache_lock:
        _range_caches[cache_key] = {"days": days, "fetched_at": time.time()}
    return jsonify({"days": days})


VALID_HISTORY_METRICS = {"vo2_run", "vo2_bike", "endurance"}


@app.route("/api/metric-history")
@auth.login_required
def api_metric_history():
    """History + tier bands behind the expandable VO2/endurance tiles.
    Cached on the same shared range cache as the calendar and trend
    endpoints — these series move at most once a day, so re-fetching on
    every expand/collapse would be pure waste."""
    username = auth.current_username()
    metric = request.args.get("metric", "")
    if metric not in VALID_HISTORY_METRICS:
        return jsonify({"error": f"metric must be one of {sorted(VALID_HISTORY_METRICS)}"}), 400

    today_str = date.today().isoformat()
    cache_key = ("metric-history", username, metric, today_str)
    with _cache_lock:
        cached = _range_caches.get(cache_key)
        if cached and (time.time() - cached["fetched_at"]) < CACHE_TTL:
            return jsonify(cached["payload"])

    try:
        client = garmin_client.get_client(username)
    except GarminAuthRequired as exc:
        return jsonify({"error": str(exc), "error_type": "auth"}), 200
    except GarminUnavailable as exc:
        return jsonify({"error": str(exc), "error_type": "unavailable"}), 200

    payload = build_metric_history(client, metric, today_str)
    with _cache_lock:
        _range_caches[cache_key] = {"payload": payload, "fetched_at": time.time()}
    return jsonify(payload)


@app.route("/api/activity/<activity_id>/training-load")
@auth.login_required
def api_activity_training_load(activity_id: str):
    username = auth.current_username()
    type_key = request.args.get("type_key", "").lower()
    total_load = request.args.get("total_load", type=float)
    try:
        client = garmin_client.get_client(username)
    except GarminAuthRequired as exc:
        return jsonify({"error": str(exc), "error_type": "auth"}), 200
    except GarminUnavailable as exc:
        return jsonify({"error": str(exc), "error_type": "unavailable"}), 200

    prefer_power = any(hint in type_key for hint in CYCLING_TYPE_HINTS)
    result = build_activity_training_load(client, activity_id, prefer_power, total_load)
    return jsonify(result)


@app.route("/api/trend")
@auth.login_required
def api_trend():
    username = auth.current_username()
    start_str = request.args.get("start", "")
    end_str = request.args.get("end", "")
    try:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
    except ValueError:
        return jsonify({"error": "start and end query params must be YYYY-MM-DD"}), 400
    if end < start:
        return jsonify({"error": "end must not be before start"}), 400
    if (end - start).days > MAX_TREND_RANGE_DAYS:
        return jsonify({"error": f"range too wide — max {MAX_TREND_RANGE_DAYS} days per request"}), 400

    # Each day in range costs one Garmin call fanned across a thread pool —
    # cache so scrolling back and forth over the same chunk doesn't re-hit
    # Garmin every time.
    cache_key = ("trend", username, start_str, end_str)
    with _cache_lock:
        cached = _range_caches.get(cache_key)
        if cached and (time.time() - cached["fetched_at"]) < CACHE_TTL:
            return jsonify(cached["result"])

    try:
        client = garmin_client.get_client(username)
    except GarminAuthRequired as exc:
        return jsonify({"error": str(exc), "error_type": "auth"}), 200
    except GarminUnavailable as exc:
        return jsonify({"error": str(exc), "error_type": "unavailable"}), 200

    race_dates = {r["date"] for r in race_days.get_race_days(username)}
    result = build_trend_range(client, start_str, end_str, race_dates, username=username)
    with _cache_lock:
        _range_caches[cache_key] = {"result": result, "fetched_at": time.time()}
    return jsonify(result)


@app.route("/api/review/<activity_id>", methods=["GET"])
@auth.login_required
def api_review_get(activity_id: str):
    note = reviews.get_review(auth.current_username(), activity_id)
    return jsonify({"activity_id": activity_id, "note": note or ""})


@app.route("/api/review/<activity_id>", methods=["POST"])
@auth.login_required
def api_review_post(activity_id: str):
    body = request.get_json(silent=True) or {}
    note = body.get("note", "")
    if not isinstance(note, str):
        return jsonify({"error": "note must be a string"}), 400
    reviews.save_review(auth.current_username(), activity_id, note)
    return jsonify({"activity_id": activity_id, "note": note, "saved": True})


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host=host, port=port, debug=debug)
