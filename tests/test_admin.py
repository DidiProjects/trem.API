"""
API de administração (/admin/users, /admin/applications) contra o SQLite da suíte,
autenticada com um access token real de admin com segundo fator.
"""
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import PermissionModel, UserModel, user_permissions
from app.main import app
from app.services.login_throttle import LoginThrottle

PASSWORD = "cavalo bateria grampo"
PASSWORD_HASH = hash_password(PASSWORD)


@pytest.fixture
async def ctx(db_session, seeded_permissions):
    admin_permission = await db_session.get(PermissionModel, uuid.UUID(seeded_permissions["trem:admin"]))
    admin = UserModel(username="chefe", email="chefe@example.com", password_hash=PASSWORD_HASH,
                      status="active", must_change_password=False, permissions=[admin_permission])
    db_session.add(admin)
    await db_session.commit()

    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    token = create_access_token(str(admin.id), "chefe", ["trem:admin"], False, amr=("pwd", "otp"))
    throttle = LoginThrottle(per_username=50, per_ip=50, window_seconds=60)
    with patch("app.services.auth_service.get_login_throttle", return_value=throttle):
        http = TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {token}"})
        yield http, db_session, str(admin.id)
    app.dependency_overrides.clear()


async def _pending(db_session, username="joana", status="pending_approval"):
    user = UserModel(username=username, email=f"{username}@example.com", password_hash=PASSWORD_HASH,
                     status=status, must_change_password=False)
    db_session.add(user)
    await db_session.commit()
    return str(user.id)


def _login(http, username="joana", **extra):
    return http.post("/auth/login", json={"username": username, "password": PASSWORD, **extra},
                     headers={"Authorization": ""})


class TestApproval:
    async def test_approve_activates_with_exactly_those_permissions(self, ctx):
        http, db_session, admin_id = ctx
        user_id = await _pending(db_session)

        response = http.post(f"/admin/users/{user_id}/approve", json={"permissions": ["trem:ai", "trem:file_editor"]})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "active"
        assert body["permissions"] == ["trem:ai", "trem:file_editor"]
        assert body["approved_by"] == admin_id and body["approved_at"]
        grants = (await db_session.execute(select(user_permissions.c.granted_by))).scalars().all()
        assert set(map(str, grants)) == {admin_id, "None"}  # the admin's own grant had no granter
        assert _login(http).status_code == 200

    async def test_approve_without_permissions_is_allowed(self, ctx):
        http, db_session, _ = ctx
        user_id = await _pending(db_session)
        assert http.post(f"/admin/users/{user_id}/approve", json={}).json()["permissions"] == []

    async def test_unknown_permission_is_400(self, ctx):
        http, db_session, _ = ctx
        user_id = await _pending(db_session)
        response = http.post(f"/admin/users/{user_id}/approve", json={"permissions": ["trem:root"]})
        assert response.status_code == 400
        assert (await db_session.get(UserModel, uuid.UUID(user_id))).status == "pending_approval"

    @pytest.mark.parametrize("status", ["active", "pending_verification", "suspended"])
    async def test_only_waiting_accounts_can_be_approved(self, ctx, status):
        http, db_session, _ = ctx
        user_id = await _pending(db_session, status=status)
        assert http.post(f"/admin/users/{user_id}/approve", json={}).status_code == 409

    async def test_reject(self, ctx):
        http, db_session, _ = ctx
        user_id = await _pending(db_session)
        assert http.post(f"/admin/users/{user_id}/reject").json()["status"] == "rejected"
        assert _login(http).status_code == 403

    async def test_list_pending(self, ctx):
        http, db_session, _ = ctx
        await _pending(db_session, "a")
        await _pending(db_session, "b", status="active")
        listed = http.get("/admin/users", params={"status": "pending_approval"}).json()
        assert [u["username"] for u in listed["items"]] == ["a"] and listed["total"] == 1
        assert http.get("/admin/users", params={"status": "inventado"}).status_code == 422

    async def test_unknown_user_is_404(self, ctx):
        http, _, _ = ctx
        assert http.post(f"/admin/users/{uuid.uuid4()}/approve", json={}).status_code == 404


class TestLifecycle:
    async def _active(self, http, db_session):
        user_id = await _pending(db_session)
        http.post(f"/admin/users/{user_id}/approve", json={"permissions": ["trem:ai"]})
        return user_id

    async def test_suspend_ends_sessions_and_keys(self, ctx):
        http, db_session, _ = ctx
        user_id = await self._active(http, db_session)
        refresh = _login(http).json()["refresh_token"]
        created = http.post("/admin/clients", json={"name": "joana-key", "permissions": ["trem:ai"], "user_id": user_id})
        assert created.status_code == 201, created.text
        key = created.json()["api_key"]

        assert http.post(f"/admin/users/{user_id}/suspend").json()["status"] == "suspended"

        assert http.post("/auth/refresh", json={"refresh_token": refresh}, headers={"Authorization": ""}).status_code == 401
        assert http.get("/llm/health", headers={"X-API-Key": key, "Authorization": ""}).status_code == 401

        assert http.post(f"/admin/users/{user_id}/reactivate").json()["status"] == "active"
        assert _login(http).status_code == 200

    async def test_admin_cannot_change_their_own_state(self, ctx):
        http, _, admin_id = ctx
        assert http.post(f"/admin/users/{admin_id}/suspend").status_code == 409
        assert http.post(f"/admin/users/{admin_id}/reject").status_code == 409

    async def test_set_permissions_replaces_and_ends_sessions(self, ctx):
        http, db_session, _ = ctx
        user_id = await self._active(http, db_session)
        refresh = _login(http).json()["refresh_token"]

        body = http.put(f"/admin/users/{user_id}/permissions", json={"permissions": ["trem:file_editor"]}).json()

        assert body["permissions"] == ["trem:file_editor"]
        assert http.post("/auth/refresh", json={"refresh_token": refresh}, headers={"Authorization": ""}).status_code == 401

    async def test_admin_cannot_drop_their_own_admin(self, ctx):
        http, _, admin_id = ctx
        assert http.put(f"/admin/users/{admin_id}/permissions", json={"permissions": ["trem:ai"]}).status_code == 409
        assert http.put(f"/admin/users/{admin_id}/permissions",
                        json={"permissions": ["trem:admin", "trem:ai"]}).status_code == 200


class TestApplications:
    def test_trem_is_listed_with_its_permissions(self, ctx):
        http, _, _ = ctx
        (trem,) = http.get("/admin/applications").json()
        assert trem["slug"] == "trem"
        assert {p["key"] for p in trem["permissions"]} >= {"trem:ai", "trem:admin"}

    async def test_new_application_end_to_end(self, ctx):
        """Registered by the admin, granted to a user, and that user logs into it: no migration involved."""
        http, db_session, _ = ctx
        assert http.post("/admin/applications", json={"slug": "financas", "name": "Finanças"}).status_code == 201
        created = http.post("/admin/applications/financas/permissions", json={"name": "read"})
        assert created.status_code == 201 and created.json()["key"] == "financas:read"

        user_id = await _pending(db_session)
        http.post(f"/admin/users/{user_id}/approve", json={"permissions": ["financas:read"]})

        response = _login(http, audience="financas")
        assert response.status_code == 200
        from app.core.security import decode_token
        assert decode_token(response.json()["access_token"], audience="financas")["perms"] == ["financas:read"]

    def test_duplicates_are_409(self, ctx):
        http, _, _ = ctx
        http.post("/admin/applications", json={"slug": "financas", "name": "F"})
        assert http.post("/admin/applications", json={"slug": "financas", "name": "F"}).status_code == 409
        http.post("/admin/applications/financas/permissions", json={"name": "read"})
        assert http.post("/admin/applications/financas/permissions", json={"name": "read"}).status_code == 409

    @pytest.mark.parametrize("slug", ["Fin", "f", "fin anças", "trem#mfa"])
    def test_malformed_slug_is_422(self, ctx, slug):
        http, _, _ = ctx
        assert http.post("/admin/applications", json={"slug": slug, "name": "x"}).status_code == 422

    async def test_deleting_an_application_removes_its_grants(self, ctx):
        http, db_session, _ = ctx
        http.post("/admin/applications", json={"slug": "financas", "name": "F"})
        http.post("/admin/applications/financas/permissions", json={"name": "read"})
        user_id = await _pending(db_session)
        http.post(f"/admin/users/{user_id}/approve", json={"permissions": ["financas:read", "trem:ai"]})

        assert http.delete("/admin/applications/financas").status_code == 200
        assert http.get(f"/admin/users/{user_id}").json()["permissions"] == ["trem:ai"]

    def test_trem_and_its_permissions_cannot_be_removed(self, ctx):
        http, _, _ = ctx
        assert http.delete("/admin/applications/trem").status_code == 409
        assert http.delete("/admin/applications/trem/permissions/admin").status_code == 409

    def test_permission_of_another_application_can_be_removed(self, ctx):
        http, _, _ = ctx
        http.post("/admin/applications", json={"slug": "financas", "name": "F"})
        http.post("/admin/applications/financas/permissions", json={"name": "read"})
        assert http.delete("/admin/applications/financas/permissions/read").status_code == 200
        assert http.delete("/admin/applications/financas/permissions/read").status_code == 404


class TestAccess:
    def test_requires_login(self, ctx):
        http, _, _ = ctx
        assert http.get("/admin/users", headers={"Authorization": ""}).status_code == 401

    def test_token_of_another_application_is_refused(self, ctx):
        http, _, admin_id = ctx
        token = create_access_token(admin_id, "chefe", ["trem:admin"], False, audience="financas", amr=("pwd", "otp"))
        assert http.get("/admin/users", headers={"Authorization": f"Bearer {token}"}).status_code == 401
