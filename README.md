# Garmin Training Dashboard

A local Flask dashboard that pulls live data from Garmin Connect: readiness,
CTL/ATL/TSB trend, training load balance, race predictions, vitals, and your
recent activities (with multisport legs) — same dark instrument-panel look as
the original static mockup, but every number is live. **Multi-user, Garmin
sign-in only**: you sign in with your Garmin Connect email and password —
there is no separate dashboard password. Each account is one Garmin account,
with its own cache and own review notes — you and anyone else sharing the
dashboard each see only your own data.

## Setup

**1. Create a virtual environment and install dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(On Windows: `.venv\Scripts\activate` instead of the `source` line.)

**2. Create `.env`**

```bash
cp .env.example .env
```

Nothing to fill in yet — the defaults work as-is. `.env` is gitignored —
never commit it. Then generate the session-signing secret:

```bash
python manage_users.py init
```

**3. Add each person**

Two ways to do this:

- **They add themselves** (nobody but them ever sees their Garmin
  password): set `SIGNUP_INVITE_CODE` in `.env` to some shared secret, and
  send that code to them privately (text, not through the app). They visit
  `/signup` and enter the code plus their Garmin email and password —
  including Garmin's verification code, right there in the browser, if their
  account asks for one. Leave `SIGNUP_INVITE_CODE` blank to disable this.

- **You add them** from a terminal on the server (needs them to type their
  Garmin password there, or tell it to you):
  ```bash
  python manage_users.py add their.garmin@email.com
  ```
  Prompts for the Garmin password and, if needed, the verification code.

Either way the Garmin password is used once to sign in and cache a session
token under `./data/<account>/garmin_tokens`, then discarded — it is never
written to `users.json`. The Flask app reuses that cached session on every
request afterwards rather than logging in fresh each time (repeated fresh
logins are what gets accounts flagged).

**4. Start the server**

```bash
python app.py
```

Then open `http://127.0.0.1:5050` (or whatever `HOST`/`PORT` you set in
`.env`) — you'll land on a sign-in page; everyone signs in with their own
Garmin email and password.

### Managing accounts

`manage_users.py` covers what the web pages don't:

| Command | What it does |
|---|---|
| `list` | Accounts and the Garmin email each signs in with |
| `add <garmin_email>` | Add someone without an invite code |
| `relogin <account or email>` | Refresh someone's Garmin session from the terminal |
| `set-email <account> <new_email>` | They changed their Garmin email — keeps all their data |
| `remove <account>` | Delete the account record (their data directory is left in place) |
| `migrate` | Convert a `users.json` from before Garmin sign-in (drops the old dashboard passwords; see below) |

**Upgrading from dashboard passwords.** Older versions had a separate
dashboard username/password per person. `python manage_users.py migrate`
drops those passwords and keeps every account exactly as it was — same
internal id, same Garmin session, same notes, plans and race days — with the
Garmin email it was already linked to becoming its sign-in. Existing browser
sessions and Claude connectors keep working; nobody has to re-link anything.

## How it works

- Every page and API route requires a signed-in session (`/login`, Garmin
  email + password + verification code if Garmin asks for one); five wrong
  attempts from the same IP locks out further tries for 15 minutes. Signing
  in is a real Garmin login, so it also refreshes that account's cached
  Garmin session — if the dashboard ever says the Garmin session expired,
  just sign in again.
- An email with no account here is turned away *before* anything is sent to
  Garmin, so the server can't be used as a Garmin password-guessing oracle
  (enough failed Garmin logins gets the server's IP rate-limited, blocking
  sign-in for everyone). The tradeoff: that error reveals whether an email
  has an account here. If Garmin does rate-limit the server, sign-ins back
  off for 15 minutes rather than retrying into the block.
- Forgot your password? It's your Garmin password — reset it at Garmin.
- Each login is fully isolated: its own Garmin client/token cache, its own
  `/api/dashboard` cache (refetches from Garmin independently of everyone
  else's), its own review notes. Nothing about one person's session ever
  touches another's data.
- `GET /api/dashboard` fetches everything from Garmin Connect for the
  logged-in user and returns one JSON payload. Responses are cached
  server-side for `DASHBOARD_CACHE_TTL` seconds (default 1200 = 20 min) per
  user; pass `?force=true` to bypass the cache and hit Garmin fresh. The
  page's **Refresh** button does exactly that.
- `GET /api/review/<activity_id>` / `POST /api/review/<activity_id>` read
  and write the logged-in user's own free-text notes on how a specific
  activity felt. Notes are stored server-side in a shared SQLite database
  at `./data/reviews.db` (path configurable via `REVIEWS_DB`), rows scoped
  per username — not in the browser.
- If Garmin login fails, times out, or a specific endpoint errors, the
  dashboard shows an error banner instead of crashing; sections with no data
  render as "No data" / "—" rather than throwing.
- `/signup` (only reachable if `SIGNUP_INVITE_CODE` is set) is the same
  Garmin sign-in plus an invite code, and creates the account on success.
  Wrong invite codes and failed Garmin logins count toward a
  5-attempts/15-minute IP lockout. A sign-in mid-MFA lives only in server
  memory and expires after 10 minutes — restarting the server during that
  window just means starting the sign-in again (an account isn't created
  until Garmin has accepted the login).

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `USERS_STORE` | `./data/users.json` | Accounts and the Garmin email each signs in with (no passwords) — edit via `manage_users.py`, not by hand |
| `REVIEWS_DB` | `./data/reviews.db` | SQLite file for review notes, shared file, rows scoped per user |
| `DASHBOARD_CACHE_TTL` | `1200` | Seconds before `/api/dashboard` refetches from Garmin, per user |
| `DASHBOARD_TREND_DAYS` | `42` | Days of history in the CTL/ATL ridge chart |
| `DASHBOARD_ROUTE_LOG_DAYS` | `21` | Days of history in the Route Log table |
| `HOST` / `PORT` | `127.0.0.1` / `5050` | Where Flask listens |
| `SESSION_SECRET` | unset | Signs the login cookie — `manage_users.py init` generates it |
| `SESSION_LIFETIME_DAYS` | `30` | How long a login stays valid |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` once served over real HTTPS (see below) |
| `SIGNUP_INVITE_CODE` | unset | Set to enable `/signup`; leave blank to only add people via `manage_users.py add` |

## Exposing this outside your LAN

If you're port-forwarding so you can reach this from your phone off-network,
the login page (above) stops a stranger from just loading the page, but it
does **not** encrypt the connection — the Flask dev server speaks plain
HTTP. Over plain HTTP, your Garmin password and every bit of Garmin health
data cross the internet readable by anything between you and your router
(public wifi, your ISP, etc.). Don't port-forward straight to `python
app.py` without fixing that.

The standard fix is a lightweight reverse proxy in front of Flask that
terminates real TLS:

1. **Get a hostname pointing at your home IP.** A dynamic DNS service (e.g.
   [duckdns.org](https://www.duckdns.org), free) gives you something like
   `yourname.duckdns.org` that follows your home IP even if it changes —
   most routers support updating it automatically.
2. **Put [Caddy](https://caddyserver.com) in front of Flask.** Caddy gets
   you automatic, auto-renewing HTTPS (Let's Encrypt) for free with a
   two-line config:
   ```
   yourname.duckdns.org {
       reverse_proxy 127.0.0.1:5050
   }
   ```
   Run Caddy on the machine, forward your router's ports 80 and 443 to it
   (not 5050 — Caddy is what should be internet-facing, Flask stays on
   localhost), and it handles the certificate for you.
3. **Set `SESSION_COOKIE_SECURE=true`** in `.env` once HTTPS is actually in
   front of it, and restart. This stops the login cookie from ever being
   sent over a plain-HTTP fallback.

Without steps 1–2, treat this as LAN-only or Tailscale-only — a login page
alone is not enough to safely expose Garmin health data to the open
internet.

## MCP server: coaching from a Claude chat

`mcp_server.py` is a separate process that lets you (or anyone with a dashboard
account) ask Claude — in any normal Claude chat, from any device — for
training advice grounded in your real live data, and have Claude's answer
show up on the dashboard itself under "Next Waypoint", labeled with how long
ago it was pushed. It exposes two tools:

- **`get_training_snapshot`** — the same data `/api/dashboard` returns, for
  whoever's Claude account is asking.
- **`push_coach_note`** — saves a note that overrides the dashboard's
  auto-generated suggestion for `COACH_NOTE_FRESHNESS_HOURS` (default 48),
  then the rule-based one quietly takes back over.

**Setup** (needs its own public HTTPS hostname, separate from the main site —
e.g. a `mcp.` subdomain through the same Cloudflare Tunnel / reverse proxy
already fronting the dashboard):

```bash
# .env — set these first:
#   MCP_PUBLIC_URL=https://mcp.yourdomain.com
#   MCP_PORT=5052

python mcp_server.py
```

Then, in Claude.ai (web, mobile, or desktop) → Settings → Connectors → Add
custom connector, enter `https://mcp.yourdomain.com/mcp` as the URL. Claude
redirects to a sign-in page served by this same process — sign in with your
Garmin email and password, same as the dashboard, to authorize it (the
account has to exist already; this page doesn't sign anyone up). Each person
who wants this connects it separately, in their own Claude.ai account, with
their own Garmin sign-in — nobody's Claude access is shared with anyone
else's data.

Under the hood this is a real, spec-compliant OAuth 2.1 authorization server
(Dynamic Client Registration, PKCE, rotating refresh tokens) built on the
official `mcp` Python SDK's `OAuthAuthorizationServerProvider` — see
`mcp_oauth_provider.py` and `oauth_store.py` if you're curious how the pieces
fit together. It's a third long-running process alongside `app.py` and
`cloudflared`; keep all three up (systemd or tmux, same as the main app).

## Running on a Raspberry Pi

Nothing here needs native compilation (`garminconnect` and `Flask` are pure
Python + `requests`), and all storage/env paths are relative and
`pathlib`-based, so the same steps above work unchanged on a Pi:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage_users.py init
python manage_users.py add their.garmin@email.com   # or let them use /signup
python app.py
```

Set `HOST=0.0.0.0` in `.env` if you want the dashboard reachable from other
devices on your network, and consider running it under `systemd` or `tmux`
so it survives SSH disconnects. Port-forwarding from the Pi follows the same
"Exposing this outside your LAN" guidance above — Flask itself still stays
bound to localhost behind Caddy, only Caddy's 80/443 get forwarded.

## Notes on the Garmin API

Garmin Connect's API isn't officially documented; the `garminconnect`
package (and this app's `dashboard_data.py`) reverse-engineer field names.
Every metric is fetched defensively — if a field is missing or Garmin
changes its response shape, that one section falls back to "No data"
instead of taking down the whole dashboard. If you notice a panel that's
always empty on your account, check `dashboard_data.py` for the relevant
`dig(...)` path against what your account's raw API response actually
contains.
