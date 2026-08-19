from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_secure import verify_api_key
from app.core.exceptions import AppException
from app.domain.schemas.common import MessageResponse, PaginatedResponse
from app.domain.schemas.user import UserCreate, UserResponse
from app.infrastructure.database.connection import get_db
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["Users"])


def _to_response(user) -> UserResponse:
    return UserResponse.model_validate(user)


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    service = UserService(db)
    try:
        user = await service.create_user(body)
        return _to_response(user)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("", response_model=PaginatedResponse[UserResponse])
async def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    service = UserService(db)
    users, total = await service.list_users(limit=limit, offset=offset)
    return PaginatedResponse(
        items=[_to_response(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    service = UserService(db)
    try:
        user = await service.get_user(user_id)
        return _to_response(user)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/{user_id}/send-provisional-password", response_model=MessageResponse)
async def send_provisional_password(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    service = UserService(db)
    try:
        await service.send_provisional_password(user_id)
        return MessageResponse(message="Senha provisória enviada com sucesso")
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
