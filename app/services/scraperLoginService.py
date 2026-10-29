import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional

TOKEN_TTL_SECONDS = 120  # from "Connect LinkedIn" to the viewer opening
VIEWER_TTL_SECONDS = 660  # the orchestrator gives up on a login after 10 min


@dataclass
class _Grant:
    login_id: str
    owner_id: str
    expires: float


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class LoginGrants:
    """
    Access to one remote login's viewer, for a browser that cannot send the API key.

    "Connect LinkedIn" (authenticated by key or JWT) gets a one-time token for the viewer URL.
    The viewer's first request trades it for a random cookie scoped to that login's path, and the
    files and the WebSocket check the cookie. The token lands in access logs once and is useless
    after that; only digests are kept. In memory, like the other jobs: one process.
    """

    def __init__(self):
        self._tokens: dict[str, _Grant] = {}
        self._cookies: dict[str, _Grant] = {}

    def _sweep(self) -> None:
        now = time.monotonic()
        for store in (self._tokens, self._cookies):
            for key in [k for k, g in store.items() if g.expires <= now]:
                del store[key]

    def issue(self, login_id: str, owner_id: str) -> str:
        self._sweep()
        token = secrets.token_urlsafe(32)
        self._tokens[_digest(token)] = _Grant(login_id, owner_id, time.monotonic() + TOKEN_TTL_SECONDS)
        return token

    def redeem(self, token: str, login_id: str) -> Optional[str]:
        """Trades a token for a viewer cookie, once, and only for the login it was issued for."""
        self._sweep()
        grant = self._tokens.pop(_digest(token or ""), None)
        if grant is None or grant.login_id != login_id:
            return None
        cookie = secrets.token_urlsafe(32)
        self._cookies[_digest(cookie)] = _Grant(login_id, grant.owner_id, time.monotonic() + VIEWER_TTL_SECONDS)
        return cookie

    def owner_for(self, cookie: str, login_id: str) -> Optional[str]:
        self._sweep()
        grant = self._cookies.get(_digest(cookie or ""))
        if grant is None or grant.login_id != login_id:
            return None
        return grant.owner_id


_grants = LoginGrants()


def get_login_grants() -> LoginGrants:
    return _grants
