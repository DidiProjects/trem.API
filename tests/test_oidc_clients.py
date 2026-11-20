"""OIDC clients of hub services (/admin/hub/services/{slug}/oidc) against the suite's SQLite."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import OidcClientModel, PermissionModel, UserModel
from app.main import app
from app.services.oidc_client_service import hash_secret

URI = "http://100.64.0.1:8096/sso/OID/redirect/trem"


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
    created = admin_http.post("/admin/hub/services", json={"slug": "didiflix", "name": "Didiflix"})
    assert created.status_code == 201, created.text
    yield admin_http, ana_http, db_session
    app.dependency_overrides.clear()


def _configure(http, uris=(URI,)):
    return http.put("/admin/hub/services/didiflix/oidc", json={"redirect_uris": list(uris)})


async def _stored(db_session) -> OidcClientModel:
    db_session.expire_all()
    return (await db_session.execute(select(OidcClientModel))).scalars().unique().one()


class TestConfigure:
    async def test_create_shows_the_secret_once_and_stores_only_its_hash(self, ctx):
        admin, _, db_session = ctx
        response = _configure(admin)
        assert response.status_code == 201
        body = response.json()
        assert body["client_id"] == "didiflix" and body["redirect_uris"] == [URI]
        assert len(body["client_secret"]) >= 43
        stored = await _stored(db_session)
        assert stored.secret_hash == hash_secret(body["client_secret"])
        assert body["client_secret"] not in stored.secret_hash

        read = admin.get("/admin/hub/services/didiflix/oidc")
        assert read.status_code == 200 and "client_secret" not in read.json()

    async def test_changing_the_uris_keeps_the_secret(self, ctx):
        admin, _, db_session = ctx
        secret = _configure(admin).json()["client_secret"]
        other = "https://flix.example/sso/OID/redirect/trem"
        response = _configure(admin, (other, other + " "))
        assert response.status_code == 200
        assert response.json()["redirect_uris"] == [other] and "client_secret" not in response.json()
        assert (await _stored(db_session)).secret_hash == hash_secret(secret)

    @pytest.mark.parametrize("uri", [
        "ftp://host/cb", "javascript:alert(1)", "http://", "http://u:p@host/cb",
        "https://host/cb#frag", "https://host/c b", "/relative/cb",
    ])
    async def test_only_plain_absolute_uris(self, ctx, uri):
        admin, _, _ = ctx
        assert _configure(admin, (uri,)).status_code == 422

    @pytest.mark.parametrize("uri", [
        "http://localhost:8096/cb", "http://127.0.0.1:5055/login", "http://192.168.1.20:8096/cb",
        "http://10.0.0.5/cb", "http://100.116.16.24:8096/cb", "https://flix.example.com/cb",
    ])
    async def test_http_only_where_the_code_stays_off_the_internet(self, ctx, uri):
        admin, _, _ = ctx
        assert _configure(admin, (uri,)).status_code == 201

    @pytest.mark.parametrize("uri", ["http://flix.example.com/cb", "http://8.8.8.8/cb", "http://100.128.0.1/cb"])
    async def test_public_hosts_need_https(self, ctx, uri):
        admin, _, _ = ctx
        assert _configure(admin, (uri,)).status_code == 422

    async def test_needs_at_least_one_and_at_most_ten(self, ctx):
        admin, _, _ = ctx
        assert _configure(admin, ()).status_code == 422
        assert _configure(admin, [f"https://h{i}.example/cb" for i in range(11)]).status_code == 422

    async def test_unknown_service_is_404(self, ctx):
        admin, _, _ = ctx
        response = admin.put("/admin/hub/services/nada/oidc", json={"redirect_uris": [URI]})
        assert response.status_code == 404
        assert admin.get("/admin/hub/services/didiflix/oidc").status_code == 404


class TestSecretAndRemoval:
    async def test_rotation_replaces_the_secret(self, ctx):
        admin, _, db_session = ctx
        old = _configure(admin).json()["client_secret"]
        rotated = admin.post("/admin/hub/services/didiflix/oidc/secret")
        assert rotated.status_code == 200
        new = rotated.json()["client_secret"]
        assert new != old and (await _stored(db_session)).secret_hash == hash_secret(new)

    async def test_rotation_needs_a_client(self, ctx):
        admin, _, _ = ctx
        assert admin.post("/admin/hub/services/didiflix/oidc/secret").status_code == 404

    async def test_delete_removes_the_client_only(self, ctx):
        admin, _, _ = ctx
        _configure(admin)
        assert admin.delete("/admin/hub/services/didiflix/oidc").status_code == 200
        assert admin.get("/admin/hub/services/didiflix/oidc").status_code == 404
        assert any(s["slug"] == "didiflix" for s in admin.get("/admin/hub/services").json())

    async def test_removing_the_service_removes_its_client(self, ctx):
        admin, _, db_session = ctx
        _configure(admin)
        assert admin.delete("/admin/hub/services/didiflix").status_code == 200
        db_session.expire_all()
        assert (await db_session.execute(select(OidcClientModel))).scalars().all() == []


class TestAccess:
    async def test_admin_only(self, ctx):
        _, ana, _ = ctx
        assert ana.get("/admin/hub/services/didiflix/oidc").status_code == 403
        assert _configure(ana).status_code == 403
        assert ana.post("/admin/hub/services/didiflix/oidc/secret").status_code == 403
        assert ana.delete("/admin/hub/services/didiflix/oidc").status_code == 403

    async def test_anonymous_is_401(self, ctx):
        anonymous = TestClient(app, base_url="https://testserver")
        assert anonymous.get("/admin/hub/services/didiflix/oidc").status_code == 401
