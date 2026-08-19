from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from app.domain.entities.api_client import ApiClient


class IApiClientRepository(ABC):

    @abstractmethod
    async def get_by_key_hash(self, key_hash: str) -> Optional[ApiClient]:
        ...

    @abstractmethod
    async def get_by_id(self, client_id: str) -> Optional[ApiClient]:
        ...

    @abstractmethod
    async def get_by_name(self, name: str) -> Optional[ApiClient]:
        ...

    @abstractmethod
    async def create(
        self,
        name: str,
        key_hash: str,
        profile_id: str,
        description: Optional[str] = None,
    ) -> ApiClient:
        ...

    @abstractmethod
    async def revoke(self, client_id: str) -> None:
        ...

    @abstractmethod
    async def touch_last_used(self, client_id: str, at: datetime) -> None:
        ...

    @abstractmethod
    async def list_all(self, limit: int = 50, offset: int = 0) -> list[ApiClient]:
        ...

    @abstractmethod
    async def count_all(self) -> int:
        ...
