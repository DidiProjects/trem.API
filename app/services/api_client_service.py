import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.domain.entities.api_client import (
    API_KEY_ENTROPY_BYTES,
    API_KEY_PREFIX,
    ApiClient,
)
from app.repositories.api_client_repository import ApiClientRepository
from app.repositories.user_repository import UserRepository


class ApiClientAlreadyExistsError(AppException):
    def __init__(self, name: str):
        super().__init__(f"Já existe um cliente com o nome '{name}'", status_code=409)


class ApiClientNotFoundError(AppException):
    def __init__(self):
        super().__init__("Cliente não encontrado", status_code=404)


def generate_api_key() -> str:
    """
    Gera uma chave com 256 bits de entropia, prefixada para identificação.

    O prefixo é público: serve para reconhecer a origem em logs e para que
    scanners de segredo detectem vazamento em repositórios.
    """
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(API_KEY_ENTROPY_BYTES)}"


def hash_api_key(raw_key: str) -> str:
    """
    SHA-256 da chave inteira.

    Deliberadamente não é Argon2id: a chave é aleatória de 256 bits, então não
    existe ataque de dicionário a encarecer, e um hash determinístico é o que
    permite localizar o cliente por índice — com salt seria preciso varrer a
    tabela verificando linha a linha a cada request.
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


class ApiClientService:

    def __init__(self, session: AsyncSession):
        self._repo = ApiClientRepository(session)
        self._users = UserRepository(session)

    async def create_client(
        self,
        name: str,
        profile_name: str,
        description: Optional[str] = None,
    ) -> tuple[ApiClient, str]:
        """
        Cria o cliente e devolve (cliente, chave_em_claro).

        A chave em claro só existe neste retorno — o banco guarda apenas o
        hash. Se for perdida, o caminho é revogar e criar outra.
        """
        if await self._repo.get_by_name(name):
            raise ApiClientAlreadyExistsError(name)

        profile = await self._users.get_profile_by_name(profile_name)
        if not profile:
            raise AppException(f"Perfil '{profile_name}' não encontrado", 400)

        raw_key = generate_api_key()
        client = await self._repo.create(
            name=name,
            key_hash=hash_api_key(raw_key),
            profile_id=str(profile.id),
            description=description,
        )
        return client, raw_key

    async def authenticate(self, raw_key: str) -> Optional[ApiClient]:
        """
        Resolve uma chave crua em cliente ativo, ou None.

        Também registra o uso — é o que permite auditar qual cliente chamou o
        quê e identificar chaves esquecidas.
        """
        if not raw_key:
            return None

        client = await self._repo.get_by_key_hash(hash_api_key(raw_key))
        if not client or client.revoked:
            return None

        await self._repo.touch_last_used(client.id, datetime.now(timezone.utc))
        return client

    async def revoke_client(self, client_id: str) -> None:
        if not await self._repo.get_by_id(client_id):
            raise ApiClientNotFoundError()
        await self._repo.revoke(client_id)

    async def get_client(self, client_id: str) -> ApiClient:
        client = await self._repo.get_by_id(client_id)
        if not client:
            raise ApiClientNotFoundError()
        return client

    async def list_clients(
        self, limit: int = 50, offset: int = 0
    ) -> tuple[list[ApiClient], int]:
        clients = await self._repo.list_all(limit=limit, offset=offset)
        total = await self._repo.count_all()
        return clients, total
