"""
Testes de app/api/v1/dependencies.py — o portão de autorização da API.

Aqui os tokens são reais (assinados com a chave RSA de teste) e o repositório
é mockado: é o ponto onde token, estado da conta e perfil se cruzam.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Depends, HTTPException
from fastapi.testclient import TestClient

from app.api.v1.dependencies import (
    get_current_active_user,
    get_current_user,
    require_profile,
)
from app.core.security import create_access_token, create_refresh_token
from app.domain.entities.profile import ProfileName
from app.domain.entities.user import User, UserStatus
from app.infrastructure.database.connection import get_db
from tests.conftest import make_mock_user, mock_get_db

USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def probe_app():
    """App mínima que expõe cada dependência numa rota, para exercitá-las via HTTP."""
    api = FastAPI()

    @api.get("/me")
    async def me(user: User = Depends(get_current_user)):
        return {"username": user.username}

    @api.get("/active")
    async def active(user: User = Depends(get_current_active_user)):
        return {"username": user.username}

    @api.get("/editor")
    async def editor(user: User = Depends(require_profile("file_editor"))):
        return {"username": user.username}

    @api.get("/airline")
    async def airline(user: User = Depends(require_profile("airline_company"))):
        return {"username": user.username}

    api.dependency_overrides[get_db] = mock_get_db
    return api


@pytest.fixture
def repo():
    mock = MagicMock()
    mock.get_by_id = AsyncMock(return_value=make_mock_user())
    return mock


@pytest.fixture
def probe(probe_app, repo):
    with patch("app.api.v1.dependencies.UserRepository", return_value=repo):
        yield TestClient(probe_app)


def _bearer(user_id=USER_ID, profile=ProfileName.FILE_EDITOR.value, must_change=False):
    token = create_access_token(user_id, "testuser", profile, must_change)
    return {"Authorization": f"Bearer {token}"}


class TestGetCurrentUser:
    def test_valid_token_resolves_user(self, probe, repo):
        response = probe.get("/me", headers=_bearer())
        assert response.status_code == 200
        assert response.json()["username"] == "testuser"

    def test_looks_user_up_by_token_subject(self, probe, repo):
        probe.get("/me", headers=_bearer(user_id="abc-123"))
        repo.get_by_id.assert_awaited_once_with("abc-123")

    def test_missing_header_returns_401(self, probe):
        assert probe.get("/me").status_code == 401

    def test_malformed_token_returns_401(self, probe):
        response = probe.get("/me", headers={"Authorization": "Bearer nao-e-jwt"})
        assert response.status_code == 401

    def test_wrong_scheme_returns_401(self, probe):
        response = probe.get("/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert response.status_code == 401

    def test_expired_token_returns_401(self, probe):
        from app.core.config import Settings, get_settings

        data = get_settings().model_dump()
        data["ACCESS_TOKEN_EXPIRE_MINUTES"] = -5
        with patch("app.core.security.get_settings", return_value=Settings(**data)):
            expired = create_access_token(USER_ID, "testuser", "file_editor", False)

        response = probe.get("/me", headers={"Authorization": f"Bearer {expired}"})
        assert response.status_code == 401
        assert "expirado" in response.json()["detail"].lower()

    def test_refresh_token_is_rejected(self, probe):
        """Um refresh token não pode servir de credencial de acesso."""
        raw, _ = create_refresh_token(USER_ID)
        response = probe.get("/me", headers={"Authorization": f"Bearer {raw}"})
        assert response.status_code == 401

    def test_deleted_user_returns_401(self, probe, repo):
        repo.get_by_id.return_value = None
        assert probe.get("/me", headers=_bearer()).status_code == 401

    def test_blocked_user_returns_403(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(status=UserStatus.BLOCKED.value)
        assert probe.get("/me", headers=_bearer()).status_code == 403

    def test_suspended_user_returns_403(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(status=UserStatus.SUSPENDED.value)
        assert probe.get("/me", headers=_bearer()).status_code == 403

    def test_status_comes_from_database_not_token(self, probe, repo):
        """
        O token é emitido antes do bloqueio; quem manda é o estado atual no banco.
        """
        repo.get_by_id.return_value = make_mock_user(status=UserStatus.BLOCKED.value)
        assert probe.get("/me", headers=_bearer()).status_code == 403


class TestGetCurrentActiveUser:
    def test_allows_user_without_pending_change(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(must_change_password=False)
        assert probe.get("/active", headers=_bearer()).status_code == 200

    def test_blocks_user_with_pending_change(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(must_change_password=True)
        response = probe.get("/active", headers=_bearer())
        assert response.status_code == 403
        assert "change-password" in response.json()["detail"]

    def test_pending_change_comes_from_database_not_token(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(must_change_password=True)
        response = probe.get("/active", headers=_bearer(must_change=False))
        assert response.status_code == 403


class TestRequireProfile:
    def test_matching_profile_is_allowed(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(
            profile_name=ProfileName.FILE_EDITOR.value
        )
        assert probe.get("/editor", headers=_bearer()).status_code == 200

    def test_wrong_profile_returns_403(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(
            profile_name=ProfileName.AIRLINE_COMPANY.value
        )
        response = probe.get("/editor", headers=_bearer())
        assert response.status_code == 403
        assert "Permissão insuficiente" in response.json()["detail"]

    def test_airline_profile_reaches_airline_route(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(
            profile_name=ProfileName.AIRLINE_COMPANY.value
        )
        assert probe.get("/airline", headers=_bearer()).status_code == 200

    def test_profile_comes_from_database_not_token(self, probe, repo):
        """Token forjado com profile elevado não vale — o perfil vem do banco."""
        repo.get_by_id.return_value = make_mock_user(
            profile_name=ProfileName.AIRLINE_COMPANY.value
        )
        response = probe.get(
            "/editor", headers=_bearer(profile=ProfileName.FILE_EDITOR.value)
        )
        assert response.status_code == 403

    def test_pending_password_change_blocks_profile_routes(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(must_change_password=True)
        assert probe.get("/editor", headers=_bearer()).status_code == 403

    def test_blocked_user_cannot_reach_profile_routes(self, probe, repo):
        repo.get_by_id.return_value = make_mock_user(status=UserStatus.BLOCKED.value)
        assert probe.get("/editor", headers=_bearer()).status_code == 403

    def test_returns_a_callable_dependency(self):
        assert callable(require_profile("file_editor"))

    def test_each_call_builds_an_independent_dependency(self):
        assert require_profile("file_editor") is not require_profile("file_editor")
