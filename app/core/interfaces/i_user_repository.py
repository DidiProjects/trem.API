from abc import ABC, abstractmethod
from typing import Optional
from datetime import datetime

from app.domain.entities.user import User


class IUserRepository(ABC):

    @abstractmethod
    async def get_by_id(self, user_id: str) -> Optional[User]:
        ...

    @abstractmethod
    async def get_by_username(self, username: str) -> Optional[User]:
        ...

    @abstractmethod
    async def create(
        self,
        username: str,
        password_hash: str,
        profile_id: str,
        email: Optional[str] = None,
    ) -> User:
        ...

    @abstractmethod
    async def update_password(
        self,
        user_id: str,
        password_hash: str,
        must_change_password: bool,
    ) -> None:
        ...

    @abstractmethod
    async def update_status(self, user_id: str, status: str) -> None:
        ...

    @abstractmethod
    async def update_last_login(self, user_id: str, at: datetime) -> None:
        ...

    @abstractmethod
    async def set_provisional_password_sent(self, user_id: str, at: datetime) -> None:
        ...

    @abstractmethod
    async def list_all(self, limit: int = 50, offset: int = 0) -> list[User]:
        ...

    @abstractmethod
    async def count_all(self) -> int:
        ...

    # --- Refresh tokens ---

    @abstractmethod
    async def create_refresh_token(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        ...

    @abstractmethod
    async def get_refresh_token(self, token_hash: str):
        ...

    @abstractmethod
    async def revoke_refresh_token(self, token_hash: str) -> None:
        ...

    @abstractmethod
    async def revoke_all_refresh_tokens(self, user_id: str) -> None:
        ...
