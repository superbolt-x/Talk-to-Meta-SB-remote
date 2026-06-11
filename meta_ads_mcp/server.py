"""
KonQuest Meta Ads MCP Server.

Supervised Meta Ads Operating System for Claude Code.
Open-core: public tools always available, premium tools require premium bundle.
"""
import logging
import os
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("konquest-meta-ads")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# ── OAuth setup (required for Claude Team remote MCP) ────────────────────────
# SERVER_URL: public Railway base URL, e.g. https://xxx.railway.app  (NO trailing slash, NO /mcp)
# MCP_AUTH_TOKEN: passphrase users enter once in the browser to authorize Claude.

_server_url = os.environ.get("SERVER_URL", "").rstrip("/")
_auth_token  = os.environ.get("MCP_AUTH_TOKEN", "")

if _server_url and _auth_token:
    from urllib.parse import urlparse
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from mcp.server.transport_security import TransportSecuritySettings
    from meta_ads_mcp.oauth import SimpleMCPOAuthProvider

    _oauth_provider = SimpleMCPOAuthProvider(auth_token=_auth_token)

    _auth_settings = AuthSettings(
        # issuer_url = base URL — used to build OAuth endpoint URLs (/authorize, /token, /register)
        issuer_url=_server_url,                         # type: ignore[arg-type]
        # resource_server_url = MCP endpoint URL — used to build /.well-known/oauth-protected-resource/mcp
        # This MUST include /mcp so the WWW-Authenticate header points Claude to the right metadata path.
        resource_server_url=f"{_server_url}/mcp",       # type: ignore[arg-type]
        client_registration_options=ClientRegistrationOptions(
            enabled=True,                               # Claude auto-registers
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
    )

    # Allow the Railway hostname in the MCP SDK's DNS-rebinding protection.
    # Without this, every POST /mcp returns 421 "Invalid Host header".
    _hostname = urlparse(_server_url).netloc  # e.g. talk-to-meta-sb-production.up.railway.app
    _transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_hostname, f"{_hostname}:*"],
        allowed_origins=[_server_url, f"{_server_url}:*"],
    )

    mcp = FastMCP(
        "KonQuest Meta Ads MCP",
        instructions="Supervised Meta Ads Operating System for Claude Code.",
        auth=_auth_settings,
        auth_server_provider=_oauth_provider,
        transport_security=_transport_security,
    )
    logger.info("OAuth enabled — issuer: %s  resource: %s/mcp", _server_url, _server_url)
else:
    _oauth_provider = None
    mcp = FastMCP(
        "KonQuest Meta Ads MCP",
        instructions="Supervised Meta Ads Operating System for Claude Code.",
    )
    if not _server_url:
        logger.warning("SERVER_URL not set — OAuth disabled (Claude Team will not be able to connect)")
    if not _auth_token:
        logger.warning("MCP_AUTH_TOKEN not set — OAuth disabled")

# ============================================================
# PUBLIC TOOLS (open-core, always available)
# ============================================================

# --- Accounts & Auth ---
from meta_ads_mcp.core import accounts  # noqa: E402, F401

# --- Campaign / Ad Set / Ad CRUD ---
from meta_ads_mcp.core import campaigns  # noqa: E402, F401
from meta_ads_mcp.core import adsets  # noqa: E402, F401
from meta_ads_mcp.core import ads  # noqa: E402, F401
from meta_ads_mcp.core import creatives  # noqa: E402, F401

# --- Read Operations ---
from meta_ads_mcp.core import insights  # noqa: E402, F401
from meta_ads_mcp.core import pixels  # noqa: E402, F401
from meta_ads_mcp.core import catalogs  # noqa: E402, F401
from meta_ads_mcp.core import audiences  # noqa: E402, F401
from meta_ads_mcp.core import targeting  # noqa: E402, F401

# --- Assets ---
from meta_ads_mcp.core import images  # noqa: E402, F401
from meta_ads_mcp.core import video  # noqa: E402, F401

# --- Write Operations ---
from meta_ads_mcp.core import naming  # noqa: E402, F401
from meta_ads_mcp.core import ad_builder  # noqa: E402, F401
from meta_ads_mcp.core import ops  # noqa: E402, F401
# vault_reader removed — no marketing-vault dependency in this deployment
from meta_ads_mcp.core import duplication  # noqa: E402, F401

# --- Setup & Readiness ---
from meta_ads_mcp.core import setup  # noqa: E402, F401
# identity.py: active internal helper (imported by ad_builder, ads, ops). Not exposed as MCP tool.

# ============================================================
# PREMIUM TOOLS (available only in premium bundle)
# ============================================================

from meta_ads_mcp.premium import PREMIUM_AVAILABLE

if PREMIUM_AVAILABLE:
    logger.info("Premium bundle detected - loading premium tools")

    # --- Premium Core Modules ---
    from meta_ads_mcp.core import copy_engine  # noqa: E402, F401
    from meta_ads_mcp.core import automation  # noqa: E402, F401
    # vault_bootstrap removed — no marketing-vault dependency in this deployment

    # --- Premium Engine: Decision & Optimization ---
    def _register_engine_tools():
        from meta_ads_mcp.engine.loop import run_optimization_cycle
        from meta_ads_mcp.engine.planner import create_launch_plan
        from meta_ads_mcp.engine.executor import build_execution_pack, execute_paused_launch
        from meta_ads_mcp.engine.mutations import build_mutation_pack, execute_mutation_pack
        from meta_ads_mcp.engine.activation import (
            build_activation_pack, execute_activation_pack,
            build_rollback_pack, execute_rollback_pack,
        )
        mcp.tool()(run_optimization_cycle)
        mcp.tool()(create_launch_plan)
        mcp.tool()(build_execution_pack)
        mcp.tool()(execute_paused_launch)
        mcp.tool()(build_mutation_pack)
        mcp.tool()(execute_mutation_pack)
        mcp.tool()(build_activation_pack)
        mcp.tool()(execute_activation_pack)
        mcp.tool()(build_rollback_pack)
        mcp.tool()(execute_rollback_pack)
        # Review Queue + Snapshots
        from meta_ads_mcp.engine.review import (
            build_review_queue, list_review_queue,
            resolve_review_item, record_outcome_snapshot,
            expire_stale_queue_items, build_operator_digest,
            run_scheduled_review_cycle,
        )
        mcp.tool()(build_review_queue)
        mcp.tool()(list_review_queue)
        mcp.tool()(resolve_review_item)
        mcp.tool()(record_outcome_snapshot)
        mcp.tool()(expire_stale_queue_items)
        mcp.tool()(build_operator_digest)
        mcp.tool()(run_scheduled_review_cycle)
        # Learning Layer + Policy Engine
        from meta_ads_mcp.engine.learning import (
            evaluate_execution_outcome, update_policy_memory,
            get_policy_memory, build_learning_digest, run_learning_cycle,
        )
        mcp.tool()(evaluate_execution_outcome)
        mcp.tool()(update_policy_memory)
        mcp.tool()(get_policy_memory)
        mcp.tool()(build_learning_digest)
        mcp.tool()(run_learning_cycle)
        # Experimentation + Budget Governor + Creative Rotation
        from meta_ads_mcp.engine.experiments import (
            build_experiment_plan, evaluate_experiment,
            rotate_creative_set, run_budget_governor,
            promote_experiment_winner, get_experiment_registry,
            run_scaling_cycle,
        )
        mcp.tool()(build_experiment_plan)
        mcp.tool()(evaluate_experiment)
        mcp.tool()(rotate_creative_set)
        mcp.tool()(run_budget_governor)
        mcp.tool()(promote_experiment_winner)
        mcp.tool()(get_experiment_registry)
        mcp.tool()(run_scaling_cycle)
        # Concept Selection + Copy Chain
        from meta_ads_mcp.engine.concepts import select_concepts
        from meta_ads_mcp.engine.copy_chain import generate_copy_package, validate_copy_output
        mcp.tool()(select_concepts)

        def generate_ad_copy_chain(
            account_id: str,
            concept_json: str,
            transcript_excerpt: str = None,
        ) -> dict:
            """
            Generate vault-grounded ad copy for a selected concept.

            Full chain: vault -> normalized data -> copy brief -> generation instructions.

            Args:
                account_id: Ad account ID.
                concept_json: JSON of selected concept from select_concepts.
                transcript_excerpt: Optional SRT text for creative alignment.
            """
            import json as _json
            from meta_ads_mcp.core.vault_reader import enforce_vault_gate
            from meta_ads_mcp.core.utils import ensure_account_id_format

            account_id = ensure_account_id_format(account_id)
            vault_error, vault_ctx = enforce_vault_gate(account_id, "create_ad")
            if vault_error:
                return vault_error

            try:
                concept = _json.loads(concept_json)
            except:
                return {"error": "Malformed concept_json"}

            return generate_copy_package(vault_ctx, concept, transcript_excerpt)

        mcp.tool()(generate_ad_copy_chain)

        # Auto Copy Generation
        from meta_ads_mcp.engine.copy_generator import auto_generate_for_write

        def generate_auto_copy(
            account_id: str,
            angle_name: str,
            icp_name: str = "",
            funnel_stage: str = "tofu",
            copy_mode: str = "auto",
            existing_primary_text: str = None,
            existing_headline: str = None,
            existing_description: str = None,
            transcript_excerpt: str = None,
        ) -> dict:
            """
            Generate vault-grounded ad copy automatically.

            Assembles primary_text, headline, description from vault intelligence.
            Validates for language integrity, forbidden words, generic content.

            Args:
                account_id: Ad account ID.
                angle_name: Marketing angle.
                icp_name: Target ICP.
                funnel_stage: 'tofu', 'mofu', 'bofu'.
                copy_mode: 'auto', 'manual', 'hybrid'.
                existing_primary_text: For manual/hybrid modes.
                existing_headline: For manual/hybrid modes.
                existing_description: For manual/hybrid modes.
                transcript_excerpt: Optional SRT text for creative alignment.
            """
            return auto_generate_for_write(
                account_id=account_id,
                angle_name=angle_name,
                icp_name=icp_name,
                funnel_stage=funnel_stage,
                existing_primary_text=existing_primary_text,
                existing_headline=existing_headline,
                existing_description=existing_description,
                copy_mode=copy_mode,
                transcript_excerpt=transcript_excerpt,
            )

        mcp.tool()(generate_auto_copy)

    _register_engine_tools()
else:
    logger.info("Open-core mode - %s public tools loaded. Premium tools not available.", "55")


def main():
    """Run the MCP server.

    Transport is controlled by the MCP_TRANSPORT env var:
      - streamable-http (default) — modern HTTP, recommended for remote deployments
      - sse                       — legacy Server-Sent Events HTTP transport
      - stdio                     — local stdio transport for Claude Desktop / CLI

    HTTP-specific env vars (ignored for stdio):
      MCP_HOST — bind address (default: 0.0.0.0)
      MCP_PORT — bind port    (default: 8000)

    Note: auth is handled at the network level (keep the Railway URL internal).
    Custom Bearer middleware is intentionally omitted — Claude Team's connector
    performs OAuth discovery and cannot use a simple bearer gate.
    """
    import os
    import uvicorn

    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    mode = "premium (98 tools)" if PREMIUM_AVAILABLE else "open-core (55 tools)"
    logger.info("Starting KonQuest Meta Ads MCP server v%s [%s] transport=%s", "2.0.0", mode, transport)

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    # Railway (and most PaaS) inject PORT; MCP_PORT is a fallback for local/Docker use
    port = int(os.environ.get("PORT") or os.environ.get("MCP_PORT", "8000"))

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
    from starlette.routing import Mount, Route

    import json as _json
    from contextlib import asynccontextmanager

    _raw_mcp_app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()

    # ── Grant-type normalisation middleware ───────────────────────────────────
    # Claude Team sends grant_types=["authorization_code"] without "refresh_token".
    # The MCP SDK's /register handler requires BOTH; this ASGI wrapper silently
    # adds "refresh_token" before the SDK ever sees the request body.

    class _GrantTypeFixMiddleware:
        """Patch POST /register to include refresh_token in grant_types."""

        def __init__(self, app):
            self._app = app

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("path") == "/register":
                # Buffer the full request body
                chunks: list[bytes] = []
                more = True
                while more:
                    msg = await receive()
                    chunks.append(msg.get("body", b""))
                    more = msg.get("more_body", False)
                body = b"".join(chunks)

                # Normalise grant_types
                try:
                    data = _json.loads(body)
                    gt = set(data.get("grant_types") or [])
                    if "authorization_code" in gt and "refresh_token" not in gt:
                        data["grant_types"] = sorted(gt | {"refresh_token"})
                        body = _json.dumps(data).encode()
                        logger.debug(
                            "register: added refresh_token to grant_types → %s",
                            data["grant_types"],
                        )
                except Exception:
                    pass  # leave body unchanged if not valid JSON

                # Replay the (possibly modified) body as a single receive call
                _body_sent = False

                async def _patched_receive():
                    nonlocal _body_sent
                    if not _body_sent:
                        _body_sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return {"type": "http.disconnect"}

                await self._app(scope, _patched_receive, send)
            else:
                await self._app(scope, receive, send)

    mcp_app = _GrantTypeFixMiddleware(_raw_mcp_app)

    # ── Lifespan: run the MCP session manager task group ─────────────────────
    # The StreamableHTTPSessionManager must be started via its own lifespan
    # before it can handle requests. We extract it from the inner Starlette app
    # and attach it to the outer app so uvicorn triggers it on startup.
    @asynccontextmanager
    async def lifespan(_app):
        async with _raw_mcp_app.router.lifespan_context(_app):
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "transport": transport, "oauth": bool(_oauth_provider)})

    async def oauth_approve(request: Request) -> HTMLResponse | RedirectResponse:
        """Passphrase approval page — the human-facing step of the OAuth flow."""
        if _oauth_provider is None:
            return HTMLResponse("OAuth not configured.", status_code=503)

        pending_id = request.query_params.get("pending_id", "")

        if request.method == "GET":
            return HTMLResponse(_oauth_provider.render_approve_form(pending_id))

        # POST — check passphrase
        form = await request.form()
        passphrase  = str(form.get("passphrase", ""))
        pending_id  = str(form.get("pending_id", pending_id))
        ok, redirect_url, error = _oauth_provider.handle_approval(pending_id, passphrase)

        if ok and redirect_url:
            return RedirectResponse(redirect_url, status_code=302)

        # Wrong passphrase — re-render form with error
        return HTMLResponse(
            _oauth_provider.render_approve_form(pending_id, error or "Authorization failed."),
            status_code=400,
        )

    routes = [
        Route("/health", health),
        Route("/oauth/approve", oauth_approve, methods=["GET", "POST"]),
        Mount("/", app=mcp_app),
    ]

    app = Starlette(lifespan=lifespan, routes=routes)
    logger.info("Listening on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
