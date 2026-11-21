import ipaddress
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

MAX_REDIRECT_URIS = 10

# Where plain http may stay: the code never crosses the internet unencrypted. Tailscale
# addresses are CGNAT (100.64.0.0/10). Any other host needs https, so exposing a service
# directly later only means adding its https URI.
_LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fd00::/8",
)]


def _is_local(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in _LOCAL_NETWORKS)


def _check_redirect_uri(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Redirect URI precisa começar com http:// ou https:// e ter um host")
    if parts.scheme == "http" and not _is_local(parts.hostname):
        raise ValueError("http só para endereço local ou da Tailscale; fora disso, https")
    if parts.username or parts.password:
        raise ValueError("Redirect URI não pode levar usuário ou senha")
    if parts.fragment or "#" in value:
        raise ValueError("Redirect URI não pode ter fragmento (#)")
    if any(c.isspace() for c in value):
        raise ValueError("Redirect URI não pode ter espaços")
    return value


class OidcClientConfig(BaseModel):
    redirect_uris: list[str] = Field(
        min_length=1, max_length=MAX_REDIRECT_URIS,
        description="Endereços exatos para onde o login volta, como o serviço os envia",
    )

    @field_validator("redirect_uris")
    @classmethod
    def check_redirect_uris(cls, values: list[str]) -> list[str]:
        checked = []
        for value in values:
            value = value.strip()
            if len(value) > 500:
                raise ValueError("Redirect URI passa de 500 caracteres")
            if _check_redirect_uri(value) not in checked:
                checked.append(value)
        return checked


class OidcClientResponse(BaseModel):
    client_id: str
    redirect_uris: list[str]
    secret_created_at: datetime
    created_at: datetime
    updated_at: datetime


class OidcClientSecretResponse(OidcClientResponse):
    client_secret: Optional[str] = Field(default=None, description="Só aparece ao criar ou rotacionar")
