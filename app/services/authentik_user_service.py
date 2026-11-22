"""The trem.API user behind an authentik token, with the permissions the token carries."""
import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import TokenInvalidError
from app.domain.entities.user import User, UserStatus
from app.repositories.permission_repository import PermissionRepository
from app.repositories.user_repository import UserRepository

# Not an argon2 hash, so verify_password() never accepts anything: the password of an account
# that logs in through authentik lives only in authentik.
NO_LOCAL_PASSWORD = "!authentik"

# authentik groups named like trem.API permissions (trem:admin, hub:didiflix); the others
# ("authentik Admins") mean nothing here.
_PERMISSION = re.compile(r"^[a-z0-9_-]+:[a-z0-9_.-]+$")


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

        groups = claims.get("groups") or []
        user.permissions = sorted({g for g in groups if isinstance(g, str) and _PERMISSION.match(g)})
        await self._store(user.id, stored, user.permissions)
        amr = {m for m in claims.get("amr") or () if isinstance(m, str)}
        if amr & {"mfa", "otp"}:
            amr.add("otp")
        user.amr = tuple(sorted(amr))
        user.must_change_password = False
        return user

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
                return await self._users.create(
                    username=username,
                    password_hash=NO_LOCAL_PASSWORD,
                    permission_ids=[],
                    email=email,
                    status=UserStatus.ACTIVE.value,
                    must_change_password=False,
                )
        except IntegrityError:
            user = await self._users.get_by_username(username)
            if user is None:
                raise
            return user
