"""
Testes dos endpoints de autenticação: /auth/* e /.well-known/jwks.json

As rotas são testadas com o AuthService mockado (contrato HTTP) e, na classe
TestSessionEndToEnd, contra o SQLite real da suíte: rotação do refresh, detecção
de reuso e o cookie da sessão do navegador.
"""
import uuid
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_current_user
from app.core.exceptions import (
    ApplicationAccessDeniedError,
    InvalidCredentialsError,
    TooManyLoginAttemptsError,
    UserBlockedError,
)
from app.core.security import hash_password
from app.domain.schemas.auth import TokenResponse
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import ApplicationModel, PermissionModel, UserModel
from app.main import app
from app.services.login_throttle import LoginThrottle, get_login_throttle
from tests.conftest import TEST_PUBLIC_KEY, make_mock_user, mock_get_db

CSRF = {"X-Requested-With": "trem"}


def _tokens(must_change=False):
    return TokenResponse(access_token="header.payload.sig", expires_in=900, must_change_password=must_change)


@pytest.fixture
def auth_client():
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app, base_url="https://testserver")
    app.dependency_overrides.clear()


@pytest.fixture
def authenticated_client():
    """Client com usuário autenticado (para change-password)."""
    app.dependency_overrides[get_db] = mock_get_db

    async def _user():
        return make_mock_user()
    app.dependency_overrides[get_current_user] = _user
    yield TestClient(app, base_url="https://testserver")
    app.dependency_overrides.clear()


@pytest.fixture
def must_change_client():
    """Client com usuário que precisa trocar a senha."""
    app.dependency_overrides[get_db] = mock_get_db

    async def _user():
        return make_mock_user(must_change_password=True)
    app.dependency_overrides[get_current_user] = _user
    yield TestClient(app, base_url="https://testserver")
    app.dependency_overrides.clear()


def _mock_service(**methods):
    patcher = patch("app.api.v1.routers.auth.AuthService")
    mock = patcher.start()
    for name, value in methods.items():
        setattr(mock.return_value, name, AsyncMock(**value))
    return patcher, mock


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

class TestLogin:
    def test_body_session_returns_refresh_in_body(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(return_value=(_tokens(), "raw-refresh"))
            response = auth_client.post("/auth/login", json={"username": "u", "password": "p"})
        assert response.status_code == 200
        data = response.json()
        assert data["refresh_token"] == "raw-refresh"
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 900
        assert "set-cookie" not in response.headers

    def test_cookie_session_keeps_refresh_out_of_the_body(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(return_value=(_tokens(), "raw-refresh"))
            response = auth_client.post(
                "/auth/login", json={"username": "u", "password": "p", "session": "cookie"}
            )
        assert response.status_code == 200
        assert "refresh_token" not in response.json()
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("trem_refresh=raw-refresh")
        for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/auth"):
            assert attribute.lower() in cookie.lower()

    def test_audience_is_passed_to_the_service(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(return_value=(_tokens(), "r"))
            auth_client.post("/auth/login", json={"username": "u", "password": "p", "audience": "financas"})
        assert Mock.return_value.login.await_args.kwargs["audience"] == "financas"

    @pytest.mark.parametrize("audience", ["", "Financas", "a", "fin anças"])
    def test_malformed_audience_is_422(self, auth_client, audience):
        response = auth_client.post("/auth/login", json={"username": "u", "password": "p", "audience": audience})
        assert response.status_code == 422

    def test_ip_comes_from_cloudflare_not_from_x_forwarded_for(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(return_value=(_tokens(), "r"))
            auth_client.post(
                "/auth/login",
                json={"username": "u", "password": "p"},
                headers={"X-Forwarded-For": "6.6.6.6", "CF-Connecting-IP": "200.1.2.3"},
            )
        assert Mock.return_value.login.await_args.kwargs["ip_address"] == "200.1.2.3"

    def test_must_change_password(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(return_value=(_tokens(must_change=True), "r"))
            response = auth_client.post("/auth/login", json={"username": "u", "password": "p"})
        assert response.json()["must_change_password"] is True

    @pytest.mark.parametrize("error,status", [
        (InvalidCredentialsError(), 401),
        (UserBlockedError(), 403),
        (ApplicationAccessDeniedError(), 403),
    ])
    def test_service_errors(self, auth_client, error, status):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(side_effect=error)
            response = auth_client.post("/auth/login", json={"username": "u", "password": "p"})
        assert response.status_code == status

    def test_throttled_login_is_429_with_retry_after(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(side_effect=TooManyLoginAttemptsError(120))
            response = auth_client.post("/auth/login", json={"username": "u", "password": "p"})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "120"

    @pytest.mark.parametrize("body", [
        {"username": "", "password": "p"},
        {"username": "u", "password": ""},
        {},
        {"username": "u", "password": "p", "session": "localstorage"},
    ])
    def test_invalid_body_is_422(self, auth_client, body):
        assert auth_client.post("/auth/login", json=body).status_code == 422


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_body_token_gets_a_new_pair_in_the_body(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.refresh = AsyncMock(return_value=(_tokens(), "next-refresh"))
            response = auth_client.post("/auth/refresh", json={"refresh_token": "old"})
        assert response.status_code == 200
        assert response.json()["refresh_token"] == "next-refresh"
        assert Mock.return_value.refresh.await_args.args[0] == "old"

    def test_cookie_token_is_rotated_in_the_cookie(self, auth_client):
        auth_client.cookies.set("trem_refresh", "old")
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.refresh = AsyncMock(return_value=(_tokens(), "next-refresh"))
            response = auth_client.post("/auth/refresh", headers=CSRF)
        assert response.status_code == 200
        assert "refresh_token" not in response.json()
        assert response.headers["set-cookie"].startswith("trem_refresh=next-refresh")

    def test_cookie_without_csrf_header_is_403(self, auth_client):
        auth_client.cookies.set("trem_refresh", "old")
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.refresh = AsyncMock(return_value=(_tokens(), "n"))
            response = auth_client.post("/auth/refresh")
        assert response.status_code == 403
        Mock.return_value.refresh.assert_not_awaited()

    def test_no_token_at_all_is_401(self, auth_client):
        assert auth_client.post("/auth/refresh", headers=CSRF).status_code == 401

    def test_rejected_cookie_is_cleared(self, auth_client):
        auth_client.cookies.set("trem_refresh", "old")
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.refresh = AsyncMock(side_effect=InvalidCredentialsError())
            response = auth_client.post("/auth/refresh", headers=CSRF)
        assert response.status_code == 401
        assert 'trem_refresh=""' in response.headers["set-cookie"] or "Max-Age=0" in response.headers["set-cookie"]

    def test_invalid_body_token_is_401(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.refresh = AsyncMock(side_effect=InvalidCredentialsError())
            response = auth_client.post("/auth/refresh", json={"refresh_token": "x"})
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /auth/change-password
# ---------------------------------------------------------------------------

class TestChangePassword:
    def test_change_password_success(self, authenticated_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.change_password = AsyncMock(return_value=None)
            response = authenticated_client.post(
                "/auth/change-password",
                json={"current_password": "Senha@123", "new_password": "NovaSenha@456"},
            )
        assert response.status_code == 200
        assert "message" in response.json()

    def test_change_password_wrong_current(self, authenticated_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.change_password = AsyncMock(
                side_effect=InvalidCredentialsError("Senha atual incorreta")
            )
            response = authenticated_client.post(
                "/auth/change-password",
                json={"current_password": "errada", "new_password": "NovaSenha@456"},
            )
        assert response.status_code == 401

    def test_change_password_weak_new_password(self, authenticated_client):
        """Pydantic deve rejeitar senha fraca antes de chamar o serviço."""
        response = authenticated_client.post(
            "/auth/change-password",
            json={"current_password": "Senha@123", "new_password": "fraca"},
        )
        assert response.status_code == 422

    def test_change_password_below_twelve_chars(self, authenticated_client):
        response = authenticated_client.post(
            "/auth/change-password",
            json={"current_password": "Senha@123", "new_password": "Nova@Senha1"},
        )
        assert response.status_code == 422

    def test_change_password_requires_auth(self, auth_client):
        """Sem token → 401."""
        response = auth_client.post(
            "/auth/change-password",
            json={"current_password": "Senha@123", "new_password": "NovaSenha@456"},
        )
        assert response.status_code == 401

    def test_change_password_allowed_with_must_change_flag(self, must_change_client):
        """
        /auth/change-password deve aceitar usuários com must_change_password=True.
        É o único endpoint desbloqueado para esse estado.
        """
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.change_password = AsyncMock(return_value=None)
            response = must_change_client.post(
                "/auth/change-password",
                json={"current_password": "Provisoria@1", "new_password": "NovaSenha@456"},
            )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------

class TestLogout:
    def test_body_token(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.logout = AsyncMock(return_value=None)
            response = auth_client.post("/auth/logout", json={"refresh_token": "r"})
        assert response.status_code == 200
        Mock.return_value.logout.assert_awaited_once_with("r")

    def test_cookie_token_is_cleared(self, auth_client):
        auth_client.cookies.set("trem_refresh", "r")
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.logout = AsyncMock(return_value=None)
            response = auth_client.post("/auth/logout", headers=CSRF)
        assert response.status_code == 200
        assert "trem_refresh=" in response.headers["set-cookie"]

    def test_cookie_without_csrf_header_is_403(self, auth_client):
        auth_client.cookies.set("trem_refresh", "r")
        assert auth_client.post("/auth/logout").status_code == 403


# ---------------------------------------------------------------------------
# GET /.well-known/jwks.json
# ---------------------------------------------------------------------------

class TestJwks:
    def test_publishes_the_signing_key(self, auth_client):
        response = auth_client.get("/.well-known/jwks.json")
        assert response.status_code == 200
        (key,) = response.json()["keys"]
        assert key["kty"] == "RSA" and key["alg"] == "RS256" and key["use"] == "sig"
        assert "d" not in key and "p" not in key  # never the private part

    def test_tokens_verify_with_the_published_key_alone(self, auth_client):
        from app.core.security import create_access_token

        token = create_access_token("u-1", "u", ["financas:read", "trem:ai"], False, audience="financas")
        jwk = auth_client.get("/.well-known/jwks.json").json()["keys"][0]
        assert pyjwt.get_unverified_header(token)["kid"] == jwk["kid"]

        public_key = pyjwt.PyJWK(jwk).key
        claims = pyjwt.decode(token, public_key, algorithms=["RS256"], audience="financas", issuer="http://testserver")
        assert claims["perms"] == ["financas:read"]


# ---------------------------------------------------------------------------
# Fluxo real contra o SQLite
# ---------------------------------------------------------------------------

PASSWORD = "SenhaValida@123"
PASSWORD_HASH = hash_password(PASSWORD)


class TestSessionEndToEnd:
    @pytest.fixture
    async def http(self, db_session, seeded_permissions):
        trem = await db_session.get(PermissionModel, uuid.UUID(seeded_permissions["trem:ai"]))
        financas = ApplicationModel(slug="financas", name="Finanças")
        read = PermissionModel(application=financas, name="read")
        db_session.add_all([
            UserModel(username="ana", password_hash=PASSWORD_HASH, status="active",
                      must_change_password=False, permissions=[trem, read]),
            UserModel(username="bia", password_hash=PASSWORD_HASH, status="active",
                      must_change_password=False, permissions=[trem]),
        ])
        await db_session.commit()

        async def _db():
            yield db_session
            await db_session.commit()

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_login_throttle] = lambda: None
        throttle = LoginThrottle(per_username=3, per_ip=100, window_seconds=60)
        with patch("app.services.auth_service.get_login_throttle", return_value=throttle):
            yield TestClient(app, base_url="https://testserver")
        app.dependency_overrides.clear()

    def _login(self, http, **extra):
        return http.post("/auth/login", json={"username": "ana", "password": PASSWORD, **extra})

    def test_access_token_opens_the_api(self, http):
        token = self._login(http).json()["access_token"]
        assert http.get("/llm/health", headers={"Authorization": f"Bearer {token}"}).status_code != 401

    def test_rotation_issues_a_new_refresh_and_kills_the_old_one(self, http):
        first = self._login(http).json()["refresh_token"]
        second = http.post("/auth/refresh", json={"refresh_token": first}).json()["refresh_token"]
        assert second != first
        assert http.post("/auth/refresh", json={"refresh_token": second}).status_code == 200

    def test_reuse_of_a_rotated_token_revokes_the_whole_login(self, http):
        first = self._login(http).json()["refresh_token"]
        second = http.post("/auth/refresh", json={"refresh_token": first}).json()["refresh_token"]

        assert http.post("/auth/refresh", json={"refresh_token": first}).status_code == 401
        # The legitimate holder of the newest token is logged out too.
        assert http.post("/auth/refresh", json={"refresh_token": second}).status_code == 401

    def test_other_logins_survive_a_reuse(self, http):
        a = self._login(http).json()["refresh_token"]
        b = self._login(http).json()["refresh_token"]
        http.post("/auth/refresh", json={"refresh_token": a})
        http.post("/auth/refresh", json={"refresh_token": a})
        assert http.post("/auth/refresh", json={"refresh_token": b}).status_code == 200

    def test_logout_ends_the_login(self, http):
        refresh = self._login(http).json()["refresh_token"]
        assert http.post("/auth/logout", json={"refresh_token": refresh}).status_code == 200
        assert http.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 401

    def test_browser_session_lives_in_the_cookie(self, http):
        assert self._login(http, session="cookie").status_code == 200
        assert http.cookies.get("trem_refresh")
        old = http.cookies.get("trem_refresh")

        refreshed = http.post("/auth/refresh", headers=CSRF)
        assert refreshed.status_code == 200
        assert http.cookies.get("trem_refresh") != old

        assert http.post("/auth/logout", headers=CSRF).status_code == 200
        assert http.post("/auth/refresh", headers=CSRF).status_code == 401

    def test_token_for_another_application(self, http):
        body = self._login(http, audience="financas").json()
        claims = pyjwt.decode(body["access_token"], TEST_PUBLIC_KEY, algorithms=["RS256"],
                              audience="financas", issuer="http://testserver")
        assert claims["perms"] == ["financas:read"]
        refreshed = http.post("/auth/refresh", json={"refresh_token": body["refresh_token"]}).json()
        assert pyjwt.decode(refreshed["access_token"], TEST_PUBLIC_KEY, algorithms=["RS256"],
                            audience="financas", issuer="http://testserver")["aud"] == "financas"

    def test_token_for_another_application_does_not_open_this_api(self, http):
        token = self._login(http, audience="financas").json()["access_token"]
        assert http.get("/llm/health", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_application_without_grants_is_refused(self, http):
        response = http.post("/auth/login", json={"username": "bia", "password": PASSWORD, "audience": "financas"})
        assert response.status_code == 403

    def test_unknown_application_is_refused(self, http):
        assert self._login(http, audience="nao-existe").status_code == 403

    def test_repeated_failures_lock_the_username_even_if_it_does_not_exist(self, http):
        for username in ("ana", "fantasma"):
            for _ in range(3):
                http.post("/auth/login", json={"username": username, "password": "errada"})
            response = http.post("/auth/login", json={"username": username, "password": PASSWORD})
            assert response.status_code == 429
            assert int(response.headers["Retry-After"]) > 0
