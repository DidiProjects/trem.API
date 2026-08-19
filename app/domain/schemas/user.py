from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.domain.entities.profile import ProfileName
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
    profile_name: ProfileName

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: Optional[str]) -> Optional[str]:
        return v.lower() if v else None


class UserResponse(BaseModel):
    # from_attributes permite UserResponse.model_validate(user_entity)
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: Optional[str]
    profile_name: str
    status: UserStatus
    must_change_password: bool
    provisional_password_sent_at: Optional[datetime]
    created_at: datetime
    last_login_at: Optional[datetime]
