from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.entities.permission import PERMISSION_NAME_PATTERN

SERVICE_SLUG_PATTERN = PERMISSION_NAME_PATTERN


def _check_url(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Endereço precisa começar com http:// ou https:// e ter um host")
    if parts.username or parts.password:
        raise ValueError("Endereço não pode levar usuário ou senha")
    if any(c.isspace() for c in value):
        raise ValueError("Endereço não pode ter espaços")
    return value


class HubServiceCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(pattern=SERVICE_SLUG_PATTERN, description="Vira a permissão hub:<slug>; não muda depois")
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    url: Optional[str] = Field(default=None, max_length=500, description="http(s)://host[:porta][/caminho]")
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        return _check_url(v)


class HubServiceUpdate(BaseModel):
    """Only the fields sent change; null clears description and url."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    url: Optional[str] = Field(default=None, max_length=500)
    enabled: Optional[bool] = None

    @field_validator("url")
    @classmethod
    def check_url(cls, v: Optional[str]) -> Optional[str]:
        return _check_url(v)


class HubServiceOrder(BaseModel):
    slugs: list[str] = Field(description="Todos os serviços, na ordem do catálogo")


class HubServiceResponse(BaseModel):
    """What the catalog shows to an account allowed to open the service."""

    slug: str
    name: str
    description: Optional[str]
    url: str
    logo_url: Optional[str] = Field(description="Caminho na API; muda quando o logo muda")


class HubServiceAdminResponse(BaseModel):
    slug: str
    permission: str
    name: str
    description: Optional[str]
    url: Optional[str]
    enabled: bool
    position: int
    logo_url: Optional[str]
    created_at: datetime
    updated_at: datetime
