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
    # Where the e-mail links (confirm sign-up, reset password, log in) open; defaults to API_URL.
    FRONTEND_URL: str = ""
    # Origins of the browser fronts that call this API directly, comma separated
    # (e.g. "https://meus-trem.netlify.app,http://localhost:3004"). Empty: no CORS at all.
    CORS_ORIGINS: str = ""

    # JWT RS256 — gere com scripts/generate_keys.py
    # Armazene as chaves PEM com \n literal no .env
    JWT_PRIVATE_KEY: str = ""
    JWT_PUBLIC_KEY: str = ""
    JWT_ALGORITHM: str = "RS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # `iss` of every token; defaults to API_URL. Other applications check it with the JWKS.
    JWT_ISSUER: str = ""
    # Retired public keys still accepted while their tokens expire, separated by "|||".
    JWT_EXTRA_PUBLIC_KEYS: str = ""

    # authentik (trem.AUTH): when set, Bearer tokens it issues to the trem client are accepted
    # next to trem.API's own. The trem provider's issuer, e.g.
    # https://auth.didilv93.com/application/o/trem/ (keys come from <issuer>jwks/).
    AUTHENTIK_ISSUER: str = ""
    AUTHENTIK_AUDIENCE: str = "trem"

    # Login throttling, in memory (the API already runs as a single process). Counted per
    # username, existing or not, so a lockout does not reveal which accounts exist.
    LOGIN_MAX_FAILURES_PER_USERNAME: int = 5
    LOGIN_MAX_FAILURES_PER_IP: int = 20
    LOGIN_FAILURE_WINDOW_SECONDS: int = 900

    # TOTP secrets are encrypted with this key (32 bytes, base64url). Without it, MFA is off,
    # and so is every admin route, which requires a login proven with a second factor.
    MFA_ENCRYPTION_KEY: str = ""
    MFA_MAX_ATTEMPTS: int = 5
    MFA_WINDOW_SECONDS: int = 300
    MFA_CHALLENGE_MINUTES: int = 5

    # Self sign-up. Admins are the active users with trem:admin; ADMIN_EMAIL (or, unset,
    # EMAIL_RECIPIENT) is notified too, which covers the time before the first admin exists.
    SIGNUP_ENABLED: bool = True
    ADMIN_EMAIL: Optional[str] = None
    EMAIL_TOKEN_TTL_HOURS: int = 24
    SIGNUP_MAX_PER_IP: int = 5
    SIGNUP_WINDOW_SECONDS: int = 3600
    # The sign-up form checks the username as it is typed (debounced in the front).
    USERNAME_CHECK_MAX_PER_IP: int = 60
    USERNAME_CHECK_WINDOW_SECONDS: int = 300
    # A reset link is a password in transit: short-lived, single use, replaced by the next one.
    PASSWORD_RESET_TTL_MINUTES: int = 60

    # New passwords are checked against Have I Been Pwned; only a 5-character SHA-1 prefix
    # leaves the server. If the service does not answer in time, the password is accepted.
    PWNED_PASSWORDS_ENABLED: bool = True
    PWNED_PASSWORDS_URL: str = "https://api.pwnedpasswords.com"
    PWNED_PASSWORDS_TIMEOUT: float = 3.0

    # Browser session: the refresh token lives in an HttpOnly cookie scoped to /auth.
    REFRESH_COOKIE_NAME: str = "trem_refresh"
    COOKIE_SECURE: bool = True

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

    def jwt_private_key_pem(self) -> str:
        return self.JWT_PRIVATE_KEY.replace("\\n", "\n")

    def jwt_public_key_pem(self) -> str:
        return self.JWT_PUBLIC_KEY.replace("\\n", "\n")

    def jwt_extra_public_key_pems(self) -> list[str]:
        return [k.replace("\\n", "\n") for k in self.JWT_EXTRA_PUBLIC_KEYS.split("|||") if k.strip()]

    def jwt_issuer(self) -> str:
        return self.JWT_ISSUER or self.API_URL

    def frontend_url(self) -> str:
        return (self.FRONTEND_URL or self.API_URL).rstrip("/")

    def cors_origins(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache()
def get_settings() -> Settings:
    return Settings()
