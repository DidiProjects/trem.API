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

PNG_LOGO = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + bytes(8)
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

    def test_hub_catalog_on_postgres(self, alembic_cfg):
        from app.repositories.user_repository import UserRepository
        from app.services.hub_service import HubService

        command.upgrade(alembic_cfg, "head")
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash, status) VALUES (:id, 'ana', 'x', 'active')", id=uid)

        async def scenario(session):
            hub = HubService(session)
            await hub.update("immich", {"url": "http://10.0.0.1:2283"})
            await hub.create("jellyfin", "Jellyfin", None, "https://tv.example", True)
            await hub.set_logo("jellyfin", PNG_LOGO)
            admin = await UserRepository(session).get_by_id(str(uid))
            for slug in ("immich", "jellyfin", "radarr"):
                await hub.grant(admin, slug, str(uid))
            await hub.reorder(["jellyfin", "immich", "radarr", "sonarr", "prowlarr", "qbittorrent"])
            await hub.delete("sonarr")
            return [s.slug for s in await hub.list_all()]

        assert self.run(scenario) == ["jellyfin", "immich", "radarr", "prowlarr", "qbittorrent"]
        assert user_permissions(uid) == {"hub:immich", "hub:jellyfin", "hub:radarr"}
        assert sql("SELECT logo_type FROM hub_services WHERE name = 'Jellyfin'")[0][0] == "image/png"

    def test_oidc_clients_on_postgres(self, alembic_cfg):
        from app.services.oidc_client_service import OidcClientService, hash_secret, secret_matches

        command.upgrade(alembic_cfg, "head")
        uri = "http://100.64.0.1:8096/sso/OID/redirect/trem"

        async def scenario(session):
            clients = OidcClientService(session)
            _, secret = await clients.configure("immich", [uri])
            await clients.configure("immich", [uri, "https://fotos.example/cb"])
            client = await clients.get("immich")
            return secret, client.client_id, client.redirect_uris, secret_matches(client, secret)

        secret, client_id, uris, matches = self.run(scenario)
        assert client_id == "hub.immich" and uris == [uri, "https://fotos.example/cb"] and matches
        assert sql("SELECT secret_hash FROM oidc_clients")[0][0] == hash_secret(secret)

    def test_oidc_login_on_postgres(self, alembic_cfg):
        from app.repositories.user_repository import UserRepository
        from app.services.hub_service import HubService
        from app.services.oidc_client_service import OidcClientService
        from app.services.oidc_provider_service import (
            AuthorizationRequest, OAuthError, OidcProviderService, TokenRequest, s256,
        )

        command.upgrade(alembic_cfg, "head")
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash, status) VALUES (:id, 'ana', 'x', 'active')", id=uid)
        uri, verifier = "http://100.64.0.1:8096/cb", "v" * 43

        async def approve(session):
            _, secret = await OidcClientService(session).configure("immich", [uri])
            user = await UserRepository(session).get_by_id(str(uid))
            await HubService(session).grant(user, "immich", str(uid))
            provider = OidcProviderService(session)
            target = await provider.authorize(AuthorizationRequest(
                "hub.immich", uri, "code", "openid profile", "st", None, s256(verifier), "S256",
            ))
            raw = target.split("request=", 1)[1]
            user = await UserRepository(session).get_by_id(str(uid))
            code = (await provider.approve(raw, user)).split("code=", 1)[1].split("&", 1)[0]
            return secret, code

        secret, code = self.run(approve)

        async def exchange(session, verifier_used):
            provider = OidcProviderService(session)
            client = await provider.authenticate_client("hub.immich", secret)
            try:
                return await provider.exchange(client, TokenRequest("authorization_code", code, uri, verifier_used))
            except OAuthError as e:
                return e.error

        assert self.run(lambda s: exchange(s, "w" * 43)) == "invalid_grant"
        assert self.run(lambda s: exchange(s, verifier)) == "invalid_grant"
        assert sql("SELECT used_at IS NOT NULL, user_id FROM oidc_grants")[0] == (True, uid)

    def test_login_refresh_and_reuse_on_postgres(self, alembic_cfg):
        """The atomic claim is an UPDATE ... WHERE revoked = false: its rowcount matters on PG."""
        from app.core.exceptions import InvalidCredentialsError
        from app.core.security import hash_password
        from app.services.auth_service import AuthService
        from app.services.login_throttle import LoginThrottle

        command.upgrade(alembic_cfg, "head")
        sql(
            "INSERT INTO users (id, username, password_hash, status, must_change_password) "
            "VALUES (:id, 'ana', :h, 'active', false)",
            id=uuid.uuid4(), h=hash_password("SenhaValida@123"),
        )
        throttle = LoginThrottle(per_username=10, per_ip=10, window_seconds=60)

        async def login(session):
            return (await AuthService(session, throttle=throttle).login("ana", "SenhaValida@123"))[1]

        first = self.run(login)

        async def refresh(session):
            return (await AuthService(session, throttle=throttle).refresh(first))[1]

        second = self.run(refresh)

        async def reuse(session):
            with pytest.raises(InvalidCredentialsError):
                await AuthService(session, throttle=throttle).refresh(first)

        self.run(reuse)
        revoked = sql("SELECT revoked, replaced_by IS NOT NULL FROM refresh_tokens ORDER BY created_at")
        assert revoked == [(True, True), (True, False)]
        assert second


    def test_sign_up_and_admin_lookup_on_postgres(self, alembic_cfg):
        from app.services.rate_limit import SlidingWindowLimiter
        from app.services.signup_service import SignupService

        command.upgrade(alembic_cfg, "head")
        admin = uuid.uuid4()
        sql(
            "INSERT INTO users (id, username, email, password_hash, status) "
            "VALUES (:id, 'chefe', 'chefe@example.com', 'x', 'active')",
            id=admin,
        )
        sql(
            "INSERT INTO user_permissions (user_id, permission_id) "
            "SELECT :u, id FROM permissions WHERE name = 'admin'",
            u=admin,
        )
        sent = []

        class Email:
            def __getattr__(self, name):
                async def send(**kwargs):
                    sent.append((name, kwargs))
                return send

        limiter = SlidingWindowLimiter(10, 60)

        async def register(session):
            service = SignupService(session, Email(), limiter)
            await service.register("nova", "Nova@Example.com", "cavalo bateria grampo", "1.2.3.4")
            for send, kwargs in service.outbox:
                await send(**kwargs)

        self.run(register)
        token = sent[0][1]["link"].split("#verify-email=", 1)[1]

        async def verify(session):
            service = SignupService(session, Email(), limiter)
            await service.verify_email(token)
            for send, kwargs in service.outbox:
                await send(**kwargs)

        self.run(verify)
        assert sql("SELECT status, email FROM users WHERE username = 'nova'") == [("pending_approval", "nova@example.com")]
        assert [k["to"] for name, k in sent if name == "send_signup_pending_approval"] == ["chefe@example.com"]

    def test_totp_replay_and_recovery_codes_on_postgres(self, alembic_cfg, monkeypatch):
        import base64
        import secrets
        import time

        from app.core import totp
        from app.repositories.user_repository import UserRepository
        from app.services.mfa_service import MfaService
        from app.services.rate_limit import SlidingWindowLimiter

        command.upgrade(alembic_cfg, "head")
        monkeypatch.setattr(get_settings(), "MFA_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        from app.core.security import hash_password
        uid = uuid.uuid4()
        sql(
            "INSERT INTO users (id, username, password_hash, status) VALUES (:id, 'ana', :h, 'active')",
            id=uid, h=hash_password("cavalo bateria grampo"),
        )
        limiter = SlidingWindowLimiter(100, 60)

        async def enroll(session):
            user = await UserRepository(session).get_by_id(str(uid))
            secret, _ = await MfaService(session, limiter).enroll(user, "cavalo bateria grampo")
            codes = await MfaService(session, limiter).confirm(user, totp.code_at(secret, time.time()))
            return secret, codes

        secret, codes = self.run(enroll)
        code = totp.code_at(secret, time.time() + 30)

        async def check(session, **kwargs):
            return await MfaService(session, limiter).check(str(uid), **kwargs)

        assert self.run(lambda s: check(s, code=code)) is True
        assert self.run(lambda s: check(s, code=code)) is False
        assert self.run(lambda s: check(s, recovery_code=codes[0])) is True
        assert self.run(lambda s: check(s, recovery_code=codes[0])) is False

class TestUpgrade006:
    def test_existing_tokens_become_single_token_families_of_trem(self, alembic_cfg):
        command.upgrade(alembic_cfg, "005")
        uid, tid = uuid.uuid4(), uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, 'u', 'x')", id=uid)
        sql(
            "INSERT INTO refresh_tokens (id, user_id, token_hash, expires_at) "
            "VALUES (:id, :u, 'h', now() + interval '1 day')",
            id=tid, u=uid,
        )

        command.upgrade(alembic_cfg, "006")

        row = sql(
            "SELECT t.family_id, a.slug, t.replaced_by FROM refresh_tokens t "
            "JOIN applications a ON a.id = t.application_id"
        )[0]
        assert row == (tid, "trem", None)

    def test_round_trip(self, alembic_cfg):
        command.upgrade(alembic_cfg, "006")
        command.downgrade(alembic_cfg, "005")
        command.upgrade(alembic_cfg, "006")
        columns = {r[0] for r in sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'refresh_tokens'"
        )}
        assert {"family_id", "replaced_by", "application_id"} <= columns


class TestUpgrade007:
    def test_new_statuses_and_email_tokens(self, alembic_cfg):
        command.upgrade(alembic_cfg, "007")
        uid = uuid.uuid4()
        sql(
            "INSERT INTO users (id, username, email, password_hash, status) "
            "VALUES (:id, 'nova', 'nova@example.com', 'x', 'pending_verification')",
            id=uid,
        )
        sql(
            "INSERT INTO email_tokens (user_id, purpose, token_hash, expires_at) "
            "VALUES (:u, 'verify_email', :h, now() + interval '1 day')",
            u=uid, h="a" * 64,
        )
        with pytest.raises(IntegrityError):
            sql("UPDATE users SET status = 'inventado' WHERE id = :id", id=uid)

        sql("DELETE FROM users WHERE id = :id", id=uid)
        assert sql("SELECT count(*) FROM email_tokens")[0][0] == 0

    def test_existing_accounts_keep_their_status(self, alembic_cfg):
        command.upgrade(alembic_cfg, "006")
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash, status) VALUES (:id, 'u', 'x', 'active')", id=uid)
        command.upgrade(alembic_cfg, "007")
        assert sql("SELECT status, email_verified_at FROM users")[0] == ("active", None)

    def test_downgrade_blocks_unapproved_sign_ups(self, alembic_cfg):
        command.upgrade(alembic_cfg, "007")
        for name, status in (("a", "active"), ("p", "pending_approval"), ("r", "rejected")):
            sql(
                "INSERT INTO users (id, username, password_hash, status) VALUES (:id, :n, 'x', :s)",
                id=uuid.uuid4(), n=name, s=status,
            )
        command.downgrade(alembic_cfg, "006")
        assert dict(sql("SELECT username, status FROM users")) == {"a": "active", "p": "blocked", "r": "blocked"}
        command.upgrade(alembic_cfg, "007")


class TestUpgrade008:
    def test_existing_sessions_are_password_only(self, alembic_cfg):
        command.upgrade(alembic_cfg, "007")
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, 'u', 'x')", id=uid)
        sql(
            "INSERT INTO refresh_tokens (user_id, token_hash, expires_at, family_id, application_id) "
            "SELECT :u, 'h', now() + interval '1 day', gen_random_uuid(), id FROM applications",
            u=uid,
        )
        command.upgrade(alembic_cfg, "008")
        assert sql("SELECT amr FROM refresh_tokens") == [("pwd",)]
        assert sql("SELECT totp_secret, totp_enabled_at FROM users") == [(None, None)]

    def test_recovery_codes_go_with_the_user(self, alembic_cfg):
        command.upgrade(alembic_cfg, "008")
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, 'u', 'x')", id=uid)
        sql("INSERT INTO mfa_recovery_codes (user_id, code_hash) VALUES (:u, :h)", u=uid, h="c" * 64)
        sql("DELETE FROM users WHERE id = :id", id=uid)
        assert sql("SELECT count(*) FROM mfa_recovery_codes")[0][0] == 0

    def test_round_trip(self, alembic_cfg):
        command.upgrade(alembic_cfg, "008")
        command.downgrade(alembic_cfg, "007")
        command.upgrade(alembic_cfg, "008")


class TestUpgrade009:
    def _user(self, name):
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, :n, 'x')", id=uid, n=name)
        return uid

    def _key(self, name, owner=None, revoked=False):
        sql(
            "INSERT INTO api_clients (name, key_hash, user_id, revoked) VALUES (:n, :h, :u, :r)",
            n=name, h=uuid.uuid4().hex * 2, u=owner, r=revoked,
        )

    def test_names_are_unique_per_owner_among_active_keys(self, alembic_cfg):
        command.upgrade(alembic_cfg, "009")
        ana, bia = self._user("ana"), self._user("bia")
        self._key("notebook", ana)
        self._key("notebook", bia)
        self._key("notebook", ana, revoked=True)
        with pytest.raises(IntegrityError):
            self._key("notebook", ana)

    def test_service_names_stay_unique_among_active_keys(self, alembic_cfg):
        command.upgrade(alembic_cfg, "009")
        self._key("flight-api")
        self._key("flight-api", revoked=True)
        with pytest.raises(IntegrityError):
            self._key("flight-api")

    def test_downgrade_renames_duplicates(self, alembic_cfg):
        command.upgrade(alembic_cfg, "009")
        ana, bia = self._user("ana"), self._user("bia")
        self._key("notebook", ana)
        self._key("notebook", bia)
        command.downgrade(alembic_cfg, "008")
        names = sorted(r[0] for r in sql("SELECT name FROM api_clients"))
        assert len(set(names)) == 2 and all(n.startswith("notebook-") for n in names)
        command.upgrade(alembic_cfg, "009")


class TestUpgrade010:
    def _hub_permission(self, name):
        sql("""
            INSERT INTO applications (slug, name) VALUES ('hub', 'trem.FRONT') ON CONFLICT (slug) DO NOTHING
        """)
        sql("""
            INSERT INTO permissions (name, application_id) SELECT :n, id FROM applications WHERE slug = 'hub'
        """, n=name)

    def test_seeds_the_known_services_without_an_address(self, alembic_cfg):
        command.upgrade(alembic_cfg, "010")
        rows = sql("""
            SELECT p.name, s.name, s.url, s.enabled, s.position, s.logo_type, length(s.logo) > 0
            FROM hub_services s JOIN permissions p ON p.id = s.permission_id ORDER BY s.position
        """)
        assert [r[0] for r in rows] == ["immich", "radarr", "sonarr", "prowlarr", "qbittorrent"]
        assert all(r[2] is None and r[3] and r[5] == "image/svg+xml" and r[6] for r in rows)

    def test_existing_hub_permissions_and_grants_are_kept(self, alembic_cfg):
        command.upgrade(alembic_cfg, "009")
        self._hub_permission("immich")
        self._hub_permission("jellyfin")
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, 'ana', 'x')", id=uid)
        sql("""
            INSERT INTO user_permissions (user_id, permission_id)
            SELECT :u, p.id FROM permissions p JOIN applications a ON a.id = p.application_id
            WHERE a.slug = 'hub' AND p.name IN ('immich', 'jellyfin')
        """, u=uid)

        command.upgrade(alembic_cfg, "010")

        assert user_permissions(uid) == {"hub:immich", "hub:jellyfin"}
        rows = dict(sql("""
            SELECT p.name, s.position FROM hub_services s JOIN permissions p ON p.id = s.permission_id
        """))
        assert set(rows) == {"immich", "radarr", "sonarr", "prowlarr", "qbittorrent", "jellyfin"}
        assert rows["jellyfin"] == 5
        assert sql("SELECT count(*) FROM applications WHERE slug = 'hub'")[0][0] == 1

    def test_deleting_the_permission_deletes_the_service(self, alembic_cfg):
        command.upgrade(alembic_cfg, "010")
        sql("""
            DELETE FROM permissions WHERE name = 'radarr'
            AND application_id = (SELECT id FROM applications WHERE slug = 'hub')
        """)
        assert sql("SELECT count(*) FROM hub_services")[0][0] == 4

    def test_round_trip(self, alembic_cfg):
        command.upgrade(alembic_cfg, "010")
        command.downgrade(alembic_cfg, "009")
        assert sql("SELECT count(*) FROM permissions p JOIN applications a ON a.id = p.application_id "
                   "WHERE a.slug = 'hub'")[0][0] == 5
        command.upgrade(alembic_cfg, "010")
        assert sql("SELECT count(*) FROM hub_services")[0][0] == 5


class TestUpgrade011:
    def test_removing_the_service_removes_its_client(self, alembic_cfg):
        command.upgrade(alembic_cfg, "011")
        sql("""
            INSERT INTO oidc_clients (hub_service_id, secret_hash, redirect_uris)
            SELECT s.id, repeat('a', 64), '["https://tv.example/cb"]'
            FROM hub_services s JOIN permissions p ON p.id = s.permission_id WHERE p.name = 'immich'
        """)
        sql("DELETE FROM hub_services WHERE permission_id = (SELECT id FROM permissions WHERE name = 'immich')")
        assert sql("SELECT count(*) FROM oidc_clients")[0][0] == 0

    def test_one_client_per_service(self, alembic_cfg):
        command.upgrade(alembic_cfg, "011")
        insert = """
            INSERT INTO oidc_clients (hub_service_id, secret_hash, redirect_uris)
            SELECT s.id, repeat('a', 64), '[]'
            FROM hub_services s JOIN permissions p ON p.id = s.permission_id WHERE p.name = 'immich'
        """
        sql(insert)
        with pytest.raises(IntegrityError):
            sql(insert)

    def test_round_trip(self, alembic_cfg):
        command.upgrade(alembic_cfg, "011")
        command.downgrade(alembic_cfg, "010")
        assert sql("SELECT to_regclass('oidc_clients')")[0][0] is None
        command.upgrade(alembic_cfg, "011")
        assert sql("SELECT count(*) FROM oidc_clients")[0][0] == 0


class TestUpgrade012:
    def test_grants_go_with_the_client_and_the_user(self, alembic_cfg):
        command.upgrade(alembic_cfg, "012")
        uid = uuid.uuid4()
        sql("INSERT INTO users (id, username, password_hash) VALUES (:id, 'ana', 'x')", id=uid)
        sql("""
            INSERT INTO oidc_clients (hub_service_id, secret_hash, redirect_uris)
            SELECT s.id, repeat('a', 64), '["https://tv.example/cb"]'
            FROM hub_services s JOIN permissions p ON p.id = s.permission_id WHERE p.name = 'immich'
        """)
        for n, user in enumerate((uid, None)):
            sql("""
                INSERT INTO oidc_grants (oidc_client_id, request_hash, redirect_uri, scope, state,
                                         code_challenge, expires_at, user_id)
                SELECT id, :h, 'https://tv.example/cb', 'openid', 's', :c, now(), :u FROM oidc_clients
            """, h=str(n) * 64, c="c" * 43, u=user)
        sql("DELETE FROM users WHERE id = :id", id=uid)
        assert sql("SELECT count(*) FROM oidc_grants")[0][0] == 1
        sql("DELETE FROM oidc_clients")
        assert sql("SELECT count(*) FROM oidc_grants")[0][0] == 0

    def test_round_trip(self, alembic_cfg):
        command.upgrade(alembic_cfg, "012")
        command.downgrade(alembic_cfg, "011")
        assert sql("SELECT to_regclass('oidc_grants')")[0][0] is None
        command.upgrade(alembic_cfg, "012")
