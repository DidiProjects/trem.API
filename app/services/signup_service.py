import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import InvalidEmailTokenError, SignupDisabledError
from app.core.security import generate_refresh_token, hash_password, hash_token
from app.domain.entities.permission import TremPermission
from app.domain.entities.user import User, UserStatus
from app.repositories.email_token_repository import VERIFY_EMAIL, EmailTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

# E-mails go out after the response, from a background task: the answer to a sign-up
# must not depend on (or take longer because of) whether the account already existed.
Outbox = list[tuple[Callable[..., Awaitable[Any]], dict]]


@lru_cache()
def get_signup_limiter() -> SlidingWindowLimiter:
    settings = get_settings()
    return SlidingWindowLimiter(settings.SIGNUP_MAX_PER_IP, settings.SIGNUP_WINDOW_SECONDS)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SignupService:

    def __init__(self, session: AsyncSession, email_client, limiter: SlidingWindowLimiter | None = None):
        self._users = UserRepository(session)
        self._tokens = EmailTokenRepository(session)
        self._email = email_client
        self._limiter = limiter or get_signup_limiter()
        self.outbox: Outbox = []

    async def register(self, username: str, email: str, password: str, ip: str) -> None:
        """
        Same answer whatever happens. A taken e-mail or username is told only to the
        owner of the e-mail typed in, never to whoever is calling the API.
        """
        settings = get_settings()
        if not settings.SIGNUP_ENABLED:
            raise SignupDisabledError()
        self._limiter.hit(f"signup:{ip}")
        email = email.strip().lower()

        # Hashed before looking anything up, so both paths cost the same Argon2id time.
        password_hash = hash_password(password)

        by_email = await self._free_if_stale(await self._users.get_by_email(email))
        if by_email:
            self._queue(self._email.send_signup_conflict, to=email, reason="email", login_url=settings.API_URL)
            return
        if await self._free_if_stale(await self._users.get_by_username(username)):
            self._queue(self._email.send_signup_conflict, to=email, reason="username", login_url=settings.API_URL)
            return

        user = await self._users.create(
            username=username,
            password_hash=password_hash,
            permission_ids=[],
            email=email,
            status=UserStatus.PENDING_VERIFICATION.value,
            must_change_password=False,
        )
        await self._issue_verification(user)

    async def resend_verification(self, email: str, ip: str) -> None:
        email = email.strip().lower()
        self._limiter.hit(f"resend:{ip}")
        self._limiter.hit(f"resend:{email}")
        user = await self._users.get_by_email(email)
        if user and user.status == UserStatus.PENDING_VERIFICATION:
            await self._issue_verification(user)

    async def verify_email(self, raw_token: str) -> None:
        now = _utcnow()
        token = await self._tokens.get(hash_token(raw_token), VERIFY_EMAIL)
        if not token or token.used_at or _aware(token.expires_at) < now:
            raise InvalidEmailTokenError()
        if not await self._tokens.use(token.id, now):
            raise InvalidEmailTokenError()

        user = await self._users.get_by_id(str(token.user_id))
        if not user:
            raise InvalidEmailTokenError()
        if user.status != UserStatus.PENDING_VERIFICATION:
            return  # already confirmed, or decided by an admin in the meantime

        await self._users.mark_email_verified(user.id, now, UserStatus.PENDING_APPROVAL.value)
        await self._notify_admins(user)

    async def _issue_verification(self, user: User) -> None:
        settings = get_settings()
        now = _utcnow()
        raw = generate_refresh_token()
        await self._tokens.invalidate(user.id, VERIFY_EMAIL, now)
        await self._tokens.create(
            user_id=user.id,
            purpose=VERIFY_EMAIL,
            token_hash=hash_token(raw),
            expires_at=now + timedelta(hours=settings.EMAIL_TOKEN_TTL_HOURS),
        )
        # In the fragment, the token never reaches a server log or a Referer header.
        link = f"{settings.API_URL.rstrip('/')}/#verify-email={raw}"
        self._queue(
            self._email.send_email_verification,
            to=user.email, username=user.username, link=link, ttl_hours=settings.EMAIL_TOKEN_TTL_HOURS,
        )

    async def _free_if_stale(self, user: User | None) -> User | None:
        """
        An address nobody confirmed in time does not keep a username or an e-mail hostage:
        the abandoned account is dropped and the name can be taken again.
        """
        if user is None or user.status != UserStatus.PENDING_VERIFICATION:
            return user
        ttl = timedelta(hours=get_settings().EMAIL_TOKEN_TTL_HOURS)
        if _aware(user.created_at) + ttl < _utcnow():
            await self._users.delete(user.id)
            return None
        return user

    async def _notify_admins(self, user: User) -> None:
        settings = get_settings()
        recipients = set(await self._users.emails_with_permission(TremPermission.ADMIN))
        fallback = settings.ADMIN_EMAIL or settings.EMAIL_RECIPIENT
        if fallback:
            recipients.add(fallback)
        if not recipients:
            logger.warning("sign-up %s awaits approval but no admin e-mail is configured", user.id)
        for to in sorted(recipients):
            self._queue(
                self._email.send_signup_pending_approval,
                to=to, username=user.username, email=user.email, user_id=user.id,
            )

    def _queue(self, send, **kwargs) -> None:
        self.outbox.append((send, kwargs))


async def deliver(outbox: Outbox) -> None:
    """Runs after the response; a failed e-mail is logged, never surfaced to the caller."""
    for send, kwargs in outbox:
        try:
            await send(**kwargs)
        except Exception:
            logger.exception("e-mail delivery failed (%s)", getattr(send, "__name__", send))
