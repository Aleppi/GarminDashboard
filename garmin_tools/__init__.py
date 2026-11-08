"""Vendored Garmin Connect tool modules from Taxuspt/garmin_mcp (MIT —
see LICENSE.upstream), pinned at commit e8554bcd, 2026-09-01.

Only the per-domain tool modules are vendored, NOT upstream's own
__init__.py/server entrypoint. Two reasons that file is deliberately left
behind:

  1. It pins `mcp>=1.28.1,<2` and imports `mcp.server.fastmcp.FastMCP`,
     which no longer exists in the mcp 2.x this project runs (renamed to
     mcp.server.mcpserver.MCPServer). Pip-installing the upstream package
     would try to downgrade mcp and break mcp_server.py.
  2. Its auth is single-user by design (GARMIN_EMAIL/GARMIN_PASSWORD env
     vars plus a ~/.garminconnect tokenstore). This project is multi-user:
     identity comes per-request from the MCP OAuth token, and clients come
     from garmin_client.get_client(username).

The tool modules themselves import no mcp at all — they just take an `app`
and call `app.tool()` — so they register onto our MCPServer unchanged.
garmin_tools_bridge.py does that registration and supplies the per-request
client. The only edit made to vendored code is one import in
workout_builders.py repointed from `garmin_mcp.` to `garmin_tools.`.
"""
