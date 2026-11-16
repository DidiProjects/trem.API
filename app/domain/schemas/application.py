from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities.permission import APPLICATION_SLUG_PATTERN, PERMISSION_NAME_PATTERN


class ApplicationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(pattern=APPLICATION_SLUG_PATTERN, description="Vira o `aud` dos tokens")
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)


class PermissionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(pattern=PERMISSION_NAME_PATTERN)
    description: Optional[str] = Field(default=None, max_length=500)


class PermissionResponse(BaseModel):
    key: str
    name: str
    description: Optional[str]


class ApplicationResponse(BaseModel):
    slug: str
    name: str
    description: Optional[str]
    created_at: datetime
    permissions: list[PermissionResponse] = []
