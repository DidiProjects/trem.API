from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import InvalidEmailTokenError, WeakPasswordError
from app.core.security import generate_refresh_token, hash_password, hash_token
from app.domain.entities.user import User, UserStatus
from app.domain.password_policy import check_password
from app.repositories.email_token_repository import RESET_PASSWORD, EmailTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.breached_password import ensure_not_breached
from app.services.login_throttle import get_login_throttle
from app.services.rate_limit import SlidingWindowLimiter
from app.services.signup_service import Outbox, get_signup_limiter


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class PasswordResetService:
    """
    "Forgot password" by e-mail. The request answers the same whether the address exists
    or not; the link goes out after the response, so timing does not tell either.

    A reset proves control of the mailbox, not of the second factor: an account with TOTP
    still needs the code (or a recovery code) to log in afterwards.
    """

    def __init__(self, session: AsyncSession, email_client, limiter: SlidingWindowLimiter | None = None):
        self._users = UserRepository(session)
        self._tokens = EmailTokenRepository(session)
        self._email = email_client
        self._limiter = limiter or get_signup_limiter()
        self.outbox: Outbox = []

    async def request(self, email: str, ip: str) -> None:
        email = email.strip().lower()
        self._limiter.hit(f"forgot:{ip}")
        self._limiter.hit(f"forgot:{email}")

        user = await self._users.get_by_email(email)
        # Pending sign-ups confirm the e-mail through their own link; suspended or rejected
        # accounts must not get a way back in without the admin.
        if not user or user.status != UserStatus.ACTIVE:
            return

        settings = get_settings()
        now = _utcnow()
        raw = generate_refresh_token()
        await self._tokens.invalidate(user.id, RESET_PASSWORD, now)
        await self._tokens.create(
            user_id=user.id,
            purpose=RESET_PASSWORD,
            token_hash=hash_token(raw),
            expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES),
        )
        # In the fragment, the token never reaches a server log or a Referer header.
        link = f"{settings.API_URL.rstrip('/')}/#reset-password={raw}"
        self._queue(
            self._email.send_password_reset,
            to=user.email, username=user.username, link=link,
            ttl_minutes=settings.PASSWORD_RESET_TTL_MINUTES,
        )

    async def reset(self, raw_token: str, new_password: str) -> User:
        now = _utcnow()
        token = await self._tokens.get(hash_token(raw_token), RESET_PASSWORD)
        if not token or token.used_at or _aware(token.expires_at) < now:
            raise InvalidEmailTokenError()

        user = await self._users.get_by_id(str(token.user_id))
        if not user or user.status != UserStatus.ACTIVE:
            raise InvalidEmailTokenError()
        try:
            check_password(new_password, user.username, user.email)
        except ValueError as e:
            # Checked before the token is spent: a rejected password does not burn the link.
            raise WeakPasswordError(str(e))
        await ensure_not_breached(new_password)

        if not await self._tokens.use(token.id, now):
            raise InvalidEmailTokenError()
        await self._tokens.invalidate(user.id, RESET_PASSWORD, now)
        await self._users.update_password(user.id, hash_password(new_password), must_change_password=False)
        # Whoever knew the old password, or held a session, is out.
        await self._users.revoke_all_refresh_tokens(user.id)
        get_login_throttle().reset(user.username)

        self._queue(self._email.send_password_changed, to=user.email, username=user.username)
        return user

    def _queue(self, send, **kwargs) -> None:
        self.outbox.append((send, kwargs))
