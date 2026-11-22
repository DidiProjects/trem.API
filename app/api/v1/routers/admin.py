from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import require_admin
from app.api.v1.errors import http_error
from app.core.exceptions import AppException
from app.domain.entities.user import User
from app.domain.schemas.application import (
    ApplicationCreate,
    ApplicationResponse,
    PermissionCreate,
    PermissionResponse,
)
from app.domain.schemas.common import MessageResponse
from app.infrastructure.database.connection import get_db
from app.services.admin_service import AdminService

router = APIRouter(prefix="/admin", tags=["Admin"])


def _app_response(application, permissions) -> ApplicationResponse:
    return ApplicationResponse(
        slug=application.slug,
        name=application.name,
        description=application.description,
        created_at=application.created_at,
        permissions=[
            PermissionResponse(key=f"{application.slug}:{p.name}", name=p.name, description=p.description)
            for p in permissions
        ],
    )


async def _run(coro):
    try:
        return await coro
    except AppException as e:
        raise http_error(e)


@router.get("/applications", response_model=list[ApplicationResponse])
async def list_applications(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    return [_app_response(a, perms) for a, perms in await AdminService(db).list_applications()]


@router.post("/applications", response_model=ApplicationResponse, status_code=status.HTTP_201_CREATED)
async def create_application(
    body: ApplicationCreate, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")
):
    application = await _run(AdminService(db).create_application(body.slug, body.name, body.description))
    return _app_response(application, [])


@router.delete("/applications/{slug}", response_model=MessageResponse)
async def delete_application(slug: str, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    """Removes the application with its permissions, grants and sessions."""
    await _run(AdminService(db).delete_application(slug))
    return MessageResponse(message="Aplicação removida")


@router.post("/applications/{slug}/permissions", response_model=PermissionResponse, status_code=status.HTTP_201_CREATED)
async def create_permission(
    slug: str, body: PermissionCreate, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")
):
    permission = await _run(AdminService(db).create_permission(slug, body.name, body.description))
    return PermissionResponse(key=f"{slug}:{permission.name}", name=permission.name, description=permission.description)


@router.delete("/applications/{slug}/permissions/{name}", response_model=MessageResponse)
async def delete_permission(
    slug: str, name: str, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")
):
    await _run(AdminService(db).delete_permission(slug, name))
    return MessageResponse(message="Permissão removida")
