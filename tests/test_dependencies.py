"""
Testes de app/api/v1/dependencies.py — o portão de autorização da API.

Os tokens são assinados pela chave de teste que a suíte faz passar pelo authentik
(tests/authentik_tokens.py); as contas vão para o SQLite da suíte.
"""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_current_user, require_permission
from app.domain.entities.api_client import Principal
from app.domain.entities.permission import TremPermission
from app.domain.entities.user import User
from app.infrastructure.database.connection import get_db
from tests.authentik_tokens import authentik_token

EDITOR = TremPermission.FILE_EDITOR.value
AIRLINE = TremPermission.AIRLINE_COMPANY.value


@pytest.fixture
def probe(db_session, seeded_permissions, authentik_login):
    """App mínima que expõe cada dependência numa rota, para exercitá-las via HTTP."""
    api = FastAPI()

    @api.get("/me")
    async def me(user: User = Depends(get_current_user)):
        return {"username": user.username}

    # require_permission devolve um Principal (usuário OU cliente de API),
    # por isso a identidade aqui é display_name e não username.
    @api.get("/editor")
    async def editor(principal: Principal = Depends(require_permission(EDITOR))):
        return {"username": principal.display_name}

    @api.get("/airline")
    async def airline(principal: Principal = Depends(require_permission(AIRLINE))):
        return {"username": principal.display_name}

    async def _db():
        yield db_session

    api.dependency_overrides[get_db] = _db
    return TestClient(api)


def _bearer(*groups):
    return {"Authorization": f"Bearer {authentik_token('ana', groups=groups)}"}


class TestGetCurrentUser:
    def test_valid_token_resolves_user(self, probe):
        assert probe.get("/me", headers=_bearer()).json() == {"username": "ana"}

    def test_missing_header_returns_401(self, probe):
        assert probe.get("/me").status_code == 401

    def test_malformed_token_returns_401(self, probe):
        assert probe.get("/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401

    def test_wrong_scheme_returns_401(self, probe):
        assert probe.get("/me", headers={"Authorization": f"Basic {authentik_token()}"}).status_code == 401


class TestRequirePermission:
    def test_matching_group_is_allowed(self, probe):
        assert probe.get("/editor", headers=_bearer(EDITOR)).status_code == 200

    def test_wrong_group_returns_403(self, probe):
        response = probe.get("/editor", headers=_bearer(AIRLINE))
        assert response.status_code == 403
        assert "Permissão insuficiente" in response.json()["detail"]

    def test_one_of_several_groups_is_enough(self, probe):
        headers = _bearer(AIRLINE, EDITOR)
        assert probe.get("/editor", headers=headers).status_code == 200
        assert probe.get("/airline", headers=headers).status_code == 200

    def test_account_without_groups_is_403(self, probe):
        assert probe.get("/editor", headers=_bearer()).status_code == 403

    def test_no_credential_is_401(self, probe):
        assert probe.get("/editor").status_code == 401

    def test_returns_a_callable_dependency(self):
        assert callable(require_permission(EDITOR))

    def test_each_call_builds_an_independent_dependency(self):
        assert require_permission(EDITOR) is not require_permission(EDITOR)
