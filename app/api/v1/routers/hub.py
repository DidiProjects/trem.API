from typing import Optional

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user, require_admin
from app.api.v1.errors import http_error
from app.core.exceptions import AppException, InvalidLogoError
from app.domain.entities.user import User
from app.domain.schemas.common import MessageResponse
from app.domain.schemas.hub import (
    HubServiceAdminResponse,
    HubServiceCreate,
    HubServiceOrder,
    HubServiceResponse,
    HubServiceUpdate,
)
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import HubServiceModel
from app.services.hub_service import LOGO_MAX_BYTES, HubService, logo_path, permission_key

router = APIRouter(prefix="/hub", tags=["Hub"])
admin_router = APIRouter(prefix="/admin/hub", tags=["Admin"])

# An SVG opened straight from this URL must not run anything: no scripts, no requests,
# no sniffing it into something else. Inside an <img> a browser never runs them anyway.
_LOGO_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


async def _run(coro):
    try:
        return await coro
    except AppException as e:
        raise http_error(e)


def _admin_view(service: HubServiceModel) -> HubServiceAdminResponse:
    return HubServiceAdminResponse(
        slug=service.slug,
        permission=permission_key(service.slug),
        name=service.name,
        description=service.description,
        url=service.url,
        enabled=service.enabled,
        position=service.position,
        logo_url=logo_path(service),
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


@router.get("/services", response_model=list[HubServiceResponse])
async def catalog(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db, scope="function")):
    """The services this account may open: enabled, with an address, and granted hub:<slug>."""
    return [
        HubServiceResponse(slug=s.slug, name=s.name, description=s.description, url=s.url, logo_url=logo_path(s))
        for s in await HubService(db).catalog(user)
    ]


@router.get("/services/{slug}/logo", response_class=Response)
async def logo(slug: str, v: Optional[str] = Query(None), db: AsyncSession = Depends(get_db, scope="function")):
    """Public, for <img>: a logo reveals nothing the catalog's front page would not."""
    service = await _run(HubService(db).get(slug))
    if not service.logo:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    # The versioned URL never changes content; an unversioned or stale one revalidates.
    cache = "public, max-age=31536000, immutable" if v == service.logo_version else "public, no-cache"
    return Response(
        content=service.logo,
        media_type=service.logo_type,
        headers={**_LOGO_HEADERS, "Cache-Control": cache, "ETag": f'"{service.logo_version}"'},
    )


@admin_router.get("/services", response_model=list[HubServiceAdminResponse])
async def list_services(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    return [_admin_view(s) for s in await HubService(db).list_all()]


@admin_router.post("/services", response_model=HubServiceAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_service(body: HubServiceCreate, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    """Creates the service and its permission hub:<slug>."""
    service = await _run(HubService(db).create(body.slug, body.name, body.description, body.url, body.enabled))
    return _admin_view(service)


@admin_router.put("/services/order", response_model=list[HubServiceAdminResponse])
async def reorder_services(body: HubServiceOrder, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    return [_admin_view(s) for s in await _run(HubService(db).reorder(body.slugs))]


@admin_router.patch("/services/{slug}", response_model=HubServiceAdminResponse)
async def update_service(
    slug: str, body: HubServiceUpdate, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")
):
    changes = body.model_dump(include=body.model_fields_set)
    return _admin_view(await _run(HubService(db).update(slug, changes)))


@admin_router.delete("/services/{slug}", response_model=MessageResponse)
async def delete_service(slug: str, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    """Removes the service, its permission and every grant of it."""
    await _run(HubService(db).delete(slug))
    return MessageResponse(message="Serviço removido")


@admin_router.put("/services/{slug}/logo", response_model=HubServiceAdminResponse)
async def upload_logo(
    slug: str,
    file: UploadFile = File(..., description="PNG, WebP ou SVG, até 256 KB"),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    data = await file.read(LOGO_MAX_BYTES + 1)
    if len(data) > LOGO_MAX_BYTES:
        raise http_error(InvalidLogoError(f"Logo passa de {LOGO_MAX_BYTES // 1024} KB"))
    return _admin_view(await _run(HubService(db).set_logo(slug, data)))


@admin_router.delete("/services/{slug}/logo", response_model=HubServiceAdminResponse)
async def delete_logo(slug: str, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db, scope="function")):
    return _admin_view(await _run(HubService(db).clear_logo(slug)))
