from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.domain.entities.permission import TREM_APP
from app.repositories.permission_repository import PermissionRepository


class AdminService:
    """Applications and their permissions. Callers are admins with a second factor."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._permissions = PermissionRepository(session)

    async def list_applications(self) -> list[tuple]:
        apps = await self._permissions.list_applications()
        return [(a, await self._permissions.permissions_of(a.id)) for a in apps]

    async def create_application(self, slug: str, name: str, description: Optional[str]):
        if await self._permissions.get_application(slug):
            raise ConflictError(f"Aplicação '{slug}' já existe")
        return await self._permissions.create_application(slug, name, description)

    async def delete_application(self, slug: str) -> None:
        application = await self._application(slug)
        if application.slug == TREM_APP:
            raise ConflictError("A aplicação trem não pode ser removida")
        await self._permissions.delete(application)

    async def create_permission(self, slug: str, name: str, description: Optional[str]):
        application = await self._application(slug)
        try:
            async with self._session.begin_nested():
                return await self._permissions.create_permission(application, name, description)
        except IntegrityError:
            raise ConflictError(f"Permissão '{slug}:{name}' já existe")

    async def delete_permission(self, slug: str, name: str) -> None:
        if slug == TREM_APP:
            raise ConflictError("Permissões da trem são usadas pelo código e não podem ser removidas")
        found = await self._permissions.get_by_keys([f"{slug}:{name}"])
        if not found:
            raise NotFoundError("Permissão não encontrada")
        await self._permissions.delete(found[0])

    async def _application(self, slug: str):
        application = await self._permissions.get_application(slug)
        if not application:
            raise NotFoundError("Aplicação não encontrada")
        return application
