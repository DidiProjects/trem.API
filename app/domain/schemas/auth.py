from pydantic import BaseModel, ConfigDict, Field, field_validator

# Regras de senha aplicadas na troca de senha (login não valida força — só compara)
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    must_change_password: bool


class RefreshRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    refresh_token: str = Field(min_length=1)


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < PASSWORD_MIN_LENGTH:
            raise ValueError(
                f"A nova senha deve ter no mínimo {PASSWORD_MIN_LENGTH} caracteres"
            )
        if not any(c.isupper() for c in v):
            raise ValueError("A nova senha deve conter ao menos uma letra maiúscula")
        if not any(c.islower() for c in v):
            raise ValueError("A nova senha deve conter ao menos uma letra minúscula")
        if not any(c.isdigit() for c in v):
            raise ValueError("A nova senha deve conter ao menos um número")
        return v

    @field_validator("new_password")
    @classmethod
    def differs_from_current(cls, v: str, info) -> str:
        current = info.data.get("current_password")
        if current is not None and v == current:
            raise ValueError("A nova senha deve ser diferente da atual")
        return v
