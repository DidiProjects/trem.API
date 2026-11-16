from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import require_admin
from app.api.v1.routers.auth import http_error
from app.core.exceptions import AppException
from app.domain.entities.user import User, UserStatus
from app.domain.schemas.application import (
    ApplicationCreate,
    ApplicationResponse,
    PermissionCreate,
    PermissionResponse,
)
from app.domain.schemas.common import MessageResponse, PaginatedResponse
from app.domain.schemas.user import PermissionSet, UserResponse
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


@router.get("/users", response_model=PaginatedResponse[UserResponse])
async def list_users(
    status_filter: Optional[UserStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    users, total = await AdminService(db).list_users(
        status_filter.value if status_filter else None, limit, offset
    )
    return PaginatedResponse(
        items=[UserResponse.model_validate(u) for u in users], total=total, limit=limit, offset=offset
    )


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: str, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    return UserResponse.model_validate(await _run(AdminService(db).get_user(user_id)))


@router.post("/users/{user_id}/approve", response_model=UserResponse)
async def approve_user(
    user_id: str,
    body: PermissionSet,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Activates the account with exactly these permissions."""
    return UserResponse.model_validate(await _run(AdminService(db).approve(admin, user_id, body.permissions)))


@router.post("/users/{user_id}/reject", response_model=UserResponse)
async def reject_user(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    return UserResponse.model_validate(await _run(AdminService(db).reject(admin, user_id)))


@router.post("/users/{user_id}/suspend", response_model=UserResponse)
async def suspend_user(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    """Blocks the account, ends its sessions and stops its API keys."""
    return UserResponse.model_validate(await _run(AdminService(db).suspend(admin, user_id)))


@router.post("/users/{user_id}/reactivate", response_model=UserResponse)
async def reactivate_user(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    return UserResponse.model_validate(await _run(AdminService(db).reactivate(admin, user_id)))


@router.put("/users/{user_id}/permissions", response_model=UserResponse)
async def set_permissions(
    user_id: str,
    body: PermissionSet,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Replaces the whole set. Sessions end so the next tokens carry the new set."""
    return UserResponse.model_validate(await _run(AdminService(db).set_permissions(admin, user_id, body.permissions)))


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
