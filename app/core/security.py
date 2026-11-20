import base64
import hashlib
import json
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import serialization
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from jwt import ExpiredSignatureError, PyJWTError

from app.core.config import get_settings
from app.core.exceptions import InvalidCredentialsError, TokenExpiredError, TokenInvalidError
from app.domain.entities.permission import TREM_APP

# Argon2id — parâmetros OWASP (memory=64MB, 3 iterações, 4 threads)
_ph = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_dummy_hash: str | None = None


def burn_password_check(plain: str) -> None:
    """
    Runs a full Argon2id verification against a throwaway hash, so a login for an
    unknown username costs the same time as one with a wrong password.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(plain, _dummy_hash)


def needs_rehash(hashed: str) -> bool:
    return _ph.check_needs_rehash(hashed)


def generate_provisional_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.isupper() for c in pwd)
            and any(c.islower() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%" for c in pwd)
        ):
            return pwd


def _load_public_key(pem: str):
    return serialization.load_pem_public_key(pem.encode())


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@lru_cache(maxsize=8)
def _jwk(pem: str) -> tuple:
    return tuple(_build_jwk(pem).items())


def public_jwk(pem: str) -> dict:
    return dict(_jwk(pem))


def _build_jwk(pem: str) -> dict:
    numbers = _load_public_key(pem).public_numbers()
    jwk = {"kty": "RSA", "e": _b64url_uint(numbers.e), "n": _b64url_uint(numbers.n)}
    # RFC 7638 thumbprint: the kid is derived from the key, so it cannot drift from it.
    canonical = json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()
    jwk["kid"] = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).decode().rstrip("=")
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


def _verification_keys() -> dict[str, str]:
    """kid -> PEM for the signing key plus retired keys still accepted during a rotation."""
    settings = get_settings()
    pems = [settings.jwt_public_key_pem(), *settings.jwt_extra_public_key_pems()]
    return {public_jwk(pem)["kid"]: pem for pem in pems if pem.strip()}


def jwks() -> dict:
    return {"keys": [public_jwk(pem) for pem in _verification_keys().values()]}


def create_access_token(
    user_id: str,
    username: str,
    permissions: list[str],
    must_change_password: bool,
    audience: str = TREM_APP,
    amr: tuple[str, ...] = ("pwd",),
) -> str:
    """
    Access token for one application. It carries only that application's permissions:
    a token issued for one interface must not open another.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    prefix = f"{audience}:"
    payload = {
        "iss": settings.jwt_issuer(),
        "aud": audience,
        "sub": user_id,
        "jti": uuid.uuid4().hex,
        "username": username,
        "perms": sorted(p for p in permissions if p.startswith(prefix)),
        "amr": list(amr),
        "must_change_password": must_change_password,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "access",
    }
    kid = public_jwk(settings.jwt_public_key_pem())["kid"]
    return jwt.encode(
        payload,
        settings.jwt_private_key_pem(),
        algorithm=settings.JWT_ALGORITHM,
        headers={"kid": kid},
    )


def sign_jwt(payload: dict) -> str:
    """Signs with the current key and names it in the header, so a rotation does not break verification."""
    settings = get_settings()
    kid = public_jwk(settings.jwt_public_key_pem())["kid"]
    return jwt.encode(payload, settings.jwt_private_key_pem(), algorithm=settings.JWT_ALGORITHM, headers={"kid": kid})


# Audience of the access tokens handed to OIDC clients: not a valid application slug, so
# trem.API's own routes and the applications never accept one.
OIDC_AUDIENCE = "trem#oidc"


# Not a valid application slug, so no application can ever be issued a token with it.
MFA_AUDIENCE = "trem#mfa"


def create_mfa_token(user_id: str, audience: str) -> str:
    """
    Proof that the password was right, valid for a few minutes and only for the second
    step of that login: it opens no route, and it remembers which application asked.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "iss": settings.jwt_issuer(),
        "aud": MFA_AUDIENCE,
        "sub": user_id,
        "app": audience,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(minutes=settings.MFA_CHALLENGE_MINUTES),
        "type": "mfa",
    }
    kid = public_jwk(settings.jwt_public_key_pem())["kid"]
    return jwt.encode(payload, settings.jwt_private_key_pem(), algorithm=settings.JWT_ALGORITHM, headers={"kid": kid})


def generate_refresh_token() -> str:
    """Opaque 256-bit token: only its SHA-256 is stored, and it means nothing outside the database."""
    return secrets.token_urlsafe(32)


def decode_token(token: str, audience: str = TREM_APP) -> dict:
    settings = get_settings()
    keys = _verification_keys()
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        pem = keys.get(kid)
        if pem is None:
            raise TokenInvalidError()
        return jwt.decode(
            token,
            pem,
            algorithms=[settings.JWT_ALGORITHM],
            audience=audience,
            issuer=settings.jwt_issuer(),
            options={"require": ["exp", "iat", "sub", "type", "aud", "iss"]},
        )
    except ExpiredSignatureError:
        raise TokenExpiredError()
    except PyJWTError:
        raise TokenInvalidError()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
