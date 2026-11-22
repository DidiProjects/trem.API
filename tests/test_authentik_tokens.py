"""Bearer tokens from authentik (trem.AUTH) next to trem.API's own, against the suite's SQLite."""
import time
import urllib.error
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWKClientConnectionError, jwks_client
from sqlalchemy import select

from app.core import authentik
from app.core.config import get_settings
from app.core.security import create_access_token, hash_password, verify_password
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import UserModel
from app.main import app
from app.services.api_client_service import ApiClientService
from app.services.authentik_user_service import NO_LOCAL_PASSWORD

ISSUER = "https://auth.example/application/o/trem/"
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key=_key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": "trem", "sub": "hashed-ana", "iat": now, "exp": now + 600,
        "preferred_username": "ana", "email": "ana@example.com", "amr": ["pwd"],
        "groups": ["trem:ai", "hub:didiflix", "authentik Admins"],
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


class _Jwks:
    """Stands for authentik's JWKS endpoint: always our test key."""

    fail = False

    def get_signing_key_from_jwt(self, _token):
        if self.fail:
            raise PyJWKClientConnectionError("down")
        return SimpleNamespace(key=_key.public_key())


@pytest.fixture
async def ctx(db_session, seeded_permissions, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "AUTHENTIK_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "AUTHENTIK_AUDIENCE", "trem")
    jwks = _Jwks()
    monkeypatch.setattr(authentik, "_jwks_client", lambda _url: jwks)

    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    yield SimpleNamespace(session=db_session, jwks=jwks)
    app.dependency_overrides.clear()


def http(bearer: str) -> TestClient:
    return TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {bearer}"})


async def _users(session) -> list[UserModel]:
    return (await session.execute(select(UserModel))).scalars().all()


class TestAuthentikToken:
    async def test_first_call_creates_the_account_with_the_token_groups(self, ctx):
        response = http(token()).get("/me")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["username"] == "ana" and body["email"] == "ana@example.com"
        # Only groups named like permissions; "authentik Admins" means nothing here.
        assert body["permissions"] == ["hub:didiflix", "trem:ai"]
        assert body["must_change_password"] is False
        [user] = await _users(ctx.session)
        assert user.status == "active" and user.password_hash == NO_LOCAL_PASSWORD
        assert not verify_password("", user.password_hash) and not verify_password(NO_LOCAL_PASSWORD, user.password_hash)

    async def test_later_calls_reuse_the_account_and_follow_the_groups(self, ctx):
        first = http(token()).get("/me").json()
        second = http(token(groups=["trem:file_editor"])).get("/me").json()

        assert second["id"] == first["id"]
        assert second["permissions"] == ["trem:file_editor"]
        assert len(await _users(ctx.session)) == 1

    async def test_the_token_groups_are_stored_for_the_personal_api_keys(self, ctx):
        created = http(token(groups=["trem:ai", "trem:file_editor", "hub:unregistered"])).post(
            "/me/api-keys", json={"name": "script", "permissions": ["trem:ai", "trem:file_editor"]}
        )
        assert created.status_code == 201, created.text
        key = created.json()["api_key"]
        [user] = await _users(ctx.session)
        await ctx.session.refresh(user, ["permissions"])
        assert sorted(p.key for p in user.permissions) == ["trem:ai", "trem:file_editor"]
        assert (await ApiClientService(ctx.session).authenticate(key)).permissions == ["trem:ai", "trem:file_editor"]

        # Out of a group in authentik: the key loses it with the owner's next call.
        http(token(groups=["trem:ai"])).get("/me")
        await ctx.session.refresh(user, ["permissions"])
        assert [p.key for p in user.permissions] == ["trem:ai"]
        assert (await ApiClientService(ctx.session).authenticate(key)).permissions == ["trem:ai"]

    async def test_links_an_existing_trem_account_by_username(self, ctx):
        ctx.session.add(UserModel(username="ana", email="old@example.com", password_hash=hash_password("x"),
                                  status="active", must_change_password=False))
        await ctx.session.commit()

        body = http(token()).get("/me").json()

        assert body["email"] == "old@example.com"
        assert body["permissions"] == ["hub:didiflix", "trem:ai"]
        assert len(await _users(ctx.session)) == 1

    async def test_a_blocked_trem_account_stays_blocked(self, ctx):
        ctx.session.add(UserModel(username="ana", email="ana@example.com", password_hash=hash_password("x"),
                                  status="blocked", must_change_password=False))
        await ctx.session.commit()

        assert http(token()).get("/me").status_code == 403

    async def test_an_e_mail_taken_by_another_account_is_left_out(self, ctx):
        ctx.session.add(UserModel(username="outra", email="ana@example.com", password_hash=hash_password("x"),
                                  status="active", must_change_password=False))
        await ctx.session.commit()

        body = http(token()).get("/me").json()

        assert body["username"] == "ana" and body["email"] is None

    async def test_hub_catalog_follows_the_token_groups(self, ctx):
        response = http(token(groups=["hub:nada"])).get("/hub/services")

        assert response.status_code == 200 and response.json() == []

    @pytest.mark.parametrize(
        ("label", "bad"),
        [
            ("expired", {"exp": int(time.time()) - 120, "iat": int(time.time()) - 900}),
            ("another client", {"aud": "didiflix"}),
            ("no subject", {"sub": None}),
            ("no username", {"preferred_username": None}),
        ],
    )
    async def test_rejected(self, ctx, label, bad):
        response = http(token(**bad)).get("/me")

        assert response.status_code == 401, label
        assert await _users(ctx.session) == []

    async def test_a_token_signed_with_another_key_is_rejected(self, ctx):
        assert http(token(key=_other_key)).get("/me").status_code == 401

    async def test_authentik_down_is_503_not_401(self, ctx):
        ctx.jwks.fail = True

        assert http(token()).get("/me").status_code == 503

    async def test_second_factor_counts_for_administration(self, ctx):
        without = http(token(groups=["trem:admin"])).get("/admin/users")
        with_mfa = http(token(groups=["trem:admin"], amr=["pwd", "mfa"])).get("/admin/users")

        assert without.status_code == 403 and "segundo fator" in without.json()["detail"]
        assert with_mfa.status_code == 200, with_mfa.text

    async def test_trem_own_tokens_still_work(self, ctx):
        ctx.session.add(UserModel(username="bia", email="bia@example.com", password_hash=hash_password("x"),
                                  status="active", must_change_password=False))
        await ctx.session.commit()
        bia = (await _users(ctx.session))[0]

        response = http(create_access_token(str(bia.id), "bia", [], False)).get("/me")

        assert response.status_code == 200 and response.json()["username"] == "bia"


async def test_without_the_setting_authentik_tokens_are_not_trusted(db_session, seeded_permissions, monkeypatch):
    monkeypatch.setattr(get_settings(), "AUTHENTIK_ISSUER", "")

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    try:
        assert http(token()).get("/me").status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_is_authentik_token_reads_only_the_issuer(monkeypatch):
    monkeypatch.setattr(get_settings(), "AUTHENTIK_ISSUER", ISSUER)

    assert authentik.is_authentik_token(token())
    assert not authentik.is_authentik_token(token(iss="https://elsewhere/"))
    assert not authentik.is_authentik_token("not-a-jwt")


def test_jwks_are_fetched_with_trem_api_user_agent(monkeypatch):
    # Cloudflare answers 403 to urllib's default "Python-urllib/3.x", which surfaced as a 503 on every call.
    sent = {}

    def fake_urlopen(request, **_kwargs):
        sent["user_agent"] = request.get_header("User-agent")
        raise urllib.error.URLError("stop")

    monkeypatch.setattr(jwks_client.urllib.request, "urlopen", fake_urlopen)
    authentik._jwks_client.cache_clear()
    try:
        with pytest.raises(PyJWKClientConnectionError):
            authentik._jwks_client("https://auth.example/application/o/trem/jwks/").fetch_data()
    finally:
        authentik._jwks_client.cache_clear()
    assert sent["user_agent"] == "trem.API"
