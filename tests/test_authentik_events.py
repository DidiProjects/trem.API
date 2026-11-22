"""POST /internal/authentik/accounts: authentik tells trem.API an account changed."""
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.infrastructure.database.connection import get_db
from app.main import app
from app.services.api_client_service import ApiClientService
from tests.authentik_tokens import authentik_token

SECRET = "test-webhook-secret"
HOOK = "/internal/authentik/accounts"


@pytest.fixture
async def ctx(db_session, seeded_permissions, authentik_login, monkeypatch):
    monkeypatch.setattr(get_settings(), "AUTHENTIK_WEBHOOK_SECRET", SECRET)

    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    http = TestClient(app, base_url="https://testserver")
    ana = {"Authorization": f"Bearer {authentik_token('ana', groups=['trem:ai', 'trem:file_editor'])}"}
    key = http.post("/me/api-keys", json={"name": "script", "permissions": ["trem:ai"]}, headers=ana).json()["api_key"]
    yield http, db_session, key
    app.dependency_overrides.clear()


def _hook(http, body, secret=SECRET):
    return http.post(HOOK, json=body, headers={"Authorization": f"Bearer {secret}"})


async def _key_permissions(session, key):
    return (await ApiClientService(session).authenticate(key)).permissions


class TestAccountEvents:
    async def test_a_disabled_account_stops_its_keys_at_once(self, ctx):
        http, session, key = ctx
        assert await _key_permissions(session, key) == ["trem:ai"]

        response = _hook(http, {"username": "ana", "active": False, "groups": ["trem:ai"]})

        assert response.status_code == 200 and response.json() == {"known": True, "permissions": []}
        assert await _key_permissions(session, key) == []

    async def test_enabled_again_the_keys_follow_its_groups(self, ctx):
        http, session, key = ctx
        _hook(http, {"username": "ana", "active": False})

        response = _hook(http, {"username": "ana", "active": True, "groups": ["trem:ai", "authentik Admins"]})

        assert response.json() == {"known": True, "permissions": ["trem:ai"]}
        assert await _key_permissions(session, key) == ["trem:ai"]

    def test_an_account_trem_never_saw_is_left_alone(self, ctx):
        http, _, _ = ctx
        assert _hook(http, {"username": "ninguem", "active": False}).json() == {"known": False, "permissions": []}

    def test_a_wrong_secret_is_refused(self, ctx):
        http, _, _ = ctx
        assert _hook(http, {"username": "ana", "active": False}, secret="outro").status_code == 401
        assert http.post(HOOK, json={"username": "ana", "active": False}).status_code == 401

    def test_without_the_setting_the_route_does_not_exist(self, ctx, monkeypatch):
        http, _, _ = ctx
        monkeypatch.setattr(get_settings(), "AUTHENTIK_WEBHOOK_SECRET", "")
        assert _hook(http, {"username": "ana", "active": False}).status_code == 404

    def test_is_hidden_from_the_schema(self):
        assert HOOK not in app.openapi()["paths"]
