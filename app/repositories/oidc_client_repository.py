from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import HubServiceModel, OidcClientModel
from app.repositories.base import BaseRepository


class OidcClientRepository(BaseRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def for_service(self, service: HubServiceModel) -> Optional[OidcClientModel]:
        result = await self._session.execute(
            select(OidcClientModel).where(OidcClientModel.hub_service_id == service.id)
        )
        return result.scalars().unique().one_or_none()

    async def add(self, client: OidcClientModel) -> OidcClientModel:
        self._session.add(client)
        await self._session.flush()
        return client

    async def save(self) -> None:
        await self._session.flush()

    async def delete(self, client: OidcClientModel) -> None:
        await self._session.delete(client)
        await self._session.flush()
