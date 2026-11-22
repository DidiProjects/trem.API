import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, UnknownPermissionsError
from app.domain.entities.api_client import (
    API_KEY_ENTROPY_BYTES,
    API_KEY_PREFIX,
    ApiClient,
)
from app.domain.entities.permission import TREM_APP, TremPermission
from app.domain.entities.user import User
from app.repositories.api_client_repository import ApiClientRepository
from app.repositories.permission_repository import PermissionRepository
from app.repositories.user_repository import UserRepository


# "trem_" plus four random characters: recognisable, and far too short to guess the rest from.
KEY_HINT_LENGTH = len(API_KEY_PREFIX) + 4
OWN_KEYS_LIMIT = 5


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
        self._permissions = PermissionRepository(session)

    async def create_client(
        self,
        name: str,
        permissions: list[str],
        description: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> tuple[ApiClient, str]:
        """
        Cria o cliente e devolve (cliente, chave_em_claro).

        A chave em claro só existe neste retorno — o banco guarda apenas o
        hash. Se for perdida, o caminho é revogar e criar outra.
        """
        if await self._repo.get_active_by_name(name, user_id):
            raise ApiClientAlreadyExistsError(name)

        found = await self._permissions.get_by_keys(permissions)
        missing = set(permissions) - {p.key for p in found}
        if missing:
            raise UnknownPermissionsError(missing)

        if user_id is not None:
            owner = await self._users.get_by_id(user_id)
            if not owner:
                raise AppException("Usuário dono da chave não encontrado", 400)
            # A chave nunca dá ao dono acesso que ele não tem pelo próprio login.
            beyond = set(permissions) - set(owner.permissions)
            if beyond:
                raise AppException(
                    f"Permissões fora das do usuário dono: {', '.join(sorted(beyond))}", 400
                )

        raw_key = generate_api_key()
        client = await self._repo.create(
            name=name,
            key_hash=hash_api_key(raw_key),
            permission_ids=[str(p.id) for p in found],
            description=description,
            user_id=user_id,
            key_hint=raw_key[:KEY_HINT_LENGTH],
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

        if client.user_id is not None:
            # Revalidated on every request against the owner's stored permissions,
            # which follow authentik at the owner's last call. An account disabled in
            # authentik makes no more calls, so its keys keep the last set until revoked.
            owner = await self._users.get_by_id(client.user_id)
            if not owner:
                return None
            client.permissions = sorted(set(client.permissions) & set(owner.permissions))

        await self._repo.touch_last_used(client.id, datetime.now(timezone.utc))
        return client

    # --- Keys a user manages for themselves ---

    async def create_own_key(
        self, owner: User, name: str, permissions: Optional[list[str]], description: Optional[str] = None
    ) -> tuple[ApiClient, str]:
        """
        A key of this API (trem:*), within the owner's own permissions and never admin:
        administration needs a person with a second factor, not a key.
        """
        allowed = {p for p in owner.permissions if p.startswith(f"{TREM_APP}:")} - {TremPermission.ADMIN}
        wanted = sorted(allowed) if permissions is None else permissions
        outside = set(wanted) - allowed
        if outside:
            raise AppException(f"Permissões fora das suas: {', '.join(sorted(outside))}", 400)
        if not wanted:
            raise AppException("A chave precisa de ao menos uma permissão", 400)
        if await self._repo.count_active_by_owner(owner.id) >= OWN_KEYS_LIMIT:
            raise AppException(f"Limite de {OWN_KEYS_LIMIT} chaves ativas. Revogue uma para criar outra.", 409)
        return await self.create_client(name, wanted, description=description, user_id=owner.id)

    async def list_own_keys(self, owner_id: str) -> list[ApiClient]:
        return await self._repo.list_by_owner(owner_id)

    async def revoke_own_key(self, owner_id: str, client_id: str) -> None:
        client = await self._repo.get_by_id(client_id)
        # Someone else's key answers exactly like a missing one.
        if not client or client.user_id != owner_id:
            raise ApiClientNotFoundError()
        await self._repo.revoke(client.id)

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
