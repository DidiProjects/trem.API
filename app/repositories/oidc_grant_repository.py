from datetime import datetime
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import OidcGrantModel
from app.repositories.base import BaseRepository


class OidcGrantRepository(BaseRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def by_request(self, request_hash: str) -> Optional[OidcGrantModel]:
        result = await self._session.execute(
            select(OidcGrantModel).where(OidcGrantModel.request_hash == request_hash)
        )
        return result.scalars().unique().one_or_none()

    async def by_code(self, code_hash: str) -> Optional[OidcGrantModel]:
        # Locked: two exchanges of the same code at once must not both see it unused.
        result = await self._session.execute(
            select(OidcGrantModel).where(OidcGrantModel.code_hash == code_hash).with_for_update(of=OidcGrantModel)
        )
        return result.scalars().unique().one_or_none()

    async def add(self, grant: OidcGrantModel) -> OidcGrantModel:
        self._session.add(grant)
        await self._session.flush()
        return grant

    async def save(self) -> None:
        await self._session.flush()

    async def delete(self, grant: OidcGrantModel) -> None:
        await self._session.delete(grant)
        await self._session.flush()

    async def purge_expired(self, before: datetime) -> None:
        await self._session.execute(delete(OidcGrantModel).where(OidcGrantModel.expires_at < before))
