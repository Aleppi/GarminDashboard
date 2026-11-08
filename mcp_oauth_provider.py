"""OAuthAuthorizationServerProvider implementation for the dashboard's MCP
server, backed by oauth_store.py. Identity comes from the same Garmin
sign-in the web dashboard uses (garmin_login.py) — see mcp_server.py's
/mcp/login route for the first-party login step that authorize() defers
to (it can only return a redirect URL, not render HTML itself).
"""

from __future__ import annotations

import secrets
import time
from typing import Optional
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import oauth_store

ACCESS_TOKEN_TTL_SECONDS = 3600
AUTH_CODE_TTL_SECONDS = 300


class DashboardOAuthProvider:
    def __init__(self, public_url: str):
        self.public_url = public_url.rstrip("/")

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        data = oauth_store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        oauth_store.save_client(client_info.client_id, client_info.model_dump(mode="json", exclude_none=True))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        continuation_id = secrets.token_urlsafe(24)
        oauth_store.save_pending_authorization(
            continuation_id,
            {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "state": params.state,
                "scopes": params.scopes or [],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
        )
        return f"{self.public_url}/mcp/login?continuation={continuation_id}"

    def complete_authorization(self, continuation_id: str, username: str) -> Optional[str]:
        """Called by the /mcp/login route once the user has authenticated
        and consented. Mints the authorization code the SDK's protocol
        docstring says implementations own, and returns the URL to send
        the browser to next (Claude's redirect_uri, code + state attached)."""
        pending = oauth_store.pop_pending_authorization(continuation_id)
        if pending is None:
            return None

        code = secrets.token_urlsafe(32)
        oauth_store.save_auth_code(
            code=code,
            client_id=pending["client_id"],
            code_challenge=pending["code_challenge"],
            redirect_uri=pending["redirect_uri"],
            redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            scopes=pending["scopes"],
            resource=pending["resource"],
            subject=username,
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
        )
        query = {"code": code}
        if pending["state"]:
            query["state"] = pending["state"]
        return f"{pending['redirect_uri']}?{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        row = oauth_store.get_auth_code(authorization_code)
        if not row or row["client_id"] != client.client_id:
            return None
        if row["expires_at"] < time.time():
            oauth_store.delete_auth_code(authorization_code)
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
            subject=row["subject"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        oauth_store.delete_auth_code(authorization_code.code)
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + ACCESS_TOKEN_TTL_SECONDS
        oauth_store.save_access_token(
            access_token, client.client_id, authorization_code.scopes,
            authorization_code.resource, authorization_code.subject, expires_at,
        )
        oauth_store.save_refresh_token(
            refresh_token, client.client_id, authorization_code.scopes,
            authorization_code.subject, None,
        )
        return OAuthToken(
            access_token=access_token, token_type="Bearer", expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token, scope=" ".join(authorization_code.scopes) or None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        row = oauth_store.get_refresh_token(refresh_token)
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
            expires_at=row["expires_at"], subject=row["subject"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotation is mandatory here: DCR registers Claude as a public client, and
        # the MCP auth spec requires rotating refresh tokens for public clients.
        oauth_store.delete_refresh_token(refresh_token.token)
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + ACCESS_TOKEN_TTL_SECONDS
        granted_scopes = scopes or refresh_token.scopes
        oauth_store.save_access_token(
            new_access, client.client_id, granted_scopes, None, refresh_token.subject, expires_at,
        )
        oauth_store.save_refresh_token(new_refresh, client.client_id, granted_scopes, refresh_token.subject, None)
        return OAuthToken(
            access_token=new_access, token_type="Bearer", expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=new_refresh, scope=" ".join(granted_scopes) or None,
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        row = oauth_store.get_access_token(token)
        if not row:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            oauth_store.delete_access_token(token)
            return None
        return AccessToken(
            token=token, client_id=row["client_id"], scopes=row["scopes"],
            expires_at=row["expires_at"], resource=row["resource"], subject=row["subject"],
        )

    async def revoke_token(self, token) -> None:
        oauth_store.delete_access_token(token.token)
        oauth_store.delete_refresh_token(token.token)
