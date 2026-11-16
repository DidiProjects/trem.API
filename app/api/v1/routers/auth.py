from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.core.config import get_settings
from app.core.exceptions import AppException, RateLimitedError
from app.core.security import jwks
from app.domain.entities.user import User
from app.domain.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
)
from app.domain.schemas.common import MessageResponse
from app.infrastructure.database.connection import get_db
from app.services.auth_service import AuthService
from app.utils.client_ip import client_ip

router = APIRouter(prefix="/auth", tags=["Auth"])
well_known_router = APIRouter(prefix="/.well-known", tags=["Auth"])

# A cross-site form cannot set custom headers, and a cross-origin fetch that does is
# preflighted and refused. With SameSite=Strict on the cookie, this closes CSRF on the
# endpoints that act on the session cookie.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "trem"


def http_error(e: AppException) -> HTTPException:
    headers = None
    if isinstance(e, RateLimitedError):
        headers = {"Retry-After": str(e.retry_after)}
    return HTTPException(status_code=e.status_code, detail=e.message, headers=headers)


def _set_session_cookie(response: Response, raw_refresh: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=raw_refresh,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/auth",
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        path="/auth",
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def _refresh_from(request: Request, body: Optional[RefreshRequest]) -> tuple[str, bool]:
    """(raw refresh token, came from the cookie)."""
    if body and body.refresh_token:
        return body.refresh_token, False
    raw = request.cookies.get(get_settings().REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token não fornecido")
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Header {CSRF_HEADER} ausente")
    return raw, True


@router.post("/login", response_model=TokenResponse, response_model_exclude_none=True)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    try:
        tokens, raw_refresh = await AuthService(db).login(
            username=body.username,
            password=body.password,
            audience=body.audience,
            ip_address=client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
    except AppException as e:
        raise http_error(e)

    if body.session == "cookie":
        _set_session_cookie(response, raw_refresh)
    else:
        tokens.refresh_token = raw_refresh
    return tokens


@router.post("/refresh", response_model=TokenResponse, response_model_exclude_none=True)
async def refresh(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = Body(default=None),
    db: AsyncSession = Depends(get_db),
):
    raw, from_cookie = _refresh_from(request, body)
    try:
        tokens, raw_refresh = await AuthService(db).refresh(
            raw,
            ip_address=client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
    except AppException as e:
        error = http_error(e)
        if from_cookie:
            error.headers = {**(error.headers or {}), "set-cookie": _expired_cookie_header()}
        raise error

    if from_cookie:
        _set_session_cookie(response, raw_refresh)
    else:
        tokens.refresh_token = raw_refresh
    return tokens


def _expired_cookie_header() -> str:
    probe = Response()
    _clear_session_cookie(probe)
    return probe.headers["set-cookie"]


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),  # não usa get_current_active_user — é o endpoint de troca
    db: AsyncSession = Depends(get_db),
):
    service = AuthService(db)
    try:
        await service.change_password(
            user_id=current_user.id,
            current_password=body.current_password,
            new_password=body.new_password,
        )
        return MessageResponse(message="Senha alterada com sucesso")
    except AppException as e:
        raise http_error(e)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = Body(default=None),
    db: AsyncSession = Depends(get_db),
):
    raw, from_cookie = _refresh_from(request, body)
    await AuthService(db).logout(raw)
    if from_cookie:
        _clear_session_cookie(response)
    return MessageResponse(message="Logout realizado com sucesso")


@well_known_router.get("/jwks.json")
async def get_jwks():
    """Public keys that sign the access tokens, for other applications to verify them locally."""
    return JSONResponse(jwks(), headers={"Cache-Control": "public, max-age=3600"})
