from abc import ABC, abstractmethod
from typing import Optional

from app.domain.entities.user import User


class IUserRepository(ABC):

    @abstractmethod
    async def get_by_id(self, user_id: str) -> Optional[User]:
        ...

    @abstractmethod
    async def get_by_username(self, username: str) -> Optional[User]:
        ...

    @abstractmethod
    async def get_by_email(self, email: str) -> Optional[User]:
        ...

    @abstractmethod
    async def create(self, username: str, email: Optional[str] = None) -> User:
        ...

    @abstractmethod
    async def replace_permissions(self, user_id: str, permission_ids: list, granted_by: Optional[str]) -> None:
        ...
