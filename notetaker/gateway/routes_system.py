"""GET /healthz and the Notion OAuth helper endpoints used by the extension."""
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from notetaker.domain.errors import ErrorCode
from notetaker.gateway.errors import ApiError
from notetaker.gateway.schemas import OAuthConfigResponse, OAuthTokenRequest, OAuthTokenResponse

log = logging.getLogger(__name__)
router = APIRouter()

NOTION_AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"


@router.get("/healthz")
async def healthz(request: Request):
    """Confirms the gateway, the state store (Redis) and the broker (RabbitMQ) are reachable."""
    state = request.app.state
    checks: dict[str, str] = {}
    queues: dict[str, int] = {}
    try:
        await state.store.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    try:
        queues = await state.broker.queue_depths()
        checks["broker"] = "ok"
    except Exception as exc:
        checks["broker"] = f"error: {type(exc).__name__}"
    healthy = all(v == "ok" for v in checks.values())
    body = {"status": "ok" if healthy else "degraded", "checks": {"gateway": "ok", **checks}, "queues": queues}
    return JSONResponse(body, status_code=200 if healthy else 503)


@router.get("/oauth/notion/config", response_model=OAuthConfigResponse)
async def oauth_config(request: Request):
    settings = request.app.state.settings
    return OAuthConfigResponse(
        enabled=settings.notion_oauth_enabled,
        client_id=settings.notion_client_id or None,
        authorize_url=NOTION_AUTHORIZE_URL,
    )


@router.post("/oauth/notion/token", response_model=OAuthTokenResponse)
async def oauth_token(body: OAuthTokenRequest, request: Request):
    """Exchange an OAuth code for a token. The client secret stays on the server; the token is
    returned to the extension and not stored here."""
    settings = request.app.state.settings
    if not settings.notion_oauth_enabled:
        raise ApiError(501, ErrorCode.OAUTH_NOT_CONFIGURED, "Notion OAuth is not configured on this server.")
    try:
        response = await request.app.state.http.post(
            f"{settings.notion_api_base.rstrip('/')}/oauth/token",
            auth=(settings.notion_client_id, settings.notion_client_secret),
            json={"grant_type": "authorization_code", "code": body.code, "redirect_uri": body.redirect_uri},
            headers={"Notion-Version": settings.notion_version},
        )
    except httpx.HTTPError as exc:
        raise ApiError(502, ErrorCode.OAUTH_EXCHANGE_FAILED, "Could not reach Notion.") from exc
    if not response.is_success:
        detail = response.json().get("error_description") if "json" in response.headers.get("content-type", "") else None
        log.warning("notion oauth exchange failed: %s %s", response.status_code, detail)
        raise ApiError(400, ErrorCode.OAUTH_EXCHANGE_FAILED, detail or "Notion rejected the authorization code.")
    data = response.json()
    return OAuthTokenResponse(
        access_token=data["access_token"],
        workspace_id=data.get("workspace_id"),
        workspace_name=data.get("workspace_name"),
        workspace_icon=data.get("workspace_icon"),
        bot_id=data.get("bot_id"),
    )
