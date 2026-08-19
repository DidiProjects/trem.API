from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities.profile import ProfileName

CLIENT_NAME_PATTERN = r"^[a-zA-Z0-9_.-]+$"


class ApiClientCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        min_length=3,
        max_length=100,
        pattern=CLIENT_NAME_PATTERN,
        description="Identificador da aplicação, ex: flight-api",
    )
    profile_name: ProfileName
    description: Optional[str] = Field(default=None, max_length=500)


class ApiClientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str]
    profile_name: str
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
