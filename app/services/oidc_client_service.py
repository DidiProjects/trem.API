import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.infrastructure.database.models import OidcClientModel
from app.repositories.oidc_client_repository import OidcClientRepository
from app.services.hub_service import HubService

SECRET_ENTROPY_BYTES = 32


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def secret_matches(client: OidcClientModel, secret: str) -> bool:
    return hmac.compare_digest(client.secret_hash, hash_secret(secret))


class OidcClientService:
    """A hub service's login through trem.API. The secret is returned once and kept only as a hash."""

    def __init__(self, session: AsyncSession):
        self._hub = HubService(session)
        self._clients = OidcClientRepository(session)

    async def get(self, slug: str) -> OidcClientModel:
        client = await self._clients.for_service(await self._hub.get(slug))
        if not client:
            raise NotFoundError("Serviço sem login único configurado")
        return client

    async def configure(self, slug: str, redirect_uris: list[str]) -> tuple[OidcClientModel, Optional[str]]:
        """Creates the client with a new secret, or changes only the redirect URIs of an existing one."""
        service = await self._hub.get(slug)
        client = await self._clients.for_service(service)
        if client:
            client.redirect_uris = redirect_uris
            await self._clients.save()
            return client, None
        secret = secrets.token_urlsafe(SECRET_ENTROPY_BYTES)
        client = await self._clients.add(
            OidcClientModel(service=service, secret_hash=hash_secret(secret), redirect_uris=redirect_uris)
        )
        return client, secret

    async def rotate_secret(self, slug: str) -> tuple[OidcClientModel, str]:
        """The old secret stops working at once: the service must get the new one."""
        client = await self.get(slug)
        secret = secrets.token_urlsafe(SECRET_ENTROPY_BYTES)
        client.secret_hash = hash_secret(secret)
        client.secret_created_at = datetime.now(timezone.utc)
        await self._clients.save()
        return client, secret

    async def delete(self, slug: str) -> None:
        await self._clients.delete(await self.get(slug))
