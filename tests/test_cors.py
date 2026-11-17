"""Browser fronts on other domains call the API directly (CORS_ORIGINS in conftest)."""

import pytest

FRONT = "https://front.example"


def _preflight(client, origin, headers="content-type,x-requested-with"):
    return client.options(
        "/auth/refresh",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": headers,
        },
    )


@pytest.mark.parametrize("origin", [FRONT, "http://localhost:3004"])
def test_preflight_from_a_listed_front_is_allowed_with_credentials(client, origin):
    response = _preflight(client, origin)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "x-requested-with" in response.headers["access-control-allow-headers"].lower()


def test_preflight_from_any_other_site_is_refused(client):
    """What keeps the cookie endpoints safe from CSRF: their custom header needs this preflight."""
    response = _preflight(client, "https://evil.example")
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_responses_name_the_front_and_expose_retry_after(client):
    response = client.get("/.well-known/jwks.json", headers={"Origin": FRONT})
    assert response.headers["access-control-allow-origin"] == FRONT
    assert "retry-after" in response.headers["access-control-expose-headers"].lower()


def test_other_origins_get_no_cors_headers(client):
    response = client.get("/.well-known/jwks.json", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_origins_are_trimmed_and_blank_means_none(monkeypatch):
    from app.core.config import Settings

    assert Settings(CORS_ORIGINS=" https://a.example/ ,,http://b.example ").cors_origins() == [
        "https://a.example",
        "http://b.example",
    ]
    assert Settings(CORS_ORIGINS="").cors_origins() == []
