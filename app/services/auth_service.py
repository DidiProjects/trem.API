from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidCredentialsError,
    PasswordChangeRequiredError,
    TokenExpiredError,
    TokenInvalidError,
    UserBlockedError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    needs_rehash,
    verify_password,
)
from app.core.config import get_settings
from app.domain.schemas.auth import TokenResponse, AccessTokenResponse
from app.repositories.user_repository import UserRepository


class AuthService:

    def __init__(self, session: AsyncSession):
        self._repo = UserRepository(session)

    async def login(
        self,
        username: str,
        password: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> TokenResponse:
        user = await self._repo.get_by_username(username)

        # Mesma exceção para usuário não encontrado e senha errada (evita enumeração)
        if not user or not verify_password(password, user.password_hash):
            raise InvalidCredentialsError()

        if user.is_blocked:
            raise UserBlockedError()

        if user.status == "suspended":
            raise UserBlockedError()

        # Rehash transparente se os parâmetros do Argon2id forem atualizados
        if needs_rehash(user.password_hash):
            await self._repo.update_password(
                user.id, hash_password(password), user.must_change_password
            )

        await self._repo.update_last_login(user.id, datetime.now(timezone.utc))

        access_token = create_access_token(
            user_id=user.id,
            username=user.username,
            profile=user.profile_name,
            must_change_password=user.must_change_password,
        )
        raw_refresh, _ = create_refresh_token(user.id)

        settings = get_settings()
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
        await self._repo.create_refresh_token(
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=raw_refresh,
            must_change_password=user.must_change_password,
        )

    async def refresh(self, raw_refresh_token: str) -> AccessTokenResponse:
        try:
            payload = decode_token(raw_refresh_token)
        except (TokenExpiredError, TokenInvalidError):
            raise InvalidCredentialsError("Refresh token inválido ou expirado")

        if payload.get("type") != "refresh":
            raise InvalidCredentialsError("Token inválido")

        token_hash = hash_token(raw_refresh_token)
        stored = await self._repo.get_refresh_token(token_hash)

        if not stored or stored.revoked:
            raise InvalidCredentialsError("Refresh token revogado")

        if stored.expires_at < datetime.now(timezone.utc):
            raise InvalidCredentialsError("Refresh token expirado")

        user = await self._repo.get_by_id(payload["sub"])
        if not user or not user.is_active:
            raise InvalidCredentialsError()

        access_token = create_access_token(
            user_id=user.id,
            username=user.username,
            profile=user.profile_name,
            must_change_password=user.must_change_password,
        )
        return AccessTokenResponse(access_token=access_token)

    async def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> None:
        user = await self._repo.get_by_id(user_id)
        if not user:
            raise InvalidCredentialsError()

        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentialsError("Senha atual incorreta")

        await self._repo.update_password(
            user_id=user_id,
            password_hash=hash_password(new_password),
            must_change_password=False,
        )
        await self._repo.revoke_all_refresh_tokens(user_id)

    async def logout(self, raw_refresh_token: str) -> None:
        token_hash = hash_token(raw_refresh_token)
        await self._repo.revoke_refresh_token(token_hash)
