import hashlib
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, InvalidLogoError, NotFoundError
from app.domain.entities.user import User
from app.infrastructure.database.models import HubServiceModel
from app.repositories.hub_repository import HUB_APP, HubRepository
from app.repositories.permission_repository import PermissionRepository

LOGO_MAX_BYTES = 256 * 1024


def permission_key(slug: str) -> str:
    return f"{HUB_APP}:{slug}"


def logo_path(service: HubServiceModel) -> Optional[str]:
    if not service.logo_version:
        return None
    return f"/hub/services/{service.slug}/logo?v={service.logo_version}"


def logo_type(data: bytes) -> str:
    """The image type read from the bytes; the declared Content-Type is not trusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:1024].lstrip(b"\xef\xbb\xbf").lstrip()
    if (head.startswith(b"<svg") or head.startswith(b"<?xml")) and b"<svg" in data[:4096]:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidLogoError("SVG precisa estar em UTF-8")
        return "image/svg+xml"
    raise InvalidLogoError("Logo precisa ser PNG, WebP ou SVG")


class HubService:
    """The catalog of self-hosted services shown by trem.FRONT; who sees each one is the
    authentik group hub:<slug>."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._services = HubRepository(session)
        self._permissions = PermissionRepository(session)

    async def catalog(self, user: User) -> list[HubServiceModel]:
        granted = set(user.permissions)
        return [
            s for s in await self._services.list()
            if s.enabled and s.url and permission_key(s.slug) in granted
        ]

    async def list_all(self) -> list[HubServiceModel]:
        return await self._services.list()

    async def get(self, slug: str) -> HubServiceModel:
        service = await self._services.get(slug)
        if not service:
            raise NotFoundError("Serviço não encontrado")
        return service

    async def create(self, slug: str, name: str, description: Optional[str], url: Optional[str], enabled: bool) -> HubServiceModel:
        application = await self._permissions.get_application(HUB_APP)
        if application is None:
            application = await self._permissions.create_application(
                HUB_APP, "trem.FRONT", "Catálogo de serviços: hub:<serviço> mostra o serviço à conta"
            )
        if await self._permissions.get_by_keys([permission_key(slug)]):
            raise ConflictError(f"Serviço '{slug}' já existe")
        permission = await self._permissions.create_permission(application, slug, name)
        return await self._services.add(HubServiceModel(
            permission=permission, name=name, description=description, url=url, enabled=enabled,
            position=await self._services.next_position(),
        ))

    async def update(self, slug: str, changes: dict) -> HubServiceModel:
        service = await self.get(slug)
        for field in ("name", "description", "url", "enabled"):
            if field in changes:
                if field in ("name", "enabled") and changes[field] is None:
                    continue
                setattr(service, field, changes[field])
        if "name" in changes and changes["name"]:
            service.permission.description = changes["name"]
        await self._services.save()
        return service

    async def reorder(self, slugs: list[str]) -> list[HubServiceModel]:
        services = await self._services.list()
        if sorted(slugs) != sorted(s.slug for s in services):
            raise ConflictError("A ordem precisa listar cada serviço uma vez")
        by_slug = {s.slug: s for s in services}
        for position, slug in enumerate(slugs):
            by_slug[slug].position = position
        await self._services.save()
        return await self._services.list()

    async def delete(self, slug: str) -> None:
        """Removes the service and its permission, and with it every grant."""
        service = await self.get(slug)
        permission = service.permission
        await self._services.delete(service)
        await self._permissions.delete(permission)
        # Users loaded in this session still list the permission the database just dropped.
        self._session.expire_all()

    async def set_logo(self, slug: str, data: bytes) -> HubServiceModel:
        service = await self.get(slug)
        if not data:
            raise InvalidLogoError("Arquivo vazio")
        if len(data) > LOGO_MAX_BYTES:
            raise InvalidLogoError(f"Logo passa de {LOGO_MAX_BYTES // 1024} KB")
        service.logo_type = logo_type(data)
        service.logo = data
        service.logo_version = hashlib.sha256(data).hexdigest()[:16]
        await self._services.save()
        return service

    async def clear_logo(self, slug: str) -> HubServiceModel:
        service = await self.get(slug)
        service.logo = service.logo_type = service.logo_version = None
        await self._services.save()
        return service
