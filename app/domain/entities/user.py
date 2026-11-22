from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class User:
    """A person logged in through authentik, which keeps the password, second factor and
    whether the account may enter. Here: who it is and what it may do."""

    id: str
    username: str
    email: Optional[str]
    permissions: list[str]  # keys "<application>:<permission>"
    created_at: datetime
    updated_at: datetime
    # Filled from the access token that authenticated this request: how the login was proven.
    amr: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return "trem:admin" in self.permissions

    def has_permission(self, key: str) -> bool:
        return key in self.permissions
