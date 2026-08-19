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


@dataclass
class User:
    id: str
    username: str
    email: Optional[str]
    password_hash: str
    profile_id: str
    profile_name: str
    status: str  # UserStatus
    must_change_password: bool
    provisional_password_sent_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime]

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

    def has_profile(self, profile_name: str) -> bool:
        return self.profile_name == profile_name

    def __repr__(self) -> str:  # nunca exponha o password_hash em log/traceback
        return (
            f"User(id={self.id!r}, username={self.username!r}, "
            f"profile={self.profile_name!r}, status={self.status!r})"
        )
