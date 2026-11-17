from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import ApplicationModel, HubServiceModel, PermissionModel
from app.repositories.base import BaseRepository

HUB_APP = "hub"


class HubRepository(BaseRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    def _select(self):
        return (
            select(HubServiceModel)
            .join(PermissionModel, HubServiceModel.permission_id == PermissionModel.id)
            .join(ApplicationModel, PermissionModel.application_id == ApplicationModel.id)
            .where(ApplicationModel.slug == HUB_APP)
        )

    async def list(self) -> list[HubServiceModel]:
        result = await self._session.execute(
            self._select().order_by(HubServiceModel.position, HubServiceModel.name)
        )
        return list(result.scalars().unique().all())

    async def get(self, slug: str) -> Optional[HubServiceModel]:
        result = await self._session.execute(self._select().where(PermissionModel.name == slug))
        return result.scalars().unique().one_or_none()

    async def next_position(self) -> int:
        result = await self._session.execute(select(func.max(HubServiceModel.position)))
        current = result.scalar_one_or_none()
        return 0 if current is None else current + 1

    async def add(self, service: HubServiceModel) -> HubServiceModel:
        self._session.add(service)
        await self._session.flush()
        return service

    async def save(self) -> None:
        await self._session.flush()

    async def delete(self, service: HubServiceModel) -> None:
        await self._session.delete(service)
        await self._session.flush()
