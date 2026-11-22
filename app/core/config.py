from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # App
    APP_NAME: str = "trem.API"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False

    API_URL: str = "http://localhost:3002"
    # Origins of the browser fronts that call this API directly, comma separated
    # (e.g. "https://meus-trem.netlify.app,http://localhost:3004"). Empty: no CORS at all.
    CORS_ORIGINS: str = ""

    # authentik (trem.AUTH), the only login for people: Bearer tokens it issues to the trem
    # client. The trem provider's issuer, e.g. https://auth.didilv93.com/application/o/trem/
    # (keys come from <issuer>jwks/). Empty: only API keys are accepted.
    AUTHENTIK_ISSUER: str = ""
    AUTHENTIK_AUDIENCE: str = "trem"
    # Shared with trem.AUTH's account webhook (POST /internal/authentik/accounts). Empty: the
    # route answers 404 and a disabled account's API keys keep their last permissions.
    AUTHENTIK_WEBHOOK_SECRET: str = ""

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

    # LLM (trem-llm, internal Docker network). The synchronous endpoint must be
    # reached without Cloudflare or nginx (100 s / 60 s); over the public URL,
    # use /llm/jobs. A model reload after idle sleep alone takes ~3.5 min.
    LLM_API_URL: Optional[str] = None
    LLM_API_KEY: Optional[str] = None
    LLM_TIMEOUT: int = 600
    LLM_MAX_TOKENS: int = 2048
    LLM_QUEUE_SIZE: int = 10
    LLM_MAX_REQUEST_SIZE: int = 65536
    LLM_WARMUP_SECONDS: int = 210  # measured cold reload, model file out of page cache
    LLM_JOB_TTL_SECONDS: int = 3600  # finished jobs hold personal data; poll within 1 h

    # LinkedIn scraper orchestrator (linkedinho), internal Docker network only.
    # SCRAPER_API_KEY is the orchestrator's INTERNAL_API_KEY; it trusts X-Owner-Id only with it.
    SCRAPER_API_URL: Optional[str] = None
    SCRAPER_API_KEY: Optional[str] = None
    SCRAPER_TIMEOUT: int = 30
    SCRAPER_MAX_REQUEST_SIZE: int = 3 * 1024 * 1024  # job JSON or a CV; the orchestrator caps CVs at 2 MB

    def cors_origins(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache()
def get_settings() -> Settings:
    return Settings()
