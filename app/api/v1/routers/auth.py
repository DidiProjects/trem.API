from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.core.exceptions import (
    AppException,
    InvalidCredentialsError,
    UserBlockedError,
)
from app.domain.entities.user import User
from app.domain.schemas.auth import (
    AccessTokenResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
)
from app.domain.schemas.common import MessageResponse
from app.infrastructure.database.connection import get_db
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Auth"])


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    service = AuthService(db)
    try:
        return await service.login(
            username=body.username,
            password=body.password,
            ip_address=_get_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
    except (InvalidCredentialsError, UserBlockedError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    service = AuthService(db)
    try:
        return await service.refresh(body.refresh_token)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


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
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    service = AuthService(db)
    await service.logout(body.refresh_token)
    return MessageResponse(message="Logout realizado com sucesso")
