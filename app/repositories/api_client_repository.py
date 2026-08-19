from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.interfaces.i_api_client_repository import IApiClientRepository
from app.domain.entities.api_client import ApiClient
from app.infrastructure.database.models import ApiClientModel
from app.repositories.base import BaseRepository
from app.repositories.user_repository import _as_uuid


def _to_entity(orm: ApiClientModel) -> ApiClient:
    return ApiClient(
        id=str(orm.id),
        name=orm.name,
        description=orm.description,
        profile_id=str(orm.profile_id),
        profile_name=orm.profile.name if orm.profile else "",
        revoked=orm.revoked,
        last_used_at=orm.last_used_at,
        created_at=orm.created_at,
    )


class ApiClientRepository(BaseRepository, IApiClientRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_key_hash(self, key_hash: str) -> Optional[ApiClient]:
        """Caminho quente: roda em toda request autenticada por API key."""
        result = await self._session.execute(
            select(ApiClientModel)
            .options(joinedload(ApiClientModel.profile))
            .where(ApiClientModel.key_hash == key_hash)
        )
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def get_by_id(self, client_id: str) -> Optional[ApiClient]:
        cid = _as_uuid(client_id)
        if cid is None:
            return None
        result = await self._session.execute(
            select(ApiClientModel)
            .options(joinedload(ApiClientModel.profile))
            .where(ApiClientModel.id == cid)
        )
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def get_by_name(self, name: str) -> Optional[ApiClient]:
        result = await self._session.execute(
            select(ApiClientModel)
            .options(joinedload(ApiClientModel.profile))
            .where(ApiClientModel.name == name)
        )
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def create(
        self,
        name: str,
        key_hash: str,
        profile_id: str,
        description: Optional[str] = None,
    ) -> ApiClient:
        orm = ApiClientModel(
            name=name,
            key_hash=key_hash,
            profile_id=_as_uuid(profile_id),
            description=description,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm, ["profile"])
        return _to_entity(orm)

    async def revoke(self, client_id: str) -> None:
        await self._session.execute(
            update(ApiClientModel)
            .where(ApiClientModel.id == _as_uuid(client_id))
            .values(revoked=True)
        )

    async def touch_last_used(self, client_id: str, at: datetime) -> None:
        await self._session.execute(
            update(ApiClientModel)
            .where(ApiClientModel.id == _as_uuid(client_id))
            .values(last_used_at=at)
        )

    async def list_all(self, limit: int = 50, offset: int = 0) -> list[ApiClient]:
        result = await self._session.execute(
            select(ApiClientModel)
            .options(joinedload(ApiClientModel.profile))
            .order_by(ApiClientModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_entity(row) for row in result.scalars().all()]

    async def count_all(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(ApiClientModel)
        )
        return result.scalar_one()
