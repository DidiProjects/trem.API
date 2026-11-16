"""
Segundo fator: o TOTP em si (RFC 6238), a cifra do segredo e o fluxo pela API
contra o SQLite da suíte — cadastro, login em dois passos, códigos de recuperação,
desativação e a exigência de TOTP nas rotas de admin.
"""
import base64
import secrets
import time
import uuid
from unittest.mock import patch

import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient

from app.core import totp
from app.core.config import get_settings
from app.core.security import decode_token, hash_password
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import PermissionModel, UserModel
from app.main import app
from app.services.login_throttle import LoginThrottle
from app.services.rate_limit import SlidingWindowLimiter

KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
PASSWORD = "cavalo bateria grampo"
PASSWORD_HASH = hash_password(PASSWORD)


class TestTotp:
    RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()

    @pytest.mark.parametrize("t,expected", [
        (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
        (1234567890, "89005924"), (2000000000, "69279037"),
    ])
    def test_rfc6238_vectors(self, t, expected):
        assert totp.code_at(self.RFC_SECRET, t) == expected[-6:]

    def test_accepts_one_step_of_drift_each_way(self):
        secret, now = totp.generate_secret(), 1_700_000_000
        for offset in (-30, 0, 30):
            assert totp.verify(secret, totp.code_at(secret, now + offset), None, now) is not None
        assert totp.verify(secret, totp.code_at(secret, now + 90), None, now) is None

    def test_a_used_step_is_refused(self):
        secret, now = totp.generate_secret(), 1_700_000_000
        step = totp.verify(secret, totp.code_at(secret, now), None, now)
        assert totp.verify(secret, totp.code_at(secret, now), step, now) is None

    @pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef"])
    def test_malformed_codes(self, code):
        assert totp.verify(totp.generate_secret(), code, None) is None

    def test_secret_is_160_bits(self):
        assert len(base64.b32decode(totp.generate_secret())) == 20

    def test_encryption_round_trip_bound_to_the_user(self):
        secret = totp.generate_secret()
        sealed = totp.encrypt_secret(secret, KEY, "user-a")
        assert secret not in sealed
        assert totp.decrypt_secret(sealed, KEY, "user-a") == secret
        with pytest.raises(InvalidTag):
            totp.decrypt_secret(sealed, KEY, "user-b")  # copied onto another account

    def test_key_must_be_32_bytes(self):
        with pytest.raises(ValueError):
            totp.encrypt_secret("x", base64.urlsafe_b64encode(b"short").decode(), "u")

    def test_recovery_codes(self):
        codes = totp.generate_recovery_codes()
        assert len(codes) == len(set(codes)) == 10
        assert totp.normalize_recovery_code(" ABCDE-FGHJK ") == totp.normalize_recovery_code("abcdefghjk")

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri("SECRET", "ana@example.com")
        assert uri.startswith("otpauth://totp/trem.API%3Aana%40example.com?secret=SECRET")
        assert "issuer=trem.API" in uri and "digits=6" in uri and "period=30" in uri


@pytest.fixture
def http(db_session, seeded_permissions, monkeypatch):
    monkeypatch.setattr(get_settings(), "MFA_ENCRYPTION_KEY", KEY)

    async def _db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    throttle = LoginThrottle(per_username=50, per_ip=50, window_seconds=60)
    limiter = SlidingWindowLimiter(limit=5, window_seconds=300)
    with patch("app.services.auth_service.get_login_throttle", return_value=throttle), patch(
        "app.services.mfa_service.get_mfa_limiter", return_value=limiter
    ):
        yield TestClient(app, base_url="https://testserver")
    app.dependency_overrides.clear()


async def _make_user(db_session, seeded_permissions, username="ana", *keys):
    permissions = [await db_session.get(PermissionModel, uuid.UUID(seeded_permissions[k])) for k in keys]
    db_session.add(UserModel(username=username, email=f"{username}@example.com", password_hash=PASSWORD_HASH,
                             status="active", must_change_password=False, permissions=permissions))
    await db_session.commit()


def _login(http, username="ana"):
    return http.post("/auth/login", json={"username": username, "password": PASSWORD})


def _bearer(body):
    return {"Authorization": f"Bearer {body['access_token']}"}


def _next_code(secret):
    # The confirmation already used the current step; the next one is within the drift window.
    return totp.code_at(secret, time.time() + 30)


def _enroll(http, username="ana"):
    auth = _bearer(_login(http, username).json())
    secret = http.post("/auth/mfa/enroll", json={"password": PASSWORD}, headers=auth).json()["secret"]
    codes = http.post("/auth/mfa/confirm", json={"code": totp.code_at(secret, time.time())}, headers=auth)
    return secret, codes.json()["recovery_codes"]


class TestEnrolment:
    async def test_enrol_and_confirm(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        auth = _bearer(_login(http).json())

        enrolled = http.post("/auth/mfa/enroll", json={"password": PASSWORD}, headers=auth)
        assert enrolled.status_code == 200
        secret = enrolled.json()["secret"]
        assert enrolled.json()["otpauth_uri"].startswith("otpauth://totp/")

        stored = (await db_session.execute(
            UserModel.__table__.select().where(UserModel.username == "ana")
        )).one()
        assert stored.totp_secret and secret not in stored.totp_secret
        assert stored.totp_enabled_at is None  # not active until confirmed

        confirmed = http.post("/auth/mfa/confirm", json={"code": totp.code_at(secret, time.time())}, headers=auth)
        assert confirmed.status_code == 200
        assert len(confirmed.json()["recovery_codes"]) == 10

    async def test_enrolment_needs_the_password(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        auth = _bearer(_login(http).json())
        assert http.post("/auth/mfa/enroll", json={"password": "errada"}, headers=auth).status_code == 401

    async def test_wrong_confirmation_code(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        auth = _bearer(_login(http).json())
        http.post("/auth/mfa/enroll", json={"password": PASSWORD}, headers=auth)
        assert http.post("/auth/mfa/confirm", json={"code": "000000"}, headers=auth).status_code == 401

    async def test_without_encryption_key_mfa_is_unavailable(self, http, db_session, seeded_permissions, monkeypatch):
        await _make_user(db_session, seeded_permissions)
        monkeypatch.setattr(get_settings(), "MFA_ENCRYPTION_KEY", "")
        auth = _bearer(_login(http).json())
        assert http.post("/auth/mfa/enroll", json={"password": PASSWORD}, headers=auth).status_code == 503

    async def test_cannot_enrol_twice(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        secret, _ = _enroll(http)
        auth = _bearer(http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "code": _next_code(secret)}).json())
        assert http.post("/auth/mfa/enroll", json={"password": PASSWORD}, headers=auth).status_code == 409


class TestTwoStepLogin:
    async def test_password_alone_gets_a_challenge_not_tokens(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        _enroll(http)

        body = _login(http).json()

        assert body["mfa_required"] is True and body["mfa_token"]
        assert "access_token" not in body and "refresh_token" not in body
        # The challenge opens nothing by itself.
        assert http.get("/auth/mfa/enroll", headers={"Authorization": f"Bearer {body['mfa_token']}"}).status_code in (401, 405)
        assert http.post("/auth/change-password", json={"current_password": "a", "new_password": "b" * 12},
                         headers={"Authorization": f"Bearer {body['mfa_token']}"}).status_code == 401

    async def test_code_completes_the_login(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        secret, _ = _enroll(http)

        tokens = http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "code": _next_code(secret)}).json()

        assert decode_token(tokens["access_token"])["amr"] == ["pwd", "otp"]
        refreshed = http.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()
        assert decode_token(refreshed["access_token"])["amr"] == ["pwd", "otp"]

    async def test_a_code_works_once(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        secret, _ = _enroll(http)
        code = _next_code(secret)

        assert http.post("/auth/mfa/verify", json={"mfa_token": _login(http).json()["mfa_token"], "code": code}).status_code == 200
        assert http.post("/auth/mfa/verify", json={"mfa_token": _login(http).json()["mfa_token"], "code": code}).status_code == 401

    async def test_recovery_code_works_once(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        _, codes = _enroll(http)

        first = http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "recovery_code": codes[0].upper()})
        again = http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "recovery_code": codes[0]})

        assert first.status_code == 200
        assert decode_token(first.json()["access_token"])["amr"] == ["pwd", "otp"]
        assert again.status_code == 401

    async def test_guessing_is_limited(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        _enroll(http)
        challenge = _login(http).json()["mfa_token"]
        statuses = [http.post("/auth/mfa/verify", json={"mfa_token": challenge, "code": "000000"}).status_code
                    for _ in range(6)]
        assert statuses[-1] == 429

    async def test_challenge_must_be_a_challenge(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        access = _login(http).json()["access_token"]
        assert http.post("/auth/mfa/verify", json={"mfa_token": access, "code": "123456"}).status_code == 401

    @pytest.mark.parametrize("body", [{"code": "123456", "recovery_code": "abcde-fghjk"}, {}])
    def test_exactly_one_kind_of_code(self, http, body):
        assert http.post("/auth/mfa/verify", json={"mfa_token": "x", **body}).status_code == 422

    async def test_cookie_session_after_the_second_step(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        secret, _ = _enroll(http)
        response = http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "code": _next_code(secret), "session": "cookie"})
        assert "refresh_token" not in response.json()
        assert response.headers["set-cookie"].startswith("trem_refresh=")


class TestDisable:
    async def test_needs_password_and_code_and_ends_sessions(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        secret, codes = _enroll(http)
        tokens = http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "recovery_code": codes[0]}).json()
        auth = _bearer(tokens)

        assert http.post("/auth/mfa/disable", json={"password": "errada", "code": codes[1]}, headers=auth).status_code == 401
        assert http.post("/auth/mfa/disable", json={"password": PASSWORD, "code": codes[1]}, headers=auth).status_code == 200

        assert http.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
        assert "access_token" in _login(http).json()  # back to password only

    async def test_regenerating_recovery_codes_kills_the_old_ones(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions)
        secret, old = _enroll(http)
        auth = _bearer(http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "recovery_code": old[0]}).json())

        new = http.post("/auth/mfa/recovery-codes", json={"password": PASSWORD, "code": _next_code(secret)}, headers=auth)

        assert new.status_code == 200
        assert http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "recovery_code": old[1]}).status_code == 401
        assert http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "recovery_code": new.json()["recovery_codes"][0]}).status_code == 200


class TestAdminNeedsSecondFactor:
    async def test_admin_with_password_only_is_refused(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions, "chefe", "trem:admin")
        auth = _bearer(_login(http, "chefe").json())
        response = http.get("/admin/users", headers=auth)
        assert response.status_code == 403
        assert "segundo fator" in response.json()["detail"]

    async def test_admin_with_totp_gets_in(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions, "chefe", "trem:admin")
        secret, _ = _enroll(http, "chefe")
        auth = _bearer(http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http, "chefe").json()["mfa_token"], "code": _next_code(secret)}).json())
        assert http.get("/admin/users", headers=auth).status_code == 200

    async def test_totp_without_admin_is_refused(self, http, db_session, seeded_permissions):
        await _make_user(db_session, seeded_permissions, "ana", "trem:ai")
        secret, _ = _enroll(http)
        auth = _bearer(http.post("/auth/mfa/verify", json={
            "mfa_token": _login(http).json()["mfa_token"], "code": _next_code(secret)}).json())
        assert http.get("/admin/users", headers=auth).status_code == 403
