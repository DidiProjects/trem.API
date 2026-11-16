"""
Cadastro pelo próprio usuário, contra o SQLite da suíte: /auth/register,
/auth/verify-email, /auth/resend-verification e o login das contas pendentes.

O cliente de e-mail é trocado por um que só registra as mensagens; as entregas
rodam como background task, depois da resposta, como em produção.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.api.v1.routers.signup import get_email_client
from app.core.config import get_settings
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import EmailTokenModel, PermissionModel, UserModel
from app.main import app
from app.services.login_throttle import LoginThrottle
from app.services.rate_limit import SlidingWindowLimiter

PASSWORD = "cavalo bateria grampo"


class FakeEmail:
    def __init__(self):
        self.sent = []
        self.fail = False

    def __getattr__(self, name):
        if not name.startswith("send_"):
            raise AttributeError(name)

        async def send(**kwargs):
            if self.fail:
                raise RuntimeError("smtp down")
            self.sent.append((name, kwargs))
        send.__name__ = name
        return send

    def of(self, kind):
        return [kwargs for name, kwargs in self.sent if name == kind]

    def token(self):
        link = self.of("send_email_verification")[-1]["link"]
        return link.split("#verify-email=", 1)[1]


@pytest.fixture
def email():
    return FakeEmail()


@pytest.fixture
def http(db_session, seeded_permissions, email, monkeypatch):
    async def _db():
        yield db_session
        await db_session.commit()

    monkeypatch.setattr(get_settings(), "ADMIN_EMAIL", "dono@example.com")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_email_client] = lambda: email
    limiter = SlidingWindowLimiter(limit=3, window_seconds=3600)
    throttle = LoginThrottle(per_username=50, per_ip=50, window_seconds=60)
    with patch("app.services.signup_service.get_signup_limiter", return_value=limiter), patch(
        "app.services.auth_service.get_login_throttle", return_value=throttle
    ):
        yield TestClient(app, base_url="https://testserver")
    app.dependency_overrides.clear()


def _register(http, username="joana", mail="joana@example.com", password=PASSWORD, ip="198.51.100.1"):
    return http.post(
        "/auth/register",
        json={"username": username, "email": mail, "password": password},
        headers={"CF-Connecting-IP": ip},
    )


async def _user(db_session, username):
    return (await db_session.execute(select(UserModel).where(UserModel.username == username))).scalar_one_or_none()


class TestRegister:
    async def test_creates_a_pending_account_without_permissions(self, http, db_session, email):
        response = _register(http)

        assert response.status_code == 202
        user = await _user(db_session, "joana")
        assert user.status == "pending_verification"
        assert user.permissions == []
        assert user.must_change_password is False
        assert user.password_hash.startswith("$argon2id$")
        assert email.of("send_email_verification")[0]["to"] == "joana@example.com"

    def test_link_carries_the_token_in_the_fragment(self, http, email):
        _register(http)
        link = email.of("send_email_verification")[0]["link"]
        assert link.startswith("http://testserver/#verify-email=")
        assert "?" not in link  # never in the query string, which lands in logs

    async def test_only_the_hash_of_the_token_is_stored(self, http, db_session, email):
        _register(http)
        stored = (await db_session.execute(select(EmailTokenModel))).scalar_one()
        assert stored.token_hash != email.token()
        assert len(stored.token_hash) == 64

    def test_email_is_normalized(self, http, email):
        _register(http, mail="Joana@Example.COM")
        assert email.of("send_email_verification")[0]["to"] == "joana@example.com"

    def test_taken_email_gets_the_same_answer(self, http, email):
        first = _register(http)
        second = _register(http, username="outra")
        assert second.status_code == first.status_code == 202
        assert second.json() == first.json()
        assert email.of("send_signup_conflict") == [
            {"to": "joana@example.com", "reason": "email", "login_url": "http://testserver"}
        ]

    async def test_taken_username_is_told_to_the_new_address_only(self, http, db_session, email):
        _register(http)
        response = _register(http, mail="intrusa@example.com")
        assert response.status_code == 202
        (conflict,) = email.of("send_signup_conflict")
        assert conflict["to"] == "intrusa@example.com" and conflict["reason"] == "username"
        assert (await _user(db_session, "joana")).email == "joana@example.com"

    async def test_abandoned_sign_up_frees_the_username(self, http, db_session):
        _register(http)
        await db_session.execute(
            update(UserModel).values(created_at=datetime.now(timezone.utc) - timedelta(days=2))
        )
        await db_session.commit()

        assert _register(http, mail="nova@example.com").status_code == 202
        assert (await _user(db_session, "joana")).email == "nova@example.com"

    @pytest.mark.parametrize("password", ["curta demais", "a" * 20, "senha da joana ok", "x" * 129])
    def test_weak_password_is_422(self, http, password):
        if password == "curta demais":
            password = "curta"
        assert _register(http, password=password).status_code == 422

    @pytest.mark.parametrize("body", [
        {"username": "jo", "email": "a@example.com", "password": PASSWORD},
        {"username": "joana", "email": "nao-e-email", "password": PASSWORD},
        {"username": "jo ana", "email": "a@example.com", "password": PASSWORD},
    ])
    def test_invalid_fields_are_422(self, http, body):
        assert http.post("/auth/register", json=body).status_code == 422

    def test_too_many_sign_ups_from_one_ip_is_429(self, http):
        for i in range(3):
            assert _register(http, username=f"user{i}", mail=f"u{i}@example.com").status_code == 202
        response = _register(http, username="user9", mail="u9@example.com")
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
        assert _register(http, username="user9", mail="u9@example.com", ip="203.0.113.5").status_code == 202

    def test_disabled_sign_up_is_403(self, http, monkeypatch):
        monkeypatch.setattr(get_settings(), "SIGNUP_ENABLED", False)
        assert _register(http).status_code == 403

    async def test_failed_email_does_not_fail_the_request(self, http, db_session, email):
        email.fail = True
        assert _register(http).status_code == 202
        assert (await _user(db_session, "joana")).status == "pending_verification"


class TestVerifyEmail:
    async def test_confirms_and_waits_for_approval(self, http, db_session, email):
        _register(http)

        response = http.post("/auth/verify-email", json={"token": email.token()})

        assert response.status_code == 200
        user = await _user(db_session, "joana")
        await db_session.refresh(user)
        assert user.status == "pending_approval"
        assert user.email_verified_at is not None

    async def test_admins_are_told(self, http, db_session, email, seeded_permissions):
        admin_permission = await db_session.get(PermissionModel, uuid.UUID(seeded_permissions["trem:admin"]))
        db_session.add(UserModel(username="chefe", email="chefe@example.com", password_hash="x",
                                 status="active", permissions=[admin_permission]))
        db_session.add(UserModel(username="ex-chefe", email="ex@example.com", password_hash="x",
                                 status="suspended", permissions=[admin_permission]))
        await db_session.commit()
        _register(http)

        http.post("/auth/verify-email", json={"token": email.token()})

        notices = email.of("send_signup_pending_approval")
        assert sorted(n["to"] for n in notices) == ["chefe@example.com", "dono@example.com"]
        assert notices[0]["username"] == "joana" and notices[0]["email"] == "joana@example.com"

    def test_token_works_once(self, http, email):
        _register(http)
        token = email.token()
        assert http.post("/auth/verify-email", json={"token": token}).status_code == 200
        assert http.post("/auth/verify-email", json={"token": token}).status_code == 400
        assert len(email.of("send_signup_pending_approval")) == 1

    async def test_expired_token_is_400(self, http, db_session, email):
        _register(http)
        await db_session.execute(
            update(EmailTokenModel).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await db_session.commit()
        assert http.post("/auth/verify-email", json={"token": email.token()}).status_code == 400

    def test_unknown_token_is_400(self, http):
        assert http.post("/auth/verify-email", json={"token": "x" * 43}).status_code == 400


class TestResendVerification:
    def test_new_link_replaces_the_old_one(self, http, email):
        _register(http)
        old = email.token()

        assert http.post("/auth/resend-verification", json={"email": "joana@example.com"}).status_code == 202
        new = email.token()

        assert new != old
        assert http.post("/auth/verify-email", json={"token": old}).status_code == 400
        assert http.post("/auth/verify-email", json={"token": new}).status_code == 200

    def test_unknown_or_confirmed_address_answers_the_same_and_sends_nothing(self, http, email):
        _register(http)
        http.post("/auth/verify-email", json={"token": email.token()})
        before = len(email.of("send_email_verification"))

        for address in ("joana@example.com", "ninguem@example.com"):
            response = http.post("/auth/resend-verification", json={"email": address})
            assert response.status_code == 202
        assert len(email.of("send_email_verification")) == before

    def test_is_rate_limited_per_address(self, http):
        for _ in range(3):
            http.post("/auth/resend-verification", json={"email": "a@example.com"},
                      headers={"CF-Connecting-IP": str(uuid.uuid4())})
        response = http.post("/auth/resend-verification", json={"email": "a@example.com"},
                             headers={"CF-Connecting-IP": "203.0.113.77"})
        assert response.status_code == 429


class TestLoginOfPendingAccounts:
    def _login(self, http):
        return http.post("/auth/login", json={"username": "joana", "password": PASSWORD})

    def test_unconfirmed_email(self, http):
        _register(http)
        response = self._login(http)
        assert response.status_code == 403
        assert "e-mail" in response.json()["detail"]

    def test_awaiting_approval(self, http, email):
        _register(http)
        http.post("/auth/verify-email", json={"token": email.token()})
        response = self._login(http)
        assert response.status_code == 403
        assert "aprovação" in response.json()["detail"]

    def test_wrong_password_on_a_pending_account_does_not_reveal_it(self, http):
        _register(http)
        response = http.post("/auth/login", json={"username": "joana", "password": "errada errada"})
        assert response.status_code == 401
