"""Access tokens as authentik (trem.AUTH) signs them, for tests that go through the real login."""
import time
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClientConnectionError

ISSUER = "https://auth.example/application/o/trem/"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def authentik_token(username: str = "ana", groups=(), amr=("pwd",), key=KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": "trem", "sub": f"hashed-{username}", "iat": now, "exp": now + 600,
        "preferred_username": username, "email": f"{username}@example.com",
        "amr": list(amr), "groups": list(groups),
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def admin_token(username: str = "chefe", groups=("trem:admin",)) -> str:
    """trem:admin with the second factor, as administration requires."""
    return authentik_token(username, groups=groups, amr=("pwd", "mfa"))


class FakeJwks:
    """Stands for authentik's JWKS endpoint: always the test key."""

    fail = False

    def get_signing_key_from_jwt(self, _token):
        if self.fail:
            raise PyJWKClientConnectionError("down")
        return SimpleNamespace(key=KEY.public_key())
