"""trem.API as an OpenID Connect provider (/oauth, /.well-known/openid-configuration) against the suite's SQLite."""
import base64
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.security import create_access_token, hash_password
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import OidcGrantModel, PermissionModel, UserModel
from app.main import app
from app.services.oidc_provider_service import s256
from tests.conftest import TEST_PUBLIC_KEY

URI = "http://100.64.0.1:8096/sso/OID/redirect/trem"
CLIENT = "hub.didiflix"
ISSUER = "http://testserver"


class Flow:
    def __init__(self, admin, ana, ana_id, db_session, secret):
        self.admin, self.ana, self.ana_id, self.db, self.secret = admin, ana, ana_id, db_session, secret
        self.browser = TestClient(app, base_url="https://testserver", follow_redirects=False)
        self.verifier = secrets.token_urlsafe(48)

    def params(self, **overrides):
        params = {
            "client_id": CLIENT, "redirect_uri": URI, "response_type": "code", "scope": "openid profile email",
            "state": "st-123", "nonce": "n-456", "code_challenge": s256(self.verifier), "code_challenge_method": "S256",
        }
        params.update(overrides)
        return {k: v for k, v in params.items() if v is not None}

    def authorize(self, **overrides):
        return self.browser.get("/oauth/authorize", params=self.params(**overrides))

    def request_id(self, **overrides) -> str:
        response = self.authorize(**overrides)
        assert response.status_code == 302, response.text
        target = urlsplit(response.headers["location"])
        assert target.path == "/sso", response.headers["location"]
        return parse_qs(target.query)["request"][0]

    def code(self, http=None, **overrides) -> str:
        approved = (http or self.ana).post(f"/oauth/authorize/{self.request_id(**overrides)}/approve")
        assert approved.status_code == 200, approved.text
        query = parse_qs(urlsplit(approved.json()["redirect"]).query)
        assert query["state"] == ["st-123"] and query["iss"] == [ISSUER]
        return query["code"][0]

    def token(self, code, verifier=None, redirect_uri=URI, secret=None, client_id=CLIENT, basic=True, **extra):
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": verifier or self.verifier, **extra}
        headers = {}
        if basic:
            raw = f"{client_id}:{secret or self.secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            form.update(client_id=client_id, client_secret=secret or self.secret)
        return self.browser.post("/oauth/token", data=form, headers=headers)


@pytest.fixture
async def flow(db_session, seeded_permissions):
    admin_permission = await db_session.get(PermissionModel, uuid.UUID(seeded_permissions["trem:admin"]))
    admin = UserModel(username="chefe", email="chefe@example.com", password_hash=hash_password("x"),
                      status="active", must_change_password=False, permissions=[admin_permission],
                      email_verified_at=datetime.now(timezone.utc))
    ana = UserModel(username="ana", email="ana@example.com", password_hash=hash_password("x"),
                    status="active", must_change_password=False)
    bia = UserModel(username="bia", email=None, password_hash=hash_password("x"),
                    status="active", must_change_password=False)
    db_session.add_all([admin, ana, bia])
    await db_session.commit()

    async def _db():
        # Like get_db: a raised error rolls back, so a code spent by a failed exchange must
        # be committed by a returned error, not lost with a raised one.
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    app.dependency_overrides[get_db] = _db

    def http(user, perms, amr=("pwd",)):
        token = create_access_token(str(user.id), user.username, perms, False, amr=amr)
        return TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {token}"})

    admin_http = http(admin, ["trem:admin"], ("pwd", "otp"))
    assert admin_http.post("/admin/hub/services", json={"slug": "didiflix", "name": "Didiflix"}).status_code == 201
    secret = admin_http.put("/admin/hub/services/didiflix/oidc", json={"redirect_uris": [URI]}).json()["client_secret"]
    for user in (admin, ana):
        assert admin_http.put(f"/admin/hub/services/didiflix/users/{user.id}").status_code == 200

    f = Flow(admin_http, http(ana, []), str(ana.id), db_session, secret)
    f.bia = http(bia, [])
    yield f
    app.dependency_overrides.clear()


def _claims(token: str, audience: str) -> dict:
    return jwt.decode(token, TEST_PUBLIC_KEY, algorithms=["RS256"], audience=audience, issuer=ISSUER)


def _error(response) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


class TestDiscovery:
    def test_announces_the_endpoints_and_what_is_supported(self):
        config = TestClient(app).get("/.well-known/openid-configuration").json()
        assert config["issuer"] == ISSUER
        assert config["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
        assert config["token_endpoint"] == f"{ISSUER}/oauth/token"
        assert config["jwks_uri"] == f"{ISSUER}/.well-known/jwks.json"
        assert config["code_challenge_methods_supported"] == ["S256"]
        assert config["response_types_supported"] == ["code"]


class TestFullLogin:
    async def test_code_becomes_tokens_for_the_account(self, flow):
        response = flow.token(flow.code())
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        tokens = response.json()
        identity = _claims(tokens["id_token"], CLIENT)
        assert identity["sub"] == flow.ana_id and identity["nonce"] == "n-456"
        assert identity["preferred_username"] == "ana" and identity["groups"] == []
        assert identity["email"] == "ana@example.com" and identity["email_verified"] is False
        assert identity["azp"] == CLIENT and identity["amr"] == ["pwd"]

        info = flow.browser.get("/oauth/userinfo", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert info.status_code == 200
        assert info.json() == {"sub": flow.ana_id, "preferred_username": "ana", "name": "ana", "groups": [],
                               "email": "ana@example.com", "email_verified": False}

    async def test_admin_of_trem_is_in_the_admin_group(self, flow):
        identity = _claims(flow.token(flow.code(flow.admin)).json()["id_token"], CLIENT)
        assert identity["groups"] == ["admin"] and identity["amr"] == ["pwd", "otp"]
        assert identity["email_verified"] is True

    async def test_only_the_scopes_asked_and_known(self, flow):
        tokens = flow.token(flow.code(scope="openid offline_access")).json()
        assert tokens["scope"] == "openid"
        identity = _claims(tokens["id_token"], CLIENT)
        assert "preferred_username" not in identity and "email" not in identity

    async def test_client_secret_post_works_too(self, flow):
        assert flow.token(flow.code(), basic=False).status_code == 200

    async def test_view_tells_the_front_what_to_show(self, flow):
        view = flow.ana.get(f"/oauth/authorize/{flow.request_id()}").json()
        assert view == {"service_slug": "didiflix", "service_name": "Didiflix", "logo_url": None,
                        "username": "ana", "allowed": True}


class TestAuthorizeRequest:
    async def test_unknown_client_never_redirects_to_the_given_uri(self, flow):
        response = flow.authorize(client_id="hub.nada")
        assert response.headers["location"] == "http://testserver/sso?error=invalid_client"
        assert flow.authorize(client_id="didiflix").headers["location"].endswith("error=invalid_client")

    @pytest.mark.parametrize("uri", [URI + "/x", URI + "?next=https://evil.example", URI.upper(), "https://evil.example/cb", None])
    async def test_redirect_uri_must_match_exactly(self, flow, uri):
        response = flow.authorize(redirect_uri=uri)
        assert response.headers["location"] == "http://testserver/sso?error=invalid_redirect_uri"

    @pytest.mark.parametrize("override, error", [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"scope": "profile"}, "invalid_scope"),
        ({"state": None}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
    ])
    async def test_other_errors_go_back_to_the_service(self, flow, override, error):
        response = flow.authorize(**override)
        assert response.headers["location"].startswith(URI + "?")
        query = _error(response)
        assert query["error"] == error and query["iss"] == ISSUER
        assert query.get("state") == override.get("state", "st-123")

    async def test_a_disabled_service_refuses(self, flow):
        flow.admin.patch("/admin/hub/services/didiflix", json={"enabled": False})
        assert _error(flow.authorize())["error"] == "unauthorized_client"


class TestApproval:
    async def test_an_account_without_the_service_cannot_approve(self, flow):
        request_id = flow.request_id()
        assert flow.bia.get(f"/oauth/authorize/{request_id}").json()["allowed"] is False
        assert flow.bia.post(f"/oauth/authorize/{request_id}/approve").status_code == 403
        assert flow.ana.post(f"/oauth/authorize/{request_id}/approve").status_code == 200

    async def test_a_request_is_answered_once(self, flow):
        request_id = flow.request_id()
        assert flow.ana.post(f"/oauth/authorize/{request_id}/approve").status_code == 200
        assert flow.ana.post(f"/oauth/authorize/{request_id}/approve").status_code == 404
        assert flow.ana.get(f"/oauth/authorize/{request_id}").status_code == 404

    async def test_an_expired_request_is_gone(self, flow):
        request_id = flow.request_id()
        await flow.db.execute(update(OidcGrantModel).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await flow.db.commit()
        assert flow.ana.post(f"/oauth/authorize/{request_id}/approve").status_code == 404

    async def test_deny_tells_the_service_and_ends_the_request(self, flow):
        request_id = flow.request_id()
        denied = flow.ana.post(f"/oauth/authorize/{request_id}/deny").json()["redirect"]
        assert denied.startswith(URI + "?")
        assert parse_qs(urlsplit(denied).query)["error"] == ["access_denied"]
        assert flow.ana.post(f"/oauth/authorize/{request_id}/approve").status_code == 404

    async def test_needs_a_logged_in_account(self, flow):
        request_id = flow.request_id()
        assert flow.browser.post(f"/oauth/authorize/{request_id}/approve").status_code == 401
        assert flow.browser.get(f"/oauth/authorize/{request_id}").status_code == 401


class TestTokenExchange:
    async def test_wrong_secret_is_invalid_client(self, flow):
        response = flow.token(flow.code(), secret="nope")
        assert response.status_code == 401 and response.json()["error"] == "invalid_client"
        assert response.headers["www-authenticate"].startswith("Basic")
        assert flow.token(flow.code(), secret="nope", basic=False).status_code == 401

    async def test_a_code_works_once(self, flow):
        code = flow.code()
        assert flow.token(code).status_code == 200
        again = flow.token(code)
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"

    async def test_a_wrong_verifier_spends_the_code(self, flow):
        code = flow.code()
        assert flow.token(code, verifier=secrets.token_urlsafe(48)).json()["error"] == "invalid_grant"
        assert flow.token(code).json()["error"] == "invalid_grant"

    @pytest.mark.parametrize("verifier", ["short", "a" * 129, "á" * 50])
    async def test_malformed_verifier(self, flow, verifier):
        assert flow.token(flow.code(), verifier=verifier).json()["error"] == "invalid_grant"

    async def test_redirect_uri_must_be_the_one_of_the_request(self, flow):
        assert flow.token(flow.code(), redirect_uri=URI + "/x").json()["error"] == "invalid_grant"

    async def test_an_expired_code_is_refused(self, flow):
        code = flow.code()
        await flow.db.execute(update(OidcGrantModel).values(code_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await flow.db.commit()
        assert flow.token(code).json()["error"] == "invalid_grant"

    async def test_a_code_of_another_client_is_refused(self, flow):
        flow.admin.post("/admin/hub/services", json={"slug": "seerr", "name": "Seerr"})
        other = flow.admin.put("/admin/hub/services/seerr/oidc", json={"redirect_uris": [URI]}).json()["client_secret"]
        response = flow.token(flow.code(), client_id="hub.seerr", secret=other)
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant"

    async def test_access_removed_before_the_exchange(self, flow):
        code = flow.code()
        assert flow.admin.delete(f"/admin/hub/services/didiflix/users/{flow.ana_id}").status_code == 200
        assert flow.token(code).json()["error"] == "invalid_grant"

    async def test_only_the_authorization_code_grant(self, flow):
        response = flow.token(flow.code(), grant_type="password")
        assert response.json()["error"] == "unsupported_grant_type"

    async def test_codes_and_requests_are_stored_only_as_hashes(self, flow):
        code = flow.code()
        stored = (await flow.db.execute(select(OidcGrantModel))).scalars().all()
        assert all(code not in (g.code_hash or "") and code not in g.request_hash for g in stored)


class TestTokensStayInTheirLane:
    async def test_userinfo_refuses_other_tokens(self, flow):
        tokens = flow.token(flow.code()).json()
        trem_token = create_access_token(flow.ana_id, "ana", [], False)
        for token in (trem_token, tokens["id_token"], "garbage"):
            response = flow.browser.get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401 and response.headers["www-authenticate"].startswith("Bearer")
        assert flow.browser.get("/oauth/userinfo").status_code == 401

    async def test_the_service_token_opens_no_trem_route(self, flow):
        access = flow.token(flow.code()).json()["access_token"]
        assert flow.browser.get("/hub/services", headers={"Authorization": f"Bearer {access}"}).status_code == 401

    async def test_userinfo_follows_access_being_removed(self, flow):
        access = flow.token(flow.code()).json()["access_token"]
        flow.admin.delete(f"/admin/hub/services/didiflix/users/{flow.ana_id}")
        assert flow.browser.post("/oauth/userinfo", headers={"Authorization": f"Bearer {access}"}).status_code == 401
