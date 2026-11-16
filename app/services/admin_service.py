from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    UnknownPermissionsError,
    UserNotFoundError,
)
from app.domain.entities.permission import TREM_APP, TremPermission
from app.domain.entities.user import User, UserStatus
from app.repositories.permission_repository import PermissionRepository
from app.repositories.user_repository import UserRepository

_APPROVABLE = {UserStatus.PENDING_APPROVAL, UserStatus.BLOCKED, UserStatus.REJECTED}


class AdminService:
    """Account approval, permissions and applications. Callers are admins with a second factor."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._users = UserRepository(session)
        self._permissions = PermissionRepository(session)

    # --- Users ---

    async def list_users(self, status: Optional[str], limit: int, offset: int) -> tuple[list[User], int]:
        return (
            await self._users.list_all(limit=limit, offset=offset, status=status),
            await self._users.count_all(status=status),
        )

    async def get_user(self, user_id: str) -> User:
        user = await self._users.get_by_id(user_id)
        if not user:
            raise UserNotFoundError()
        return user

    async def approve(self, admin: User, user_id: str, permissions: list[str]) -> User:
        user = await self.get_user(user_id)
        if user.status not in _APPROVABLE:
            raise ConflictError(f"Conta em estado '{user.status}' não pode ser aprovada")
        await self._grant(admin, user, permissions)
        await self._users.approve(user.id, admin.id, datetime.now(timezone.utc))
        return await self.get_user(user.id)

    async def reject(self, admin: User, user_id: str) -> User:
        user = await self._other_user(admin, user_id)
        if user.status == UserStatus.ACTIVE:
            raise ConflictError("Conta ativa: use suspend")
        await self._users.update_status(user.id, UserStatus.REJECTED.value)
        await self._users.revoke_all_refresh_tokens(user.id)
        return await self.get_user(user.id)

    async def suspend(self, admin: User, user_id: str) -> User:
        user = await self._other_user(admin, user_id)
        if user.status != UserStatus.ACTIVE:
            raise ConflictError("Só contas ativas podem ser suspensas")
        await self._users.update_status(user.id, UserStatus.SUSPENDED.value)
        # API keys of the owner stop on the next request: they are checked against the owner.
        await self._users.revoke_all_refresh_tokens(user.id)
        return await self.get_user(user.id)

    async def reactivate(self, admin: User, user_id: str) -> User:
        user = await self._other_user(admin, user_id)
        if user.status != UserStatus.SUSPENDED:
            raise ConflictError("Só contas suspensas podem ser reativadas")
        await self._users.update_status(user.id, UserStatus.ACTIVE.value)
        return await self.get_user(user.id)

    async def set_permissions(self, admin: User, user_id: str, permissions: list[str]) -> User:
        user = await self.get_user(user_id)
        if user.id == admin.id and TremPermission.ADMIN not in permissions:
            raise ConflictError("Um admin não remove a própria permissão de admin")
        await self._grant(admin, user, permissions)
        # Access tokens carry the permissions other applications read: new ones only come
        # with a new login, so the old sessions end here.
        await self._users.revoke_all_refresh_tokens(user.id)
        return await self.get_user(user.id)

    async def _grant(self, admin: User, user: User, keys: list[str]) -> None:
        found = await self._permissions.get_by_keys(keys)
        missing = set(keys) - {p.key for p in found}
        if missing:
            raise UnknownPermissionsError(missing)
        await self._users.replace_permissions(user.id, [p.id for p in found], granted_by=admin.id)

    async def _other_user(self, admin: User, user_id: str) -> User:
        user = await self.get_user(user_id)
        if user.id == admin.id:
            raise ConflictError("Um admin não altera o estado da própria conta")
        return user

    # --- Applications ---

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
