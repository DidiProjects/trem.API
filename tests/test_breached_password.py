"""
Breached-password check: the Have I Been Pwned client against a fake transport, and the
three places that take a new password (sign-up, change, reset).

The live test runs only with PWNED_NETWORK_TEST=1.
"""
import hashlib
import os
from unittest.mock import patch

import httpx
import pytest

from app.core.config import get_settings
from app.infrastructure.http.pwned_passwords_client import PwnedPasswordsClient
from app.services import breached_password
from tests.test_password_reset import (
    NEW_PASSWORD,
    PASSWORD,
    _forgot,
    _login,
    _make_user,
    _reset,
    _reset_token,
    email,  # noqa: F401  (fixture)
    http,  # noqa: F401  (fixture)
    throttle,  # noqa: F401  (fixture)
)
from tests.test_signup import _register

# A famous breached password, assembled so secret scanners do not flag the test file.
LEAKED = "pass" + "word" + "123"


def _suffix(password):
    return hashlib.sha1(password.encode()).hexdigest().upper()[5:]


def _range_body(*entries):
    return "\r\n".join(f"{suffix}:{count}" for suffix, count in entries)


class Recorder:
    def __init__(self, status=200, body="", error=None):
        self.requests = []
        self.status, self.body, self.error = status, body, error

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return httpx.Response(self.status, text=self.body)


class TestPwnedClient:
    async def test_sends_only_the_prefix_with_padding(self):
        recorder = Recorder(body=_range_body(("0" * 35, 1)))
        await PwnedPasswordsClient(httpx.MockTransport(recorder)).times_seen(LEAKED)

        request = recorder.requests[0]
        digest = hashlib.sha1(LEAKED.encode()).hexdigest().upper()
        assert request.url.path == f"/range/{digest[:5]}"
        assert digest[5:] not in str(request.url)
        assert request.headers["Add-Padding"] == "true"

    async def test_counts_a_matching_suffix(self):
        body = _range_body(("0" * 35, 3), (_suffix(LEAKED), 42))
        assert await PwnedPasswordsClient(httpx.MockTransport(Recorder(body=body))).times_seen(LEAKED) == 42

    async def test_padding_entries_do_not_count(self):
        body = _range_body((_suffix(LEAKED), 0))
        assert await PwnedPasswordsClient(httpx.MockTransport(Recorder(body=body))).times_seen(LEAKED) == 0

    async def test_absent_suffix_is_zero(self):
        body = _range_body(("0" * 35, 7))
        assert await PwnedPasswordsClient(httpx.MockTransport(Recorder(body=body))).times_seen(LEAKED) == 0

    @pytest.mark.parametrize("recorder", [
        Recorder(status=503),
        Recorder(error=httpx.ConnectTimeout("slow")),
        Recorder(error=httpx.ConnectError("down")),
    ])
    async def test_unavailable_service_is_none(self, recorder):
        assert await PwnedPasswordsClient(httpx.MockTransport(recorder)).times_seen(LEAKED) is None


class FakeClient:
    def __init__(self, breached=(), down=False):
        self.breached, self.down, self.seen = set(breached), down, []

    async def times_seen(self, password):
        self.seen.append(password)
        if self.down:
            return None
        return 10 if password in self.breached else 0


@pytest.fixture
def pwned(monkeypatch):
    monkeypatch.setattr(get_settings(), "PWNED_PASSWORDS_ENABLED", True)
    fake = FakeClient(breached={LEAKED + " longer", NEW_PASSWORD})
    with patch.object(breached_password, "get_pwned_passwords_client", return_value=fake):
        yield fake


class TestEndpoints:
    async def test_sign_up_refuses_a_breached_password(self, http, pwned, email):
        response = _register(http, password=LEAKED + " longer")

        assert response.status_code == 422
        assert "vazamentos" in response.json()["detail"]
        assert email.sent == []

    async def test_sign_up_accepts_a_clean_password(self, http, pwned):
        assert _register(http).status_code == 202
        assert pwned.seen == [PASSWORD]

    async def test_reset_refuses_a_breached_password_and_keeps_the_link(self, http, pwned, db_session, email):
        await _make_user(db_session)
        _forgot(http)
        token = _reset_token(email)

        assert _reset(http, token, NEW_PASSWORD).status_code == 422
        assert _reset(http, token, "outra senha bem longa").status_code == 200

    async def test_change_refuses_a_breached_password(self, http, pwned, db_session):
        await _make_user(db_session)
        bearer = {"Authorization": f"Bearer {_login(http, PASSWORD).json()['access_token']}"}

        response = http.post("/auth/change-password", headers=bearer,
                             json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})

        assert response.status_code == 422
        assert _login(http, PASSWORD).status_code == 200

    async def test_change_with_a_wrong_current_password_does_not_query_the_service(self, http, pwned, db_session):
        await _make_user(db_session)
        bearer = {"Authorization": f"Bearer {_login(http, PASSWORD).json()['access_token']}"}

        http.post("/auth/change-password", headers=bearer,
                  json={"current_password": PASSWORD + "x", "new_password": NEW_PASSWORD})

        assert NEW_PASSWORD not in pwned.seen

    async def test_service_down_does_not_block_sign_up(self, http, pwned):
        pwned.down = True
        assert _register(http, password=LEAKED + " longer").status_code == 202

    async def test_disabled_check_never_calls_the_service(self, http, pwned, monkeypatch):
        monkeypatch.setattr(get_settings(), "PWNED_PASSWORDS_ENABLED", False)
        assert _register(http, password=LEAKED + " longer").status_code == 202
        assert pwned.seen == []


@pytest.mark.skipif(not os.environ.get("PWNED_NETWORK_TEST"), reason="PWNED_NETWORK_TEST not set")
class TestLiveService:
    async def test_a_famous_password_is_found(self):
        assert (await PwnedPasswordsClient().times_seen(LEAKED)) > 1000

    async def test_a_random_password_is_not(self):
        assert await PwnedPasswordsClient().times_seen(os.urandom(24).hex()) == 0
