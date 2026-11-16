import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    ApplicationAccessDeniedError,
    AwaitingApprovalError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    InvalidMfaCodeError,
    TokenExpiredError,
    TokenInvalidError,
    UserBlockedError,
)
from app.core.security import (
    MFA_AUDIENCE,
    burn_password_check,
    create_access_token,
    create_mfa_token,
    decode_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    needs_rehash,
    verify_password,
)
from app.domain.entities.permission import TREM_APP
from app.domain.entities.user import User, UserStatus
from app.domain.schemas.auth import TokenResponse
from app.repositories.permission_repository import PermissionRepository
from app.repositories.user_repository import UserRepository
from app.services.breached_password import ensure_not_breached
from app.services.login_throttle import LoginThrottle, get_login_throttle
from app.services.mfa_service import MfaService

logger = logging.getLogger(__name__)


def _has_access(user: User, application: str) -> bool:
    # trem is where every account manages its password and keys, so it never needs a grant.
    if application == TREM_APP:
        return True
    return any(p.startswith(f"{application}:") for p in user.permissions)


class AuthService:

    def __init__(self, session: AsyncSession, throttle: Optional[LoginThrottle] = None):
        self._session = session
        self._repo = UserRepository(session)
        self._permissions = PermissionRepository(session)
        self._throttle = throttle or get_login_throttle()

    async def login(
        self,
        username: str,
        password: str,
        audience: str = TREM_APP,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> tuple[TokenResponse, Optional[str]]:
        """
        Returns the response body and the raw refresh token, which the caller places. With
        TOTP on, the body is a challenge for /auth/mfa/verify and there is no refresh token.
        """
        self._throttle.check(username, ip_address)

        user = await self._repo.get_by_username(username)
        if not user:
            burn_password_check(password)
            self._throttle.record_failure(username, ip_address)
            raise InvalidCredentialsError()
        if not verify_password(password, user.password_hash):
            self._throttle.record_failure(username, ip_address)
            raise InvalidCredentialsError()
        self._throttle.reset(username)

        # The password is already proven here, so naming the state reveals nothing new.
        if user.status == UserStatus.PENDING_VERIFICATION:
            raise EmailNotVerifiedError()
        if user.status == UserStatus.PENDING_APPROVAL:
            raise AwaitingApprovalError()
        if not user.is_active:
            raise UserBlockedError()

        application = await self._permissions.get_application(audience)
        if application is None or not _has_access(user, audience):
            raise ApplicationAccessDeniedError()

        if needs_rehash(user.password_hash):
            await self._repo.update_password(
                user.id, hash_password(password), user.must_change_password
            )

        if user.totp_enabled:
            challenge = TokenResponse(
                mfa_required=True, mfa_token=create_mfa_token(user.id, audience), token_type=None
            )
            return challenge, None
        return await self._issue(user, application, ("pwd",), ip_address, user_agent)

    async def verify_mfa(
        self,
        mfa_token: str,
        code: Optional[str] = None,
        recovery_code: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> tuple[TokenResponse, str]:
        """Second step of a login with TOTP: the challenge from /auth/login plus a code."""
        try:
            claims = decode_token(mfa_token, audience=MFA_AUDIENCE)
        except (TokenExpiredError, TokenInvalidError):
            raise InvalidCredentialsError("Desafio expirado. Faça o login de novo.")
        if claims.get("type") != "mfa":
            raise InvalidCredentialsError()

        user = await self._repo.get_by_id(claims["sub"])
        if not user or not user.is_active:
            raise InvalidCredentialsError()
        application = await self._permissions.get_application(claims["app"])
        if application is None or not _has_access(user, application.slug):
            raise ApplicationAccessDeniedError()

        if not await MfaService(self._session).check(user.id, code=code, recovery_code=recovery_code):
            raise InvalidMfaCodeError()
        return await self._issue(user, application, ("pwd", "otp"), ip_address, user_agent)

    async def _issue(self, user: User, application, amr: tuple[str, ...], ip_address, user_agent):
        await self._repo.update_last_login(user.id, datetime.now(timezone.utc))
        raw_refresh = generate_refresh_token()
        await self._repo.create_refresh_token(
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            expires_at=self._refresh_expiry(),
            application_id=application.id,
            ip_address=ip_address,
            user_agent=user_agent,
            amr=" ".join(amr),
        )
        return self._response(user, application.slug, amr), raw_refresh

    async def refresh(
        self,
        raw_refresh_token: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> tuple[TokenResponse, str]:
        """
        Trades a refresh token for a new pair. The old token dies here; if it comes back,
        someone else holds a copy, and the whole login is revoked.
        """
        stored = await self._repo.get_refresh_token(hash_token(raw_refresh_token))
        if not stored:
            raise InvalidCredentialsError("Refresh token inválido")

        if stored.revoked or not await self._repo.claim_refresh_token(stored.id):
            if stored.replaced_by is not None or not stored.revoked:
                logger.warning("refresh token reuse, revoking family %s", stored.family_id)
                await self._revoke_family_now(stored.family_id)
            raise InvalidCredentialsError("Refresh token revogado")

        expires_at = stored.expires_at
        if expires_at.tzinfo is None:  # SQLite drops the zone; the column is always UTC
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise InvalidCredentialsError("Refresh token expirado")

        audience = stored.application.slug
        user = await self._repo.get_by_id(str(stored.user_id))
        if not user or not user.is_active or not _has_access(user, audience):
            await self._revoke_family_now(stored.family_id)
            raise InvalidCredentialsError()

        raw_refresh = generate_refresh_token()
        successor = await self._repo.create_refresh_token(
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            expires_at=self._refresh_expiry(),
            application_id=stored.application_id,
            family_id=stored.family_id,
            ip_address=ip_address,
            user_agent=user_agent,
            amr=stored.amr,
        )
        await self._repo.set_refresh_token_successor(stored.id, successor.id)
        return self._response(user, audience, tuple(stored.amr.split())), raw_refresh

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
        await ensure_not_breached(new_password)

        await self._repo.update_password(
            user_id=user_id,
            password_hash=hash_password(new_password),
            must_change_password=False,
        )
        await self._repo.revoke_all_refresh_tokens(user_id)

    async def logout(self, raw_refresh_token: str) -> None:
        """Ends the whole login the token belongs to, not just this token."""
        stored = await self._repo.get_refresh_token(hash_token(raw_refresh_token))
        if stored:
            await self._repo.revoke_refresh_token_family(stored.family_id)

    async def _revoke_family_now(self, family_id) -> None:
        # The request fails right after this, and a failed request rolls the session back:
        # the revocation is committed on its own so it survives the error response.
        await self._repo.revoke_refresh_token_family(family_id)
        await self._session.commit()

    def _refresh_expiry(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS)

    def _response(self, user: User, audience: str, amr: tuple[str, ...]) -> TokenResponse:
        settings = get_settings()
        access_token = create_access_token(
            user_id=user.id,
            username=user.username,
            permissions=user.permissions,
            must_change_password=user.must_change_password,
            audience=audience,
            amr=amr,
        )
        return TokenResponse(
            access_token=access_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            must_change_password=user.must_change_password,
        )
