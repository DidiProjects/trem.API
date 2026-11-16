from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.domain.entities.permission import split_permission_key
from app.domain.entities.user import UserStatus

USERNAME_PATTERN = r"^[a-zA-Z0-9_.-]+$"


class UserCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(
        min_length=3,
        max_length=50,
        pattern=USERNAME_PATTERN,
        description="Letras, números, _, . e -",
    )
    email: Optional[EmailStr] = None
    permissions: list[str] = Field(
        min_length=1,
        description="Chaves \"<aplicação>:<permissão>\", ex: trem:ai",
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: Optional[str]) -> Optional[str]:
        return v.lower() if v else None

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, v: list[str]) -> list[str]:
        for key in v:
            split_permission_key(key)
        return sorted(set(v))


class UserResponse(BaseModel):
    # from_attributes permite UserResponse.model_validate(user_entity)
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: Optional[str]
    permissions: list[str]
    status: UserStatus
    must_change_password: bool
    provisional_password_sent_at: Optional[datetime]
    created_at: datetime
    last_login_at: Optional[datetime]
