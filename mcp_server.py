"""MCP server: lets a Claude chat read this account's live training data
and push a coaching note back to the dashboard — the piece that closes
the loop between "ask Claude for advice" and "see it on the dashboard".

Runs as its own process (own port, own uvicorn), separate from the Flask
web app, because the MCP streamable-HTTP transport needs an ASGI server
and Flask is WSGI. Identity is shared with the web app, though: OAuth
authorization here defers to the same Garmin sign-in (garmin_login.py) via
the /mcp/login route (see mcp_oauth_provider.py for why authorize() can't
just render that page itself).
"""

from __future__ import annotations

import html
import logging
import os
from datetime import date
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import auth
import coach_notes
import garmin_client
import garmin_login
import oauth_store
import race_days
import race_plan
from dashboard_data import MAX_ACTIVITY_RANGE_DAYS, build_activity_calendar, coach_note_is_stale, get_dashboard_payload
from garmin_client import GarminAuthRequired, GarminUnavailable
from garmin_tools_bridge import register_garmin_tools
from mcp_oauth_provider import DashboardOAuthProvider

logger = logging.getLogger(__name__)

MCP_PUBLIC_URL = os.environ["MCP_PUBLIC_URL"].rstrip("/")
RESOURCE_URL = f"{MCP_PUBLIC_URL}/mcp"
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = os.getenv("MCP_PORT", "5052")
PUBLIC_HOSTNAME = urlparse(MCP_PUBLIC_URL).netloc  # e.g. "mcp.trainstate.app", no port

# Flask app (separate process, no shared memory) — called directly on
# localhost after a write, so its dashboard-payload cache drops for this
# user immediately instead of the change sitting behind CACHE_TTL for up
# to 20 minutes. Best-effort: this is a freshness optimization, not
# something that should make the actual save (already committed by the
# time this runs) look like it failed.
FLASK_HOST = os.getenv("HOST", "127.0.0.1")
FLASK_PORT = os.getenv("PORT", "5050")
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET")


def _invalidate_dashboard_cache(username: str) -> None:
    if not INTERNAL_API_SECRET:
        return
    try:
        requests.post(
            f"http://{FLASK_HOST}:{FLASK_PORT}/internal/invalidate-cache",
            json={"username": username},
            headers={"X-Internal-Secret": INTERNAL_API_SECRET},
            timeout=3,
        )
    except requests.RequestException as exc:
        logger.warning("Could not invalidate dashboard cache for %s: %s", username, exc)

# The SDK's DNS-rebinding protection rejects any Host header it wasn't told
# to expect — since we're reached through a reverse proxy/tunnel, the Host
# header on incoming requests is the public hostname, not 127.0.0.1, so it
# has to be allow-listed explicitly or every real tool call gets a 421.
TRANSPORT_SECURITY = TransportSecuritySettings(
    allowed_hosts=[PUBLIC_HOSTNAME, f"{MCP_HOST}:{MCP_PORT}", f"localhost:{MCP_PORT}"],
    allowed_origins=[MCP_PUBLIC_URL],
)

provider = DashboardOAuthProvider(MCP_PUBLIC_URL)

mcp = MCPServer(
    name="training-dashboard",
    instructions=(
        "Tools for a personal Garmin training dashboard. Call get_training_snapshot "
        "before suggesting any workout so the suggestion is grounded in the account's "
        "real, current data. Call push_coach_note afterward so the recommendation shows "
        "up on the dashboard itself, not just in this chat. If the athlete wants a "
        "structured plan toward a goal (not just a single next session), use "
        "push_training_plan instead — it lays out dated sessions across any sport "
        "(running, cycling, swimming, strength, etc.) for the current week on the "
        "dashboard and marks them completed automatically as matching activities come "
        "in. get_training_snapshot only covers roughly the last 3 weeks of activity — "
        "for anything further back, call list_activities with an explicit date range "
        "instead."
    ),
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=MCP_PUBLIC_URL,
        resource_server_url=RESOURCE_URL,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["dashboard"], default_scopes=["dashboard"]
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
)


def _current_username() -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise RuntimeError("No authenticated dashboard user on this token.")
    return token.subject


@mcp.tool()
def get_training_snapshot() -> dict:
    """Return this account's current training dashboard: readiness, CTL/ATL/TSB
    trend, training load balance, VO2 max, endurance score, recent activities,
    and any coach note already on file. Call this before suggesting a workout.

    Some activities (last_activity, route_log entries, training_plan sessions'
    completed_activity) may carry a "review_note" field — the athlete's own
    "how did it feel" text for that run. This is the one place pain,
    tightness, fatigue, or "felt great" shows up; it doesn't appear in any
    Garmin metric. Always check for it and weigh it — a note flagging a
    tender tendon should change what you suggest next even if every number
    looks fine."""
    username = _current_username()
    try:
        client = garmin_client.get_client(username)
        payload = get_dashboard_payload(client, username=username)
    except (GarminAuthRequired, GarminUnavailable) as exc:
        return {"error": str(exc)}
    coach_note = coach_notes.get_latest(username)
    if coach_note_is_stale(coach_note, payload.get("last_activity")):
        coach_note = None
    payload["coach_note"] = coach_note
    return payload


@mcp.tool()
def push_coach_note(session: str, reasoning: str = "") -> dict:
    """Save a coaching recommendation for this account. It replaces the
    dashboard's auto-generated 'Next Waypoint' suggestion until a fresher
    note is pushed, so the user sees it from any device, not just this chat.

    Args:
        session: A short, concrete session recommendation — e.g. "4x4
            intervals on the bike or run" or "Easy 40min zone 2 run". The
            dashboard shows this in bold, on its own, so keep it something
            that reads at a glance without the reasoning attached.
        reasoning: The supporting explanation — current readiness/load/ACWR,
            why this session, anything else worth knowing. Shown underneath
            in normal body text for anyone who wants to read further.
    """
    username = _current_username()
    coach_notes.save_note(username, session, reasoning)
    _invalidate_dashboard_cache(username)
    return {"saved": True}


@mcp.tool()
def push_training_plan(race_name: str, goal_time: str, race_date: str, sessions: list[dict]) -> dict:
    """Save a structured training plan working toward a goal race, across
    any mix of sports — running, cycling, swimming, strength, or anything
    else. The dashboard shows whichever of `sessions` fall in the current
    calendar week, and marks each one completed automatically once a
    matching activity shows up in Garmin activity history, so re-push the
    plan (e.g. weekly) to lay out the next block once one week's sessions
    are done.

    Matching is scoped to each session's own sport — a completed strength
    session never fills a planned run's slot, or vice versa. Within
    running/cycling/swimming specifically, matching also prefers same
    EFFORT (easy/tempo/interval/long, inferred from Garmin's own Training
    Effect data on the completed side, and from keywords like "tempo",
    "interval"/"sprint", "long", "easy"/"recovery" on the planned side) over
    nearest date — so mention effort in the session text when it matters
    (e.g. "Tempo run — 6km @ 4:15/km", not just "6km run"). Strength (and
    any other sport without a meaningful "effort") matches by nearest date
    alone.

    Args:
        race_name: e.g. "Half Marathon" or a specific race name.
        goal_time: Goal finish time, e.g. "1:30:00". Compare against
            get_training_snapshot's race prediction to check it's realistic
            before pushing a plan built around it.
        race_date: Race day, "YYYY-MM-DD".
        sessions: List of {"date": "YYYY-MM-DD", "session": "<description>",
            "activity_type": "running"|"cycling"|"swimming"|"strength"|
            "other"}, e.g. {"date": "2026-09-01", "session": "Tempo run —
            6km @ 4:15/km", "activity_type": "running"}. activity_type
            defaults to "running" if omitted. Only past/present/future dates
            within the plan's timeframe are meaningful; the dashboard only
            displays the current week's.
    """
    username = _current_username()
    race_plan.save_plan(username, race_name, goal_time, race_date, sessions)
    race_days.mark_race_day(username, race_date, race_name)  # also flags it on the fitness chart
    _invalidate_dashboard_cache(username)
    return {"saved": True}


@mcp.tool()
def mark_race_day(race_date: str, name: str = "") -> dict:
    """Flag a date as a race on the fitness (CTL/ATL) chart — nothing does
    this automatically (Garmin has no reliable "this was a race" signal for
    an untagged activity), so a day only shows up flagged if the athlete
    actually said it was a race. Use this whenever someone mentions an
    upcoming or past race, even casually ("I have a race on Oct 11th") —
    it doesn't require a training plan; for a full plan toward a goal race,
    use push_training_plan instead, which flags its own race_date automatically.

    Args:
        race_date: "YYYY-MM-DD".
        name: Optional label, e.g. "Basel Half Marathon".
    """
    username = _current_username()
    race_days.mark_race_day(username, race_date, name)
    _invalidate_dashboard_cache(username)
    return {"saved": True}


@mcp.tool()
def unmark_race_day(race_date: str) -> dict:
    """Remove a race-day flag — e.g. the athlete mistyped a date, or a race
    got cancelled/rescheduled. Args: race_date: "YYYY-MM-DD"."""
    username = _current_username()
    race_days.unmark_race_day(username, race_date)
    _invalidate_dashboard_cache(username)
    return {"saved": True}


@mcp.tool()
def list_activities(start_date: str, end_date: str) -> dict:
    """Look up this account's Garmin activities over an arbitrary date range —
    use this for any question about training history older than
    get_training_snapshot's rolling ~3-week window, e.g. "what did I run in
    March" or "how much did I train last year". Only days with at least one
    activity are included in the result.

    Args:
        start_date: "YYYY-MM-DD", inclusive.
        end_date: "YYYY-MM-DD", inclusive. Range can't exceed
            MAX_ACTIVITY_RANGE_DAYS (400) days — make multiple calls to cover
            a longer span.

    Returns: {"days": {"YYYY-MM-DD": [activity, ...], ...}}
    """
    username = _current_username()
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return {"error": "start_date and end_date must be YYYY-MM-DD"}
    if end < start:
        return {"error": "end_date must not be before start_date"}
    if (end - start).days > MAX_ACTIVITY_RANGE_DAYS:
        return {"error": f"range too wide — max {MAX_ACTIVITY_RANGE_DAYS} days per call"}
    try:
        client = garmin_client.get_client(username)
    except (GarminAuthRequired, GarminUnavailable) as exc:
        return {"error": str(exc)}
    race_dates = {r["date"] for r in race_days.get_race_days(username)}
    return {"days": build_activity_calendar(client, start_date, end_date, race_dates)}


# ------------------------------------------------ raw Garmin Connect tools --
# Everything above is this project's own curated/derived view of the data.
# Registering the vendored upstream tools on top means a chat with only this
# connector can also reach raw Garmin endpoints (sleep, HRV, splits, gear,
# weigh-ins, ...) without a second MCP server or a second login — they
# authenticate off this same OAuth token and the same dashboard account.
# See garmin_tools_bridge for how per-request identity is kept isolated,
# and GARMIN_TOOLS_MODE in .env to widen or disable the set.
_garmin_tools = register_garmin_tools(mcp, _current_username)


# ---------------------------------------------------------- login page --

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_page(
    client_name: str | None, continuation_id: str, error: str | None,
    mfa_token: str | None = None, garmin_email: str = "",
) -> str:
    # Everything interpolated is escaped: client_name is whatever a client
    # chose to register via DCR, and error text can carry Garmin's own
    # exception messages — neither belongs unescaped on a password page.
    error_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
    client_label = html.escape(client_name or "An app")
    continuation = html.escape(continuation_id)
    if mfa_token:
        fields = f"""
    <input type="hidden" name="mfa_token" value="{html.escape(mfa_token)}">
    <div class="field"><label for="mfa_code">Garmin sent you a verification code</label>
      <input type="text" id="mfa_code" name="mfa_code" inputmode="numeric" autocomplete="one-time-code" required autofocus></div>
    <button type="submit">Verify &amp; allow access</button>"""
    else:
        fields = f"""
    <div class="field"><label for="garmin_email">Garmin Connect email</label>
      <input type="email" id="garmin_email" name="garmin_email" value="{html.escape(garmin_email)}" autocomplete="username" required autofocus></div>
    <div class="field"><label for="garmin_password">Garmin password</label>
      <input type="password" id="garmin_password" name="garmin_password" autocomplete="current-password" required></div>
    <button type="submit">Sign in &amp; allow access</button>"""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Connect · Training Dashboard</title>
<style>
  body {{ background:#12181A; color:#EDEAE0; font-family:-apple-system,BlinkMacSystemFont,sans-serif;
    min-height:100vh; margin:0; display:flex; align-items:center; justify-content:center; }}
  .card {{ width:100%; max-width:360px; background:#1B2224; border:1px solid #2B3436;
    border-radius:4px; padding:28px 26px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ font-size:12.5px; color:#9BA39C; margin-bottom:22px; line-height:1.5; }}
  .field {{ margin-bottom:16px; }}
  label {{ display:block; font-size:12.5px; color:#9BA39C; margin-bottom:6px; }}
  input {{ width:100%; background:#212A2C; border:1px solid #2B3436; border-radius:3px;
    color:#EDEAE0; font-size:14px; padding:10px 12px; box-sizing:border-box; }}
  .err {{ border:1px solid #B0503A; background:rgba(176,80,58,0.10); border-radius:3px;
    padding:10px 12px; font-size:13px; color:#D98A6E; margin-bottom:16px; }}
  button {{ width:100%; font-size:13px; background:rgba(201,138,46,0.14); border:1px solid #C98A2E;
    color:#C98A2E; padding:10px 14px; border-radius:3px; cursor:pointer; }}
  button:hover {{ background:#C98A2E; color:#12181A; }}
</style></head>
<body><div class="card">
  <h1>Connect to your dashboard</h1>
  <div class="sub">{client_label} wants to read your training data and push coaching notes to your dashboard. Sign in with your Garmin Connect account to allow it — the same sign-in as the dashboard. Your password goes straight to Garmin and isn't stored.</div>
  {error_html}
  <form method="post">
    <input type="hidden" name="continuation" value="{continuation}">{fields}
  </form>
</div></body></html>"""


@mcp.custom_route("/mcp/login", methods=["GET", "POST"])
async def mcp_login(request: Request) -> Response:
    expired = HTMLResponse(
        "This connection request has expired. Go back to Claude and try connecting again.",
        status_code=400,
    )
    if request.method == "POST":
        form = await request.form()
        continuation_id = str(form.get("continuation") or "")
    else:
        continuation_id = request.query_params.get("continuation") or ""

    pending = oauth_store.get_pending_authorization(continuation_id)
    if pending is None:
        return expired
    client_name = pending.get("client_name")
    if request.method != "POST":
        return HTMLResponse(_login_page(client_name, continuation_id, None))

    rl_key = _client_ip(request) + ":mcp_login"
    mfa_token = str(form.get("mfa_token") or "")
    garmin_email = str(form.get("garmin_email") or "").strip()
    if auth.is_locked_out(rl_key):
        return HTMLResponse(_login_page(
            client_name, continuation_id, "Too many attempts. Try again in a few minutes.",
            mfa_token or None, garmin_email,
        ))

    # Garmin's login is blocking network I/O (several round trips) — keep it
    # off the event loop so live MCP tool calls aren't stalled behind it.
    if mfa_token:
        result = await run_in_threadpool(
            garmin_login.complete_mfa, mfa_token, str(form.get("mfa_code") or "").strip()
        )
    else:
        # No account creation from here — signing up needs the dashboard's
        # invite-code page, so an unknown email is just turned away.
        result = await run_in_threadpool(
            garmin_login.start_login, garmin_email, str(form.get("garmin_password") or "")
        )

    if result["status"] == "done":
        auth.clear_failed_attempts(rl_key)
        redirect_url = provider.complete_authorization(continuation_id, result["username"])
        if redirect_url is None:
            return expired
        return RedirectResponse(redirect_url, status_code=302)
    if result["status"] == "mfa":
        if mfa_token:  # a wrong code on the MFA step
            auth.record_failed_attempt(rl_key)
        return HTMLResponse(_login_page(client_name, continuation_id, result.get("message"), result["token"]))
    if result.get("suspicious", True):
        auth.record_failed_attempt(rl_key)
    return HTMLResponse(_login_page(client_name, continuation_id, result["message"], None, garmin_email))


app = mcp.streamable_http_app(host=MCP_HOST, transport_security=TRANSPORT_SECURITY)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=MCP_HOST, port=int(MCP_PORT))
