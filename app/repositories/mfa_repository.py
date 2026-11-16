from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import RecoveryCodeModel, UserModel
from app.repositories.base import BaseRepository
from app.repositories.user_repository import _as_uuid


class MfaRepository(BaseRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_totp(self, user_id: str) -> tuple[Optional[str], Optional[datetime], Optional[int]]:
        """(encrypted secret, enabled at, last accepted step)."""
        row = (await self._session.execute(
            select(UserModel.totp_secret, UserModel.totp_enabled_at, UserModel.totp_last_step)
            .where(UserModel.id == _as_uuid(user_id))
        )).one_or_none()
        return tuple(row) if row else (None, None, None)

    async def set_pending_secret(self, user_id: str, encrypted: str) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == _as_uuid(user_id))
            .values(totp_secret=encrypted, totp_enabled_at=None, totp_last_step=None)
        )

    async def enable(self, user_id: str, at: datetime, step: int) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == _as_uuid(user_id))
            .values(totp_enabled_at=at, totp_last_step=step)
        )

    async def accept_step(self, user_id: str, step: int) -> bool:
        """Records the step only if it is newer: the same code cannot log in twice, even racing."""
        result = await self._session.execute(
            update(UserModel)
            .where(
                UserModel.id == _as_uuid(user_id),
                or_(UserModel.totp_last_step.is_(None), UserModel.totp_last_step < step),
            )
            .values(totp_last_step=step)
        )
        return result.rowcount == 1

    async def disable(self, user_id: str) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == _as_uuid(user_id))
            .values(totp_secret=None, totp_enabled_at=None, totp_last_step=None)
        )
        await self._session.execute(
            delete(RecoveryCodeModel).where(RecoveryCodeModel.user_id == _as_uuid(user_id))
        )

    async def replace_recovery_codes(self, user_id: str, code_hashes: list[str]) -> None:
        uid = _as_uuid(user_id)
        await self._session.execute(delete(RecoveryCodeModel).where(RecoveryCodeModel.user_id == uid))
        self._session.add_all(RecoveryCodeModel(user_id=uid, code_hash=h) for h in code_hashes)
        await self._session.flush()

    async def use_recovery_code(self, user_id: str, code_hash: str, at: datetime) -> bool:
        result = await self._session.execute(
            update(RecoveryCodeModel)
            .where(
                RecoveryCodeModel.user_id == _as_uuid(user_id),
                RecoveryCodeModel.code_hash == code_hash,
                RecoveryCodeModel.used_at.is_(None),
            )
            .values(used_at=at)
        )
        return result.rowcount == 1

    async def remaining_recovery_codes(self, user_id: str) -> int:
        return (await self._session.execute(
            select(func.count()).select_from(RecoveryCodeModel).where(
                RecoveryCodeModel.user_id == _as_uuid(user_id), RecoveryCodeModel.used_at.is_(None)
            )
        )).scalar_one()
