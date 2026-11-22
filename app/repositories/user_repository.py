import uuid
from typing import Optional, Union

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.i_user_repository import IUserRepository
from app.domain.entities.user import User
from app.infrastructure.database.models import UserModel, user_permissions
from app.repositories.base import BaseRepository


def _as_uuid(value: Union[str, uuid.UUID, None]) -> Optional[uuid.UUID]:
    """
    Converte o id textual usado nas camadas superiores em uuid.UUID.

    Retorna None quando a string não é um UUID válido — assim um id malformado
    vindo da URL vira "não encontrado" (404) em vez de erro de driver (500).
    """
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _to_entity(orm: UserModel) -> User:
    return User(
        id=str(orm.id),
        username=orm.username,
        email=orm.email,
        permissions=sorted(p.key for p in orm.permissions),
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class UserRepository(BaseRepository, IUserRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_id(self, user_id: str) -> Optional[User]:
        uid = _as_uuid(user_id)
        if uid is None:
            return None
        orm = (await self._session.execute(select(UserModel).where(UserModel.id == uid))).scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def get_by_username(self, username: str) -> Optional[User]:
        orm = (await self._session.execute(select(UserModel).where(UserModel.username == username))).scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def get_by_email(self, email: str) -> Optional[User]:
        result = await self._session.execute(
            select(UserModel).where(func.lower(UserModel.email) == email.lower())
        )
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def create(self, username: str, email: Optional[str] = None) -> User:
        orm = UserModel(username=username, email=email, permissions=[])
        self._session.add(orm)
        await self._session.flush()
        return _to_entity(orm)

    async def replace_permissions(self, user_id: str, permission_ids: list, granted_by: Optional[str]) -> None:
        """Keeps the grant date of permissions the user already had; only new ones get granted_by."""
        uid = _as_uuid(user_id)
        wanted = {_as_uuid(p) for p in permission_ids}
        current = set((await self._session.execute(
            select(user_permissions.c.permission_id).where(user_permissions.c.user_id == uid)
        )).scalars().all())
        if current - wanted:
            await self._session.execute(
                user_permissions.delete().where(
                    user_permissions.c.user_id == uid,
                    user_permissions.c.permission_id.in_(current - wanted),
                )
            )
        for pid in wanted - current:
            await self._session.execute(
                user_permissions.insert().values(user_id=uid, permission_id=pid, granted_by=_as_uuid(granted_by))
            )
        # The ORM relationship cached on this session must not hide the change.
        orm = await self._session.get(UserModel, uid)
        if orm is not None:
            await self._session.refresh(orm, ["permissions"])
