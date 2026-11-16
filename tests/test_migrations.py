"""
Migrations run against a real PostgreSQL: the SQLite used by the rest of the
suite builds the schema from the ORM metadata and never executes them.

The schema of the target database is dropped on every test, so it only runs
against a database whose name contains "test":

    docker run -d --name trem-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=trem_test \
        -p 127.0.0.1:55432:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@127.0.0.1:55432/trem_test pytest tests/test_migrations.py
"""
import asyncio
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

DB_URL = os.environ.get("TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

ALL_PROFILES = {"file_editor", "airline_company", "ai", "linkedin_scraper"}


def sql(statement: str, **params) -> list:
    async def run():
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(statement), params)
                return result.fetchall() if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture
def alembic_cfg(monkeypatch):
    if "test" not in DB_URL.rsplit("/", 1)[-1]:
        pytest.fail("TEST_DATABASE_URL must point to a database named *test*: its schema is dropped")
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    get_settings.cache_clear()
    sql("DROP SCHEMA public CASCADE")
    sql("CREATE SCHEMA public")
    # No ini file: env.py would run logging.fileConfig on it, which disables every
    # logger already created and leaves caplog empty for the tests that follow.
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    yield cfg
    get_settings.cache_clear()


def profile_id(name: str) -> uuid.UUID:
    return sql("SELECT id FROM profiles WHERE name = :n", n=name)[0][0]


def seed_user(username: str, profile: str) -> uuid.UUID:
    uid = uuid.uuid4()
    sql(
        "INSERT INTO users (id, username, password_hash, profile_id, status, must_change_password) "
        "VALUES (:id, :u, 'x', :p, 'active', false)",
        id=uid, u=username, p=profile_id(profile),
    )
    return uid


def seed_client(name: str, profile: str) -> uuid.UUID:
    cid = uuid.uuid4()
    sql(
        "INSERT INTO api_clients (id, name, key_hash, profile_id) VALUES (:id, :n, :h, :p)",
        id=cid, n=name, h=uuid.uuid4().hex * 2, p=profile_id(profile),
    )
    return cid


def user_profiles(uid: uuid.UUID) -> set[str]:
    rows = sql(
        "SELECT p.name FROM user_profiles l JOIN profiles p ON p.id = l.profile_id WHERE l.user_id = :id",
        id=uid,
    )
    return {r[0] for r in rows}


def client_profiles(cid: uuid.UUID) -> set[str]:
    rows = sql(
        "SELECT p.name FROM api_client_profiles l JOIN profiles p ON p.id = l.profile_id "
        "WHERE l.client_id = :id",
        id=cid,
    )
    return {r[0] for r in rows}


def user_permissions(uid: uuid.UUID) -> set[str]:
    rows = sql(
        "SELECT a.slug || ':' || p.name FROM user_permissions l "
        "JOIN permissions p ON p.id = l.permission_id "
        "JOIN applications a ON a.id = p.application_id WHERE l.user_id = :id",
        id=uid,
    )
    return {r[0] for r in rows}


def client_permissions(cid: uuid.UUID) -> set[str]:
    rows = sql(
        "SELECT a.slug || ':' || p.name FROM api_client_permissions l "
        "JOIN permissions p ON p.id = l.permission_id "
        "JOIN applications a ON a.id = p.application_id WHERE l.client_id = :id",
        id=cid,
    )
    return {r[0] for r in rows}


def legacy_profile(table: str, row_id: uuid.UUID) -> str:
    return sql(
        f"SELECT p.name FROM {table} t JOIN profiles p ON p.id = t.profile_id WHERE t.id = :id",
        id=row_id,
    )[0][0]


class TestUpgrade004:
    def test_every_existing_user_gets_every_profile(self, alembic_cfg):
        command.upgrade(alembic_cfg, "003")
        editor = seed_user("editor", "file_editor")
        airline = seed_user("airline", "airline_company")

        command.upgrade(alembic_cfg, "004")

        assert {r[0] for r in sql("SELECT name FROM profiles")} == ALL_PROFILES
        assert user_profiles(editor) == ALL_PROFILES
        assert user_profiles(airline) == ALL_PROFILES

    def test_existing_clients_keep_their_profile_and_gain_only_ai(self, alembic_cfg):
        command.upgrade(alembic_cfg, "003")
        files = seed_client("files-app", "file_editor")
        flights = seed_client("flight-api", "airline_company")

        command.upgrade(alembic_cfg, "004")

        assert client_profiles(files) == {"file_editor", "ai"}
        assert client_profiles(flights) == {"airline_company", "ai"}
        assert sql("SELECT user_id FROM api_clients WHERE id = :id", id=files)[0][0] is None

    def test_legacy_column_is_kept_but_optional(self, alembic_cfg):
        command.upgrade(alembic_cfg, "003")
        editor = seed_user("editor", "file_editor")
        command.upgrade(alembic_cfg, "004")

        assert legacy_profile("users", editor) == "file_editor"
        sql(
            "INSERT INTO users (id, username, password_hash) VALUES (:id, 'new', 'x')",
            id=uuid.uuid4(),
        )

    def test_unknown_profile_name_is_rejected(self, alembic_cfg):
        command.upgrade(alembic_cfg, "004")
        with pytest.raises(IntegrityError):
            sql("INSERT INTO profiles (name) VALUES ('admin')")

    def test_deleting_a_user_deletes_their_keys_and_links(self, alembic_cfg):
        command.upgrade(alembic_cfg, "004")
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, 'owner', 'x')", id=uid)
        sql(
            "INSERT INTO user_profiles (user_id, profile_id) VALUES (:u, :p)",
            u=uid, p=profile_id("ai"),
        )
        sql(
            "INSERT INTO api_clients (id, name, key_hash, user_id) VALUES (:id, 'owned', :h, :u)",
            id=uuid.uuid4(), h="a" * 64, u=uid,
        )

        sql("DELETE FROM users WHERE id = :id", id=uid)

        assert sql("SELECT count(*) FROM api_clients")[0][0] == 0
        assert sql("SELECT count(*) FROM user_profiles")[0][0] == 0


class TestDowngrade004:
    def test_rows_from_before_004_get_their_exact_profile_back(self, alembic_cfg):
        command.upgrade(alembic_cfg, "003")
        editor = seed_user("editor", "file_editor")
        airline = seed_user("airline", "airline_company")
        flights = seed_client("flight-api", "airline_company")
        command.upgrade(alembic_cfg, "004")

        command.downgrade(alembic_cfg, "003")

        assert legacy_profile("users", editor) == "file_editor"
        assert legacy_profile("users", airline) == "airline_company"
        assert legacy_profile("api_clients", flights) == "airline_company"
        assert {r[0] for r in sql("SELECT name FROM profiles")} == {"file_editor", "airline_company"}
        tables = {r[0] for r in sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        assert not tables & {"user_profiles", "api_client_profiles"}

    def test_rows_with_only_new_profiles_lose_access_instead_of_gaining_it(self, alembic_cfg):
        command.upgrade(alembic_cfg, "004")
        ai_only, mixed, client = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for uid, name in ((ai_only, "ai-only"), (mixed, "mixed")):
            sql(
                "INSERT INTO users (id, username, password_hash, status) VALUES (:id, :n, 'x', 'active')",
                id=uid, n=name,
            )
        for uid, profile in ((ai_only, "ai"), (mixed, "ai"), (mixed, "airline_company")):
            sql("INSERT INTO user_profiles VALUES (:u, :p)", u=uid, p=profile_id(profile))
        sql("INSERT INTO api_clients (id, name, key_hash) VALUES (:id, 'scraper', :h)", id=client, h="b" * 64)
        sql("INSERT INTO api_client_profiles VALUES (:c, :p)", c=client, p=profile_id("linkedin_scraper"))

        command.downgrade(alembic_cfg, "003")

        status = dict(sql("SELECT username, status FROM users"))
        assert status == {"ai-only": "blocked", "mixed": "active"}
        assert legacy_profile("users", mixed) == "airline_company"
        assert sql("SELECT revoked FROM api_clients WHERE id = :id", id=client)[0][0] is True

    def test_round_trip(self, alembic_cfg):
        command.upgrade(alembic_cfg, "003")
        editor = seed_user("editor", "file_editor")
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "003")
        command.upgrade(alembic_cfg, "head")

        assert user_permissions(editor) == {f"trem:{p}" for p in ALL_PROFILES}


class TestUpgrade005:
    def test_every_grant_is_kept_under_the_trem_application(self, alembic_cfg):
        command.upgrade(alembic_cfg, "003")
        editor = seed_user("editor", "file_editor")
        files = seed_client("files-app", "file_editor")
        command.upgrade(alembic_cfg, "005")

        assert user_permissions(editor) == {f"trem:{p}" for p in ALL_PROFILES}
        assert client_permissions(files) == {"trem:file_editor", "trem:ai"}
        assert sql("SELECT count(*) FROM user_permissions WHERE granted_at IS NULL")[0][0] == 0
        assert sql("SELECT count(*) FROM user_permissions WHERE granted_by IS NOT NULL")[0][0] == 0

    def test_admin_exists_but_is_granted_to_no_one(self, alembic_cfg):
        command.upgrade(alembic_cfg, "003")
        seed_user("editor", "file_editor")
        command.upgrade(alembic_cfg, "005")

        names = {r[0] for r in sql("SELECT name FROM permissions")}
        assert names == ALL_PROFILES | {"admin"}
        assert sql(
            "SELECT count(*) FROM user_permissions l JOIN permissions p ON p.id = l.permission_id "
            "WHERE p.name = 'admin'"
        )[0][0] == 0

    def test_new_permissions_need_no_migration(self, alembic_cfg):
        command.upgrade(alembic_cfg, "005")
        app_id = uuid.uuid4()
        sql("INSERT INTO applications (id, slug, name) VALUES (:id, 'financas', 'Finanças')", id=app_id)
        sql("INSERT INTO permissions (name, application_id) VALUES ('ai', :a), ('read', :a)", a=app_id)

        with pytest.raises(IntegrityError):
            sql("INSERT INTO permissions (name, application_id) VALUES ('read', :a)", a=app_id)

    def test_deleting_an_application_removes_its_grants(self, alembic_cfg):
        command.upgrade(alembic_cfg, "005")
        app_id, uid = uuid.uuid4(), uuid.uuid4()
        sql("INSERT INTO applications (id, slug, name) VALUES (:id, 'financas', 'F')", id=app_id)
        perm = sql(
            "INSERT INTO permissions (name, application_id) VALUES ('read', :a) RETURNING id", a=app_id
        )[0][0]
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, 'u', 'x')", id=uid)
        sql("INSERT INTO user_permissions (user_id, permission_id) VALUES (:u, :p)", u=uid, p=perm)

        sql("DELETE FROM applications WHERE id = :id", id=app_id)

        assert user_permissions(uid) == set()

    def test_granted_by_is_cleared_when_the_granter_is_deleted(self, alembic_cfg):
        command.upgrade(alembic_cfg, "005")
        admin, user = uuid.uuid4(), uuid.uuid4()
        for uid, name in ((admin, "admin"), (user, "user")):
            sql("INSERT INTO users (id, username, password_hash) VALUES (:id, :n, 'x')", id=uid, n=name)
        sql(
            "INSERT INTO user_permissions (user_id, permission_id, granted_by) "
            "SELECT :u, id, :a FROM permissions WHERE name = 'ai'",
            u=user, a=admin,
        )

        sql("DELETE FROM users WHERE id = :id", id=admin)

        assert sql("SELECT granted_by FROM user_permissions WHERE user_id = :u", u=user) == [(None,)]


class TestDowngrade005:
    def test_trem_grants_survive_and_the_rest_is_dropped(self, alembic_cfg):
        command.upgrade(alembic_cfg, "005")
        uid, app_id = uuid.uuid4(), uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, 'u', 'x')", id=uid)
        sql("INSERT INTO applications (id, slug, name) VALUES (:id, 'financas', 'F')", id=app_id)
        sql("INSERT INTO permissions (name, application_id) VALUES ('read', :a)", a=app_id)
        sql(
            "INSERT INTO user_permissions (user_id, permission_id) "
            "SELECT :u, id FROM permissions WHERE name IN ('ai', 'admin', 'read')",
            u=uid,
        )

        command.downgrade(alembic_cfg, "004")

        assert user_profiles(uid) == {"ai"}
        assert {r[0] for r in sql("SELECT name FROM profiles")} == ALL_PROFILES
        with pytest.raises(IntegrityError):
            sql("INSERT INTO profiles (name) VALUES ('admin')")


class TestAppOnMigratedSchema:
    """The ORM does not map profile_id: these run the real repositories on the migrated tables."""

    def run(self, coro_factory):
        async def go():
            engine = create_async_engine(DB_URL)
            try:
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    result = await coro_factory(session)
                    await session.commit()
                    return result
            finally:
                await engine.dispose()

        return asyncio.run(go())

    def test_repositories_read_and_write_permission_sets(self, alembic_cfg):
        from app.domain.schemas.user import UserCreate
        from app.repositories.user_repository import UserRepository
        from app.services.api_client_service import ApiClientService
        from app.services.user_service import UserService

        command.upgrade(alembic_cfg, "003")
        seed_user("editor", "file_editor")
        command.upgrade(alembic_cfg, "head")

        async def scenario(session):
            existing = await UserRepository(session).get_by_username("editor")
            created = await UserService(session).create_user(
                UserCreate(username="scraper", permissions=["trem:ai", "trem:linkedin_scraper"])
            )
            sql_status = "UPDATE users SET status = 'active' WHERE username = 'scraper'"
            await session.execute(text(sql_status))
            service = ApiClientService(session)
            client, raw_key = await service.create_client(
                name="scraper-key", permissions=["trem:linkedin_scraper"], user_id=created.id
            )
            authenticated = await service.authenticate(raw_key)
            return existing, created, client, authenticated

        existing, created, client, authenticated = self.run(scenario)

        assert set(existing.permissions) == {f"trem:{p}" for p in ALL_PROFILES}
        assert created.permissions == ["trem:ai", "trem:linkedin_scraper"]
        assert client.user_id == created.id
        assert authenticated.permissions == ["trem:linkedin_scraper"]
        assert sql(
            "SELECT p.name FROM users t JOIN permissions p ON p.id = t.profile_id WHERE t.id = :id",
            id=uuid.UUID(existing.id),
        )[0][0] == "file_editor"
