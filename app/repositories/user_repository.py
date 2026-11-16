import uuid
from datetime import datetime, timezone
from typing import Optional, Union

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.i_user_repository import IUserRepository
from app.domain.entities.user import User
from app.infrastructure.database.models import (
    ApplicationModel,
    PermissionModel,
    RefreshTokenModel,
    UserModel,
    user_permissions,
)
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
        password_hash=orm.password_hash,
        permissions=sorted(p.key for p in orm.permissions),
        status=orm.status,
        must_change_password=orm.must_change_password,
        provisional_password_sent_at=orm.provisional_password_sent_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        last_login_at=orm.last_login_at,
        email_verified_at=orm.email_verified_at,
        approved_at=orm.approved_at,
        approved_by=str(orm.approved_by) if orm.approved_by else None,
    )


class UserRepository(BaseRepository, IUserRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_id(self, user_id: str) -> Optional[User]:
        uid = _as_uuid(user_id)
        if uid is None:
            return None
        result = await self._session.execute(
            select(UserModel)
            .where(UserModel.id == uid)
        )
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def get_by_username(self, username: str) -> Optional[User]:
        result = await self._session.execute(
            select(UserModel)
            .where(UserModel.username == username)
        )
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def create(
        self,
        username: str,
        password_hash: str,
        permission_ids: list[str],
        email: Optional[str] = None,
        status: Optional[str] = None,
        must_change_password: bool = True,
    ) -> User:
        ids = [i for i in (_as_uuid(p) for p in permission_ids) if i]
        permissions = (
            await self._session.execute(select(PermissionModel).where(PermissionModel.id.in_(ids)))
        ).scalars().all()
        orm = UserModel(
            username=username,
            email=email,
            password_hash=password_hash,
            permissions=list(permissions),
            must_change_password=must_change_password,
        )
        if status:
            orm.status = status
        self._session.add(orm)
        await self._session.flush()
        return _to_entity(orm)

    async def get_by_email(self, email: str) -> Optional[User]:
        result = await self._session.execute(
            select(UserModel).where(func.lower(UserModel.email) == email.lower())
        )
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def delete(self, user_id: str) -> None:
        orm = await self._session.get(UserModel, _as_uuid(user_id))
        if orm:
            await self._session.delete(orm)
            await self._session.flush()

    async def mark_email_verified(self, user_id: str, at: datetime, status: str) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == _as_uuid(user_id))
            .values(email_verified_at=at, status=status, updated_at=at)
        )

    async def emails_with_permission(self, key: str) -> list[str]:
        """E-mails of active users holding a permission, e.g. who to tell about a new sign-up."""
        application, name = key.split(":", 1)
        result = await self._session.execute(
            select(UserModel.email)
            .join(user_permissions, user_permissions.c.user_id == UserModel.id)
            .join(PermissionModel, PermissionModel.id == user_permissions.c.permission_id)
            .join(ApplicationModel, ApplicationModel.id == PermissionModel.application_id)
            .where(
                ApplicationModel.slug == application,
                PermissionModel.name == name,
                UserModel.status == "active",
                UserModel.email.is_not(None),
            )
        )
        return sorted(set(result.scalars().all()))

    async def update_password(
        self,
        user_id: str,
        password_hash: str,
        must_change_password: bool,
    ) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == _as_uuid(user_id))
            .values(
                password_hash=password_hash,
                must_change_password=must_change_password,
                updated_at=datetime.now(timezone.utc),
            )
        )

    async def update_status(self, user_id: str, status: str) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == _as_uuid(user_id))
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )

    async def update_last_login(self, user_id: str, at: datetime) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == _as_uuid(user_id))
            .values(last_login_at=at, updated_at=at)
        )

    async def set_provisional_password_sent(self, user_id: str, at: datetime) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == _as_uuid(user_id))
            .values(
                provisional_password_sent_at=at,
                updated_at=datetime.now(timezone.utc),
            )
        )

    async def list_all(self, limit: int = 50, offset: int = 0) -> list[User]:
        result = await self._session.execute(
            select(UserModel)
            .order_by(UserModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_entity(row) for row in result.scalars().all()]

    async def count_all(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(UserModel)
        )
        return result.scalar_one()

    # --- Refresh tokens ---

    async def create_refresh_token(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        application_id: Union[str, uuid.UUID],
        family_id: Optional[Union[str, uuid.UUID]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> RefreshTokenModel:
        token = RefreshTokenModel(
            id=uuid.uuid4(),
            user_id=_as_uuid(user_id),
            token_hash=token_hash,
            expires_at=expires_at,
            application_id=_as_uuid(application_id),
            ip_address=ip_address,
            user_agent=user_agent,
        )
        token.family_id = _as_uuid(family_id) if family_id else token.id
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_refresh_token(self, token_hash: str) -> Optional[RefreshTokenModel]:
        result = await self._session.execute(
            select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def claim_refresh_token(self, token_id: uuid.UUID) -> bool:
        """
        Revokes a live token and reports whether this call did it. Two refreshes racing
        with the same token cannot both succeed: the loser sees a reuse.
        """
        result = await self._session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.id == token_id, RefreshTokenModel.revoked.is_(False))
            .values(revoked=True)
        )
        return result.rowcount == 1

    async def set_refresh_token_successor(self, token_id: uuid.UUID, successor_id: uuid.UUID) -> None:
        await self._session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.id == token_id)
            .values(replaced_by=successor_id)
        )

    async def revoke_refresh_token_family(self, family_id: uuid.UUID) -> None:
        await self._session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.family_id == family_id)
            .values(revoked=True)
        )

    async def revoke_all_refresh_tokens(self, user_id: str) -> None:
        await self._session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.user_id == _as_uuid(user_id))
            .values(revoked=True)
        )
