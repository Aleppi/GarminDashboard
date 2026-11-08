"""Registers the vendored Garmin Connect tools (see garmin_tools/) onto this
project's own MCP server, so a chat with only the TrainState connector can
reach raw Garmin data — not just the curated dashboard payload.

The one real problem this module exists to solve is IDENTITY. Upstream's
tool modules each hold a module-level `garmin_client` global, set once at
startup by `configure(client)`, because upstream is a single-user server:
one process, one Garmin account, credentials from env vars.

This server is multi-user. Identity arrives per-request on the MCP OAuth
token, and each dashboard user has their own Garmin session from
garmin_client.get_client(username). Pointing that module global at "the
current user" would be a cross-user data leak the moment two people's
requests overlap: the tool bodies read the global at call time, so whoever
wrote to it last wins for everyone in flight.

So the global is set ONCE, to a proxy that resolves per-request through a
ContextVar. Each async request gets its own context, so concurrent calls
from different users each see their own client, with no lock and no
serialization — and the ~150 vendored tool bodies stay untouched, still
just calling `garmin_client.get_whatever(...)`.

Blocking-call note: upstream's tools call garminconnect synchronously
inside `async def`, which stalls the event loop for the duration. That's
inherited as-is (the dashboard's own Garmin calls behave the same way);
it's a throughput characteristic, not a correctness bug.
"""

from __future__ import annotations

import contextvars
import functools
import importlib
import logging
import os
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# The Garmin client for the request currently being served. Set by the
# wrapper in _bind_identity; read (indirectly, via _ClientProxy) by every
# vendored tool body.
_current_client: contextvars.ContextVar = contextvars.ContextVar("garmin_tools_current_client")


class _ClientProxy:
    """Stands in for a real garminconnect.Garmin in the vendored modules'
    `garmin_client` global, forwarding every attribute to the client bound
    to the current request's context."""

    def __getattr__(self, name: str) -> Any:
        try:
            client = _current_client.get()
        except LookupError as exc:  # pragma: no cover - defensive
            raise RuntimeError(
                "No Garmin client bound for this request — a vendored Garmin "
                "tool was called outside the identity wrapper."
            ) from exc
        return getattr(client, name)


_PROXY = _ClientProxy()

# Every vendored module that exposes register_tools()/configure(). Order is
# irrelevant; each is independent.
VENDORED_MODULES = (
    "activity_management",
    "health_wellness",
    "training",
    "user_profile",
    "devices",
    "gear_management",
    "weight_management",
    "challenges",
    "workouts",
    "data_management",
    "womens_health",
    "nutrition",
    "workout_builders",
    "courses",
    "activity_analysis",
    "calendar_events",
)

# Registered by default. Upstream ships ~148 tools, whose JSON schemas come
# to roughly 142KB — that would sit in the context of every conversation
# using this connector, alongside the dashboard's own tools. This is the
# read-oriented subset that covers the actual training/health data, at
# roughly a quarter of that cost. Anything omitted here is still available:
# see GARMIN_TOOLS_MODE / GARMIN_EXTRA_TOOLS below.
CURATED_TOOLS = frozenset(
    {
        # --- activities ---
        "get_activities",
        "get_activities_by_date",
        "get_activity",
        "get_activity_splits",
        "get_activity_weather",
        "get_activity_hr_in_timezones",
        "get_activity_exercise_sets",
        "get_activity_types",
        # --- health & wellness ---
        "get_stats",
        "get_body_composition",
        "get_steps_data",
        "get_training_readiness",
        "get_body_battery",
        "get_rhr_day",
        "get_heart_rates",
        "get_sleep_data",
        "get_sleep_summary",
        "get_stress_data",
        "get_respiration_data",
        "get_spo2_data",
        "get_weekly_steps",
        "get_hydration_data",
        # --- training & performance ---
        "get_hill_score",
        "get_endurance_score",
        "get_training_effect",
        "get_hrv_data",
        "get_fitnessage_data",
        "get_training_status",
        "get_cycling_ftp",
        "get_lactate_threshold",
        "get_training_load_trend",
        "get_training_load_balance",
        "get_vo2max_trend",
        "get_running_tolerance",
        # --- profile, devices, gear, records ---
        "get_user_profile",
        "get_heart_rate_zones",
        "get_devices",
        "get_device_last_used",
        "get_gear",
        "get_weigh_ins",
        "get_personal_record",
        "get_race_predictions",
    }
)


def _parse_name_set(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    return {name.strip() for name in value.split(",") if name.strip()}


def _selected_tool_names() -> Optional[frozenset]:
    """Which vendored tools to register.

    GARMIN_TOOLS_MODE:
      "curated" (default) — the CURATED_TOOLS set above.
      "all"               — every vendored tool (~148; the full ~142KB).
      "off"               — register none; only the dashboard's own tools.
    GARMIN_EXTRA_TOOLS    — comma-separated names to add on top of curated,
                            for pulling in a specific extra tool without
                            paying for all of them.

    Returns None to mean "no filter, register everything".
    """
    mode = os.getenv("GARMIN_TOOLS_MODE", "curated").strip().lower()
    if mode == "all":
        return None
    if mode == "off":
        return frozenset()
    if mode != "curated":
        logger.warning("Unknown GARMIN_TOOLS_MODE %r — falling back to 'curated'.", mode)
    return CURATED_TOOLS | _parse_name_set(os.getenv("GARMIN_EXTRA_TOOLS"))


class _FilteringRegistrar:
    """Quacks like the MCPServer the vendored modules expect, so their
    unmodified `register_tools(app)` calls land here first. Each tool gets
    wrapped with identity binding, and filtered-out tools are simply never
    registered (rather than registered and then rejected)."""

    def __init__(self, server: Any, allowed: Optional[frozenset], username_getter: Callable[[], str]):
        self._server = server
        self._allowed = allowed
        self._username_getter = username_getter
        self.registered: list[str] = []
        self.skipped: list[str] = []

    def tool(self, *dec_args: Any, **dec_kwargs: Any):
        def decorator(fn: Callable) -> Callable:
            name = dec_kwargs.get("name") or fn.__name__
            if self._allowed is not None and name not in self._allowed:
                self.skipped.append(name)
                return fn  # not registered; module keeps its own reference
            wrapped = _bind_identity(fn, self._username_getter)
            self._server.tool(*dec_args, **dec_kwargs)(wrapped)
            self.registered.append(name)
            return fn

        return decorator


def _bind_identity(fn: Callable, username_getter: Callable[[], str]) -> Callable:
    """Resolve the caller's Garmin client and bind it to this request's
    context before running the tool. functools.wraps keeps __name__/__doc__
    and sets __wrapped__, so the MCP schema generator still reads the
    original signature and docstring off the wrapper."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any):
        import garmin_client  # local import: keeps this module importable standalone

        try:
            username = username_getter()
            client = garmin_client.get_client(username)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            # Tools here return strings, so an auth failure should read as a
            # normal (recoverable) result rather than a transport-level error.
            return f"Could not reach Garmin for this account: {exc}"

        token = _current_client.set(client)
        try:
            return await fn(*args, **kwargs)
        finally:
            _current_client.reset(token)

    return wrapper


def register_garmin_tools(server: Any, username_getter: Callable[[], str]) -> dict:
    """Point the vendored modules at the request-scoped proxy and register
    their tools onto `server`. `username_getter` is called per tool
    invocation and must return the authenticated dashboard username.

    A module that fails to import or register is logged and skipped rather
    than taking down the whole MCP server — some carry optional third-party
    dependencies (e.g. activity_analysis needs `fitparse`, which isn't
    installed here) and are not worth a hard failure when nothing in the
    curated set uses them.
    """
    allowed = _selected_tool_names()
    if allowed is not None and not allowed:
        logger.info("GARMIN_TOOLS_MODE=off — no vendored Garmin tools registered.")
        return {"registered": [], "skipped": [], "failed_modules": []}

    registrar = _FilteringRegistrar(server, allowed, username_getter)
    failed: list[tuple[str, str]] = []

    for module_name in VENDORED_MODULES:
        try:
            module = importlib.import_module(f"garmin_tools.{module_name}")
            module.configure(_PROXY)  # the global is the proxy, set once, never rebound
            module.register_tools(registrar)
        except Exception as exc:  # noqa: BLE001 - see docstring
            failed.append((module_name, repr(exc)))
            logger.warning("Skipping vendored Garmin module %s: %s", module_name, exc)

    logger.info(
        "Vendored Garmin tools: %d registered, %d filtered out, %d modules skipped.",
        len(registrar.registered),
        len(registrar.skipped),
        len(failed),
    )
    return {
        "registered": sorted(registrar.registered),
        "skipped": sorted(registrar.skipped),
        "failed_modules": failed,
    }
