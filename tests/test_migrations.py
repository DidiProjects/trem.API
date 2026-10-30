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

        assert user_profiles(editor) == ALL_PROFILES


class TestAppOnMigratedSchema:
    """The ORM no longer maps profile_id: these run the real repositories on the migrated tables."""

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

    def test_repositories_read_and_write_profile_sets(self, alembic_cfg):
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
                UserCreate(username="scraper", profile_names=["ai", "linkedin_scraper"])
            )
            sql_status = "UPDATE users SET status = 'active' WHERE username = 'scraper'"
            await session.execute(text(sql_status))
            service = ApiClientService(session)
            client, raw_key = await service.create_client(
                name="scraper-key", profile_names=["linkedin_scraper"], user_id=created.id
            )
            authenticated = await service.authenticate(raw_key)
            return existing, created, client, authenticated

        existing, created, client, authenticated = self.run(scenario)

        assert set(existing.profile_names) == ALL_PROFILES
        assert created.profile_names == ["ai", "linkedin_scraper"]
        assert client.user_id == created.id
        assert authenticated.profile_names == ["linkedin_scraper"]
        assert legacy_profile("users", uuid.UUID(existing.id)) == "file_editor"
