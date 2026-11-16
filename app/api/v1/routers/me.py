from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_active_user, get_current_user
from app.api.v1.routers.auth import http_error
from app.core.exceptions import AppException
from app.domain.entities.user import User
from app.domain.schemas.api_client import (
    MeResponse,
    OwnApiKeyCreate,
    OwnApiKeyCreatedResponse,
    OwnApiKeyResponse,
)
from app.domain.schemas.common import MessageResponse
from app.infrastructure.database.connection import get_db
from app.services.api_client_service import OWN_KEYS_LIMIT, ApiClientService
from app.services.mfa_service import MfaService

# A person behind a login (JWT), never an API key: a key must not mint more keys.
router = APIRouter(prefix="/me", tags=["Me"])


@router.get("", response_model=MeResponse)
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Who is logged in; works before the first password change, to tell the page to ask for it."""
    keys = await ApiClientService(db).list_own_keys(user.id)
    return MeResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        status=user.status,
        permissions=user.permissions,
        must_change_password=user.must_change_password,
        totp_enabled=user.totp_enabled,
        recovery_codes_left=await MfaService(db).remaining_recovery_codes(user.id) if user.totp_enabled else 0,
        api_keys_active=sum(1 for k in keys if not k.revoked),
        api_keys_limit=OWN_KEYS_LIMIT,
    )


@router.get("/api-keys", response_model=list[OwnApiKeyResponse])
async def list_keys(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    return [OwnApiKeyResponse.model_validate(k) for k in await ApiClientService(db).list_own_keys(user.id)]


@router.post("/api-keys", response_model=OwnApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: OwnApiKeyCreate,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """The full key is in this response only; the database keeps its hash."""
    try:
        client, raw_key = await ApiClientService(db).create_own_key(
            user, body.name, body.permissions, body.description
        )
    except AppException as e:
        raise http_error(e)
    return OwnApiKeyCreatedResponse(**OwnApiKeyResponse.model_validate(client).model_dump(), api_key=raw_key)


@router.delete("/api-keys/{key_id}", response_model=MessageResponse)
async def revoke_key(key_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    try:
        await ApiClientService(db).revoke_own_key(user.id, key_id)
    except AppException as e:
        raise http_error(e)
    return MessageResponse(message="Chave revogada")
