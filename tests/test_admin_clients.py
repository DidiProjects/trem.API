"""
/admin/clients and the CLI against a real SQLite database: profile sets, key
owners, and an issued key going through the real authentication.
"""
import asyncio
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import ApplicationModel, Base, PermissionModel, UserModel
from app.domain.entities.permission import TremPermission
from app.main import app
from tests.authentik_tokens import admin_token

# The admin credential travels as the client's default Authorization header (see `http`).
MASTER: dict = {}


@pytest.fixture
async def http(db_session, seeded_permissions, authentik_login):
    token = admin_token()

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    yield TestClient(app, headers={"Authorization": f"Bearer {token}"})
    app.dependency_overrides.clear()


async def _owner(db_session, seeded_permissions, *names):
    profiles = [await db_session.get(PermissionModel, uuid.UUID(seeded_permissions[n])) for n in names]
    orm = UserModel(username=f"owner-{uuid.uuid4().hex[:6]}", permissions=profiles)
    db_session.add(orm)
    await db_session.flush()
    return str(orm.id)


class TestCreate:
    def test_several_profiles(self, http):
        response = http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": "multi", "permissions": ["trem:file_editor", "trem:ai", "trem:ai"]},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["permissions"] == ["trem:ai", "trem:file_editor"]
        assert body["user_id"] is None
        assert body["api_key"].startswith("trem_")

    async def test_owned_key(self, http, db_session, seeded_permissions):
        owner = await _owner(db_session, seeded_permissions, "trem:ai", "trem:linkedin_scraper")
        response = http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": "mine", "permissions": ["trem:linkedin_scraper"], "user_id": owner},
        )
        assert response.status_code == 201
        assert response.json()["user_id"] == owner

    async def test_key_beyond_owner_profiles_is_400(self, http, db_session, seeded_permissions):
        owner = await _owner(db_session, seeded_permissions, "trem:ai")
        response = http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": "escalate", "permissions": ["trem:ai", "trem:linkedin_scraper"], "user_id": owner},
        )
        assert response.status_code == 400
        assert "trem:linkedin_scraper" in response.json()["detail"]

    def test_unknown_owner_is_400(self, http):
        response = http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": "orphan", "permissions": ["trem:ai"], "user_id": str(uuid.uuid4())},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("profiles", [[], ["admin"], "trem:ai", ["Trem:ai"]])
    def test_invalid_permissions_are_422(self, http, profiles):
        response = http.post(
            "/admin/clients", headers=MASTER, json={"name": "bad", "permissions": profiles}
        )
        assert response.status_code == 422

    def test_old_single_profile_payload_is_422(self, http):
        response = http.post(
            "/admin/clients", headers=MASTER, json={"name": "old", "profile_name": "trem:ai"}
        )
        assert response.status_code == 422

    def test_requires_an_admin_login(self, http):
        body = {"name": "x", "permissions": ["trem:ai"]}
        assert http.post("/admin/clients", json=body, headers={"Authorization": ""}).status_code == 401

    def test_issued_key_cannot_administer(self, http):
        issued = http.post(
            "/admin/clients", headers=MASTER, json={"name": "app", "permissions": ["trem:ai"]}
        ).json()["api_key"]
        response = http.post(
            "/admin/clients",
            headers={"X-API-Key": issued, "Authorization": ""},
            json={"name": "y", "permissions": ["trem:ai"]},
        )
        assert response.status_code == 401


class TestReadBack:
    def test_list_and_get_show_profiles_and_owner(self, http):
        created = http.post(
            "/admin/clients", headers=MASTER, json={"name": "app", "permissions": ["trem:ai", "trem:file_editor"]}
        ).json()

        listed = http.get("/admin/clients", headers=MASTER).json()["items"]
        assert [(c["name"], c["permissions"], c["user_id"]) for c in listed] == [
            ("app", ["trem:ai", "trem:file_editor"], None)
        ]
        one = http.get(f"/admin/clients/{created['id']}", headers=MASTER).json()
        assert one["permissions"] == ["trem:ai", "trem:file_editor"]
        assert "api_key" not in one


class TestIssuedKeyEndToEnd:
    """The LLM is not configured in tests, so a route that lets the key in answers 503."""

    def _key(self, http, profiles, **extra):
        return http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": f"k-{uuid.uuid4().hex[:6]}", "permissions": profiles, **extra},
        ).json()["api_key"]

    def test_ai_key_reaches_llm(self, http):
        key = self._key(http, ["trem:ai"])
        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 503

    def test_key_without_ai_is_403_on_llm(self, http):
        key = self._key(http, ["trem:file_editor", "trem:linkedin_scraper"])
        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 403

    async def test_deleting_the_owner_turns_key_into_401(self, http, db_session, seeded_permissions):
        owner = await _owner(db_session, seeded_permissions, "trem:ai")
        key = self._key(http, ["trem:ai"], user_id=owner)
        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 503

        await db_session.delete(await db_session.get(UserModel, uuid.UUID(owner)))
        await db_session.flush()

        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 401

    async def test_removing_ai_from_owner_turns_key_into_403(self, http, db_session, seeded_permissions):
        owner = await _owner(db_session, seeded_permissions, "trem:ai", "trem:file_editor")
        key = self._key(http, ["trem:ai"], user_id=owner)

        user = await db_session.get(UserModel, uuid.UUID(owner))
        user.permissions = [p for p in user.permissions if p.key != "trem:ai"]
        await db_session.flush()

        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 403

    def test_revoked_key_is_401(self, http):
        created = http.post(
            "/admin/clients", headers=MASTER, json={"name": "rev", "permissions": ["trem:ai"]}
        ).json()
        http.delete(f"/admin/clients/{created['id']}", headers=MASTER)
        assert http.get("/llm/health", headers={"X-API-Key": created["api_key"]}).status_code == 401


class TestCli:
    """scripts.manage_api_clients on a file database, as it runs against Postgres on the server."""

    @pytest.fixture
    def cli_db(self, tmp_path, monkeypatch):
        url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"

        async def setup():
            engine = create_async_engine(url)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine)() as session:
                trem = ApplicationModel(slug="trem", name="trem.API")
                profiles = {
                    n.value: PermissionModel(application=trem, name=n.value.split(":", 1)[1])
                    for n in TremPermission
                }
                session.add_all(profiles.values())
                session.add(UserModel(
                    username="diego",
                    permissions=[profiles["trem:ai"], profiles["trem:linkedin_scraper"]],
                ))
                await session.commit()
            await engine.dispose()

        asyncio.run(setup())

        import scripts.manage_api_clients as cli

        monkeypatch.setattr(
            cli, "_session_factory",
            lambda: (lambda e: (e, async_sessionmaker(e, expire_on_commit=False)))(create_async_engine(url)),
        )
        return cli

    def run(self, cli, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["manage_api_clients", *argv])
        return cli.main()

    def test_create_with_several_profiles_and_owner(self, cli_db, monkeypatch, capsys):
        code = self.run(
            cli_db, monkeypatch, "create", "diego-key",
            "--permission", "trem:linkedin_scraper", "--permission", "trem:ai", "--user", "diego",
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "permissões: trem:ai, trem:linkedin_scraper" in out
        assert "trem_" in out

        assert self.run(cli_db, monkeypatch, "list") == 0
        assert "trem:ai,trem:linkedin_scraper" in capsys.readouterr().out

    def test_unknown_owner_fails(self, cli_db, monkeypatch, capsys):
        code = self.run(cli_db, monkeypatch, "create", "k", "--permission", "trem:ai", "--user", "ninguem")
        assert code == 1
        assert "não encontrado" in capsys.readouterr().err

    def test_profile_beyond_owner_fails(self, cli_db, monkeypatch, capsys):
        code = self.run(
            cli_db, monkeypatch, "create", "k", "--permission", "trem:file_editor", "--user", "diego"
        )
        assert code == 1
        assert "trem:file_editor" in capsys.readouterr().err

    def test_service_key_without_owner(self, cli_db, monkeypatch, capsys):
        assert self.run(cli_db, monkeypatch, "create", "svc", "--permission", "trem:file_editor") == 0
