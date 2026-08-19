"""
Testes dos endpoints de gestão de usuários: /users/*

Endpoints admin protegidos por X-API-Key (verify_api_key).
UserService é mockado para isolar da camada de banco de dados.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth_secure import verify_api_key
from app.core.exceptions import UserAlreadyExistsError, UserNotFoundError, AppException
from app.domain.entities.user import User
from app.infrastructure.database.connection import get_db
from app.main import app
from tests.conftest import mock_get_db


def _make_user(
    username: str = "testuser",
    profile_name: str = "file_editor",
    status: str = "blocked",
    email: str = "test@example.com",
) -> User:
    return User(
        id="00000000-0000-0000-0000-000000000001",
        username=username,
        email=email,
        password_hash="$argon2id$placeholder",
        profile_id="00000000-0000-0000-0000-000000000010",
        profile_name=profile_name,
        status=status,
        must_change_password=True,
        provisional_password_sent_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        last_login_at=None,
    )


async def _mock_api_key():
    return "test-api-key"


@pytest.fixture
def admin_client():
    """Client com API key válida para endpoints admin."""
    app.dependency_overrides[verify_api_key] = _mock_api_key
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def no_auth_client():
    """Client sem nenhuma autenticação."""
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /users
# ---------------------------------------------------------------------------

class TestCreateUser:
    def test_create_user_success(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.create_user = AsyncMock(return_value=_make_user())
            response = admin_client.post(
                "/users",
                json={
                    "username": "novousuario",
                    "email": "novo@example.com",
                    "profile_name": "file_editor",
                },
            )
        assert response.status_code == 201
        data = response.json()
        assert data["username"] == "testuser"
        assert data["status"] == "blocked"
        assert data["must_change_password"] is True

    def test_create_user_duplicate(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.create_user = AsyncMock(side_effect=UserAlreadyExistsError())
            response = admin_client.post(
                "/users",
                json={
                    "username": "existente",
                    "email": "existente@example.com",
                    "profile_name": "file_editor",
                },
            )
        assert response.status_code == 409

    def test_create_user_invalid_profile(self, admin_client):
        response = admin_client.post(
            "/users",
            json={
                "username": "usuario",
                "email": "usuario@example.com",
                "profile_name": "perfil_invalido",
            },
        )
        assert response.status_code == 422

    def test_create_user_short_username(self, admin_client):
        response = admin_client.post(
            "/users",
            json={"username": "ab", "profile_name": "file_editor"},
        )
        assert response.status_code == 422

    def test_create_user_invalid_email(self, admin_client):
        response = admin_client.post(
            "/users",
            json={
                "username": "usuario",
                "email": "nao-e-email",
                "profile_name": "file_editor",
            },
        )
        assert response.status_code == 422

    def test_create_user_no_api_key(self, no_auth_client):
        response = no_auth_client.post(
            "/users",
            json={"username": "usuario", "profile_name": "file_editor"},
        )
        assert response.status_code == 401

    def test_create_airline_company_user(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.create_user = AsyncMock(
                return_value=_make_user(profile_name="airline_company")
            )
            response = admin_client.post(
                "/users",
                json={"username": "aerolinea", "profile_name": "airline_company"},
            )
        assert response.status_code == 201
        assert response.json()["profile_name"] == "airline_company"


# ---------------------------------------------------------------------------
# GET /users
# ---------------------------------------------------------------------------

class TestListUsers:
    def test_list_users_success(self, admin_client):
        users = [_make_user(f"user{i}") for i in range(3)]
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.list_users = AsyncMock(return_value=(users, 3))
            response = admin_client.get("/users")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_list_users_empty(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.list_users = AsyncMock(return_value=([], 0))
            response = admin_client.get("/users")
        assert response.status_code == 200
        assert response.json()["total"] == 0

    def test_list_users_pagination(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.list_users = AsyncMock(return_value=([], 0))
            response = admin_client.get("/users?limit=10&offset=20")
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 20

    def test_list_users_total_is_table_count_not_page_size(self, admin_client):
        """total precisa refletir a tabela inteira, não o tamanho da página."""
        page = [_make_user(f"user{i}") for i in range(2)]
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.list_users = AsyncMock(return_value=(page, 57))
            response = admin_client.get("/users?limit=2&offset=0")
        data = response.json()
        assert len(data["items"]) == 2
        assert data["total"] == 57
        assert data["has_more"] is True

    def test_list_users_has_more_false_on_last_page(self, admin_client):
        page = [_make_user("user0")]
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.list_users = AsyncMock(return_value=(page, 3))
            response = admin_client.get("/users?limit=2&offset=2")
        assert response.json()["has_more"] is False

    def test_list_users_no_api_key(self, no_auth_client):
        response = no_auth_client.get("/users")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /users/{id}
# ---------------------------------------------------------------------------

class TestGetUser:
    def test_get_user_success(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.get_user = AsyncMock(return_value=_make_user())
            response = admin_client.get("/users/00000000-0000-0000-0000-000000000001")
        assert response.status_code == 200
        assert "id" in response.json()

    def test_get_user_not_found(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.get_user = AsyncMock(side_effect=UserNotFoundError())
            response = admin_client.get("/users/nao-existe")
        assert response.status_code == 404

    def test_get_user_no_api_key(self, no_auth_client):
        response = no_auth_client.get("/users/qualquer-id")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /users/{id}/send-provisional-password
# ---------------------------------------------------------------------------

class TestSendProvisionalPassword:
    def test_send_password_success(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.send_provisional_password = AsyncMock(return_value=None)
            response = admin_client.post(
                "/users/00000000-0000-0000-0000-000000000001/send-provisional-password"
            )
        assert response.status_code == 200
        assert "message" in response.json()

    def test_send_password_user_not_found(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.send_provisional_password = AsyncMock(
                side_effect=UserNotFoundError()
            )
            response = admin_client.post("/users/nao-existe/send-provisional-password")
        assert response.status_code == 404

    def test_send_password_no_email(self, admin_client):
        with patch("app.api.v1.routers.users.UserService") as Mock:
            Mock.return_value.send_provisional_password = AsyncMock(
                side_effect=AppException("Usuário não possui email cadastrado", 422)
            )
            response = admin_client.post(
                "/users/00000000-0000-0000-0000-000000000001/send-provisional-password"
            )
        assert response.status_code == 422

    def test_send_password_no_api_key(self, no_auth_client):
        response = no_auth_client.post(
            "/users/qualquer-id/send-provisional-password"
        )
        assert response.status_code == 401
