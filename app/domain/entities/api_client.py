from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional

# Prefixo visível da chave. Serve para (a) reconhecer a origem em log/suporte e
# (b) permitir que scanners de segredo (GitHub secret scanning) detectem
# vazamentos por padrão. Não é secreto.
API_KEY_PREFIX = "trem_"

# 32 bytes = 256 bits de entropia. É o que dispensa hashing lento (ver
# ApiClientService.generate_key).
API_KEY_ENTROPY_BYTES = 32


class PrincipalKind(StrEnum):
    """Quem está por trás da requisição."""

    USER = "user"
    CLIENT = "client"


@dataclass
class ApiClient:
    """Uma aplicação consumidora — outra API, um script, o playground."""

    id: str
    name: str
    description: Optional[str]
    profile_names: list[str]
    revoked: bool
    last_used_at: Optional[datetime]
    created_at: datetime
    # Usuário dono da chave; None para chave de serviço, sem pessoa por trás.
    user_id: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return not self.revoked

    def __repr__(self) -> str:  # nunca inclua key_hash aqui
        return (
            f"ApiClient(id={self.id!r}, name={self.name!r}, "
            f"profiles={self.profile_names!r}, revoked={self.revoked!r})"
        )


@dataclass
class Principal:
    """
    Identidade normalizada de quem chamou, seja usuário (JWT) ou cliente
    (API key). É o que `require_profile` inspeciona, para que as rotas não
    precisem saber qual das duas autenticações foi usada.
    """

    id: str
    display_name: str
    profile_names: frozenset[str]
    kind: PrincipalKind
    # Só se aplica a usuários; cliente nunca tem senha para trocar.
    must_change_password: bool = False
    # A pessoa por trás da chamada: o próprio usuário, ou o dono da chave.
    # Recursos pessoais (jobs de scraping) pertencem a ela, não à chave, para
    # sobreviverem à rotação da chave e aparecerem também no login por JWT.
    user_id: Optional[str] = None

    @property
    def is_user(self) -> bool:
        return self.kind == PrincipalKind.USER

    @property
    def is_client(self) -> bool:
        return self.kind == PrincipalKind.CLIENT

    def has_profile(self, profile_name: str) -> bool:
        return profile_name in self.profile_names

    @classmethod
    def from_user(cls, user) -> "Principal":
        return cls(
            id=user.id,
            display_name=user.username,
            profile_names=frozenset(user.profile_names),
            kind=PrincipalKind.USER,
            must_change_password=user.must_change_password,
            user_id=user.id,
        )

    @classmethod
    def from_client(cls, client: ApiClient) -> "Principal":
        return cls(
            id=client.id,
            display_name=client.name,
            profile_names=frozenset(client.profile_names),
            kind=PrincipalKind.CLIENT,
            must_change_password=False,
            user_id=client.user_id,
        )
