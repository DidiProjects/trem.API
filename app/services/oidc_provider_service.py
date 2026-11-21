"""
trem.API as an OpenID Connect provider for the hub services: authorization code + PKCE (S256),
confidential clients only. The login screen is trem.FRONT's: /oauth/authorize stores the
request and sends the browser there, and the front approves it with its own access token,
because trem.API's refresh cookie is partitioned under the front's site.
"""
import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError
from app.core.security import OIDC_AUDIENCE, decode_token, sign_jwt
from app.domain.entities.user import User
from app.infrastructure.database.models import OIDC_CLIENT_PREFIX, OidcClientModel, OidcGrantModel
from app.repositories.hub_repository import HubRepository
from app.repositories.oidc_client_repository import OidcClientRepository
from app.repositories.oidc_grant_repository import OidcGrantRepository
from app.repositories.user_repository import UserRepository
from app.services.hub_service import permission_key
from app.services.oidc_client_service import secret_matches

REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(seconds=60)
ID_TOKEN_TTL = timedelta(minutes=5)
SUPPORTED_SCOPES = ("openid", "profile", "email")
ADMIN_GROUP = "admin"
# RFC 7636: the verifier is 43 to 128 characters of this alphabet.
_VERIFIER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    """SQLite gives back naive datetimes; they were stored in UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")


def _redirect(uri: str, params: dict) -> str:
    separator = "&" if "?" in uri else "?"
    return f"{uri}{separator}{urlencode({k: v for k, v in params.items() if v is not None})}"


class OAuthError(Exception):
    """An error in the shape of RFC 6749 §5.2."""

    def __init__(self, error: str, description: str, status_code: int = 400):
        self.error = error
        self.description = description
        self.status_code = status_code
        super().__init__(description)


class InvalidClientRequest(Exception):
    """No known client or no registered redirect URI: the request is never redirected anywhere."""

    def __init__(self, error: str):
        self.error = error
        super().__init__(error)


class GrantNotFound(Exception):
    """Unknown, expired or already answered: the front asks to start the login again."""


class NotAllowed(Exception):
    """The account may not open this service: no hub:<slug>, or the service is off."""


@dataclass
class AuthorizationRequest:
    client_id: Optional[str] = None
    redirect_uri: Optional[str] = None
    response_type: Optional[str] = None
    scope: Optional[str] = None
    state: Optional[str] = None
    nonce: Optional[str] = None
    code_challenge: Optional[str] = None
    code_challenge_method: Optional[str] = None


@dataclass
class TokenRequest:
    grant_type: Optional[str] = None
    code: Optional[str] = None
    redirect_uri: Optional[str] = None
    code_verifier: Optional[str] = None


def discovery() -> dict:
    settings = get_settings()
    base = settings.API_URL.rstrip("/")
    return {
        "issuer": settings.jwt_issuer(),
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "userinfo_endpoint": f"{base}/oauth/userinfo",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": [settings.JWT_ALGORITHM],
        "scopes_supported": list(SUPPORTED_SCOPES),
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        "claims_supported": [
            "sub", "iss", "aud", "exp", "iat", "nonce", "amr", "azp",
            "preferred_username", "name", "email", "email_verified", "groups",
        ],
        "authorization_response_iss_parameter_supported": True,
    }


def claims(user: User, scopes: list[str]) -> dict:
    found: dict = {}
    if "profile" in scopes:
        found["preferred_username"] = user.username
        found["name"] = user.username
        # Who is admin is decided in trem.API; the service maps this group to its own admin.
        found["groups"] = [ADMIN_GROUP] if user.is_admin else []
    if "email" in scopes and user.email:
        found["email"] = user.email
        found["email_verified"] = user.email_verified_at is not None
    return found


class OidcProviderService:

    def __init__(self, session: AsyncSession):
        self._clients = OidcClientRepository(session)
        self._grants = OidcGrantRepository(session)
        self._hub = HubRepository(session)
        self._users = UserRepository(session)

    async def _client(self, client_id: Optional[str]) -> Optional[OidcClientModel]:
        if not client_id or not client_id.startswith(OIDC_CLIENT_PREFIX):
            return None
        service = await self._hub.get(client_id[len(OIDC_CLIENT_PREFIX):])
        return await self._clients.for_service(service) if service else None

    @staticmethod
    def _may_open(client: OidcClientModel, user: User) -> bool:
        service = client.service
        return user.is_active and service.enabled and permission_key(service.slug) in user.permissions

    # --- authorization -------------------------------------------------------------------

    async def authorize(self, request: AuthorizationRequest) -> str:
        """
        Where the browser goes next: trem.FRONT's login screen, or back to the service with
        an error. Unknown client or redirect URI raise InvalidClientRequest instead.
        """
        client = await self._client(request.client_id)
        if client is None:
            raise InvalidClientRequest("invalid_client")
        # Exact match only: a prefix match turns the login into an open redirect.
        if not request.redirect_uri or request.redirect_uri not in client.redirect_uris:
            raise InvalidClientRequest("invalid_redirect_uri")

        def back(error: str, description: str) -> str:
            return _redirect(request.redirect_uri, {
                "error": error, "error_description": description, "state": request.state,
                "iss": get_settings().jwt_issuer(),
            })

        if request.response_type != "code":
            return back("unsupported_response_type", "Only response_type=code is supported")
        scopes = (request.scope or "").split()
        if "openid" not in scopes:
            return back("invalid_scope", "The openid scope is required")
        if not request.state or len(request.state) > 500:
            return back("invalid_request", "state is required, up to 500 characters")
        if request.nonce is not None and len(request.nonce) > 500:
            return back("invalid_request", "nonce is longer than 500 characters")
        if request.code_challenge_method != "S256" or not request.code_challenge \
                or not 43 <= len(request.code_challenge) <= 128:
            return back("invalid_request", "PKCE with code_challenge_method=S256 is required")
        if not client.service.enabled:
            return back("unauthorized_client", "This service is turned off")

        now = _now()
        await self._grants.purge_expired(now)
        raw = secrets.token_urlsafe(32)
        await self._grants.add(OidcGrantModel(
            client=client,
            request_hash=_hash(raw),
            redirect_uri=request.redirect_uri,
            scope=" ".join(s for s in SUPPORTED_SCOPES if s in scopes),
            state=request.state,
            nonce=request.nonce,
            code_challenge=request.code_challenge,
            expires_at=now + REQUEST_TTL,
        ))
        return f"{get_settings().frontend_url()}/sso?{urlencode({'request': raw})}"

    async def _pending(self, raw: str) -> OidcGrantModel:
        grant = await self._grants.by_request(_hash(raw))
        if grant is None or grant.user_id is not None or _aware(grant.expires_at) <= _now():
            raise GrantNotFound()
        return grant

    async def view(self, raw: str, user: User) -> tuple[OidcGrantModel, bool]:
        """The pending request for the front's screen, and whether this account may go on."""
        grant = await self._pending(raw)
        return grant, self._may_open(grant.client, user)

    async def approve(self, raw: str, user: User) -> str:
        grant = await self._pending(raw)
        if not self._may_open(grant.client, user):
            raise NotAllowed()
        code = secrets.token_urlsafe(32)
        grant.user_id = uuid.UUID(user.id)
        grant.amr = list(user.amr)
        grant.code_hash = _hash(code)
        grant.code_expires_at = _now() + CODE_TTL
        await self._grants.save()
        return _redirect(grant.redirect_uri, {"code": code, "state": grant.state, "iss": get_settings().jwt_issuer()})

    async def deny(self, raw: str) -> str:
        grant = await self._pending(raw)
        target = _redirect(grant.redirect_uri, {
            "error": "access_denied", "error_description": "The login was cancelled",
            "state": grant.state, "iss": get_settings().jwt_issuer(),
        })
        await self._grants.delete(grant)
        return target

    # --- token ---------------------------------------------------------------------------

    async def authenticate_client(self, client_id: Optional[str], secret: Optional[str]) -> OidcClientModel:
        client = await self._client(client_id)
        if client is None or not secret or not secret_matches(client, secret):
            raise OAuthError("invalid_client", "Client authentication failed", 401)
        return client

    async def exchange(self, client: OidcClientModel, request: TokenRequest) -> dict:
        if request.grant_type != "authorization_code":
            raise OAuthError("unsupported_grant_type", "Only authorization_code is supported")
        if not request.code:
            raise OAuthError("invalid_request", "code is required")
        invalid = OAuthError("invalid_grant", "The code is invalid, expired or already used")
        grant = await self._grants.by_code(_hash(request.code))
        if grant is None or grant.oidc_client_id != client.id or grant.used_at is not None \
                or grant.code_expires_at is None or _aware(grant.code_expires_at) <= _now():
            raise invalid
        # Spent before the other checks: a leaked code is worth one attempt, not many.
        grant.used_at = _now()
        await self._grants.save()
        if request.redirect_uri != grant.redirect_uri:
            raise invalid
        verifier = request.code_verifier or ""
        if not 43 <= len(verifier) <= 128 or not set(verifier) <= _VERIFIER_CHARS \
                or not hmac.compare_digest(s256(verifier), grant.code_challenge):
            raise invalid
        user = await self._users.get_by_id(str(grant.user_id))
        if user is None or not self._may_open(client, user):
            raise invalid
        return self._tokens(client, grant, user)

    @staticmethod
    def _tokens(client: OidcClientModel, grant: OidcGrantModel, user: User) -> dict:
        settings = get_settings()
        now = _now()
        access_ttl = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        id_token = {
            "iss": settings.jwt_issuer(),
            "sub": user.id,
            "aud": client.client_id,
            "azp": client.client_id,
            "iat": now,
            "exp": now + ID_TOKEN_TTL,
            "amr": grant.amr or [],
            "type": "id",
            **claims(user, grant.scope.split()),
        }
        if grant.nonce is not None:
            id_token["nonce"] = grant.nonce
        access_token = {
            "iss": settings.jwt_issuer(),
            "sub": user.id,
            "aud": OIDC_AUDIENCE,
            "azp": client.client_id,
            "scope": grant.scope,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + access_ttl,
            "type": "oidc_access",
        }
        return {
            "access_token": sign_jwt(access_token),
            "token_type": "Bearer",
            "expires_in": int(access_ttl.total_seconds()),
            "id_token": sign_jwt(id_token),
            "scope": grant.scope,
        }

    # --- userinfo ------------------------------------------------------------------------

    async def userinfo(self, token: Optional[str]) -> dict:
        invalid = OAuthError("invalid_token", "The access token is invalid or expired", 401)
        if not token:
            raise invalid
        try:
            payload = decode_token(token, audience=OIDC_AUDIENCE)
        except (TokenExpiredError, TokenInvalidError):
            raise invalid
        if payload.get("type") != "oidc_access":
            raise invalid
        client = await self._client(payload.get("azp"))
        user = await self._users.get_by_id(payload["sub"])
        if client is None or user is None or not self._may_open(client, user):
            raise invalid
        return {"sub": user.id, **claims(user, str(payload.get("scope", "")).split())}
