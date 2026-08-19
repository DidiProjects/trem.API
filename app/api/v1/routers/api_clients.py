from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_secure import verify_api_key
from app.core.exceptions import AppException
from app.domain.schemas.api_client import (
    ApiClientCreate,
    ApiClientCreatedResponse,
    ApiClientResponse,
)
from app.domain.schemas.common import MessageResponse, PaginatedResponse
from app.infrastructure.database.connection import get_db
from app.services.api_client_service import ApiClientService

# Administração de clientes fica atrás da master key (settings.API_KEY), a
# mesma que protege /users. A master key não autentica as rotas de negócio —
# serve só para gerir credenciais.
router = APIRouter(prefix="/admin/clients", tags=["Admin: API Clients"])


@router.post("", response_model=ApiClientCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_client(
    body: ApiClientCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """Cria um cliente e devolve a chave em claro — só desta vez."""
    service = ApiClientService(db)
    try:
        client, raw_key = await service.create_client(
            name=body.name,
            profile_name=body.profile_name.value,
            description=body.description,
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
    _: str = Depends(verify_api_key),
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
    _: str = Depends(verify_api_key),
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
    _: str = Depends(verify_api_key),
):
    """Revoga a chave. É irreversível — para religar, crie outro cliente."""
    try:
        await ApiClientService(db).revoke_client(client_id)
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return MessageResponse(message="Cliente revogado com sucesso")
