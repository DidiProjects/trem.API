"""Bearer tokens issued by authentik (trem.AUTH) to the trem client."""
from functools import lru_cache

import jwt
from jwt import ExpiredSignatureError, PyJWKClient, PyJWKClientConnectionError, PyJWTError
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.exceptions import AppException, TokenExpiredError, TokenInvalidError


class AuthentikUnavailableError(AppException):
    def __init__(self):
        super().__init__("Servidor de login indisponível. Tente de novo em instantes.", status_code=503)


def is_authentik_token(token: str) -> bool:
    """Routing only, before any check: the issuer claim says which validation applies."""
    issuer = get_settings().AUTHENTIK_ISSUER
    if not issuer:
        return False
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except PyJWTError:
        return False
    return claims.get("iss") == issuer


@lru_cache
def _jwks_client(url: str) -> PyJWKClient:
    # Keys are cached for an hour; an unknown `kid` (authentik rotated) refetches once.
    # Cloudflare in front of authentik answers 403 to urllib's default "Python-urllib/3.x".
    return PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=5, headers={"User-Agent": "trem.API"})


def _decode(token: str) -> dict:
    settings = get_settings()
    key = _jwks_client(f"{settings.AUTHENTIK_ISSUER}jwks/").get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=settings.AUTHENTIK_AUDIENCE,
        issuer=settings.AUTHENTIK_ISSUER,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        leeway=30,
    )


async def decode_authentik_token(token: str) -> dict:
    try:
        # PyJWKClient fetches the keys with urllib: off the event loop.
        return await run_in_threadpool(_decode, token)
    except ExpiredSignatureError as error:
        raise TokenExpiredError() from error
    except PyJWKClientConnectionError as error:
        raise AuthentikUnavailableError() from error
    except PyJWTError as error:
        raise TokenInvalidError() from error
