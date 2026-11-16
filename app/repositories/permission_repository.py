from typing import Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.permission import split_permission_key
from app.infrastructure.database.models import ApplicationModel, PermissionModel
from app.repositories.base import BaseRepository
from app.repositories.user_repository import _as_uuid


class PermissionRepository(BaseRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_keys(self, keys: Iterable[str]) -> list[PermissionModel]:
        """Permissions matching "<application>:<permission>" keys; unknown keys are left out."""
        pairs = {split_permission_key(k) for k in keys}
        if not pairs:
            return []
        result = await self._session.execute(
            select(PermissionModel)
            .join(ApplicationModel)
            .where(or_(*(
                and_(ApplicationModel.slug == app, PermissionModel.name == name)
                for app, name in pairs
            )))
        )
        return list(result.scalars().all())

    async def get_by_ids(self, permission_ids: Iterable[str]) -> list[PermissionModel]:
        ids = [i for i in (_as_uuid(p) for p in permission_ids) if i]
        if not ids:
            return []
        result = await self._session.execute(
            select(PermissionModel).where(PermissionModel.id.in_(ids))
        )
        return list(result.scalars().all())
