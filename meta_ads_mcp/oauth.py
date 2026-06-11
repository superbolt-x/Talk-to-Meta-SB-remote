"""
Minimal OAuth 2.0 Authorization Server for Claude Team integration.

Flow:
  1. Claude Team discovers /.well-known/oauth-authorization-server
  2. Claude registers as a client (dynamic client registration)
  3. Claude redirects the user to /authorize
  4. User is forwarded to /oauth/approve — a passphrase form
  5. User enters MCP_AUTH_TOKEN → server redirects back with auth code
  6. Claude exchanges the code for an access token at /token
  7. Subsequent MCP calls carry the access token

Set MCP_AUTH_TOKEN in Railway env vars.
Set SERVER_URL to your public Railway URL (e.g. https://xxx.railway.app).
"""
import logging
import secrets
import time
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger("konquest-meta-ads.oauth")

# ── HTML templates ────────────────────────────────────────────────────────────

_APPROVE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Talk-to-Meta — Authorize</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #f5f5f5; display: flex; align-items: center;
            justify-content: center; min-height: 100vh; }}
    .card {{ background: #fff; border-radius: 12px; padding: 40px;
             box-shadow: 0 4px 24px rgba(0,0,0,.08); max-width: 420px; width: 100%; }}
    h1 {{ font-size: 1.4rem; color: #111; margin-bottom: 8px; }}
    p  {{ font-size: .9rem; color: #555; margin-bottom: 24px; line-height: 1.5; }}
    label {{ font-size: .85rem; font-weight: 600; color: #333; display: block; margin-bottom: 6px; }}
    input {{ width: 100%; padding: 10px 14px; border: 1px solid #ddd; border-radius: 8px;
             font-size: 1rem; margin-bottom: 16px; }}
    input:focus {{ outline: none; border-color: #0066ff; box-shadow: 0 0 0 3px rgba(0,102,255,.12); }}
    button {{ width: 100%; padding: 12px; background: #0066ff; color: #fff;
              border: none; border-radius: 8px; font-size: 1rem;
              font-weight: 600; cursor: pointer; }}
    button:hover {{ background: #0052cc; }}
    .error {{ color: #cc0000; font-size: .85rem; margin-bottom: 14px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🔌 Talk-to-Meta</h1>
    <p>Enter the access passphrase to connect Claude to the Meta Ads MCP server.</p>
    {error}
    <form method="post">
      <label for="passphrase">Passphrase</label>
      <input id="passphrase" name="passphrase" type="password"
             placeholder="Enter passphrase" autocomplete="current-password" required>
      <input type="hidden" name="pending_id" value="{pending_id}">
      <button type="submit">Authorize</button>
    </form>
  </div>
</body>
</html>"""


class SimpleMCPOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """
    In-memory OAuth provider.  All state is lost on server restart — users
    will just need to re-authorize, which is acceptable for a team tool.
    """

    def __init__(self, auth_token: str) -> None:
        self._auth_token = auth_token
        self._clients: dict[str, OAuthClientInformationFull] = {}
        # pending_id -> (AuthorizationCode object, redirect_uri, state)
        self._pending: dict[str, tuple[AuthorizationCode, str, str | None]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

    # ── Client registry ───────────────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        logger.info("OAuth client registered: %s", client_info.client_id)

    # ── Authorization ─────────────────────────────────────────────────────────

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """
        Called by the SDK after validating the /authorize request.
        We stash the pending params and return a URL to our passphrase page.
        """
        pending_id = secrets.token_urlsafe(24)
        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 600,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self._pending[pending_id] = (auth_code, str(params.redirect_uri), params.state)
        return f"/oauth/approve?pending_id={pending_id}"

    # ── Code exchange ─────────────────────────────────────────────────────────

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code and code.expires_at > time.time() and code.client_id == client.client_id:
            return code
        return None

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=None,
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
        )
        logger.info("Token issued for client: %s", client.client_id)
        return OAuthToken(access_token=access, token_type="Bearer", refresh_token=refresh)

    # ── Refresh tokens ────────────────────────────────────────────────────────

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        rt = self._refresh_tokens.get(refresh_token)
        return rt if (rt and rt.client_id == client.client_id) else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        new_access = secrets.token_urlsafe(32)
        self._access_tokens[new_access] = AccessToken(
            token=new_access,
            client_id=client.client_id,
            scopes=refresh_token.scopes,
            expires_at=None,
        )
        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",
            refresh_token=refresh_token.token,
        )

    # ── Access token verification ─────────────────────────────────────────────

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self._access_tokens.get(token)

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
        token_hint_type: str | None = None,
    ) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        else:
            self._refresh_tokens.pop(token.token, None)

    # ── Approval page helpers ─────────────────────────────────────────────────

    def render_approve_form(self, pending_id: str, error: str = "") -> str:
        error_html = f'<p class="error">{error}</p>' if error else ""
        return _APPROVE_HTML.format(pending_id=pending_id, error=error_html)

    def handle_approval(
        self, pending_id: str, passphrase: str
    ) -> tuple[bool, str | None, str | None]:
        """
        Returns (ok, redirect_url, error_message).
        """
        entry = self._pending.get(pending_id)
        if not entry:
            return False, None, "Session expired. Please try again."

        auth_code, redirect_uri, state = entry

        if passphrase != self._auth_token:
            return False, None, "Incorrect passphrase."

        # Commit the code
        self._codes[auth_code.code] = auth_code
        del self._pending[pending_id]

        params: dict[str, str] = {"code": auth_code.code}
        if state:
            params["state"] = state
        redirect_url = construct_redirect_uri(redirect_uri, **params)
        return True, redirect_url, None
