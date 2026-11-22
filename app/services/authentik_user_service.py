"""The trem.API user behind an authentik token, with the permissions the token carries."""
import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import TokenInvalidError
from app.domain.entities.user import User
from app.repositories.permission_repository import PermissionRepository
from app.repositories.user_repository import UserRepository

# authentik groups named like trem.API permissions (trem:admin, hub:didiflix); the others
# ("authentik Admins") mean nothing here.
_PERMISSION = re.compile(r"^[a-z0-9_-]+:[a-z0-9_.-]+$")


def _permissions_of(groups) -> list[str]:
    return sorted({g for g in groups or () if isinstance(g, str) and _PERMISSION.match(g)})


class AuthentikUserService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._users = UserRepository(session)
        self._permissions = PermissionRepository(session)

    async def resolve(self, claims: dict) -> User:
        """
        Linked by username (authentik's preferred_username), created on its first call.

        Permissions come from the token's `groups`, not from the database: authentik decides
        them, and the token lives 10 minutes. A second factor (amr "mfa") counts as the "otp"
        that administration requires.
        """
        username = claims.get("preferred_username")
        if not isinstance(username, str) or not username.strip():
            raise TokenInvalidError()
        user = await self._users.get_by_username(username) or await self._create(username, claims.get("email"))
        stored = set(user.permissions)

        user.permissions = _permissions_of(claims.get("groups"))
        await self._store(user.id, stored, user.permissions)
        amr = {m for m in claims.get("amr") or () if isinstance(m, str)}
        if amr & {"mfa", "otp"}:
            amr.add("otp")
        user.amr = tuple(sorted(amr))
        return user

    async def sync_account(self, username: str, active: bool, groups) -> list[str] | None:
        """
        authentik changed an account (trem.AUTH account-events.yaml). Disabled: its stored
        permissions go, so its personal API keys stop at once; enabled again, they follow its
        groups. An account trem.API never saw is left alone. Returns what is now stored.
        """
        user = await self._users.get_by_username(username)
        if user is None:
            return None
        wanted = _permissions_of(groups) if active else []
        await self._store(user.id, set(user.permissions), wanted)
        return sorted((await self._users.get_by_id(user.id)).permissions)

    async def _store(self, user_id, stored: set[str], wanted: list[str]) -> None:
        """
        The token's groups also go to the database, as of this call: a personal API key is
        capped by its owner's stored permissions and has no token to read them from.
        Groups that name no registered permission are left out.
        """
        if set(wanted) == stored:
            return
        found = await self._permissions.get_by_keys(wanted)
        if {p.key for p in found} == stored:
            return
        await self._users.replace_permissions(user_id, [p.id for p in found], granted_by=None)

    async def _create(self, username: str, email) -> User:
        if not isinstance(email, str) or not email or await self._users.get_by_email(email):
            email = None
        try:
            # Savepoint: two first calls at once both try to create; the loser reads the winner.
            async with self._session.begin_nested():
                return await self._users.create(username=username, email=email)
        except IntegrityError:
            user = await self._users.get_by_username(username)
            if user is None:
                raise
            return user
