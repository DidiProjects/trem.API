"""Catalog of trem.FRONT services (/hub, /admin/hub) against the suite's SQLite."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token, hash_password
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import PermissionModel, UserModel
from app.main import app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'


@pytest.fixture
async def ctx(db_session, seeded_permissions):
    admin_permission = await db_session.get(PermissionModel, uuid.UUID(seeded_permissions["trem:admin"]))
    admin = UserModel(username="chefe", email="chefe@example.com", password_hash=hash_password("x"),
                      status="active", must_change_password=False, permissions=[admin_permission])
    ana = UserModel(username="ana", email="ana@example.com", password_hash=hash_password("x"),
                    status="active", must_change_password=False)
    db_session.add_all([admin, ana])
    await db_session.commit()

    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    admin_token = create_access_token(str(admin.id), "chefe", ["trem:admin"], False, amr=("pwd", "otp"))
    ana_token = create_access_token(str(ana.id), "ana", [], False)
    admin_http = TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {admin_token}"})
    ana_http = TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {ana_token}"})
    yield admin_http, ana_http, str(ana.id)
    app.dependency_overrides.clear()


def _anonymous():
    return TestClient(app, base_url="https://testserver")


def _create(http, slug="immich", **extra):
    body = {"slug": slug, "name": slug.title(), "url": f"http://10.0.0.1:{2000 + len(slug)}", **extra}
    response = http.post("/admin/hub/services", json=body)
    assert response.status_code == 201, response.text
    return response.json()


class TestAdmin:
    async def test_create_makes_the_permission(self, ctx):
        admin, _, _ = ctx
        created = _create(admin, description="Fotos")
        assert created["permission"] == "hub:immich" and created["position"] == 0 and created["enabled"]
        hub = next(a for a in admin.get("/admin/applications").json() if a["slug"] == "hub")
        assert [p["key"] for p in hub["permissions"]] == ["hub:immich"]

    async def test_duplicate_slug_is_409(self, ctx):
        admin, _, _ = ctx
        _create(admin)
        assert admin.post("/admin/hub/services", json={"slug": "immich", "name": "Outro"}).status_code == 409

    @pytest.mark.parametrize("url", ["ftp://host", "javascript:alert(1)", "http://", "http://u:p@host", "http://a b"])
    async def test_only_plain_http_addresses(self, ctx, url):
        admin, _, _ = ctx
        assert admin.post("/admin/hub/services", json={"slug": "x1", "name": "X", "url": url}).status_code == 422

    async def test_update_only_what_is_sent_and_null_clears(self, ctx):
        admin, _, _ = ctx
        _create(admin, description="Fotos")
        updated = admin.patch("/admin/hub/services/immich", json={"name": "Fotos", "url": None}).json()
        assert updated["name"] == "Fotos" and updated["url"] is None and updated["description"] == "Fotos"
        assert admin.patch("/admin/hub/services/nada", json={"name": "x"}).status_code == 404

    async def test_reorder_needs_every_service_once(self, ctx):
        admin, _, _ = ctx
        for slug in ("immich", "radarr", "sonarr"):
            _create(admin, slug)
        ordered = admin.put("/admin/hub/services/order", json={"slugs": ["sonarr", "immich", "radarr"]}).json()
        assert [s["slug"] for s in ordered] == ["sonarr", "immich", "radarr"]
        assert admin.put("/admin/hub/services/order", json={"slugs": ["sonarr", "immich"]}).status_code == 409

    async def test_delete_takes_the_permission_and_the_grants(self, ctx):
        admin, _, ana_id = ctx
        _create(admin)
        admin.put(f"/admin/hub/services/immich/users/{ana_id}")
        assert admin.delete("/admin/hub/services/immich").status_code == 200
        assert admin.get("/admin/hub/services").json() == []
        assert "hub:immich" not in admin.get(f"/admin/users/{ana_id}").json()["permissions"]

    async def test_needs_an_admin(self, ctx):
        _, ana, _ = ctx
        assert ana.get("/admin/hub/services").status_code == 403
        assert ana.post("/admin/hub/services", json={"slug": "x1", "name": "X"}).status_code == 403


class TestAccess:
    async def test_catalog_shows_granted_enabled_services_with_an_address(self, ctx):
        admin, ana, ana_id = ctx
        _create(admin, "immich")
        _create(admin, "radarr")
        _create(admin, "sonarr", url=None)
        _create(admin, "prowlarr", enabled=False)
        for slug in ("immich", "sonarr", "prowlarr"):
            assert admin.put(f"/admin/hub/services/{slug}/users/{ana_id}").status_code == 200

        catalog = ana.get("/hub/services").json()
        assert [s["slug"] for s in catalog] == ["immich"]
        assert catalog[0]["url"] == "http://10.0.0.1:2006" and catalog[0]["logo_url"] is None

    async def test_grant_is_idempotent_and_revoke_hides_it(self, ctx):
        admin, ana, ana_id = ctx
        _create(admin)
        admin.put(f"/admin/hub/services/immich/users/{ana_id}")
        admin.put(f"/admin/hub/services/immich/users/{ana_id}")
        assert len(ana.get("/hub/services").json()) == 1
        assert admin.delete(f"/admin/hub/services/immich/users/{ana_id}").status_code == 200
        assert ana.get("/hub/services").json() == []

    async def test_grant_keeps_the_other_permissions(self, ctx):
        admin, _, ana_id = ctx
        _create(admin)
        admin.put(f"/admin/users/{ana_id}/permissions", json={"permissions": ["trem:ai"]})
        admin.put(f"/admin/hub/services/immich/users/{ana_id}")
        assert admin.get(f"/admin/users/{ana_id}").json()["permissions"] == ["hub:immich", "trem:ai"]

    async def test_unknown_user_or_service_is_404(self, ctx):
        admin, _, ana_id = ctx
        _create(admin)
        assert admin.put(f"/admin/hub/services/immich/users/{uuid.uuid4()}").status_code == 404
        assert admin.put(f"/admin/hub/services/nada/users/{ana_id}").status_code == 404

    async def test_catalog_needs_a_login(self, ctx):
        assert _anonymous().get("/hub/services").status_code in (401, 403)


class TestLogo:
    def _upload(self, http, data, slug="immich"):
        return http.put(f"/admin/hub/services/{slug}/logo", files={"file": ("logo", data, "application/octet-stream")})

    @pytest.mark.parametrize("data,kind", [
        (PNG, "image/png"),
        (b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 16, "image/webp"),
        (SVG, "image/svg+xml"),
        (b'<?xml version="1.0"?>\n' + SVG, "image/svg+xml"),
    ])
    async def test_type_comes_from_the_bytes(self, ctx, data, kind):
        admin, _, _ = ctx
        _create(admin)
        logo_url = self._upload(admin, data).json()["logo_url"]
        assert logo_url.startswith("/hub/services/immich/logo?v=")
        served = _anonymous().get(logo_url)
        assert served.status_code == 200 and served.content == data
        assert served.headers["content-type"].startswith(kind)
        assert "immutable" in served.headers["cache-control"]
        assert "sandbox" in served.headers["content-security-policy"]
        assert served.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize("data", [b"GIF89a....", b"<html><script>1</script></html>", b""])
    async def test_other_files_are_refused(self, ctx, data):
        admin, _, _ = ctx
        _create(admin)
        assert self._upload(admin, data).status_code == 400

    async def test_size_limit(self, ctx):
        admin, _, _ = ctx
        _create(admin)
        response = self._upload(admin, PNG + b"\x00" * (256 * 1024))
        assert response.status_code == 400 and "256 KB" in response.json()["detail"]

    async def test_new_logo_new_url_and_stale_url_revalidates(self, ctx):
        admin, _, _ = ctx
        _create(admin)
        first = self._upload(admin, PNG).json()["logo_url"]
        second = self._upload(admin, SVG).json()["logo_url"]
        assert first != second
        stale = _anonymous().get(first)
        assert stale.content == SVG and "no-cache" in stale.headers["cache-control"]

    async def test_cleared_logo_is_404(self, ctx):
        admin, _, _ = ctx
        _create(admin)
        self._upload(admin, PNG)
        assert admin.delete("/admin/hub/services/immich/logo").json()["logo_url"] is None
        assert _anonymous().get("/hub/services/immich/logo").status_code == 404
