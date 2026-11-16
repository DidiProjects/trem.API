from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.domain.entities.permission import APPLICATION_SLUG_PATTERN, TREM_APP

from app.domain.password_policy import PASSWORD_MAX_LENGTH, check_password
from app.domain.schemas.user import USERNAME_PATTERN


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    audience: str = Field(
        default=TREM_APP,
        pattern=APPLICATION_SLUG_PATTERN,
        description="Aplicação para a qual o token é emitido",
    )
    session: Literal["body", "cookie"] = Field(
        default="body",
        description="cookie: o refresh token vai num cookie HttpOnly, fora do alcance do JavaScript",
    )


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: int
    must_change_password: bool


class RefreshRequest(BaseModel):
    """Sem corpo, o refresh token vem do cookie da sessão do navegador."""

    model_config = ConfigDict(str_strip_whitespace=True)

    refresh_token: Optional[str] = Field(default=None, min_length=1)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return check_password(v)

    @field_validator("new_password")
    @classmethod
    def differs_from_current(cls, v: str, info) -> str:
        current = info.data.get("current_password")
        if current is not None and v == current:
            raise ValueError("A nova senha deve ser diferente da atual")
        return v


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=3, max_length=50, pattern=USERNAME_PATTERN)
    email: EmailStr
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str, info) -> str:
        return check_password(v, info.data.get("username"), info.data.get("email"))


class EmailTokenRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(min_length=20, max_length=100)


class ResendVerificationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()
