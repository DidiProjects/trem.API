"""Catalog of trem.FRONT services (/hub, /admin/hub) against the suite's SQLite."""
import pytest
from fastapi.testclient import TestClient

from app.infrastructure.database.connection import get_db
from app.main import app
from tests.authentik_tokens import admin_token, authentik_token

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'


@pytest.fixture
async def ctx(db_session, seeded_permissions, authentik_login):
    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    admin_http = TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {admin_token()}"})
    yield admin_http, _person
    app.dependency_overrides.clear()


def _person(*groups):
    """Someone logged in through authentik with these groups."""
    token = authentik_token("ana", groups=groups)
    return TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {token}"})


def _anonymous():
    return TestClient(app, base_url="https://testserver")


def _create(http, slug="immich", **extra):
    body = {"slug": slug, "name": slug.title(), "url": f"http://10.0.0.1:{2000 + len(slug)}", **extra}
    response = http.post("/admin/hub/services", json=body)
    assert response.status_code == 201, response.text
    return response.json()


class TestAdmin:
    async def test_create_makes_the_permission(self, ctx):
        admin, _ = ctx
        created = _create(admin, description="Fotos")
        assert created["permission"] == "hub:immich" and created["position"] == 0 and created["enabled"]
        hub = next(a for a in admin.get("/admin/applications").json() if a["slug"] == "hub")
        assert [p["key"] for p in hub["permissions"]] == ["hub:immich"]

    async def test_duplicate_slug_is_409(self, ctx):
        admin, _ = ctx
        _create(admin)
        assert admin.post("/admin/hub/services", json={"slug": "immich", "name": "Outro"}).status_code == 409

    @pytest.mark.parametrize("url", ["ftp://host", "javascript:alert(1)", "http://", "http://u:p@host", "http://a b"])
    async def test_only_plain_http_addresses(self, ctx, url):
        admin, _ = ctx
        assert admin.post("/admin/hub/services", json={"slug": "x1", "name": "X", "url": url}).status_code == 422

    async def test_update_only_what_is_sent_and_null_clears(self, ctx):
        admin, _ = ctx
        _create(admin, description="Fotos")
        updated = admin.patch("/admin/hub/services/immich", json={"name": "Fotos", "url": None}).json()
        assert updated["name"] == "Fotos" and updated["url"] is None and updated["description"] == "Fotos"
        assert admin.patch("/admin/hub/services/nada", json={"name": "x"}).status_code == 404

    async def test_reorder_needs_every_service_once(self, ctx):
        admin, _ = ctx
        for slug in ("immich", "radarr", "sonarr"):
            _create(admin, slug)
        ordered = admin.put("/admin/hub/services/order", json={"slugs": ["sonarr", "immich", "radarr"]}).json()
        assert [s["slug"] for s in ordered] == ["sonarr", "immich", "radarr"]
        assert admin.put("/admin/hub/services/order", json={"slugs": ["sonarr", "immich"]}).status_code == 409

    async def test_delete_takes_the_permission(self, ctx):
        admin, _ = ctx
        _create(admin)
        assert admin.delete("/admin/hub/services/immich").status_code == 200
        assert admin.get("/admin/hub/services").json() == []
        hub = next(a for a in admin.get("/admin/applications").json() if a["slug"] == "hub")
        assert hub["permissions"] == []

    async def test_needs_an_admin(self, ctx):
        _, person = ctx
        ana = person("hub:immich")
        assert ana.get("/admin/hub/services").status_code == 403
        assert ana.post("/admin/hub/services", json={"slug": "x1", "name": "X"}).status_code == 403


class TestAccess:
    async def test_catalog_shows_the_enabled_services_with_an_address_of_the_token_groups(self, ctx):
        admin, person = ctx
        _create(admin, "immich")
        _create(admin, "radarr")
        _create(admin, "sonarr", url=None)
        _create(admin, "prowlarr", enabled=False)

        catalog = person("hub:immich", "hub:sonarr", "hub:prowlarr").get("/hub/services").json()

        assert [s["slug"] for s in catalog] == ["immich"]
        assert catalog[0]["url"] == "http://10.0.0.1:2006" and catalog[0]["logo_url"] is None

    async def test_out_of_the_group_the_service_is_gone(self, ctx):
        admin, person = ctx
        _create(admin)
        assert len(person("hub:immich").get("/hub/services").json()) == 1
        assert person().get("/hub/services").json() == []

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
        admin, _ = ctx
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
        admin, _ = ctx
        _create(admin)
        assert self._upload(admin, data).status_code == 400

    async def test_size_limit(self, ctx):
        admin, _ = ctx
        _create(admin)
        response = self._upload(admin, PNG + b"\x00" * (256 * 1024))
        assert response.status_code == 400 and "256 KB" in response.json()["detail"]

    async def test_new_logo_new_url_and_stale_url_revalidates(self, ctx):
        admin, _ = ctx
        _create(admin)
        first = self._upload(admin, PNG).json()["logo_url"]
        second = self._upload(admin, SVG).json()["logo_url"]
        assert first != second
        stale = _anonymous().get(first)
        assert stale.content == SVG and "no-cache" in stale.headers["cache-control"]

    async def test_cleared_logo_is_404(self, ctx):
        admin, _ = ctx
        _create(admin)
        self._upload(admin, PNG)
        assert admin.delete("/admin/hub/services/immich/logo").json()["logo_url"] is None
        assert _anonymous().get("/hub/services/immich/logo").status_code == 404
