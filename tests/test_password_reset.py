"""
"Forgot password" against the suite's SQLite: /auth/forgot-password and /auth/reset-password.

The e-mail client only records messages; deliveries run as background tasks after the
response, as in production.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.api.v1.routers.signup import get_email_client
from app.core.security import hash_password, verify_password
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import EmailTokenModel, RefreshTokenModel, UserModel
from app.main import app
from app.services.login_throttle import LoginThrottle
from app.services.rate_limit import SlidingWindowLimiter
from tests.test_signup import FakeEmail

PASSWORD = "cavalo bateria grampo"
NEW_PASSWORD = "lanterna girassol pedra"
OTHER_PASSWORD = "janela azul montanha"
SHORT_PASSWORD = "curta"


@pytest.fixture
def email():
    return FakeEmail()


@pytest.fixture
def throttle():
    return LoginThrottle(per_username=3, per_ip=50, window_seconds=60)


@pytest.fixture
def http(db_session, seeded_permissions, email, throttle):
    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_email_client] = lambda: email
    limiter = SlidingWindowLimiter(limit=3, window_seconds=3600)
    with patch("app.services.password_reset_service.get_signup_limiter", return_value=limiter), patch(
        "app.services.password_reset_service.get_login_throttle", return_value=throttle
    ), patch("app.services.auth_service.get_login_throttle", return_value=throttle):
        yield TestClient(app, base_url="https://testserver")
    app.dependency_overrides.clear()


async def _make_user(db_session, username="ana", status="active"):
    db_session.add(UserModel(username=username, email=f"{username}@example.com",
                             password_hash=hash_password(PASSWORD), status=status,
                             must_change_password=False, permissions=[]))
    await db_session.commit()


async def _user(db_session, username="ana"):
    db_session.expire_all()
    return (await db_session.execute(select(UserModel).where(UserModel.username == username))).scalar_one()


def _forgot(http, mail="ana@example.com", ip="198.51.100.1"):
    return http.post("/auth/forgot-password", json={"email": mail}, headers={"CF-Connecting-IP": ip})


def _reset_token(email):
    link = email.of("send_password_reset")[-1]["link"]
    return link.split("#reset-password=", 1)[1]


def _reset(http, token, password=NEW_PASSWORD):
    return http.post("/auth/reset-password", json={"token": token, "new_password": password})


def _login(http, password, username="ana"):
    return http.post("/auth/login", json={"username": username, "password": password})


class TestForgotPassword:
    async def test_sends_a_link_to_an_active_account(self, http, db_session, email):
        await _make_user(db_session)

        response = _forgot(http)

        assert response.status_code == 202
        sent = email.of("send_password_reset")
        assert len(sent) == 1
        assert sent[0]["to"] == "ana@example.com"
        assert sent[0]["link"].startswith("http://testserver/#reset-password=")

    async def test_unknown_e_mail_gets_the_same_answer_and_no_message(self, http, db_session, email):
        await _make_user(db_session)

        known, unknown = _forgot(http), _forgot(http, mail="ninguem@example.com")

        assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
        assert [m["to"] for m in email.of("send_password_reset")] == ["ana@example.com"]

    @pytest.mark.parametrize("status", ["pending_verification", "pending_approval", "suspended", "rejected", "blocked"])
    async def test_accounts_that_are_not_active_get_no_link(self, http, db_session, email, status):
        await _make_user(db_session, status=status)

        assert _forgot(http).status_code == 202
        assert email.of("send_password_reset") == []

    async def test_e_mail_is_matched_case_insensitively(self, http, db_session, email):
        await _make_user(db_session)

        _forgot(http, mail="ANA@Example.com")

        assert len(email.of("send_password_reset")) == 1

    async def test_a_new_link_replaces_the_previous_one(self, http, db_session, email):
        await _make_user(db_session)
        _forgot(http)
        first = _reset_token(email)
        _forgot(http)

        assert _reset(http, first).status_code == 400
        assert _reset(http, _reset_token(email)).status_code == 200

    async def test_is_rate_limited_per_e_mail(self, http, db_session):
        await _make_user(db_session)
        for i in range(3):
            assert _forgot(http, ip=f"198.51.100.{i + 10}").status_code == 202

        response = _forgot(http, ip="198.51.100.99")
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0

    async def test_a_failed_delivery_does_not_change_the_answer(self, http, db_session, email):
        await _make_user(db_session)
        email.fail = True

        assert _forgot(http).status_code == 202


class TestResetPassword:
    async def test_sets_the_new_password_and_ends_every_session(self, http, db_session, email):
        await _make_user(db_session)
        refresh = _login(http, PASSWORD).json()["refresh_token"]
        _forgot(http)

        response = _reset(http, _reset_token(email))

        assert response.status_code == 200
        user = await _user(db_session)
        assert verify_password(NEW_PASSWORD, user.password_hash)
        assert _login(http, PASSWORD).status_code == 401
        assert _login(http, NEW_PASSWORD).status_code == 200
        assert http.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 401
        assert email.of("send_password_changed")[0]["to"] == "ana@example.com"

    async def test_clears_a_forced_password_change(self, http, db_session, email):
        await _make_user(db_session)
        await db_session.execute(update(UserModel).values(must_change_password=True))
        await db_session.commit()
        _forgot(http)

        _reset(http, _reset_token(email))

        assert (await _user(db_session)).must_change_password is False

    async def test_link_works_once(self, http, db_session, email):
        await _make_user(db_session)
        _forgot(http)
        token = _reset_token(email)

        assert _reset(http, token).status_code == 200
        assert _reset(http, token, OTHER_PASSWORD).status_code == 400

    async def test_expired_link_is_refused(self, http, db_session, email):
        await _make_user(db_session)
        _forgot(http)
        await db_session.execute(
            update(EmailTokenModel).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await db_session.commit()

        assert _reset(http, _reset_token(email)).status_code == 400

    async def test_unknown_token_is_refused(self, http):
        assert _reset(http, "x" * 43).status_code == 400

    async def test_e_mail_verification_token_does_not_reset_a_password(self, http, db_session, email):
        await _make_user(db_session)
        _forgot(http)
        await db_session.execute(update(EmailTokenModel).values(purpose="verify_email"))
        await db_session.commit()

        assert _reset(http, _reset_token(email)).status_code == 400

    async def test_weak_password_is_422_and_keeps_the_link(self, http, db_session, email):
        await _make_user(db_session)
        _forgot(http)
        token = _reset_token(email)

        assert _reset(http, token, SHORT_PASSWORD).status_code == 422
        assert _reset(http, token, "ana" + NEW_PASSWORD).status_code == 422
        assert _reset(http, token).status_code == 200

    async def test_account_suspended_after_the_request_cannot_reset(self, http, db_session, email):
        await _make_user(db_session)
        _forgot(http)
        await db_session.execute(update(UserModel).values(status="suspended"))
        await db_session.commit()

        assert _reset(http, _reset_token(email)).status_code == 400
        assert verify_password(PASSWORD, (await _user(db_session)).password_hash)

    async def test_lifts_the_login_lockout(self, http, db_session, email):
        await _make_user(db_session)
        wrong = PASSWORD + "x"
        for _ in range(3):
            _login(http, wrong)
        assert _login(http, PASSWORD).status_code == 429
        _forgot(http)

        _reset(http, _reset_token(email))

        assert _login(http, NEW_PASSWORD).status_code == 200

    async def test_second_factor_is_still_required(self, http, db_session, email):
        await _make_user(db_session)
        await db_session.execute(update(UserModel).values(totp_enabled_at=datetime.now(timezone.utc)))
        await db_session.commit()
        _forgot(http)

        _reset(http, _reset_token(email))

        body = _login(http, NEW_PASSWORD).json()
        assert body["mfa_required"] is True
        assert "access_token" not in body

    async def test_tokens_are_stored_hashed(self, http, db_session, email):
        await _make_user(db_session)
        _forgot(http)

        stored = (await db_session.execute(select(EmailTokenModel.token_hash))).scalars().all()
        assert _reset_token(email) not in stored
        assert not (await db_session.execute(select(RefreshTokenModel))).scalars().all()
