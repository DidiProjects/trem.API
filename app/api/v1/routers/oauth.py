import base64
import binascii
from typing import Optional
from urllib.parse import unquote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_active_user
from app.core.config import get_settings
from app.domain.entities.user import User
from app.domain.schemas.oidc import OidcAuthorizationView, OidcRedirect
from app.infrastructure.database.connection import get_db
from app.services.hub_service import logo_path
from app.services.oidc_provider_service import (
    AuthorizationRequest,
    GrantNotFound,
    InvalidClientRequest,
    NotAllowed,
    OAuthError,
    OidcProviderService,
    TokenRequest,
    discovery,
)

router = APIRouter(prefix="/oauth", tags=["OIDC"])
well_known_router = APIRouter(prefix="/.well-known", tags=["OIDC"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _oauth_error(e: OAuthError, challenge: Optional[str] = None) -> JSONResponse:
    # Returned, not raised: a raised error rolls the session back, and a code spent by a
    # failed exchange must stay spent.
    headers = dict(_NO_STORE)
    if challenge:
        headers["WWW-Authenticate"] = challenge
    return JSONResponse({"error": e.error, "error_description": e.description}, e.status_code, headers=headers)


def _grant_error(e: Exception) -> HTTPException:
    if isinstance(e, NotAllowed):
        return HTTPException(status.HTTP_403_FORBIDDEN, "Sua conta não tem acesso a este serviço")
    return HTTPException(status.HTTP_404_NOT_FOUND, "Pedido de login expirado ou já usado: entre de novo pelo serviço")


@well_known_router.get("/openid-configuration")
async def openid_configuration():
    """OpenID Connect discovery for the hub services' login."""
    return JSONResponse(discovery(), headers={"Cache-Control": "public, max-age=3600"})


@router.get("/authorize", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
async def authorize(
    client_id: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    response_type: Optional[str] = None,
    scope: Optional[str] = None,
    state: Optional[str] = None,
    nonce: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Opened by the service in the browser: stores the request and goes to trem.FRONT's login screen."""
    request = AuthorizationRequest(
        client_id, redirect_uri, response_type, scope, state, nonce, code_challenge, code_challenge_method
    )
    try:
        target = await OidcProviderService(db).authorize(request)
    except InvalidClientRequest as e:
        # Never back to an address that is not registered: the front explains instead.
        target = f"{get_settings().frontend_url()}/sso?{urlencode({'error': e.error})}"
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND, headers=_NO_STORE)


@router.get("/authorize/{request_id}", response_model=OidcAuthorizationView)
async def authorization_view(
    request_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db, scope="function")
):
    """The pending login for trem.FRONT's screen: which service, and whether this account may enter."""
    try:
        grant, allowed = await OidcProviderService(db).view(request_id, user)
    except GrantNotFound as e:
        raise _grant_error(e)
    service = grant.client.service
    return OidcAuthorizationView(
        service_slug=service.slug, service_name=service.name, logo_url=logo_path(service),
        username=user.username, allowed=allowed,
    )


@router.post("/authorize/{request_id}/approve", response_model=OidcRedirect)
async def approve(
    request_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db, scope="function")
):
    """Logs this account into the service: the front sends the browser to the address returned."""
    try:
        return OidcRedirect(redirect=await OidcProviderService(db).approve(request_id, user))
    except (GrantNotFound, NotAllowed) as e:
        raise _grant_error(e)


@router.post("/authorize/{request_id}/deny", response_model=OidcRedirect)
async def deny(
    request_id: str, _: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db, scope="function")
):
    """Cancels the login: the service is told access_denied."""
    try:
        return OidcRedirect(redirect=await OidcProviderService(db).deny(request_id))
    except GrantNotFound as e:
        raise _grant_error(e)


def _basic_credentials(request: Request) -> tuple[Optional[str], Optional[str]]:
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None, None
    try:
        decoded = base64.b64decode(value, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None, None
    client_id, sep, secret = decoded.partition(":")
    if not sep:
        return None, None
    # RFC 6749 §2.3.1: both parts are form-urlencoded before the Basic encoding.
    return unquote(client_id.replace("+", " ")), unquote(secret.replace("+", " "))


@router.post("/token")
async def token(
    request: Request,
    grant_type: Optional[str] = Form(None),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Called by the service's server: trades the code (with the PKCE verifier) for an id_token and an access token."""
    basic_id, basic_secret = _basic_credentials(request)
    used_basic = basic_id is not None
    if used_basic and client_id and client_id != basic_id:
        return _oauth_error(OAuthError("invalid_request", "client_id differs from the Basic credentials"))
    provider = OidcProviderService(db)
    try:
        client = await provider.authenticate_client(
            basic_id if used_basic else client_id, basic_secret if used_basic else client_secret
        )
        tokens = await provider.exchange(client, TokenRequest(grant_type, code, redirect_uri, code_verifier))
    except OAuthError as e:
        challenge = 'Basic realm="trem"' if e.error == "invalid_client" and used_basic else None
        return _oauth_error(e, challenge)
    return JSONResponse(tokens, headers=_NO_STORE)


async def _userinfo(request: Request, db: AsyncSession) -> JSONResponse:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    try:
        found = await OidcProviderService(db).userinfo(value if scheme.lower() == "bearer" else None)
    except OAuthError as e:
        return _oauth_error(e, f'Bearer error="{e.error}"')
    return JSONResponse(found, headers=_NO_STORE)


@router.get("/userinfo")
async def userinfo(request: Request, db: AsyncSession = Depends(get_db, scope="function")):
    """Claims of the account, for an access token issued by /oauth/token."""
    return await _userinfo(request, db)


@router.post("/userinfo")
async def userinfo_post(request: Request, db: AsyncSession = Depends(get_db, scope="function")):
    return await _userinfo(request, db)
