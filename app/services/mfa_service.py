import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.config import get_settings
from app.core.exceptions import (
    InvalidCredentialsError,
    InvalidMfaCodeError,
    MfaNotConfiguredError,
    MfaStateError,
)
from app.core.security import hash_token, verify_password
from app.domain.entities.user import User
from app.repositories.mfa_repository import MfaRepository
from app.repositories.user_repository import UserRepository
from app.services.rate_limit import SlidingWindowLimiter


@lru_cache()
def get_mfa_limiter() -> SlidingWindowLimiter:
    settings = get_settings()
    return SlidingWindowLimiter(settings.MFA_MAX_ATTEMPTS, settings.MFA_WINDOW_SECONDS)


def _key() -> str:
    key = get_settings().MFA_ENCRYPTION_KEY
    if not key:
        raise MfaNotConfiguredError()
    return key


class MfaService:

    def __init__(self, session: AsyncSession, limiter: Optional[SlidingWindowLimiter] = None, clock=time.time):
        self._mfa = MfaRepository(session)
        self._users = UserRepository(session)
        self._limiter = limiter or get_mfa_limiter()
        self._clock = clock

    async def enroll(self, user: User, password: str) -> tuple[str, str]:
        """New secret, not active until a code from it is confirmed. Returns (secret, otpauth URI)."""
        key = _key()
        self._check_password(user, password)
        if user.totp_enabled:
            raise MfaStateError("Segundo fator já ativo. Desative antes de cadastrar outro.")
        secret = totp.generate_secret()
        await self._mfa.set_pending_secret(user.id, totp.encrypt_secret(secret, key, user.id))
        return secret, totp.provisioning_uri(secret, user.email or user.username)

    async def confirm(self, user: User, code: str) -> list[str]:
        """Activates the pending secret and returns the recovery codes, shown only now."""
        key = _key()
        encrypted, enabled_at, _ = await self._mfa.get_totp(user.id)
        if enabled_at is not None:
            raise MfaStateError("Segundo fator já ativo")
        if not encrypted:
            raise MfaStateError("Nenhum cadastro de segundo fator em andamento")
        self._limiter.hit(f"mfa:{user.id}")

        step = totp.verify(totp.decrypt_secret(encrypted, key, user.id), code, None, self._clock())
        if step is None:
            raise InvalidMfaCodeError()
        await self._mfa.enable(user.id, datetime.now(timezone.utc), step)
        return await self._new_recovery_codes(user.id)

    async def disable(self, user: User, password: str, code: str) -> None:
        self._check_password(user, password)
        if not user.totp_enabled:
            raise MfaStateError("Segundo fator não está ativo")
        if not await self.check(user.id, code=code if code.isdigit() else None,
                                recovery_code=None if code.isdigit() else code):
            raise InvalidMfaCodeError()
        await self._mfa.disable(user.id)
        await self._users.revoke_all_refresh_tokens(user.id)

    async def regenerate_recovery_codes(self, user: User, password: str, code: str) -> list[str]:
        self._check_password(user, password)
        if not user.totp_enabled or not await self.check(user.id, code=code):
            raise InvalidMfaCodeError()
        return await self._new_recovery_codes(user.id)

    async def remaining_recovery_codes(self, user_id: str) -> int:
        return await self._mfa.remaining_recovery_codes(user_id)

    async def check(self, user_id: str, code: Optional[str] = None, recovery_code: Optional[str] = None) -> bool:
        """
        Second factor of a login or of a sensitive change. Attempts per account are limited:
        six digits do not survive unlimited guessing.
        """
        key = _key()
        self._limiter.hit(f"mfa:{user_id}")
        encrypted, enabled_at, last_step = await self._mfa.get_totp(user_id)
        if not encrypted or enabled_at is None:
            return False
        if recovery_code:
            normalized = totp.normalize_recovery_code(recovery_code)
            return await self._mfa.use_recovery_code(user_id, hash_token(normalized), datetime.now(timezone.utc))
        step = totp.verify(totp.decrypt_secret(encrypted, key, user_id), code or "", last_step, self._clock())
        return step is not None and await self._mfa.accept_step(user_id, step)

    async def _new_recovery_codes(self, user_id: str) -> list[str]:
        codes = totp.generate_recovery_codes()
        await self._mfa.replace_recovery_codes(
            user_id, [hash_token(totp.normalize_recovery_code(c)) for c in codes]
        )
        return codes

    @staticmethod
    def _check_password(user: User, password: str) -> None:
        # Changing the second factor needs the password again: a stolen session alone must
        # not be able to swap it for the attacker's phone.
        if not verify_password(password, user.password_hash):
            raise InvalidCredentialsError("Senha incorreta")
