from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import require_admin
from app.domain.entities.user import User
from app.core.exceptions import AppException
from app.domain.schemas.api_client import (
    ApiClientCreate,
    ApiClientCreatedResponse,
    ApiClientResponse,
)
from app.domain.schemas.common import MessageResponse, PaginatedResponse
from app.infrastructure.database.connection import get_db
from app.services.api_client_service import ApiClientService

# Administração de clientes exige um admin logado com segundo fator, como /users.
router = APIRouter(prefix="/admin/clients", tags=["Admin: API Clients"])


@router.post("", response_model=ApiClientCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_client(
    body: ApiClientCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Cria um cliente e devolve a chave em claro — só desta vez."""
    service = ApiClientService(db)
    try:
        client, raw_key = await service.create_client(
            name=body.name,
            permissions=body.permissions,
            description=body.description,
            user_id=body.user_id,
        )
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    return ApiClientCreatedResponse(
        **ApiClientResponse.model_validate(client).model_dump(),
        api_key=raw_key,
    )


@router.get("", response_model=PaginatedResponse[ApiClientResponse])
async def list_clients(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    clients, total = await ApiClientService(db).list_clients(limit=limit, offset=offset)
    return PaginatedResponse(
        items=[ApiClientResponse.model_validate(c) for c in clients],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{client_id}", response_model=ApiClientResponse)
async def get_client(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        client = await ApiClientService(db).get_client(client_id)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return ApiClientResponse.model_validate(client)


@router.delete("/{client_id}", response_model=MessageResponse)
async def revoke_client(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Revoga a chave. É irreversível — para religar, crie outro cliente."""
    try:
        await ApiClientService(db).revoke_client(client_id)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return MessageResponse(message="Cliente revogado com sucesso")
