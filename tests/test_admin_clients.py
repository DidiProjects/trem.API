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
from app.infrastructure.database.models import Base, ProfileModel, UserModel
from app.domain.entities.profile import ProfileName
from app.main import app
from tests.conftest import TEST_API_KEY

MASTER = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def http(db_session, seeded_profiles):
    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    yield TestClient(app)
    app.dependency_overrides.clear()


async def _owner(db_session, seeded_profiles, *names, status="active"):
    profiles = [await db_session.get(ProfileModel, uuid.UUID(seeded_profiles[n])) for n in names]
    orm = UserModel(
        username=f"owner-{uuid.uuid4().hex[:6]}",
        password_hash="x",
        profiles=profiles,
        status=status,
        must_change_password=False,
    )
    db_session.add(orm)
    await db_session.flush()
    return str(orm.id)


class TestCreate:
    def test_several_profiles(self, http):
        response = http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": "multi", "profile_names": ["file_editor", "ai", "ai"]},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["profile_names"] == ["ai", "file_editor"]
        assert body["user_id"] is None
        assert body["api_key"].startswith("trem_")

    async def test_owned_key(self, http, db_session, seeded_profiles):
        owner = await _owner(db_session, seeded_profiles, "ai", "linkedin_scraper")
        response = http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": "mine", "profile_names": ["linkedin_scraper"], "user_id": owner},
        )
        assert response.status_code == 201
        assert response.json()["user_id"] == owner

    async def test_key_beyond_owner_profiles_is_400(self, http, db_session, seeded_profiles):
        owner = await _owner(db_session, seeded_profiles, "ai")
        response = http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": "escalate", "profile_names": ["ai", "linkedin_scraper"], "user_id": owner},
        )
        assert response.status_code == 400
        assert "linkedin_scraper" in response.json()["detail"]

    def test_unknown_owner_is_400(self, http):
        response = http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": "orphan", "profile_names": ["ai"], "user_id": str(uuid.uuid4())},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("profiles", [[], ["admin"], "ai"])
    def test_invalid_profiles_are_422(self, http, profiles):
        response = http.post(
            "/admin/clients", headers=MASTER, json={"name": "bad", "profile_names": profiles}
        )
        assert response.status_code == 422

    def test_old_single_profile_payload_is_422(self, http):
        response = http.post(
            "/admin/clients", headers=MASTER, json={"name": "old", "profile_name": "ai"}
        )
        assert response.status_code == 422

    def test_requires_master_key(self, http):
        body = {"name": "x", "profile_names": ["ai"]}
        assert http.post("/admin/clients", json=body).status_code == 401

    def test_issued_key_cannot_administer(self, http):
        issued = http.post(
            "/admin/clients", headers=MASTER, json={"name": "app", "profile_names": ["ai"]}
        ).json()["api_key"]
        response = http.post(
            "/admin/clients",
            headers={"X-API-Key": issued},
            json={"name": "y", "profile_names": ["ai"]},
        )
        assert response.status_code == 403


class TestReadBack:
    def test_list_and_get_show_profiles_and_owner(self, http):
        created = http.post(
            "/admin/clients", headers=MASTER, json={"name": "app", "profile_names": ["ai", "file_editor"]}
        ).json()

        listed = http.get("/admin/clients", headers=MASTER).json()["items"]
        assert [(c["name"], c["profile_names"], c["user_id"]) for c in listed] == [
            ("app", ["ai", "file_editor"], None)
        ]
        one = http.get(f"/admin/clients/{created['id']}", headers=MASTER).json()
        assert one["profile_names"] == ["ai", "file_editor"]
        assert "api_key" not in one


class TestIssuedKeyEndToEnd:
    """The LLM is not configured in tests, so a route that lets the key in answers 503."""

    def _key(self, http, profiles, **extra):
        return http.post(
            "/admin/clients",
            headers=MASTER,
            json={"name": f"k-{uuid.uuid4().hex[:6]}", "profile_names": profiles, **extra},
        ).json()["api_key"]

    def test_ai_key_reaches_llm(self, http):
        key = self._key(http, ["ai"])
        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 503

    def test_key_without_ai_is_403_on_llm(self, http):
        key = self._key(http, ["file_editor", "linkedin_scraper"])
        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 403

    async def test_suspending_owner_turns_key_into_401(self, http, db_session, seeded_profiles):
        owner = await _owner(db_session, seeded_profiles, "ai")
        key = self._key(http, ["ai"], user_id=owner)
        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 503

        user = await db_session.get(UserModel, uuid.UUID(owner))
        user.status = "suspended"
        await db_session.flush()

        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 401

    async def test_removing_ai_from_owner_turns_key_into_403(self, http, db_session, seeded_profiles):
        owner = await _owner(db_session, seeded_profiles, "ai", "file_editor")
        key = self._key(http, ["ai"], user_id=owner)

        user = await db_session.get(UserModel, uuid.UUID(owner))
        user.profiles = [p for p in user.profiles if p.name != "ai"]
        await db_session.flush()

        assert http.get("/llm/health", headers={"X-API-Key": key}).status_code == 403

    def test_revoked_key_is_401(self, http):
        created = http.post(
            "/admin/clients", headers=MASTER, json={"name": "rev", "profile_names": ["ai"]}
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
                profiles = {n.value: ProfileModel(name=n.value) for n in ProfileName}
                session.add_all(profiles.values())
                session.add(UserModel(
                    username="diego", password_hash="x", status="active",
                    profiles=[profiles["ai"], profiles["linkedin_scraper"]],
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
            "--profile", "linkedin_scraper", "--profile", "ai", "--user", "diego",
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "perfis: ai, linkedin_scraper" in out
        assert "trem_" in out

        assert self.run(cli_db, monkeypatch, "list") == 0
        assert "ai,linkedin_scraper" in capsys.readouterr().out

    def test_unknown_owner_fails(self, cli_db, monkeypatch, capsys):
        code = self.run(cli_db, monkeypatch, "create", "k", "--profile", "ai", "--user", "ninguem")
        assert code == 1
        assert "não encontrado" in capsys.readouterr().err

    def test_profile_beyond_owner_fails(self, cli_db, monkeypatch, capsys):
        code = self.run(
            cli_db, monkeypatch, "create", "k", "--profile", "file_editor", "--user", "diego"
        )
        assert code == 1
        assert "file_editor" in capsys.readouterr().err

    def test_service_key_without_owner(self, cli_db, monkeypatch, capsys):
        assert self.run(cli_db, monkeypatch, "create", "svc", "--profile", "file_editor") == 0
