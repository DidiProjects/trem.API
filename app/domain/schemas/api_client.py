from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.entities.permission import split_permission_key

CLIENT_NAME_PATTERN = r"^[a-zA-Z0-9_.-]+$"


class ApiClientCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        min_length=3,
        max_length=100,
        pattern=CLIENT_NAME_PATTERN,
        description="Identificador da aplicação, ex: flight-api",
    )
    permissions: list[str] = Field(
        min_length=1,
        description="Chaves \"<aplicação>:<permissão>\", ex: trem:ai",
    )
    description: Optional[str] = Field(default=None, max_length=500)
    user_id: Optional[str] = Field(
        default=None,
        description="Dono da chave. As permissões precisam estar entre as dele; "
        "sem dono, é chave de serviço",
    )

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, v: list[str]) -> list[str]:
        for key in v:
            split_permission_key(key)
        return sorted(set(v))


class ApiClientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str]
    permissions: list[str]
    user_id: Optional[str]
    revoked: bool
    last_used_at: Optional[datetime]
    created_at: datetime


class ApiClientCreatedResponse(ApiClientResponse):
    """
    Resposta da criação — único momento em que a chave existe em claro.

    O banco guarda só o hash; se a chave for perdida, revogue e crie outra.
    """

    api_key: str
    warning: str = (
        "Guarde esta chave agora: ela não pode ser recuperada depois."
    )
