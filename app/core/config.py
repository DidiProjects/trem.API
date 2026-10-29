from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # App
    APP_NAME: str = "trem.API"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False

    # Legacy API key — usado para operações administrativas de bootstrap
    API_KEY: str = "change-me"
    API_URL: str = "http://localhost:3002"

    # JWT RS256 — gere com scripts/generate_keys.py
    # Armazene as chaves PEM com \n literal no .env
    JWT_PRIVATE_KEY: str = ""
    JWT_PUBLIC_KEY: str = ""
    JWT_ALGORITHM: str = "RS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # PostgreSQL (asyncpg)
    DATABASE_URL: str = "postgresql+asyncpg://trem:trem@localhost:5432/trem"

    # Arquivos
    UPLOAD_DIR: str = "/tmp/uploads"
    MAX_FILE_SIZE: int = 52428800  # 50 MB
    # Whole-request caps enforced by BodySizeLimitMiddleware; /audio and /movie
    # use the media one. Behind Cloudflare, bodies above the plan cap (100 MB on
    # Free/Pro) are rejected before reaching the app.
    MAX_REQUEST_SIZE: int = 52428800  # 50 MB
    MAX_MEDIA_REQUEST_SIZE: int = 5368709120  # 5 GB
    # Large media is uploaded in parts so each request stays under the
    # Cloudflare cap; abandoned uploads and unread results expire after the TTL.
    TRANSCRIPTION_PART_SIZE: int = 52428800  # 50 MB
    TRANSCRIPTION_TTL_SECONDS: int = 86400  # 24 h

    # Email
    EMAIL_SMTP_HOST: str = "smtp.gmail.com"
    EMAIL_SMTP_PORT: int = 587
    EMAIL_SMTP_USER: str = ""
    EMAIL_SMTP_PASSWORD: str = ""
    EMAIL_RECIPIENT: Optional[str] = None

    # API aérea (mesma rede Docker)
    AIRLINE_API_URL: Optional[str] = None
    AIRLINE_API_KEY: Optional[str] = None
    AIRLINE_API_TIMEOUT: int = 30

    # LLM (trem-llm, internal Docker network). Callers must reach /llm without
    # Cloudflare or nginx: a model reload after idle sleep alone takes ~3.5 min.
    LLM_API_URL: Optional[str] = None
    LLM_API_KEY: Optional[str] = None
    LLM_TIMEOUT: int = 600
    LLM_MAX_TOKENS: int = 2048
    LLM_QUEUE_SIZE: int = 10
    LLM_MAX_REQUEST_SIZE: int = 65536
    LLM_WARMUP_SECONDS: int = 210  # measured cold reload, model file out of page cache
    LLM_JOB_TTL_SECONDS: int = 3600  # finished jobs hold personal data; poll within 1 h

    def jwt_private_key_pem(self) -> str:
        return self.JWT_PRIVATE_KEY.replace("\\n", "\n")

    def jwt_public_key_pem(self) -> str:
        return self.JWT_PUBLIC_KEY.replace("\\n", "\n")


@lru_cache()
def get_settings() -> Settings:
    return Settings()
