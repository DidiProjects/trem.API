"""
API de administração (/admin/applications) contra o SQLite da suíte, autenticada com um
token do authentik de admin com segundo fator.
"""
import pytest
from fastapi.testclient import TestClient

from app.infrastructure.database.connection import get_db
from app.main import app
from tests.authentik_tokens import admin_token, authentik_token


@pytest.fixture
async def ctx(db_session, seeded_permissions, authentik_login):
    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    http = TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {admin_token()}"})
    yield http, db_session
    app.dependency_overrides.clear()


class TestApplications:
    def test_trem_is_listed_with_its_permissions(self, ctx):
        http, _ = ctx
        (trem,) = http.get("/admin/applications").json()
        assert trem["slug"] == "trem"
        assert {p["key"] for p in trem["permissions"]} >= {"trem:ai", "trem:admin"}

    def test_new_application_and_permission(self, ctx):
        http, _ = ctx
        assert http.post("/admin/applications", json={"slug": "financas", "name": "Finanças"}).status_code == 201
        created = http.post("/admin/applications/financas/permissions", json={"name": "read"})
        assert created.status_code == 201 and created.json()["key"] == "financas:read"

    def test_duplicates_are_409(self, ctx):
        http, _ = ctx
        http.post("/admin/applications", json={"slug": "financas", "name": "F"})
        assert http.post("/admin/applications", json={"slug": "financas", "name": "F"}).status_code == 409
        http.post("/admin/applications/financas/permissions", json={"name": "read"})
        assert http.post("/admin/applications/financas/permissions", json={"name": "read"}).status_code == 409

    @pytest.mark.parametrize("slug", ["Fin", "f", "fin anças", "trem#mfa"])
    def test_malformed_slug_is_422(self, ctx, slug):
        http, _ = ctx
        assert http.post("/admin/applications", json={"slug": slug, "name": "x"}).status_code == 422

    def test_deleting_an_application_removes_its_permissions(self, ctx):
        http, _ = ctx
        http.post("/admin/applications", json={"slug": "financas", "name": "F"})
        http.post("/admin/applications/financas/permissions", json={"name": "read"})

        assert http.delete("/admin/applications/financas").status_code == 200
        assert [a["slug"] for a in http.get("/admin/applications").json()] == ["trem"]

    def test_trem_and_its_permissions_cannot_be_removed(self, ctx):
        http, _ = ctx
        assert http.delete("/admin/applications/trem").status_code == 409
        assert http.delete("/admin/applications/trem/permissions/admin").status_code == 409

    def test_permission_of_another_application_can_be_removed(self, ctx):
        http, _ = ctx
        http.post("/admin/applications", json={"slug": "financas", "name": "F"})
        http.post("/admin/applications/financas/permissions", json={"name": "read"})
        assert http.delete("/admin/applications/financas/permissions/read").status_code == 200
        assert http.delete("/admin/applications/financas/permissions/read").status_code == 404


class TestAccess:
    def test_requires_login(self, ctx):
        http, _ = ctx
        assert http.get("/admin/applications", headers={"Authorization": ""}).status_code == 401

    def test_admin_without_the_second_factor_is_refused(self, ctx):
        http, _ = ctx
        token = authentik_token("chefe", groups=["trem:admin"], amr=["pwd"])
        assert http.get("/admin/applications", headers={"Authorization": f"Bearer {token}"}).status_code == 403

    def test_a_token_for_another_client_is_refused(self, ctx):
        http, _ = ctx
        token = authentik_token("chefe", groups=["trem:admin"], amr=["pwd", "mfa"], aud="seerr")
        assert http.get("/admin/applications", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_not_admin_is_refused(self, ctx):
        http, _ = ctx
        token = authentik_token("ana", groups=["trem:ai"], amr=["pwd", "mfa"])
        assert http.get("/admin/applications", headers={"Authorization": f"Bearer {token}"}).status_code == 403
