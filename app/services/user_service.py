from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UserAlreadyExistsError, UserNotFoundError
from app.core.security import generate_provisional_password, hash_password
from app.domain.entities.user import User
from app.domain.schemas.user import UserCreate
from app.infrastructure.email.smtp_client import SmtpEmailClient
from app.repositories.user_repository import UserRepository


class UserService:

    def __init__(self, session: AsyncSession):
        self._repo = UserRepository(session)
        self._email = SmtpEmailClient()

    async def create_user(self, data: UserCreate) -> User:
        existing = await self._repo.get_by_username(data.username)
        if existing:
            raise UserAlreadyExistsError()

        profile = await self._repo.get_profile_by_name(data.profile_name)
        if not profile:
            from app.core.exceptions import AppException
            raise AppException(f"Perfil '{data.profile_name}' não encontrado", 400)

        # Usuário criado sem senha — admin envia a senha provisória depois
        placeholder_hash = hash_password("__NO_PASSWORD_SET__")

        user = await self._repo.create(
            username=data.username,
            password_hash=placeholder_hash,
            profile_id=str(profile.id),
            email=data.email,
        )
        return user

    async def send_provisional_password(self, user_id: str) -> None:
        user = await self._repo.get_by_id(user_id)
        if not user:
            raise UserNotFoundError()

        if not user.email:
            from app.core.exceptions import AppException
            raise AppException(
                "Usuário não possui email cadastrado para envio da senha provisória", 422
            )

        provisional = generate_provisional_password()
        await self._repo.update_password(
            user_id=user_id,
            password_hash=hash_password(provisional),
            must_change_password=True,
        )
        await self._repo.set_provisional_password_sent(
            user_id=user_id, at=datetime.now(timezone.utc)
        )

        await self._email.send_provisional_password(
            to=user.email,
            username=user.username,
            provisional_password=provisional,
        )

    async def get_user(self, user_id: str) -> User:
        user = await self._repo.get_by_id(user_id)
        if not user:
            raise UserNotFoundError()
        return user

    async def list_users(
        self, limit: int = 50, offset: int = 0
    ) -> tuple[list[User], int]:
        """Retorna (página de usuários, total de registros na tabela)."""
        users = await self._repo.list_all(limit=limit, offset=offset)
        total = await self._repo.count_all()
        return users, total
