"""
Testes dos endpoints de autenticação: /auth/*

Estratégia: AuthService é mockado via unittest.mock.patch para isolar
os testes da camada de banco de dados.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_current_user
from app.core.exceptions import InvalidCredentialsError, UserBlockedError
from app.domain.schemas.auth import AccessTokenResponse, TokenResponse
from app.infrastructure.database.connection import get_db
from app.main import app
from tests.conftest import make_mock_user, mock_get_db


_MOCK_TOKEN_RESPONSE = TokenResponse(
    access_token="header.payload.sig",
    refresh_token="header.payload.refresh",
    must_change_password=False,
)

_MOCK_TOKEN_MUST_CHANGE = TokenResponse(
    access_token="header.payload.sig",
    refresh_token="header.payload.refresh",
    must_change_password=True,
)

_MOCK_ACCESS_RESPONSE = AccessTokenResponse(access_token="header.payload.new")


@pytest.fixture
def auth_client():
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def authenticated_client():
    """Client com usuário autenticado (para change-password e logout)."""
    app.dependency_overrides[get_db] = mock_get_db
    async def _user():
        return make_mock_user()
    app.dependency_overrides[get_current_user] = _user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def must_change_client():
    """Client com usuário que precisa trocar a senha."""
    app.dependency_overrides[get_db] = mock_get_db

    async def _user():
        return make_mock_user(must_change_password=True)
    app.dependency_overrides[get_current_user] = _user
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

class TestLogin:
    def test_login_success(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(return_value=_MOCK_TOKEN_RESPONSE)
            response = auth_client.post(
                "/auth/login", json={"username": "testuser", "password": "senha123"}
            )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["must_change_password"] is False

    def test_login_must_change_password(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(return_value=_MOCK_TOKEN_MUST_CHANGE)
            response = auth_client.post(
                "/auth/login", json={"username": "testuser", "password": "provisoria"}
            )
        assert response.status_code == 200
        assert response.json()["must_change_password"] is True

    def test_login_wrong_credentials(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(side_effect=InvalidCredentialsError())
            response = auth_client.post(
                "/auth/login", json={"username": "testuser", "password": "errada"}
            )
        assert response.status_code == 401

    def test_login_blocked_user(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.login = AsyncMock(side_effect=UserBlockedError())
            response = auth_client.post(
                "/auth/login", json={"username": "testuser", "password": "senha123"}
            )
        assert response.status_code == 403

    def test_login_empty_username(self, auth_client):
        response = auth_client.post(
            "/auth/login", json={"username": "", "password": "senha123"}
        )
        assert response.status_code == 422

    def test_login_empty_password(self, auth_client):
        response = auth_client.post(
            "/auth/login", json={"username": "testuser", "password": ""}
        )
        assert response.status_code == 422

    def test_login_missing_body(self, auth_client):
        response = auth_client.post("/auth/login", json={})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_refresh_success(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.refresh = AsyncMock(return_value=_MOCK_ACCESS_RESPONSE)
            response = auth_client.post(
                "/auth/refresh", json={"refresh_token": "header.payload.refresh"}
            )
        assert response.status_code == 200
        assert "access_token" in response.json()
        assert response.json()["token_type"] == "bearer"

    def test_refresh_invalid_token(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.refresh = AsyncMock(side_effect=InvalidCredentialsError())
            response = auth_client.post(
                "/auth/refresh", json={"refresh_token": "token.invalido"}
            )
        assert response.status_code == 401

    def test_refresh_missing_token(self, auth_client):
        response = auth_client.post("/auth/refresh", json={})
        assert response.status_code == 422


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

    def test_change_password_no_uppercase(self, authenticated_client):
        response = authenticated_client.post(
            "/auth/change-password",
            json={"current_password": "Senha@123", "new_password": "somin%sculas123"},
        )
        assert response.status_code == 422

    def test_change_password_no_digit(self, authenticated_client):
        response = authenticated_client.post(
            "/auth/change-password",
            json={"current_password": "Senha@123", "new_password": "SemNumerosAqui"},
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
    def test_logout_success(self, authenticated_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.logout = AsyncMock(return_value=None)
            response = authenticated_client.post(
                "/auth/logout", json={"refresh_token": "header.payload.refresh"}
            )
        assert response.status_code == 200
        assert "message" in response.json()

    def test_logout_requires_auth(self, auth_client):
        with patch("app.api.v1.routers.auth.AuthService") as Mock:
            Mock.return_value.logout = AsyncMock(return_value=None)
            response = auth_client.post(
                "/auth/logout", json={"refresh_token": "header.payload.refresh"}
            )
        assert response.status_code == 401
