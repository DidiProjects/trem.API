"""
/me e as API keys do próprio usuário, contra o SQLite da suíte, com login real.
"""
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import hash_password
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import ApiClientModel, ApplicationModel, PermissionModel, UserModel
from app.main import app
from app.services.api_client_service import hash_api_key
from app.services.login_throttle import LoginThrottle

PASSWORD = "cavalo bateria grampo"
PASSWORD_HASH = hash_password(PASSWORD)


@pytest.fixture
async def http(db_session, seeded_permissions):
    async def grant(*keys):
        return [await db_session.get(PermissionModel, uuid.UUID(seeded_permissions[k])) for k in keys]

    financas = PermissionModel(application=ApplicationModel(slug="financas", name="F"), name="read")
    db_session.add_all([
        UserModel(username="ana", email="ana@example.com", password_hash=PASSWORD_HASH, status="active",
                  must_change_password=False,
                  permissions=await grant("trem:ai", "trem:file_editor", "trem:admin") + [financas]),
        UserModel(username="bia", email="bia@example.com", password_hash=PASSWORD_HASH, status="active",
                  must_change_password=False, permissions=await grant("trem:ai")),
        UserModel(username="nova", email="nova@example.com", password_hash=PASSWORD_HASH, status="active",
                  must_change_password=True, permissions=await grant("trem:ai")),
    ])
    await db_session.commit()

    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    throttle = LoginThrottle(per_username=50, per_ip=50, window_seconds=60)
    with patch("app.services.auth_service.get_login_throttle", return_value=throttle):
        yield TestClient(app, base_url="https://testserver")
    app.dependency_overrides.clear()


def _auth(http, username="ana"):
    token = http.post("/auth/login", json={"username": username, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestMe:
    def test_profile(self, http):
        body = http.get("/me", headers=_auth(http)).json()
        assert body["username"] == "ana"
        assert "financas:read" in body["permissions"]
        assert body["totp_enabled"] is False
        assert (body["api_keys_active"], body["api_keys_limit"]) == (0, 5)
        assert "password_hash" not in body

    def test_works_before_the_first_password_change(self, http):
        body = http.get("/me", headers=_auth(http, "nova")).json()
        assert body["must_change_password"] is True

    def test_requires_a_login(self, http):
        assert http.get("/me").status_code == 401


class TestOwnKeys:
    def test_create_shows_the_key_once(self, http, db_session):
        auth = _auth(http)
        created = http.post("/me/api-keys", json={"name": "notebook", "permissions": ["trem:ai"]}, headers=auth)

        assert created.status_code == 201
        body = created.json()
        key = body["api_key"]
        assert key.startswith("trem_") and body["key_hint"] == key[:9]
        assert body["permissions"] == ["trem:ai"]

        listed = http.get("/me/api-keys", headers=auth).json()
        assert [k["name"] for k in listed] == ["notebook"]
        assert "api_key" not in listed[0]
        assert key not in str(listed)

    async def test_only_the_hash_is_stored(self, http, db_session):
        key = http.post("/me/api-keys", json={"name": "notebook"}, headers=_auth(http)).json()["api_key"]
        stored = (await db_session.execute(select(ApiClientModel))).scalar_one()
        assert stored.key_hash == hash_api_key(key)
        assert key not in (stored.key_hash, stored.key_hint)

    def test_key_authenticates_with_its_permissions(self, http):
        key = http.post("/me/api-keys", json={"name": "so-ia", "permissions": ["trem:ai"]}, headers=_auth(http)).json()["api_key"]
        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code != 401
        assert http.post("/pdf/info", headers={"X-API-Key": key}, files={"file": ("a.pdf", b"%PDF")}).status_code == 403

    def test_default_permissions_are_the_users_trem_ones_without_admin(self, http):
        body = http.post("/me/api-keys", json={"name": "tudo"}, headers=_auth(http)).json()
        assert body["permissions"] == ["trem:ai", "trem:file_editor"]

    @pytest.mark.parametrize("permissions", [["trem:admin"], ["trem:linkedin_scraper"], ["financas:read"], []])
    def test_permissions_outside_the_allowed_set_are_400(self, http, permissions):
        response = http.post("/me/api-keys", json={"name": "x1", "permissions": permissions}, headers=_auth(http))
        assert response.status_code == 400

    def test_at_most_five_active_keys(self, http):
        auth = _auth(http)
        ids = [http.post("/me/api-keys", json={"name": f"k{i}"}, headers=auth).json()["id"] for i in range(5)]
        sixth = http.post("/me/api-keys", json={"name": "k5"}, headers=auth)
        assert sixth.status_code == 409

        assert http.delete(f"/me/api-keys/{ids[0]}", headers=auth).status_code == 200
        assert http.post("/me/api-keys", json={"name": "k5"}, headers=auth).status_code == 201

    def test_revoked_key_stops_working_and_its_name_is_free_again(self, http):
        auth = _auth(http)
        created = http.post("/me/api-keys", json={"name": "notebook"}, headers=auth).json()
        assert http.post("/me/api-keys", json={"name": "notebook"}, headers=auth).status_code == 409

        http.delete(f"/me/api-keys/{created['id']}", headers=auth)

        assert http.get("/llm/health", headers={"X-API-Key": created["api_key"]}).status_code == 401
        assert http.post("/me/api-keys", json={"name": "notebook"}, headers=auth).status_code == 201
        assert [k["revoked"] for k in http.get("/me/api-keys", headers=auth).json()] == [False, True]

    def test_two_users_can_use_the_same_name(self, http):
        assert http.post("/me/api-keys", json={"name": "notebook"}, headers=_auth(http, "ana")).status_code == 201
        assert http.post("/me/api-keys", json={"name": "notebook"}, headers=_auth(http, "bia")).status_code == 201

    def test_someone_elses_key_looks_missing(self, http):
        key_id = http.post("/me/api-keys", json={"name": "notebook"}, headers=_auth(http, "ana")).json()["id"]
        bia = _auth(http, "bia")
        assert http.delete(f"/me/api-keys/{key_id}", headers=bia).status_code == 404
        assert http.get("/me/api-keys", headers=bia).json() == []
        assert http.delete(f"/me/api-keys/{uuid.uuid4()}", headers=bia).status_code == 404

    def test_a_key_cannot_create_keys(self, http):
        key = http.post("/me/api-keys", json={"name": "notebook"}, headers=_auth(http)).json()["api_key"]
        assert http.post("/me/api-keys", json={"name": "outra"}, headers={"X-API-Key": key}).status_code == 401
        assert http.get("/me", headers={"X-API-Key": key}).status_code == 401

    def test_pending_password_change_blocks_key_management(self, http):
        assert http.post("/me/api-keys", json={"name": "x1"}, headers=_auth(http, "nova")).status_code == 403

    @pytest.mark.parametrize("name", ["", "com espaço", "x" * 51])
    def test_invalid_name_is_422(self, http, name):
        assert http.post("/me/api-keys", json={"name": name}, headers=_auth(http)).status_code == 422
