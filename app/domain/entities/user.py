from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional


class UserStatus(StrEnum):
    """
    Estados possíveis de uma conta.

    Herda de StrEnum: `UserStatus.ACTIVE == "active"` é verdadeiro, então
    comparações e serializações com strings soltas continuam funcionando.
    """

    BLOCKED = "blocked"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    # Self sign-up: the e-mail is confirmed first, then an admin approves the account.
    PENDING_VERIFICATION = "pending_verification"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"


@dataclass
class User:
    id: str
    username: str
    email: Optional[str]
    password_hash: str
    permissions: list[str]  # keys "<application>:<permission>"
    status: str  # UserStatus
    must_change_password: bool
    provisional_password_sent_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime]
    email_verified_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    totp_enabled: bool = False
    # Filled from the access token that authenticated this request: how the login was proven.
    amr: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return "trem:admin" in self.permissions

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE

    @property
    def is_blocked(self) -> bool:
        return self.status == UserStatus.BLOCKED

    @property
    def is_suspended(self) -> bool:
        return self.status == UserStatus.SUSPENDED

    @property
    def can_authenticate(self) -> bool:
        """Só contas ativas podem obter tokens."""
        return self.is_active

    def has_permission(self, key: str) -> bool:
        return key in self.permissions

    def __repr__(self) -> str:  # nunca exponha o password_hash em log/traceback
        return (
            f"User(id={self.id!r}, username={self.username!r}, "
            f"permissions={self.permissions!r}, status={self.status!r})"
        )
