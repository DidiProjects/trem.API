from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import EmailTokenModel
from app.repositories.base import BaseRepository
from app.repositories.user_repository import _as_uuid

VERIFY_EMAIL = "verify_email"
RESET_PASSWORD = "reset_password"


class EmailTokenRepository(BaseRepository):

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, user_id: str, purpose: str, token_hash: str, expires_at: datetime) -> None:
        self._session.add(EmailTokenModel(
            user_id=_as_uuid(user_id), purpose=purpose, token_hash=token_hash, expires_at=expires_at,
        ))
        await self._session.flush()

    async def get(self, token_hash: str, purpose: str) -> Optional[EmailTokenModel]:
        result = await self._session.execute(
            select(EmailTokenModel).where(
                EmailTokenModel.token_hash == token_hash, EmailTokenModel.purpose == purpose
            )
        )
        return result.scalar_one_or_none()

    async def use(self, token_id, at: datetime) -> bool:
        """Marks the token used; False if it already was (single use, even under a race)."""
        result = await self._session.execute(
            update(EmailTokenModel)
            .where(EmailTokenModel.id == token_id, EmailTokenModel.used_at.is_(None))
            .values(used_at=at)
        )
        return result.rowcount == 1

    async def invalidate(self, user_id: str, purpose: str, at: datetime) -> None:
        """A new link replaces the previous ones: only the latest e-mail works."""
        await self._session.execute(
            update(EmailTokenModel)
            .where(
                EmailTokenModel.user_id == _as_uuid(user_id),
                EmailTokenModel.purpose == purpose,
                EmailTokenModel.used_at.is_(None),
            )
            .values(used_at=at)
        )
