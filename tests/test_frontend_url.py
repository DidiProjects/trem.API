"""E-mail links open at FRONTEND_URL when it is set, at API_URL otherwise."""
from unittest.mock import patch

import pytest

from app.core.config import get_settings
from app.services.rate_limit import SlidingWindowLimiter
from tests.test_password_reset import _forgot, _make_user, email, http, throttle  # noqa: F401  (fixtures)
from tests.test_signup import _register

FRONT = "https://hub.example.com"


@pytest.fixture(autouse=True)
def signup_limiter():
    # The reset fixtures isolate only the reset limiter; sign-up would share the global one.
    with patch("app.services.signup_service.get_signup_limiter", return_value=SlidingWindowLimiter(limit=3, window_seconds=3600)):
        yield


@pytest.fixture
def front(monkeypatch):
    monkeypatch.setattr(get_settings(), "FRONTEND_URL", FRONT + "/")


def test_falls_back_to_the_api_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "FRONTEND_URL", "")
    assert get_settings().frontend_url() == get_settings().API_URL.rstrip("/")


async def test_verification_link_opens_the_front(http, front, email):
    _register(http)
    assert email.of("send_email_verification")[-1]["link"].startswith(f"{FRONT}/#verify-email=")


async def test_conflict_points_to_the_front(http, front, email, db_session):
    await _make_user(db_session, username="joana")
    _register(http, username="outra", mail="joana@example.com")
    assert email.of("send_signup_conflict")[-1]["login_url"] == FRONT


async def test_reset_link_opens_the_front(http, front, email, db_session):
    await _make_user(db_session)
    _forgot(http)
    assert email.of("send_password_reset")[-1]["link"].startswith(f"{FRONT}/#reset-password=")
